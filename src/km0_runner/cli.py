"""Command line: `km0-runner run | smoke | swebench | summarize | plot | dataset | calibrate | bridge`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

from .handoff import load_km0_package, load_swebench_instances
from .pi_client import PiError, pi_version
from .runner import default_pi_factory, new_run_id, run_package
from .summary import load_reports, plot, summarize, write_points
from .tiers import RunnerConfig, load_config


def checked_pi_version(config: RunnerConfig) -> str:
    """The installed pi's version; refuses a version other than the pinned one."""
    version = pi_version(config.pi.command)
    if config.pi.check_version and version != config.pi.version:
        raise SystemExit(
            f"pi {version} is installed but the config pins {config.pi.version}; install that version "
            f"(npm i -g @earendil-works/pi-coding-agent@{config.pi.version}) or set pi.check_version: false"
        )
    return version


def run_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    version = checked_pi_version(config)
    package = load_km0_package(args.package)
    only = [f"{int(t):02d}" for t in args.tasks] if args.tasks else None
    run_dir, reports = run_package(
        package, args.repo, config, args.runs, args.run_id, pi_version=version, only=only, start=args.start_tier
    )
    summary = json.loads((run_dir / "summary.json").read_text())
    print(
        f"run {run_dir.name}: {summary['passed']} passed, {summary['unjudged']} for KM0 to judge, "
        f"{summary['failed']} failed, {summary['blocked']} blocked; cost ${summary['cost']:.4f}"
        + (f" (all on the top tier: ~${summary['top_tier_estimate']:.4f})" if summary["top_tier_estimate"] else "")
    )
    print(f"reports in {run_dir / 'reports'}; branches km0/{run_dir.name}/NN in {Path(args.repo).resolve()}")
    return 0 if all(r.failure_category in (None,) for r in reports) else 1


def run_smoke(args: argparse.Namespace) -> int:
    """Stage 0: pi does a small task in a scratch repository on the bottom rung, then switches to the next."""
    from .pi_client import PiClient, agent_dir, pi_argv, tool_text

    config = load_config(args.config)
    version = checked_pi_version(config)
    with tempfile.TemporaryDirectory(prefix="km0-smoke-") as scratch:
        repo = Path(scratch) / "repo"
        repo.mkdir()
        (repo / "greet.py").write_text('def greet(name):\n    return "hi"\n')
        argv, env = pi_argv(config.pi, config.ladder[0], repo, agent_dir(config.pi, Path(scratch)))
        events: list[str] = []

        def watch(event: dict) -> None:
            if event.get("type") == "tool_execution_start":
                events.append(f"  {event['toolName']} {json.dumps(event.get('args'), ensure_ascii=False)[:100]}")
            if event.get("type") == "tool_execution_end":
                events.append(f"    → {'error' if event.get('isError') else 'ok'}: {tool_text(event.get('result'))[:100]!r}")

        with PiClient(argv, repo, env, Path(scratch) / "pi.log") as pi:
            outcome = pi.run('Change greet.py so greet("Ada") returns "Hello, Ada!". Then stop.', watch, timeout=600)
            first = pi.stats()
            print(f"pi {version}, {config.ladder[0].model}: aborted={outcome.aborted} error={outcome.error}")
            print("\n".join(events))
            print(f"  tokens {first.get('tokens')} cost ${first.get('cost', 0):.6f}")
            if len(config.ladder) > 1:
                events.clear()
                pi.set_model(config.ladder[1])
                outcome = pi.run("Read greet.py and confirm the change in one sentence.", watch, timeout=600)
                second = pi.stats()
                print(f"switched to {config.ladder[1].model}: aborted={outcome.aborted} error={outcome.error}")
                print("\n".join(events))
                print(f"  session tokens {second.get('tokens')} cost ${second.get('cost', 0):.6f}")
        print("greet.py now:\n" + (repo / "greet.py").read_text())
    return 0


def run_swebench(args: argparse.Namespace) -> int:
    from .swebench import run_instances

    config = load_config(args.config)
    version = checked_pi_version(config)
    predicted = json.loads(Path(args.predicted).read_text()) if args.predicted else None
    tasks = load_swebench_instances(args.instances, predicted=predicted, ids=args.ids)
    run_id = args.run_id or new_run_id()
    run_dir = Path(args.runs).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_instances(
        tasks, config, run_dir, run_id, default_pi_factory(config, run_dir), version,
        dataset=args.dataset, judge=not args.no_judge, repeats=args.repeats, start=args.start_tier,
    )  # fmt: skip
    return 0


def run_summarize(args: argparse.Namespace) -> int:
    print(summarize(load_reports(args.paths)).model_dump_json(indent=2))
    return 0


def run_plot(args: argparse.Namespace) -> int:
    """One point per run directory, named after it."""
    summaries = [summarize(load_reports([path]), name=Path(path).name) for path in args.runs]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_points(summaries, out.with_suffix(".csv"))
    try:
        plot(summaries, out)
    except ImportError:
        print(f"matplotlib is not installed; wrote {out.with_suffix('.csv')} only")
        return 0
    print(f"wrote {out} and {out.with_suffix('.csv')}")
    return 0


