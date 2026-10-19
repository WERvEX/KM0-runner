import json

import pytest

from km0_runner.calibrate import calibrate, evaluate, samples, task_cost, tier_costs
from km0_runner.dataset import split
from km0_runner.report import Attempt, ExecutionReport, TestResult
from km0_runner.scoring import RoutingRules, TaskFeatures, decide_tier, score
from km0_runner.stats import bootstrap, unstable_tasks
from km0_runner.summary import plot, summarize, write_points

BASE = {"scope": "single_file", "ambiguity": "low", "reasoning": "follow", "context": "small",
        "hasTests": True, "risks": [], "kind": "code"}


def features(**overrides):
    return TaskFeatures.model_validate({**BASE, **overrides})


# Mirrors KM0's packages/core/src/routing/routing.test.ts.
@pytest.mark.parametrize("overrides,points", [
    ({}, 0), ({"scope": "few_files"}, 1), ({"scope": "new_subsystem"}, 3), ({"ambiguity": "high"}, 2),
    ({"reasoning": "judgement"}, 1), ({"context": "large"}, 1), ({"hasTests": False}, 1),
])
def test_score_matches_km0(overrides, points):
    assert score(features(**overrides), RoutingRules()) == points


def test_hard_rules_match_km0():
    heavy = {"scope": "few_files", "ambiguity": "high", "context": "large", "hasTests": False}
    assert decide_tier(features(**heavy)) == "T2"
    assert decide_tier(features(**heavy, kind="docs")) == "T1"
    assert decide_tier(features(**{**heavy, "scope": "many_files"}, kind="docs")) == "T3"
    assert decide_tier(features(risks=["security"])) == "T2"
    assert decide_tier(features(kind="fix")) == "T2"
    assert decide_tier(features(reasoning="design")) == "T3"


def report(task_id, feats, solved, start="T1", costs=None, run_index=0, judged=True):
    tiers = ["T1", "T2", "T3"]
    end = tiers.index(solved) if solved else 2
    attempts = [
        Attempt(tier=t, model=t, exit_status="Submitted", signal=None, steps=1,
                cost=(costs or {"T1": 0.1, "T2": 0.3, "T3": 1.0})[t], wall_time=1, test_result=None, trajectory=None)
        for t in tiers[tiers.index(start): end + 1]
    ]
    return ExecutionReport(
        task_id=task_id, source="swebench", predicted_tier=decide_tier(TaskFeatures.model_validate(feats)),
        predicted_features=feats, start_tier=start, solved_tier=solved, cost=sum(a.cost for a in attempts),
        steps=len(attempts), wall_time=1, test_result=TestResult(passed=bool(solved) if judged else None, judge="t"),
        failure_category=None if solved else "tests_failed", handoff="discard", config_hash="h",
        runner_version="0", attempts=attempts, run_index=run_index,
    )


def world(n, seed):
    """Tasks whose real difficulty depends on context size more than KM0's default points say."""
    import random
    rng = random.Random(seed)
    out = []
    for i in range(n):
        feats = {**BASE, "scope": rng.choice(["single_file", "few_files"]), "context": rng.choice(["small", "large"]),
                 "ambiguity": rng.choice(["low", "medium"])}
        needed = "T3" if feats["context"] == "large" and feats["ambiguity"] == "medium" else (
            "T2" if feats["context"] == "large" else "T1")
        out.append(report(f"t{seed}-{i}", feats, needed))
    return out


def test_task_cost_counts_every_tier_climbed():
    costs = {"T1": 1, "T2": 3, "T3": 10}
    assert task_cost("T1", "T3", costs) == 14
    assert task_cost("T3", "T1", costs) == 10
    assert task_cost("T2", None, costs) == 13


def test_calibration_improves_dev_and_holds_on_test():
    dev, test = world(80, 1), world(80, 2)
    result = calibrate(dev, test)
    assert result.dev_after.expected_cost < result.dev_before.expected_cost
    assert result.dev_after.accuracy > result.dev_before.accuracy
    assert result.test_after.accuracy > result.test_before.accuracy
    assert result.tier_costs == {"T1": 0.1, "T2": 0.3, "T3": 1.0}
    # The fitted rules are KM0's import format.
    assert set(json.loads(result.rules.model_dump_json())) == {"featurePoints", "tierLimits"}


def test_samples_skip_unjudged_unfeatured_and_censored_reports():
    feats = dict(BASE)
    kept = report("a", feats, "T1")
    started_high = report("b", feats, "T3", start="T2")
    unjudged = report("c", feats, None, judged=False)
    no_features = report("d", feats, "T1").model_copy(update={"predicted_features": None})
    assert [s.task_id for s in samples([kept, started_high, unjudged, no_features])] == ["a"]


def test_tier_costs_fall_back_for_tiers_never_run():
    assert tier_costs([report("a", dict(BASE), "T1")]) == {"T1": 0.1, "T2": 3.0, "T3": 10.0}


def test_bootstrap_and_repeats():
    assert bootstrap([1.0]) is None
    low, high = bootstrap([0, 1] * 20)
    assert low < 0.5 < high
    runs = [report("a", dict(BASE), "T1", run_index=0), report("a", dict(BASE), None, run_index=1),
            report("b", dict(BASE), "T1")]
    assert unstable_tasks(runs) == ["a"]
    summary = summarize(runs)
    assert summary.tasks == 2 and summary.runs == 3
    assert summary.solve_rate == 0.75  # a: 1 of 2 repeats, b: 1 of 1


def test_split_is_stratified_and_reproducible():
    rows = [{"instance_id": f"{repo}__{repo}-{i}", "repo": repo} for repo, n in (("a", 60), ("b", 30), ("c", 10))
            for i in range(n)]
    dev, test = split(rows, 20, seed=3)
    assert len(dev) == len(test) == 10
    odd, rest = split([{"instance_id": f"r{i % 4}__x-{i}"} for i in range(40)], 12)
    assert len(odd) == len(rest) == 6  # 3 per repository must not round to 2 + 1 everywhere
    repos = [r["repo"] for r in dev + test]
    assert (repos.count("a"), repos.count("b"), repos.count("c")) == (12, 6, 2)
    assert not {r["instance_id"] for r in dev} & {r["instance_id"] for r in test}
    assert split(rows, 20, seed=3) == (dev, test)
    assert split(rows, 20, seed=4) != (dev, test)


def test_plot_and_points(tmp_path):
    summaries = [summarize(world(20, 1), name="cascade"), summarize(world(20, 2), name="all-T3")]
    write_points(summaries, tmp_path / "points.csv")
    assert (tmp_path / "points.csv").read_text().splitlines()[0].startswith("run,tasks,mean_cost")
    pytest.importorskip("matplotlib")
    plot(summaries, tmp_path / "scatter.png")
    assert (tmp_path / "scatter.png").stat().st_size > 1000


def test_evaluate_confusion_counts_unsolved():
    data = samples([report("a", dict(BASE), None)])
    result = evaluate(RoutingRules(), data, {"T1": 1, "T2": 1, "T3": 1})
    assert result.confusion["T1"]["unsolved"] == 1 and result.under == 1
