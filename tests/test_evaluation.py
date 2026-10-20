"""analysis/evaluation.py: gap closed, split-seed regret and recovered gain, switch table, on synthetic results."""

import numpy as np
import pandas as pd
import pytest

from analysis import evaluation


def policy_runs(n_seeds=20, seed=0):
    g = np.random.default_rng(seed)
    base = 20000 + 100 * g.standard_normal(n_seeds)
    levels = {"all_on_B": 0.0, "random": 200.0, "tier": 900.0, "oracle": 1200.0, "pi_hourB": 1500.0}
    rows = []
    for s in range(n_seeds):
        for label, lift in levels.items():
            rows.append({"seed": s, "label": label, "policy": "threshold" if label not in ("all_on_B",) else "all_on",
                         "theta": 2.0 if label == "pi_hourB" else 0.0, "N_completed": base[s] + lift + 30 * g.standard_normal(),
                         "n_switches_per_cell_day": 6.0 + g.standard_normal() if label == "pi_hourB" else 0.0,
                         "share_cells_off": 0.5 if label == "pi_hourB" else 0.0})
    return pd.DataFrame(rows)


def test_gap_closed_is_the_paired_ratio_and_cell_tiers_can_exceed_one():
    runs = policy_runs()
    t = evaluation.gap_table(runs)
    g = t.set_index("label")
    assert g.loc["oracle", "gap_closed"] == pytest.approx(1.0)
    assert 0.6 < g.loc["tier", "gap_closed"] < 0.9 and g.loc["tier", "ci_lo"] < g.loc["tier", "gap_closed"] < g.loc["tier", "ci_hi"]
    assert g.loc["pi_hourB", "gap_closed"] > 1.0
    assert g.loc["random", "dN_vs_ref"] == pytest.approx(runs.pivot(index="seed", columns="label", values="N_completed")
                                                           .pipe(lambda w: (w["random"] - w["all_on_B"]).mean()))


def sweep_runs(n_seeds=20, seed=1):
    g = np.random.default_rng(seed)
    thetas = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]
    curve = {0.0: 0.0, 0.5: 100.0, 1.0: 220.0, 1.5: 300.0, 2.0: 320.0, 3.0: 250.0, 5.0: 50.0}
    base = 20000 + 100 * g.standard_normal(n_seeds)
    rows = [{"seed": s, "policy": "threshold", "theta": t, "N_completed": base[s] + curve[t] + 25 * g.standard_normal(),
             "share_cells_off": t / 10, "voucher_spent_vnd": 1e8, "budget_B_vnd": 1e8} for s in range(n_seeds) for t in thetas]
    return pd.DataFrame(rows)


def test_gain_recovered_and_split_seed_regret():
    pr = sweep_runs()
    full = evaluation.gain_recovered(pr, 1.5)
    assert full["theta_best"] == 2.0 and 0.85 < full["gain_recovered"] < 1.0 and full["max_gain"] > 250
    assert evaluation.gain_recovered(pr, 0.0)["gain_recovered"] == pytest.approx(0.0)
    split = evaluation.split_seed_regret(pr)
    assert list(split["chosen_on"]) == ["A", "B"] and (split["n_seeds_heldout"] == 10).all()
    assert (split["theta_chosen"].isin([1.5, 2.0, 3.0])).all()          # the plateau around the true optimum
    assert (split["regret"] < 60).all() and (split["gain_recovered"] > 0.8).all()
    with pytest.raises(ValueError):
        evaluation.gain_recovered(pr[pr["theta"] > 0], 1.0)


def test_switch_table_proposes_hold_times_only_for_rows_that_switch():
    t = evaluation.switch_table(policy_runs()).set_index("label")
    assert t.loc["pi_hourB", "switches_mean"] == pytest.approx(6.0, abs=1.0)
    assert t.loc["pi_hourB", "hold_min_for_4_per_day"] == 360.0 and t.loc["pi_hourB", "hold_min_for_8_per_day"] == 0.0
    assert t.loc["tier", "hold_min_for_2_per_day"] == 0.0


def test_markdown_renders_numbers_and_strings():
    text = evaluation.markdown(pd.DataFrame({"a": ["x"], "b": [1.2345]}), ["a", "b"], 2)
    assert "| x | 1.23 |" in text


def test_shock_slot_mask_and_gain_split():
    shocks = pd.DataFrame({"kind": ["day_demand", "attendance", "rain", "event"], "cell": [-1, -1, -1, 7],
                           "start_s": [0.0, 0.0, 150.0, 820.0], "end_s": [1000.0, 1000.0, 260.0, 900.0]})
    mask = evaluation.shock_slot_mask(shocks, slot_s=100.0, n_slots=10)
    assert mask.tolist() == [False, True, True, False, False, False, False, False, True, False]   # day-level kinds ignored
    assert evaluation.shock_slot_mask(shocks, 100.0, 10, kinds=("event",)).sum() == 1
    assert evaluation.shock_slot_mask(shocks, 100.0, 10, kinds=("day_demand",)).all()
    delta = np.array([0, 5, 5, -1, 0, 0, 0, 0, 2, -1])
    g = evaluation.gain_by_shock_slots(delta, mask)
    assert g["gain_shock"] == 12 and g["gain_quiet"] == -2 and g["gain_total"] == 10
    assert g["share_gain_shock"] == pytest.approx(1.2) and g["share_slots_shock"] == pytest.approx(0.3)
    assert g["gain_per_shock_slot"] == pytest.approx(4.0) and g["gain_per_quiet_slot"] == pytest.approx(-2 / 7)


def test_completed_by_slot_counts_completed_in_window_orders_at_their_request_slot():
    orders = pd.DataFrame({"request_time_s": [10.0, 150.0, 199.0, 250.0, 310.0],
                           "status": ["Completed", "Completed", "Cancelled", "Completed", "Completed"],
                           "in_window": [False, True, True, True, True]})
    assert evaluation.completed_by_slot(orders, 100.0, 4).tolist() == [0, 1, 1, 1]


def test_shock_split_summary_pairs_rates_by_seed():
    per_seed = pd.DataFrame([evaluation.gain_by_shock_slots(d, m) for d, m in (
        (np.array([3, 3, 0, 0]), np.array([True, True, False, False])),
        (np.array([2, 0, 0, 1]), np.array([True, False, False, False])),
        (np.array([0, 0, 1, 1]), np.array([False, False, False, False])))])
    out = evaluation.shock_split_summary(per_seed)
    assert out["n_seeds"] == 3 and out["n_seeds_with_shock"] == 2
    assert out["gain_total"] == pytest.approx(11 / 3) and out["share_gain_shock"] == pytest.approx(8 / 11)
    assert out["rate_diff"] == pytest.approx(((3 - 0) + (2 - 1 / 3)) / 2)
    assert out["rate_diff_lo"] < out["rate_diff"] < out["rate_diff_hi"]
