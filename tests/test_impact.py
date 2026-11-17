from fractions import Fraction
from types import SimpleNamespace

import pytest

from mihari import guardrail, impact
from mihari.risk_confirmation import REFERENCE
from mihari.rollout import GRACE, ONSET, SLOW_MS


@pytest.mark.parametrize("case", impact.CASES)
def test_opt_in_keeps_requests_and_supplies_baseline_outcomes(case):
    seed = 950_000
    seen = []
    trace = guardrail.make_workload(case, seed, on_canary=lambda *entry: seen.append(entry))
    assert trace == guardrail.make_workload(case, seed)
    assert [r for r, *_ in seen] == [r for rows in trace for r in rows if r.canary]
    baseline = trace if case in guardrail.NULLS else guardrail.make_workload("healthy", seed)
    assert [(failed, slow) for _, failed, slow in seen] == [
        (r.failed, r.latency_ms > SLOW_MS) for rows in baseline for r in rows if r.canary
    ]


@pytest.mark.parametrize("case", impact.CASES)
def test_ledger_preserves_original_policy_result_and_exposure(case):
    _, path, blocks = impact.workload(case, 950_000)
    if case in guardrail.NULLS:
        assert impact.total_harm(blocks) == impact.Harm(0, 0, 0)
    for rule in guardrail.RULES:
        cutoff, _ = REFERENCE[rule]
        outcome = impact.evaluate(path, blocks, rule, cutoff)
        delay = outcome.at - ONSET if outcome.branch and outcome.window >= ONSET else None
        assert (outcome.branch, delay, outcome.affected_sends) == guardrail.result(path, rule, cutoff)


def test_overlap_counts_incremental_not_baseline_outcomes(monkeypatch):
    from mihari.rollout import Request

    trace = ((Request(0, True, None, True, 170, 9),) * 4,)

    def make(case, seed, *, on_canary):
        for row, (failed, slow) in zip(trace[0], [(False, False), (True, False), (False, True), (True, True)]):
            on_canary(row, failed, slow)
        return trace

    monkeypatch.setattr(impact, "make_workload", make)
    _, _, blocks = impact.workload("combined", 950_000)
    assert blocks[0] == impact.Harm(2, 2, 1)
    assert blocks[0].slow_only == 1
    assert impact.total_harm(blocks[1:]) == impact.Harm(0, 0, 0)


def test_decision_includes_sent_but_unseen_requests_and_excludes_later_sends():
    def item(block, branch):
        return SimpleNamespace(
            look=SimpleNamespace(block=block, at=block + GRACE, exposed=2 if branch else 0),
            trigger=lambda *_: branch,
        )

    path = (item(ONSET - 2, None), item(ONSET - 1, "errors"), item(ONSET, "errors"))
    zero = impact.Harm(0, 0, 0)
    harm = (zero,) * ONSET + (impact.Harm(1, 1, 1), impact.Harm(0, 1, 0), impact.Harm(3, 0, 0))
    outcome = impact.evaluate(path, harm, "errors", 0.1)
    assert outcome == impact.Outcome("errors", ONSET - 1, ONSET + 1, 2, impact.Harm(1, 2, 1))


def test_cost_charges_prefault_evidence_stops_separately_and_overlap_once(capsys):
    early = impact.Outcome("errors", ONSET - 1, ONSET + 1, 5, impact.Harm(1, 2, 1))
    detected = impact.Outcome("latency", ONSET, ONSET + 2, 5, impact.Harm(0, 2, 0))
    terms = impact.summarize("combined", (early, detected))
    assert terms == impact.CostTerms(Fraction(1, 2), Fraction(3, 2), Fraction(1, 2))
    assert terms.score(Fraction(1, 4), 100) == Fraction(407, 8)
    assert "detected 1/2; no-version-fault rollbacks 0; pre-fault-evidence stops 1" in capsys.readouterr().out


@pytest.mark.parametrize(("base", "candidate", "expected"), [
    ((0, 0, 1), (2, 0, 0), "crossover at stop weight 2.00"),
    ((3, 0, 0), (0, 0, 1), "crossover at stop weight 3.00"),
    ((0, 0, 0), (2, 0, 1), "no nonnegative crossover"),
    ((0, 0, 0), (2, 0, 0), "no nonnegative crossover"),
    ((0, 0, 0), (0, 0, 1), "tie at stop weight zero"),
    ((0, 0, 0), (0, 0, 0), "tie at every stop weight"),
])
def test_exact_crossover_and_ties(base, candidate, expected):
    left, right = (impact.CostTerms(*map(Fraction, values)) for values in (base, candidate))
    assert impact.crossover(left, right, Fraction(1, 4)) == expected


def test_mix_and_report_use_frozen_weight_grid(capsys):
    x = impact.CostTerms(Fraction(10), Fraction(4), Fraction(0))
    y = impact.CostTerms(Fraction(0), Fraction(0), Fraction(1, 2))
    assert impact.mix_terms((x, y), (Fraction(1, 10), Fraction(9, 10))) == impact.CostTerms(
        Fraction(1), Fraction(2, 5), Fraction(9, 20)
    )
    assert all(sum(weights) == 100 for weights in impact.MIXES.values())
    by_case = {case: (x, y) for case in impact.CASES}
    impact.sensitivity(by_case)
    output = capsys.readouterr().out
    assert output.count("joint minus error-only") == 12
    assert "crossover at stop weight 22.00" in output
    assert "crossover at stop weight 28.00" in output