def run_dataset(args: argparse.Namespace) -> int:
    from .dataset import load_source, split, write_split

    rows = load_source(args.source)
    dev, test = split(rows, args.size, dev_share=args.dev_share, seed=args.seed)
    meta = {"source": args.source, "size": len(dev) + len(test), "dev_share": args.dev_share, "seed": args.seed}
    write_split(dev, test, args.out, meta)
    print(f"{len(dev)} dev and {len(test)} test instances in {args.out}")
    return 0


def run_calibrate(args: argparse.Namespace) -> int:
    from .acceptance import load_verdicts
    from .calibrate import calibrate
    from .scoring import RoutingRules

    start = RoutingRules.model_validate_json(Path(args.rules).read_text()) if args.rules else None
    test = load_reports(args.test) if args.test else None
    verdicts = load_verdicts([*args.dev, *(args.test or []), *(args.acceptance or [])])
    result = calibrate(load_reports(args.dev), test, start, verdicts)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.rules.model_dump_json(indent=2), encoding="utf-8")
    out.with_name(out.stem + ".evaluation.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    for name in ("dev", "test"):
        before, after = getattr(result, f"{name}_before"), getattr(result, f"{name}_after")
        if before and after:
            print(
                f"{name}: accuracy {before.accuracy:.2%} → {after.accuracy:.2%}, "
                f"expected cost {before.expected_cost:.4f} → {after.expected_cost:.4f} "
                f"({after.tasks} tasks, {after.censored} upper-bound labels)"
            )
    print(f"wrote {out} (import it into KM0) and its evaluation")
    return 0


def run_bridge(args: argparse.Namespace) -> int:
    from .llm_bridge import main as bridge_main

    return bridge_main([args.folder, "--port", str(args.port), *(["--script", args.script] if args.script else [])])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="km0-runner", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", required=True, help="runner config YAML (pi, ladder, signals, handoff)")
        p.add_argument("--runs", default="runs", help="folder that holds run directories")
        p.add_argument("--run-id", help="default: a timestamp")
        p.add_argument("--start-tier", help="ignore the predicted tier and start every task on this rung")

    run = sub.add_parser("run", help="run a KM0 handoff package on a repository")
    run.add_argument("package", help="the package: a km0/ folder, a repository holding one, or KM0's package JSON")
    run.add_argument("--repo", required=True, help="git repository to work in; each task gets a worktree and branch")
    common(run)
    run.add_argument("--tasks", nargs="*", help="task seq numbers to run; default: all")
    run.set_defaults(func=run_run)

    smoke = sub.add_parser("smoke", help="stage 0 check: pi edits a file, then switches model once")
    smoke.add_argument("-c", "--config", required=True)
    smoke.set_defaults(func=run_smoke)

    swe = sub.add_parser("swebench", help="optional regression benchmark on SWE-bench instances")
    common(swe)
    swe.add_argument("--instances", required=True, help="JSON or JSONL file of SWE-bench instances")
    swe.add_argument("--ids", nargs="*", help="instance ids to run; default: all in the file")
    swe.add_argument("--predicted", help="JSON object instance_id → tier, or {tier, features}")
    swe.add_argument("--dataset", default="verified", help="harness dataset: verified, lite or a HF name")
    swe.add_argument("--no-judge", action="store_true", help="skip the harness")
    swe.add_argument("--repeats", type=int, default=1, help="run each instance this many times")
    swe.set_defaults(func=run_swebench)

    summary = sub.add_parser("summarize", help="aggregate execution reports")
    summary.add_argument("paths", nargs="+", help="run directories or report files")
    summary.set_defaults(func=run_summarize)

    plotting = sub.add_parser("plot", help="cost vs solve rate, one point per run directory")
    plotting.add_argument("runs", nargs="+", help="run directories, one per configuration")
    plotting.add_argument("-o", "--out", default="scatter.png", help="PNG path; a CSV is written beside it")
    plotting.set_defaults(func=run_plot)

    data = sub.add_parser("dataset", help="draw a fixed SWE-bench subset and split it into dev and test")
    data.add_argument("--source", default="verified", help="verified, lite, a HF dataset name or a JSON/JSONL file")
    data.add_argument("--size", type=int, default=60, help="instances to draw in total")
    data.add_argument("--dev-share", type=float, default=0.5, help="share of each repository's draw for dev")
    data.add_argument("--seed", type=int, default=0)
    data.add_argument("-o", "--out", default="data", help="writes dev.jsonl, test.jsonl and split.json here")
    data.set_defaults(func=run_dataset)

    cal = sub.add_parser("calibrate", help="fit KM0's tier rules to reports and KM0's acceptance verdicts")
    cal.add_argument("--dev", nargs="+", required=True, help="dev run directories (their acceptance.jsonl is read too)")
    cal.add_argument("--test", nargs="*", help="test run directories, to check the fit")
    cal.add_argument("--acceptance", nargs="*", help="extra acceptance.jsonl files")
    cal.add_argument("--rules", help="rules to start from; default: KM0's current defaults")
    cal.add_argument("-o", "--out", default="routing-rules.json", help="calibrated rules for KM0 to import")
    cal.set_defaults(func=run_calibrate)

    bridge = sub.add_parser("bridge", help="a stand-in model provider answered through files (no API key)")
    bridge.add_argument("folder")
    bridge.add_argument("--port", type=int, default=8765)
    bridge.add_argument("--script", help="JSONL of answers to replay")
    bridge.set_defaults(func=run_bridge)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    try:
        return args.func(args)
    except PiError as error:
        print(f"pi: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
