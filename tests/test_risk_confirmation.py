import pytest

from mihari import matched_risk as old
from mihari import risk_confirmation as confirm


def test_predeclared_seed_ranges_are_disjoint():
    previous = range(old.SEED_BASE, old.SEED_BASE + 2 * max(old.CALIBRATION_TRIALS, old.HOLDOUT_TRIALS))
    assert confirm.NULL_SEEDS == range(900_000, 912_000)
    assert confirm.FAULT_SEEDS == range(950_000, 953_000)
    assert set(confirm.NULL_SEEDS).isdisjoint(previous)
    assert set(confirm.FAULT_SEEDS).isdisjoint(previous)
    assert set(confirm.NULL_SEEDS).isdisjoint(confirm.FAULT_SEEDS)


def test_calibration_uses_original_size_and_rejects_drift(monkeypatch):
    points = {
        rule: old.OperatingPoint(cutoff, counts)
        for rule, (cutoff, counts) in confirm.REFERENCE.items()
    }
    calls = []

    def calibrate(trials):
        calls.append(trials)
        return points

    monkeypatch.setattr(confirm, "calibrate", calibrate)
    assert confirm.frozen_points() is points
    assert calls == [2_000]
    points["joint"] = old.OperatingPoint(points["joint"].cutoff, (39, 99))
    with pytest.raises(ValueError, match="original joint calibration"):
        confirm.frozen_points()


@pytest.mark.parametrize(("scenario", "onset"), [("healthy", None), ("combined", confirm.ONSET)])
def test_pair_evaluates_both_frozen_rules_on_one_path(monkeypatch, scenario, onset):
    points = {"errors": old.OperatingPoint(0.1, (0, 0)), "joint": old.OperatingPoint(0.2, (0, 0))}
    generated = []
    evaluated = []

    def workload(case, seed):
        generated.append((case, seed))
        return seed

    def result(path, rule, cutoff):
        evaluated.append((path, rule, cutoff))
        return None, None, 5

    monkeypatch.setattr(confirm, "make_workload", workload)
    monkeypatch.setattr(confirm, "decisions", lambda seed, injected: iter((seed, injected)))
    monkeypatch.setattr(confirm, "result", result)
    assert len(list(confirm.paired_results(scenario, (900_000, 900_001), points))) == 2
    assert generated == [(scenario, 900_000), (scenario, 900_001)]
    for i, seed in enumerate((900_000, 900_001)):
        left, right = evaluated[2 * i:2 * i + 2]
        assert left == ((seed, onset), "errors", 0.1)
        assert right == ((seed, onset), "joint", 0.2)
        assert left[0] is right[0]


def test_null_reports_pairing_and_uncertainty_with_no_disagreements(capsys):
    neither = ((None, None, 6), (None, None, 6))
    low, high = confirm.report_null("healthy", [neither] * 1_000)
    assert -old.MARGIN < low < 0 < high < old.MARGIN
    output = capsys.readouterr().out
    assert "neither 1000" in output and "simultaneous 95% interval" in output


@pytest.mark.parametrize(("second_null", "matched"), [((-.01, .01), True), ((-.015, .01), False)])
def test_decision_requires_both_nulls_and_reports_every_fault(monkeypatch, capsys, second_null, matched):
    intervals = iter(((-.01, .01), second_null))
    calls = []
    points = {rule: old.OperatingPoint(0.1, (0, 0)) for rule in confirm.RULES}

    def paired(scenario, seeds, used):
        assert used is points
        calls.append((scenario, seeds))
        return iter([(("errors", None, 0), ("latency", 3, 20)),
                     ((None, None, 40), ("errors", 5, 30))])

    monkeypatch.setattr(confirm, "frozen_points", lambda: points)
    monkeypatch.setattr(confirm, "report_null", lambda *_: next(intervals))
    monkeypatch.setattr(confirm, "paired_results", paired)
    confirm.run()
    output = capsys.readouterr().out
    assert ("false-rollback risks matched within" in output) is matched
    assert ("not an equal-risk ranking" in output) is not matched
    assert "5% held-out risk cap" in output
    for scenario in confirm.FAULTS:
        assert f"{scenario} / fresh faults" in output
        assert (scenario, confirm.FAULT_SEEDS) in calls
    assert all((scenario, confirm.NULL_SEEDS) in calls for scenario in confirm.NULLS)
    assert "missed 2 (1 pre-fault stops)" in output
    assert "missed 0 (0 pre-fault stops)" in output
    assert f"{confirm.HORIZON:.2f}; mean affected-window canary sends 20.0" in output
    assert "4.00; mean affected-window canary sends 25.0" in output
    assert "Paired detection difference (joint minus errors): +100.00 pp" in output
