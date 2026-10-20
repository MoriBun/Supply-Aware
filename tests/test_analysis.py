"""analysis/: policy value with CI, paired differences, Qini / AUUC, run loading."""

import math

import numpy as np
import pandas as pd
import pytest

from analysis import check_budget
from analysis.check_dataset import check_run_dir, markdown_row
from analysis.io import HIDDEN_TABLES, completed_outcome, load_hidden, load_run
from analysis.metrics import (
    bootstrap_mean_ci, paired_difference, qini_auuc, sweep_regret, t_quantile, theta_star_gaps, theta_star_interval,
    theta_star_set, uplift_curve, value_table,
)
from sim.config import load_config
from sim.runner import evaluate, generate, gte
from tests.conftest import ROOT

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]


# --- policy value ------------------------------------------------------------------------------


def results_frame() -> pd.DataFrame:
    rows = []
    for seed, (n_on, n_off) in enumerate(((100, 90), (110, 95), (105, 100), (120, 101))):
        rows.append({"policy": "all_on", "theta": np.nan, "seed": seed, "N_completed": n_on,
                     "V_profit_vnd": 1.0 * n_on, "voucher_spent_vnd": 50.0})
        rows.append({"policy": "all_off", "theta": np.nan, "seed": seed, "N_completed": n_off,
                     "V_profit_vnd": 2.0 * n_off, "voucher_spent_vnd": 0.0})
    return pd.DataFrame(rows)


def test_value_table_means_and_cis():
    t = value_table(results_frame()).set_index("policy")
    assert list(t.index) == ["all_off", "all_on"] and (t["n_seeds"] == 4).all()
    on = t.loc["all_on"]
    assert on["N_mean"] == 108.75 and on["N_se"] == pytest.approx(np.std([100, 110, 105, 120], ddof=1) / 2)
    assert on["N_lo"] == pytest.approx(on["N_mean"] - 1.959964 * on["N_se"], rel=1e-6)
    assert on["N_hi"] > on["N_mean"] > on["N_lo"] and on["spent_mean"] == 50.0 and on["V_mean"] == 108.75
    assert "theta" not in t.columns                                           # all NaN: not a key
    single = value_table(results_frame().iloc[:2])
    assert math.isnan(single["N_se"][0])                                      # one seed: no SE


def test_value_table_groups_by_theta_when_present():
    df = results_frame()
    df["theta"] = [0.0, 0.0, 0.5, 0.5] * 2
    t = value_table(df)
    assert list(t.columns[:2]) == ["policy", "theta"] and len(t) == 4
    assert t[(t.policy == "all_on") & (t.theta == 0.5)]["n_seeds"].iloc[0] == 2


def test_paired_difference_by_seed():
    d = paired_difference(results_frame(), "all_on", "all_off")
    assert d["n_seeds"] == 4 and d["mean"] == pytest.approx(np.mean([10, 15, 5, 19]))
    assert d["se"] == pytest.approx(np.std([10, 15, 5, 19], ddof=1) / 2) and d["share_a_better"] == 1.0
    assert d["lo"] < d["mean"] < d["hi"]
    v = paired_difference(results_frame(), "all_on", "all_off", metric="V_profit_vnd")
    assert v["mean"] == pytest.approx(np.mean([100 - 180, 110 - 190, 105 - 200, 120 - 202]))
    with pytest.raises(ValueError):
        paired_difference(results_frame(), "all_on", "legacy")


def test_bootstrap_ci_contains_the_mean_and_is_deterministic():
    x = np.array([100, 110, 105, 120], dtype=float)
    lo, hi = bootstrap_mean_ci(x, n_boot=500, seed=1)
    assert lo <= x.mean() <= hi and (lo, hi) == bootstrap_mean_ci(x, n_boot=500, seed=1)
    assert all(math.isnan(v) for v in bootstrap_mean_ci([]))


# --- theta* as a set, and regret of an estimated threshold -------------------------------------


