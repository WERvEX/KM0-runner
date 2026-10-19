"""Command line: `km0-runner km0 | swebench | summarize`."""

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
from .summary import load_reports, summarize  # noqa: E402
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
    cascade = Cascade(config, args.out, judge=judge, patch_command="git add -A -- . ':!km0' && git diff --cached")
    return _run_all(cascade, tasks, args)


def run_swebench(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    predicted = json.loads(Path(args.predicted).read_text()) if args.predicted else None
    tasks = load_swebench_instances(args.instances, predicted=predicted, ids=args.ids)
    dataset = SWEBENCH_DATASETS.get(args.dataset, args.dataset)
    judge: Judge = NoJudge() if args.no_judge else SwebenchHarnessJudge(dataset, Path(args.out) / "_harness")
    cascade = Cascade(config, args.out, judge=judge)
    return _run_all(cascade, tasks, args)


def _run_all(cascade: Cascade, tasks, args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "reports.jsonl").open("a", encoding="utf-8") as reports:
        for task in tasks:
            report = cascade.run(task, start_tier=args.start_tier)
            reports.write(report.model_dump_json() + "\n")
            reports.flush()
            print(
                f"{task.task_id}: predicted={report.predicted_tier} solved={report.solved_tier} "
                f"escalations={len(report.escalations)} cost=${report.cost:.4f} "
                f"failure={report.failure_category}"
            )
    return 0


def run_summarize(args: argparse.Namespace) -> int:
    print(summarize(load_reports(args.paths)).model_dump_json(indent=2))
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
    swe.add_argument("--predicted", help="JSON object instance_id → KM0's predicted tier")
    swe.add_argument("--dataset", default="verified", help="harness dataset: verified, lite or a HF name")
    swe.add_argument("--no-judge", action="store_true", help="skip the harness (no escalation on tests)")
    swe.set_defaults(func=run_swebench)

    summary = sub.add_parser("summarize", help="aggregate execution reports")
    summary.add_argument("paths", nargs="+", help="run directories or report.json files")
    summary.set_defaults(func=run_summarize)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
