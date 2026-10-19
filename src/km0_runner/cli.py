"""Command line: `km0-runner km0 | swebench | summarize | plot | dataset | calibrate`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from .cascade import Cascade  # noqa: E402
from .handoff import load_km0_package, load_swebench_instances  # noqa: E402
from .judge import CommandJudge, Judge, NoJudge, SwebenchHarnessJudge  # noqa: E402
from .summary import load_reports, plot, summarize, write_points  # noqa: E402
from .tiers import TIERS, load_config  # noqa: E402

SWEBENCH_DATASETS = {
    "verified": "princeton-nlp/SWE-bench_Verified",
    "lite": "princeton-nlp/SWE-bench_Lite",
}


def run_km0(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    repo = Path(args.repo).resolve()
    config.environment = {"cwd": str(repo), **config.environment}
    tasks = load_km0_package(args.package or repo)
    if args.tasks:
        tasks = [task for task in tasks if task.task_id in {f"{int(t):02d}" for t in args.tasks}]
    judge: Judge = CommandJudge(args.test_command) if args.test_command else NoJudge()
    # The package's own folder is not part of the work.
    cascade = Cascade(config, args.out, judge=judge, patch_command="git add -A -- . ':!km0' ':!**/__pycache__/**' ':!*.pyc' && git diff --cached")
    return _run_all(cascade, tasks, args)


def run_swebench(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    predicted = json.loads(Path(args.predicted).read_text()) if args.predicted else None
    tasks = load_swebench_instances(args.instances, predicted=predicted, ids=args.ids)
    dataset = SWEBENCH_DATASETS.get(args.dataset, args.dataset)
    judge: Judge = NoJudge() if args.no_judge else SwebenchHarnessJudge(dataset, Path(args.out) / "_harness")
    cascade = Cascade(config, args.out, judge=judge)
    return _run_all(cascade, tasks, args, repeats=args.repeats)


def _run_all(cascade: Cascade, tasks, args: argparse.Namespace, repeats: int = 1) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "reports.jsonl").open("a", encoding="utf-8") as reports:
        for task in tasks:
            for run_index in range(repeats):
                report = cascade.run(task, start_tier=args.start_tier, run_index=run_index)
                reports.write(report.model_dump_json() + "\n")
                reports.flush()
                print(
                    f"{task.task_id}#{run_index}: predicted={report.predicted_tier} solved={report.solved_tier} "
                    f"escalations={len(report.escalations)} cost=${report.cost:.4f} "
                    f"failure={report.failure_category}"
                )
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
    from .calibrate import calibrate
    from .scoring import RoutingRules

    start = RoutingRules.model_validate_json(Path(args.rules).read_text()) if args.rules else None
    test = load_reports(args.test) if args.test else None
    result = calibrate(load_reports(args.dev), test, start)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.rules.model_dump_json(indent=2), encoding="utf-8")
    out.with_name(out.stem + ".evaluation.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    for name in ("dev", "test"):
        before, after = getattr(result, f"{name}_before"), getattr(result, f"{name}_after")
        if before and after:
            print(
                f"{name}: accuracy {before.accuracy:.2%} → {after.accuracy:.2%}, "
                f"expected cost {before.expected_cost:.4f} → {after.expected_cost:.4f} ({after.tasks} tasks)"
            )
    print(f"wrote {out} (import it into KM0) and its evaluation")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="km0-runner", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", required=True, help="runner config YAML (tiers, signals, handoff)")
        p.add_argument("-o", "--out", required=True, help="run directory for trajectories and reports")
        p.add_argument("--start-tier", choices=TIERS, help="ignore the predicted tier and start here")

    km0 = sub.add_parser("km0", help="run a KM0 handoff package on a repository")
    common(km0)
    km0.add_argument("--repo", required=True, help="repository the package was unpacked into")
    km0.add_argument("--package", help="package JSON; default: km0/tasks.json inside --repo")
    km0.add_argument("--tasks", nargs="*", help="task seq numbers to run; default: all")
    km0.add_argument("--test-command", help="judge: a command whose exit code 0 means solved")
    km0.set_defaults(func=run_km0)

    swe = sub.add_parser("swebench", help="run SWE-bench instances")
    common(swe)
    swe.add_argument("--instances", required=True, help="JSON or JSONL file of SWE-bench instances")
    swe.add_argument("--ids", nargs="*", help="instance ids to run; default: all in the file")
    swe.add_argument("--predicted", help="JSON object instance_id → tier, or {tier, features}")
    swe.add_argument("--dataset", default="verified", help="harness dataset: verified, lite or a HF name")
    swe.add_argument("--no-judge", action="store_true", help="skip the harness (no escalation on tests)")
    swe.add_argument("--repeats", type=int, default=1, help="run each instance this many times")
    swe.set_defaults(func=run_swebench)

    summary = sub.add_parser("summarize", help="aggregate execution reports")
    summary.add_argument("paths", nargs="+", help="run directories or report.json files")
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

    cal = sub.add_parser("calibrate", help="fit KM0's tier rules to execution reports")
    cal.add_argument("--dev", nargs="+", required=True, help="dev-set run directories or reports")
    cal.add_argument("--test", nargs="*", help="test-set run directories or reports, to check the fit")
    cal.add_argument("--rules", help="rules to start from; default: KM0's current defaults")
    cal.add_argument("-o", "--out", default="routing-rules.json", help="calibrated rules for KM0 to import")
    cal.set_defaults(func=run_calibrate)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