def sweep_results() -> pd.DataFrame:
    """4 seeds x 4 thetas: 0.5 is best, 1.0 is on the plateau, 0 and 5 are clearly worse."""
    base = np.array([100.0, 110.0, 105.0, 120.0])
    by_theta = {0.0: base - 10 + np.array([0, 1, -1, 0]), 0.5: base + np.array([1, -1, 0, 1]),
                1.0: base + np.array([0, 1, 1, -2]), 5.0: base - 20 + np.array([1, 0, -1, 0])}
    return pd.DataFrame([{"policy": "threshold", "theta": theta, "seed": seed, "N_completed": values[seed]}
                         for theta, values in by_theta.items() for seed in range(4)])


def test_t_quantile_matches_table_values():
    assert t_quantile(0.975, 9) == pytest.approx(2.262, abs=0.005)
    assert t_quantile(0.975, 29) == pytest.approx(2.045, abs=0.005)
    assert t_quantile(0.95, 4) == pytest.approx(2.132, abs=0.01)
    assert t_quantile(0.975, 1000) == pytest.approx(1.962, abs=0.002)        # tends to the normal quantile


def test_theta_star_set_corrects_for_comparing_many_thetas_with_the_best():
    # A theta whose gap would fail a single 95% test stays in the set under the simultaneous bound.
    gen = np.random.default_rng(5)
    seeds = 10
    noise = {theta: gen.normal(0.0, 6.0, seeds) for theta in range(8)}
    rows = [{"policy": "threshold", "theta": float(theta), "seed": s,
             "N_completed": 1000.0 + (0.0 if theta else -60.0) + noise[theta][s]}
            for theta in range(8) for s in range(seeds)]
    star = theta_star_set(pd.DataFrame(rows))
    assert star["crit"].iloc[0] == pytest.approx(t_quantile(1 - 0.05 / 7, 9)) and star["crit"].iloc[0] > 3.0
    assert not star["in_set"].iloc[0]                                           # theta 0 is 60 below: out
    assert star["in_set"].iloc[1:].all()                                        # the seven equal thetas: all in
    assert theta_star_interval(star)[1:] == (1.0, 7.0)
    assert (star["gap_lo"] <= star["gap_to_best"]).all() and (star["gap_to_best"] >= 0).all()


def test_theta_star_set_keeps_the_plateau_and_drops_the_clearly_worse():
    star = theta_star_set(sweep_results())
    assert star["theta"].tolist() == [0.0, 0.5, 1.0, 5.0] and (star["n_seeds"] == 4).all()
    assert star["in_set"].tolist() == [False, True, True, False]
    row0 = star.iloc[0]
    assert row0["gap_to_best"] == pytest.approx(10.25) and row0["gap_se"] == pytest.approx(0.75)
    assert row0["gap_lo"] > 0                                                    # significantly worse than the best
    assert star.iloc[1]["gap_to_best"] == 0.0 and star.iloc[2]["gap_to_best"] == pytest.approx(0.25)
    assert star.iloc[2]["gap_lo"] < 0 < star.iloc[2]["gap_hi"]
    assert theta_star_interval(star) == (0.5, 0.5, 1.0) and theta_star_gaps(star) == []
    # The interval is the hull of the set: a theta dropped inside the plateau is reported as a gap.
    holed = star.copy()
    holed["theta"] = [0.0, 0.5, 0.75, 1.0]
    holed["in_set"] = [False, True, False, True]
    assert theta_star_interval(holed) == (0.5, 0.5, 1.0) and theta_star_gaps(holed) == [0.75]


def test_sweep_regret_on_grid_between_grid_points_and_outside():
    results = sweep_results()
    on_plateau = sweep_regret(results, 1.0)
    assert on_plateau["theta_best"] == 0.5 and on_plateau["regret"] == pytest.approx(0.25)
    assert on_plateau["lo"] < 0 < on_plateau["hi"]                               # no measurable regret
    assert sweep_regret(results, 0.5)["regret"] == 0.0
    assert sweep_regret(results, 0.0)["regret"] == pytest.approx(10.25)
    halfway = sweep_regret(results, 0.25)                                        # interpolated per seed
    assert halfway["regret"] == pytest.approx(10.25 / 2) and halfway["n_seeds"] == 4
    assert halfway["relative"] == pytest.approx(halfway["regret"] / 109.0)
    with pytest.raises(ValueError):
        sweep_regret(results, 6.0)


