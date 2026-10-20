"""analysis/tension.py: choice of the supply-tension indicator."""

import numpy as np
import pandas as pd
import pytest

from analysis import tension


def test_eta_r2_is_one_for_a_perfect_step_and_zero_for_noise():
    x = np.repeat(np.arange(10.0), 50)
    assert tension.eta_r2(x, 3.0 * x, n_bins=10) == pytest.approx(1.0)
    g = np.random.default_rng(0)
    assert tension.eta_r2(g.random(5000), g.random(5000), n_bins=10) < 0.01
    assert tension.eta_r2([np.nan, 1.0, 2.0], [5.0, 1.0, 2.0], n_bins=2) == pytest.approx(1.0)   # NaN row dropped


def test_eta_r2_never_splits_ties_across_bins():
    x = np.r_[np.zeros(900), np.arange(1.0, 101.0)]                 # 90% ties at 0
    y = np.r_[np.full(900, 4.0), np.full(100, 1.0)]
    assert tension.eta_r2(x, y, n_bins=10) == pytest.approx(1.0)


def test_bad_capture_takes_the_lowest_values_and_splits_ties_pro_rata():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    bad = np.array([True, True, False, False])
    assert tension.bad_capture(x, bad, 0.5) == pytest.approx(1.0)
    assert tension.bad_capture(-x, bad, 0.5) == pytest.approx(0.0)
    # Two-thirds of the rows tie at 0 and half of them are bad: flagging a third takes half the tie.
    tie = np.array([0.0, 0.0, 0.0, 0.0, 1.0, 1.0])
    bad_tie = np.array([True, True, False, False, False, False])
    assert tension.bad_capture(tie, bad_tie, 1 / 3) == pytest.approx(0.5)


def tiny_market():
    """Two slots x four cells; cells 0, 1 form cluster 0 and cells 2, 3 cluster 1."""
    snaps = pd.DataFrame({
        "slot": np.repeat([0, 1], 4), "cell": np.tile([0, 1, 2, 3], 2),
        "idle_avg": [0.0, 2.0, 1.0, 3.0, 1.0, 1.0, 1.0, 1.0],
        "enroute_avg": [1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        "slack_lag_slot": [np.nan] * 4 + [0.0, 2.0, np.inf, 3.0],
        "slack_lag_day": [np.nan] * 4 + [np.nan, 1.0, 1.0, 1.0],
    })
    sessions = pd.DataFrame({"session_id": [10, 11, 12, 13], "slot": [1, 1, 1, 1], "hour": [5, 5, 6, 6],
                             "pu_cell": [0, 1, 2, 3], "quoted_eta_min": [9.0, 3.0, 2.0, 4.0],
                             "no_supply": [True, False, False, False], "in_window": True})
    return sessions, snaps, np.array([0, 0, 1, 1])


def test_tension_frame_uses_only_the_previous_slot():
    sessions, snaps, clusters = tiny_market()
    neighbors = np.array([[1, -1], [0, 2], [1, 3], [2, -1]])                        # a line 0-1-2-3
    f = tension.tension_frame(sessions, snaps, clusters, slack_cap=10.0, ar_weights=(0.7, 0.3), neighbors=neighbors)
    np.testing.assert_allclose(f["cell_persistence"], [0.0, 2.0, 10.0, 3.0])          # inf capped
    np.testing.assert_allclose(f["cell_ar"], [0.0, 0.7 * 2 + 0.3, 0.7 * 10 + 0.3, 0.7 * 3 + 0.3])  # NaN day -> lag
    np.testing.assert_allclose(f["cell_idle"], [0.0, 2.0, 1.0, 3.0])                  # slot 0 values
    np.testing.assert_allclose(f["cluster7"], [2.0 / 2.0, 2.0 / 2.0, 4.0 / 1.0, 4.0 / 1.0])
    np.testing.assert_allclose(f["system"], [6.0 / 3.0] * 4)
    # ring of cell 0 = {0, 1}: idle 2 / en-route 2; cell 1 = {0, 1, 2}: 3 / 2; cell 2 = {1, 2, 3}: 6 / 2; cell 3 = {2, 3}: 4 / 1
    np.testing.assert_allclose(f["ring1"], [1.0, 1.5, 3.0, 4.0])


def test_score_indicators_ranks_an_informative_indicator_first():
    g = np.random.default_rng(1)
    n = 4000
    tight = g.random(n)
    frame = pd.DataFrame({"quoted_eta_min": 2 + 10 * (1 - tight) ** 4 + g.normal(0, 0.5, n),
                          "no_supply": False, "hour": g.integers(0, 24, n)})
    for c in tension.CANDIDATES:
        if c != "hour":
            frame[c] = g.random(n)                                           # uninformative
    frame["system"] = tight
    table = tension.score_indicators(frame, n_bins=10, bad_eta_min=8.0, share=0.2).set_index("indicator")
    assert table["eta_r2"].idxmax() == "system" and table["bad_capture"].idxmax() == "system"
    assert table.loc["cell_persistence", "eta_r2"] < 0.02
    assert (table["coverage"] == 1.0).all()
