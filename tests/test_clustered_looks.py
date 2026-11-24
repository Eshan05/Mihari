import numpy as np
import pytest
from scipy.stats import rankdata

from mihari import clustered_looks as looks
from mihari import clustered_requests as clustered


def test_look_ten_matches_single_checkpoint_calculation():
    blocks, canary = clustered.draw_blocks(41, bursts=True, fault=True, blocks=looks.LOOKS)
    errors = blocks[:, :clustered.BLOCKS].sum(axis=1)
    paths = looks.p_value_paths(blocks, canary)
    expected = clustered.p_values(errors, canary)
    assert [path[clustered.BLOCKS - 1] for path in paths] == pytest.approx(expected)


def test_p_values_at_each_look_use_only_the_prefix():
    blocks, canary = clustered.draw_blocks(41, bursts=True, fault=True, blocks=looks.LOOKS)
    changed = blocks.copy()
    changed[canary, 5:] = clustered.ATTEMPTS
    for a, b in zip(looks.p_value_paths(blocks, canary), looks.p_value_paths(changed, canary), strict=True):
        np.testing.assert_array_equal(a[:5], b[:5])
        assert a[-1] != b[-1]


def test_fixed_repeated_and_split_schedules_use_their_own_first_crossings():
    path = np.ones(looks.LOOKS)
    path[1] = clustered.ALPHA
    path[4] = clustered.ALPHA / looks.LOOKS
    assert looks.rollback_looks(path) == (looks.NO_STOP, 2, 5)
    path[-1] = 0
    assert looks.rollback_looks(path) == (looks.LOOKS, 2, 5)
    assert looks.rollback_looks(np.ones(looks.LOOKS)) == (looks.NO_STOP,) * 3


def test_exposure_counts_decision_batch_and_caps_misses_at_horizon():
    sent = looks.CANARY_PER_LOOK
    actual = looks.canary_sends([1, 5, looks.LOOKS, looks.NO_STOP])
    assert actual.tolist() == [sent, 5 * sent, looks.LOOKS * sent, looks.LOOKS * sent]


def test_split_tenant_budget_on_every_assignment_of_one_fixed_null_path():
    blocks, canary = clustered.draw_blocks(41, bursts=True, fault=False, blocks=looks.LOOKS)
    sums = blocks.cumsum(axis=1)[clustered.ASSIGNMENTS].sum(axis=1)
    exact_paths = rankdata(-sums, method="max", axis=0) / len(clustered.ASSIGNMENTS)
    selected, = np.flatnonzero(np.all(clustered.ASSIGNMENTS == np.sort(canary), axis=1))
    np.testing.assert_array_equal(looks.p_value_paths(blocks, canary)[1], exact_paths[selected])
    raw_rate = (exact_paths.min(axis=1) <= clustered.ALPHA).mean()
    split_rate = (exact_paths.min(axis=1) <= clustered.ALPHA / looks.LOOKS).mean()
    assert raw_rate > clustered.ALPHA
    assert 0 < split_rate <= clustered.ALPHA


def test_example_shows_time_correction_alone_cannot_fix_the_assignment_unit(capsys):
    looks.show()
    output = capsys.readouterr().out
    request, tenant = output.split("tenant p at look 2:")
    assert "split 5% budget rollback at look 2" in request
    assert "split 5% budget no rollback" in tenant