def test_sweep_by_theta_and_overview():
    from analysis.metrics import sweep_by_theta, sweep_overview
    results = sweep_results().assign(share_cells_off=lambda d: d["theta"] / 10, voucher_spent_vnd=40.0,
                                     budget_B_vnd=50.0)
    by = sweep_by_theta(results)
    assert by["theta"].tolist() == [0.0, 0.5, 1.0, 5.0] and by["in_set"].tolist() == [False, True, True, False]
    assert by["mean"].iloc[1] == pytest.approx(109.0) and by["spent_over_B"].eq(0.8).all()
    assert by["share_cells_off"].tolist() == pytest.approx([0.0, 0.05, 0.1, 0.5])
    over = sweep_overview({"a": results, "b": results.assign(N_completed=results["N_completed"] + 1)},
                          theta_hats=(0.25,)).set_index("sweep")
    assert over.loc["a", "theta_best"] == 0.5 and (over.loc["a", "star_lo"], over.loc["a", "star_hi"]) == (0.5, 1.0)
    assert over.loc["a", "regret_0"] == pytest.approx(10.25) and over.loc["a", "regret_0.25"] == pytest.approx(5.125)
    assert over.loc["b", "mean_theta0"] == pytest.approx(over.loc["a", "mean_theta0"] + 1)


# --- Qini / AUUC --------------------------------------------------------------------------------


def test_uplift_curve_perfect_ranking():
    y, t, score = [1, 0, 0, 0], [1, 0, 1, 0], [0.9, 0.8, 0.2, 0.1]
    curve = uplift_curve(y, t, score)
    assert curve["k"].tolist() == [0, 1, 2, 3, 4] and curve["frac"].tolist() == [0, 0.25, 0.5, 0.75, 1.0]
    assert curve["n_t"].tolist() == [0, 1, 1, 2, 2] and curve["n_c"].tolist() == [0, 0, 1, 1, 2]
    assert curve["qini"].tolist() == [0, 1, 1, 1, 1]                             # empty control group counts 0
    assert curve["uplift"].tolist() == [0, 1, 2, 1.5, 2]
    areas = qini_auuc(y, t, score)
    assert areas["qini_end"] == 1.0 and areas["qini_random_area"] == 0.5
    assert areas["qini_area"] == pytest.approx(0.875) and areas["qini_coef"] == pytest.approx(0.375)
    assert areas["auuc"] == pytest.approx(np.trapezoid([0, 1, 2, 1.5, 2], [0, 0.25, 0.5, 0.75, 1.0]))


