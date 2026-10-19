"""A fixed, reproducible SWE-bench subset split into dev and test, stratified by repository."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from .handoff import read_rows

HF_DATASETS = {
    "verified": "princeton-nlp/SWE-bench_Verified",
    "lite": "princeton-nlp/SWE-bench_Lite",
}


def load_source(source: str) -> list[dict[str, Any]]:
    """Instances from a JSON/JSONL file, or from Hugging Face (`verified`, `lite`, a dataset name)."""
    if Path(source).exists():
        return read_rows(source)
    from datasets import load_dataset  # installed with the `swebench` extra

    return [dict(row) for row in load_dataset(HF_DATASETS.get(source, source), split="test")]


def split(
    rows: list[dict[str, Any]], size: int, dev_share: float = 0.5, seed: int = 0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Draw `size` instances, spread over repositories in proportion, then split each repository's draw.

    The same rows, size and seed always give the same split, so the test set stays fixed across runs.
    """
    rng = random.Random(seed)
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda row: row["instance_id"]):
        by_repo[row.get("repo") or row["instance_id"].split("__")[0]].append(row)
    size = min(size, len(rows))
    # Largest-remainder allocation of the sample over repositories.
    quotas = {repo: size * len(items) / len(rows) for repo, items in by_repo.items()}
    counts = {repo: int(quota) for repo, quota in quotas.items()}
    for repo in sorted(quotas, key=lambda r: (quotas[r] - counts[r], r), reverse=True)[: size - sum(counts.values())]:
        counts[repo] += 1
    drawn = {repo: rng.sample(by_repo[repo], counts[repo]) for repo in sorted(by_repo)}
    # The dev share is allocated over repositories the same way, so the totals hit it exactly.
    dev_quotas = {repo: len(items) * dev_share for repo, items in drawn.items()}
    cuts = {repo: int(quota) for repo, quota in dev_quotas.items()}
    extra = round(size * dev_share) - sum(cuts.values())
    for repo in sorted(dev_quotas, key=lambda r: (dev_quotas[r] - cuts[r], r), reverse=True)[:extra]:
        cuts[repo] += 1
    dev, test = [], []
    for repo, items in drawn.items():
        dev.extend(items[: cuts[repo]])
        test.extend(items[cuts[repo] :])
    return dev, test


def write_split(dev: list[dict[str, Any]], test: list[dict[str, Any]], out_dir: str | Path, meta: dict) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("dev", dev), ("test", test)):
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
    ids = {"dev": [r["instance_id"] for r in dev], "test": [r["instance_id"] for r in test]}
    (out / "split.json").write_text(json.dumps({**meta, **ids}, indent=2), encoding="utf-8")