def test_uplift_curve_groups_tied_scores():
    y, t = [1, 0, 1, 0, 0, 1], [1, 0, 1, 0, 1, 0]
    curve = uplift_curve(y, t, [0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    assert curve["k"].tolist() == [0, 3, 6]                                      # one point per distinct score
    assert curve["qini"].iloc[1] == pytest.approx(2 - 0 * 2 / 1) and curve["qini"].iloc[2] == pytest.approx(2 - 1 * 3 / 3)
    constant = qini_auuc(y, t, [0.5] * 6)
    assert constant["qini_coef"] == pytest.approx(0.0)                            # a constant score is the random line


def test_qini_is_nan_without_both_arms_and_checks_lengths():
    assert all(math.isnan(v) for v in qini_auuc([1, 0], [1, 1], [0.2, 0.1]).values())
    assert all(math.isnan(v) for v in qini_auuc([1, 0], [0, 0], [0.2, 0.1]).values())
    with pytest.raises(ValueError):
        uplift_curve([1, 0], [1], [0.5, 0.4])


def test_random_score_has_small_qini_and_true_uplift_ranking_a_large_one():
    gen = np.random.default_rng(3)
    n = 20000
    tau = gen.uniform(0.0, 0.4, n)                                              # true uplift per session
    t = gen.random(n) < 0.5
    y = (gen.random(n) < 0.2 + tau * t).astype(int)
    good = qini_auuc(y, t, tau)["qini_coef"]
    noise = qini_auuc(y, t, gen.random(n))["qini_coef"]
    assert good > 5 * abs(noise) and good > 0


# --- loading runs -------------------------------------------------------------------------------


def test_completed_outcome_maps_orders_to_sessions():
    sessions = pd.DataFrame({"session_id": [10, 11, 12, 13]})
    orders = pd.DataFrame({"session_id": [11, 13, 12], "status": ["Completed", "Cancelled", "Completed"]})
    np.testing.assert_array_equal(completed_outcome(sessions, orders), [0, 1, 1, 0])


def test_load_run_never_returns_hidden_tables(tmp_path):
    cfg = load_config(TINY, ["policy.name=all_on", "sweep.n_seeds=1", "runner.n_procs=1", "budget.enforce=false"])
    evaluate(cfg, tmp_path, log_level="full")                                  # one run: tables at the root
    run = load_run(tmp_path)
    assert {"riders", "sessions", "orders", "slot_snapshots", "policy_results", "run_metadata"} <= set(run)
    assert not {"riders_hidden", "sessions_hidden"} & set(run)
    hidden = load_hidden(tmp_path)
    assert set(hidden) == {name.split("/")[-1] for name in HIDDEN_TABLES}
    y = completed_outcome(run["sessions"], run["orders"])
    assert y.sum() == run["policy_results"]["N_completed"][0] + (
        (run["orders"]["status"] == "Completed") & ~run["orders"]["in_window"]).sum()
    assert (run["sessions"]["arm"] == 1).all()
    # Rider features live in observed/riders and are joined on rider_id, never read from hidden/.
    x_freq = run["riders"].set_index("rider_id").loc[run["sessions"]["rider_id"], "x_freq"].to_numpy()
    assert len(x_freq) == len(y)
    assert math.isnan(qini_auuc(y, run["sessions"]["arm"], x_freq)["qini_coef"])   # no control arm under all_on


# --- data set validation ----------------------------------------------------------------------


def test_check_dataset_accepts_generated_data_and_catches_a_leak(tmp_path):
    cfg = load_config(TINY, ["policy.name=legacy", "generate.days=1", "runner.n_procs=1"])
    generate(cfg, tmp_path / "gen")
    problems, summary = check_run_dir(tmp_path / "gen")
    assert problems == []
    assert summary["mode"] == "generate" and summary["policy"] == "legacy" and summary["days"] == 1.0
    assert summary["sessions"] > 0 and summary["N_completed"] > 0 and summary["budget_B_vnd"] > 0
    assert 0 < summary["share_arm"] < 1 and summary["truncated"] == 0
    assert markdown_row(summary).startswith("| `gen` | generate | legacy | 1 |")
    # A hidden column smuggled into observed/ must be reported.
    path = tmp_path / "gen" / "observed" / "sessions.parquet"
    leaked = pd.read_parquet(path)
    leaked["u_latent"] = 0.0
    leaked.to_parquet(path, index=False)
    problems, _ = check_run_dir(tmp_path / "gen")
    assert any("observed/sessions" in p for p in problems)


def test_check_budget_audits_legacy_and_all_on_runs(tmp_path, capsys):
    # Legacy with a small fixed B: sessions are blocked, including explore sessions of cells that are off.
    legacy = load_config(TINY, ["policy.name=legacy", "generate.days=1", "runner.n_procs=1",
                                "budget.mode=fixed", "budget.fixed_vnd=400", "policy.legacy.explore_frac=0.3"])
    generate(legacy, tmp_path / "legacy")
    assert check_budget.main(["check_budget", str(tmp_path / "legacy")]) == 0
    out = capsys.readouterr().out
    assert "mọi kỳ trong ngân sách" in out and "bị chặn" in out and "VI PHẠM" not in out
    sessions = pd.read_parquet(tmp_path / "legacy" / "observed" / "sessions.parquet")
    blocked_off = sessions[sessions["budget_blocked"] & ~sessions["promo_on_cell"]]
    assert len(blocked_off) > 0 and (blocked_off["assign_mechanism"] == "explore").all()
    # Several seeds with a full log: one folder per run, each audited.
    on = load_config(TINY, ["policy.name=all_on", "sweep.n_seeds=2", "runner.n_procs=1",
                            "budget.mode=fixed", "budget.fixed_vnd=400"])
    evaluate(on, tmp_path / "on", log_level="full")
    assert check_budget.main(["check_budget", str(tmp_path / "on")]) == 0
    assert capsys.readouterr().out.count("policy=all_on") == 2
    assert check_budget.main(["check_budget"]) == 2


def test_gate_plots_write_png_files(tmp_path):
    pytest.importorskip("matplotlib")
    from analysis.plots import plot_theta_sweep, plot_throughput

    sweep = pd.DataFrame({"theta": [0.0, 0.2, 0.5, 1.0], "N_mean": [100.0, 110.0, 108.0, 95.0],
                          "N_se": [2.0, 2.5, 2.0, 3.0], "V_mean": [1.0, 2.0, 3.0, 4.0], "V_se": [0.1] * 4,
                          "spent_mean": [50.0, 50.0, 48.0, 30.0], "n_seeds": [10] * 4,
                          "is_argmax": [False, True, False, False]})
    png = plot_theta_sweep(sweep, tmp_path / "plots" / "sweep.png", budget_vnd=50.0,
                           baselines={"all_off": 90.0, "all_on không ngân sách": 105.0})
    assert png.exists() and png.stat().st_size > 10_000 and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    wide = sweep.assign(theta=[0.0, 0.5, 5.0, 30.0])                           # two orders of magnitude: ordinal axis
    png = plot_theta_sweep(wide, tmp_path / "plots" / "wide.png", share_cells_off=[0.0, 0.3, 0.6, 0.75],
                           star_interval=(0.5, 5.0))
    assert png.exists() and png.stat().st_size > 10_000
    curve = pd.DataFrame({"demand_scale": np.repeat([0.5, 1.0, 2.0, 4.0], 2), "seed": [0, 1] * 4,
                          "completed_per_h": [100, 104, 190, 196, 240, 236, 200, 204],
                          "mean_slack": [5.0, 5.2, 1.0, 1.1, 0.2, 0.25, 0.05, 0.06],
                          "mean_pickup_eta_min": [2.0, 2.1, 3.0, 3.1, 6.0, 6.2, 9.0, 9.1]})
    png = plot_throughput(curve, tmp_path / "plots" / "throughput.png")
    assert png.exists() and png.stat().st_size > 10_000


def test_check_dataset_on_results_only_and_experiment_dirs(tmp_path):
    cfg = load_config(TINY, ["gte.n_seeds=2", "runner.n_procs=1"])
    gte(cfg, tmp_path / "gte")
    problems, summary = check_run_dir(tmp_path / "gte")
    assert problems == [] and summary["mode"] == "gte" and summary["n_runs"] == 4 and summary["seeds"] == "0–1"
    assert "sessions" not in summary and summary["policy"] == "all_off/all_on" and summary["N_mean"] > 0
    exp = load_config(TINY, ["policy.name=experiment", "experiment.cluster_level=all", "generate.days=1",
                             "runner.n_procs=1"])
    generate(exp, tmp_path / "exp")
    problems, summary = check_run_dir(tmp_path / "exp")
    assert problems == [] and "cluster_switchback, cụm all" in summary["design"]
    assert math.isnan(summary["budget_B_vnd"]) and summary["share_blocked"] == 0.0
    problems, _ = check_run_dir(tmp_path / "missing")
    assert problems and "missing" in problems[0]
