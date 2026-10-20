"""analysis/estimate.py (effect by supply tension, A4, threshold estimate) and analysis/scores.py."""

import dataclasses
import json
import multiprocessing

import numpy as np
import pandas as pd
import pytest

from analysis import estimate, scores
from analysis.io import load_run
from analysis.estimate import (
    assign_bins, effect_by_bin, experiment_frame, isotonic_increasing, naive_total_effect, ratio_slack,
    sign_changes, theta_hat,
)
from sim.config import load_config
from sim.policies.scores import load_score_fn, score_batch
from sim.runner import generate, with_policy
from tests.conftest import ROOT
from tests.fakes import make_batch

B7A = ROOT / "runs" / "b7a"
S8_W0 = B7A.parent / "s8" / "w0"       # Hanoi-scale VND data; the runs/b7a sets use the old USD config


def synthetic_frame(effect_of, *, n_units=600, per_unit=60, base=0.15, seed=1) -> pd.DataFrame:
    """Sessions of ``n_units`` (cluster, block) units; the moderator is fixed per unit, the arm is a coin.

    ``effect_of(moderator)`` is the true difference in completion rate, on minus off.
    """
    gen = np.random.default_rng(seed)
    unit = np.repeat(np.arange(n_units), per_unit)
    arm = np.repeat(gen.integers(0, 2, n_units), per_unit)
    moderator = np.repeat(gen.uniform(0.0, 4.0, n_units), per_unit)
    p = base + arm * effect_of(moderator)
    y = (gen.random(len(unit)) < p).astype(np.int8)
    return pd.DataFrame({"unit": unit, "arm": arm.astype(np.int8), "completed": y, "in_burnin": False,
                         "cell_lag": moderator, "cell_pre": moderator, "ring_pre": moderator, "cluster_pre": moderator,
                         "system_pre": moderator})


# --- small pieces ----------------------------------------------------------------


def test_ratio_slack_follows_the_simulator_rule():
    np.testing.assert_array_equal(ratio_slack([2, 3, 0, 0], [4, 0, 0, 5]), [0.5, np.inf, 0.0, 0.0])


def test_sign_changes_skips_zero_and_nan():
    assert sign_changes([0.1, 0.2, 0.3]) == 0
    assert sign_changes([-0.1, 0.2, 0.3]) == 1
    assert sign_changes([-0.1, 0.0, np.nan, 0.2, -0.3]) == 2
    assert sign_changes([]) == 0


def test_isotonic_fit_is_monotone_and_keeps_the_weighted_mean():
    values, weights = np.array([0.3, -0.1, 0.2, 0.1, 0.5]), np.array([1.0, 3.0, 1.0, 1.0, 2.0])
    fit = isotonic_increasing(values, weights)
    assert (np.diff(fit) >= 0).all()
    assert np.average(fit, weights=weights) == pytest.approx(np.average(values, weights=weights))
    np.testing.assert_allclose(isotonic_increasing([1.0, 2.0, 3.0], [1, 1, 1]), [1.0, 2.0, 3.0])


def test_bins_put_infinity_last_and_never_split_ties():
    frame = pd.DataFrame({"cell_pre": [0.0] * 50 + [0.5] * 20 + [np.inf] * 20 + [np.nan] * 10})
    fixed = assign_bins(frame, "cell_pre", edges=[0, 0.2, 1.0, np.inf])
    assert len(fixed) == 90                                        # rows without a moderator are dropped
    assert fixed["bin"].value_counts().to_dict() == {"[0, 0.2)": 50, "[0.2, 1)": 20, "[1, inf)": 20}
    quart = assign_bins(frame, "cell_pre", quantiles=4)
    assert quart.groupby("cell_pre")["bin"].nunique().max() == 1    # a value never sits in two bins
    assert quart["bin"].nunique() >= 2 and len(quart) == 90
    with pytest.raises(ValueError):
        assign_bins(frame, "slack", edges=[0, 1])


# --- effect by bin ---------------------------------------------------------------


def test_effect_by_bin_recovers_a_planted_effect_with_unit_bootstrap():
    frame = synthetic_frame(lambda m: np.where(m < 2.0, 0.02, 0.10))
    table = effect_by_bin(frame, "cell_pre", edges=[0, 2.0, np.inf], n_boot=300)
    assert table["bin"].tolist() == ["[0, 2)", "[2, inf)"]
    for truth, row in zip((0.02, 0.10), table.itertuples()):
        assert row.ci_lo < truth < row.ci_hi
        assert row.effect == pytest.approx(row.rate_on - row.rate_off)
        assert 0.4 < row.share_on < 0.6
    assert sign_changes(table["effect"]) == 0
    assert (table["n_off"] + table["n_on"]).sum() == len(frame)


def test_bootstrap_is_reproducible_and_resamples_units_not_sessions():
    # 40 units x 500 sessions: sessions of a unit share a common shock, so the unit interval is wide.
    gen = np.random.default_rng(3)
    shock = np.repeat(gen.normal(0, 0.05, 40), 500)
    frame = synthetic_frame(lambda m: 0.05, n_units=40, per_unit=500)
    frame["completed"] = (gen.random(len(frame)) < 0.15 + 0.05 * frame["arm"] + shock).astype(np.int8)
    a = effect_by_bin(frame, "cell_pre", edges=[0, np.inf], n_boot=300, seed=7)
    b = effect_by_bin(frame, "cell_pre", edges=[0, np.inf], n_boot=300, seed=7)
    pd.testing.assert_frame_equal(a, b)
    n1, n0 = a["n_on"][0], a["n_off"][0]
    naive_se = np.sqrt(0.2 * 0.8 / n1 + 0.15 * 0.85 / n0)           # as if sessions were independent
    assert (a["ci_hi"][0] - a["ci_lo"][0]) > 3 * 2 * 1.96 * naive_se


def test_burn_in_sessions_are_dropped_by_default():
    frame = synthetic_frame(lambda m: 0.05)
    frame.loc[frame.index % 4 == 0, "in_burnin"] = True
    kept = effect_by_bin(frame, "cell_pre", edges=[0, np.inf], n_boot=50)
    everything = effect_by_bin(frame, "cell_pre", edges=[0, np.inf], n_boot=50, drop_burnin=False)
    assert kept["n_on"][0] + kept["n_off"][0] == (~frame["in_burnin"]).sum()
    assert everything["n_on"][0] + everything["n_off"][0] == len(frame)


# --- threshold estimate ----------------------------------------------------------


def test_theta_hat_finds_the_zero_crossing():
    frame = synthetic_frame(lambda m: np.where(m < 1.0, -0.04, 0.08), n_units=1500)
    out = theta_hat(frame, "cell_pre", quantiles=16, n_boot=200)
    assert out["theta_hat"] == pytest.approx(1.0, abs=0.3)
    assert out["ci_lo"] <= out["theta_hat"] <= out["ci_hi"] and out["ci_hi"] - out["ci_lo"] < 1.0
    assert out["n_units"] == 1500 and out["share_boot_zero"] == 0.0


def test_theta_hat_is_zero_when_the_voucher_always_helps():
    out = theta_hat(synthetic_frame(lambda m: 0.03 + 0.01 * m, n_units=1500), "cell_pre", quantiles=10, n_boot=200)
    assert out["theta_hat"] == 0.0 and out["ci_hi"] == 0.0 and out["share_boot_zero"] == 1.0


def test_naive_total_effect_scales_to_trips_per_day():
    frame = synthetic_frame(lambda m: 0.05)
    out = naive_total_effect(frame, n_days=4, n_boot=200)
    assert out["ci_lo"] < 0.05 * len(frame) / 4 < out["ci_hi"]
    assert out["per_day"] == pytest.approx(
        (frame.completed[frame.arm == 1].mean() - frame.completed[frame.arm == 0].mean()) * len(frame) / 4)


# --- frame from a real (tiny) run ------------------------------------------------


@pytest.fixture(scope="module")
def tiny_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("switchback")
    # generate always runs generate.days x 1440 minutes: one day of the 7-cell world, a few seconds.
    cfg = load_config([ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"],
                      ["demand.demand_scale=4", "supply.shift_mode=always_on", "experiment.cluster_level=1",
                       "runner.n_procs=1"])
    cfg = dataclasses.replace(with_policy(cfg, "experiment"), generate=dataclasses.replace(cfg.generate, days=1))
    generate(cfg, out)
    return out, cfg


def test_experiment_frame_from_a_generated_run(tiny_run):
    out, cfg = tiny_run
    frame = experiment_frame(out)
    assert len(frame) > 500 and set(estimate.MODERATORS) <= set(frame.columns)
    assert frame.groupby("unit")["arm"].nunique().max() == 1                    # one arm per (cluster, block)
    assert frame["unit"].nunique() == frame[["cluster_id", "block"]].drop_duplicates().shape[0]
    assert set(frame["completed"].unique()) <= {0, 1} and frame["completed"].sum() > 0
    assert (frame.loc[frame["completed"] == 1, "requested"]).all()
    # Pre-block moderators are constant within a (cell, block) and, for the system, within a block.
    assert frame.groupby(["block", "pu_cell"])["cell_pre"].nunique(dropna=False).max() == 1
    assert frame.groupby("block")["system_pre"].nunique(dropna=False).max() == 1
    assert frame.groupby(["block", "pu_cell"])["ring_pre"].nunique(dropna=False).max() == 1
    # A cell's ring holds its own idle drivers, so a ring has no less slack than "0 idle anywhere".
    assert (frame["ring_pre"].notna()).all() and (frame.loc[frame["cell_pre"] > 0, "ring_pre"] > 0).all()
    assert frame["cell_pre"].notna().all() and (frame["cell_pre"] >= 0).all()   # warm-up gives block 1 a past
    table = effect_by_bin(frame, "system_pre", edges=[0, np.inf], n_boot=50)
    assert table["n_on"][0] > 0 and table["n_off"][0] > 0


def test_stored_config_accepts_a_run_made_before_a_key_was_added(tiny_run):
    # runs/b7a was generated before policy.threshold.scope / kappa_max_iter existed.
    out, cfg = tiny_run
    raw = estimate.run_config(load_run(out))
    del raw["policy"]["threshold"]["scope"], raw["policy"]["threshold"]["kappa_max_iter"]
    old = estimate.stored_config(raw)
    assert old.policy.threshold.scope == "cell" and old.space.grid_radius == 1          # stored values win


def test_frame_rejects_a_run_without_experiment(tmp_path):
    cfg = load_config([ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"], ["runner.n_procs=1"])
    cfg = dataclasses.replace(with_policy(cfg, "legacy"), generate=dataclasses.replace(cfg.generate, days=1))
    generate(cfg, tmp_path)                                            # legacy data: block = -1 everywhere
    with pytest.raises(ValueError, match="not an experiment run"):
        experiment_frame(tmp_path)


# --- uplift scores ---------------------------------------------------------------


def explore_data(n=60_000, seed=2):
    gen = np.random.default_rng(seed)
    riders = pd.DataFrame({"rider_id": np.arange(3000), "x_freq": gen.gamma(2.0, 1.0, 3000),
                           "x_segment": gen.integers(0, 3, 3000).astype(np.int8)})
    rid = gen.integers(0, 3000, n)
    arm = gen.integers(0, 2, n).astype(np.int8)
    seg = riders["x_segment"].to_numpy()[rid]
    tau_true = np.array([0.02, 0.06, 0.12])[seg]                       # effect depends on the segment only
    y = (gen.random(n) < 0.10 + arm * tau_true).astype(np.int8)
    sessions = pd.DataFrame({"rider_id": rid, "arm": arm, "assign_mechanism": "explore", "budget_blocked": False,
                             "voucher_value_vnd": np.where(arm == 1, 4.0, 0.0).astype(np.float32)})
    return sessions, riders, y


def test_fit_recovers_the_effect_by_segment():
    sessions, riders, y = explore_data()
    table = scores.fit_tau_strata(sessions, riders, y)
    tau = np.asarray(table["tau"])
    assert tau.shape == (3, 3) and table["n_treated"] + table["n_control"] == len(sessions)
    np.testing.assert_allclose(tau.mean(axis=1), [0.02, 0.06, 0.12], atol=0.012)
    assert table["overall_effect"] == pytest.approx(np.mean([0.02, 0.06, 0.12]), abs=0.01)
    cost = np.asarray(table["cost_per_offer_vnd"])
    np.testing.assert_allclose(cost.mean(axis=1), 4.0 * (0.10 + np.array([0.02, 0.06, 0.12])), atol=0.06)


def test_fit_uses_only_the_explore_slice():
    sessions, riders, y = explore_data()
    confounded = sessions.copy()
    confounded["assign_mechanism"] = "legacy_rule"
    confounded["arm"] = 1
    both = pd.concat([sessions, confounded], ignore_index=True)
    y_both = np.concatenate([y, np.ones(len(y), dtype=np.int8)])       # the rule-based rows would bias the effect
    assert scores.fit_tau_strata(both, riders, y_both)["overall_effect"] == \
        scores.fit_tau_strata(sessions, riders, y)["overall_effect"]


def test_saved_baseline_table_is_well_formed():
    table = json.loads(scores.BASELINE_PATH.read_text(encoding="utf-8"))
    tau, cost = np.asarray(table["tau"]), np.asarray(table["cost_per_offer_vnd"])
    assert tau.shape == cost.shape == (3, 3) and len(table["x_freq_edges"]) == 2
    assert np.isfinite(tau).all() and (cost > 0).all()
    assert table["outcome"] == "completed" and table["run_id"].startswith("generate-")


@pytest.mark.parametrize("spec", ["analysis.scores:tau_x_baseline", "analysis.scores:tau_per_dollar_baseline"])
def test_score_functions_load_by_spec_and_read_only_rider_features(spec):
    fn = load_score_fn(spec)                                           # the string a policy config carries
    batch = make_batch(9, 37)
    batch = dataclasses.replace(batch, x_freq=np.array([0.5, 1.5, 3.0] * 3, dtype=np.float32),
                                x_segment=np.repeat([0, 1, 2], 3).astype(np.int8))
    out = score_batch(fn, batch, np.full(9, np.nan))
    assert out.shape == (9,) and out.dtype == np.float32 and np.isfinite(out).all()
    np.testing.assert_array_equal(out, score_batch(fn, batch, np.arange(9, dtype=float)))     # ignores s_hat
    other = dataclasses.replace(batch, quoted_fare_vnd=batch.quoted_fare_vnd * 3, pu_cell=batch.pu_cell[::-1].copy(),
                                u_score=np.zeros(9))
    np.testing.assert_array_equal(out, score_batch(fn, other, np.full(9, np.nan)))            # only x_freq, x_segment
    edges, tau, cost = scores.load_table()
    expected = tau if spec.endswith("tau_x_baseline") else tau / cost
    np.testing.assert_allclose(out, expected.ravel().astype(np.float32))


def _score_in_worker(spec: str, x_freq: np.ndarray, x_segment: np.ndarray) -> np.ndarray:
    batch = dataclasses.replace(make_batch(len(x_freq), 37), x_freq=x_freq, x_segment=x_segment)
    return score_batch(load_score_fn(spec), batch, np.full(len(x_freq), np.nan))


@pytest.mark.parametrize("spec", ["analysis.scores:tau_x_baseline", "analysis.scores:tau_per_dollar_baseline"])
def test_score_functions_load_in_a_spawned_worker(spec):
    # Runner workers start with spawn on Windows and only get the score_fn string.
    x_freq = np.array([0.5, 1.5, 3.0] * 3, dtype=np.float32)
    x_segment = np.repeat([0, 1, 2], 3).astype(np.int8)
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        remote = pool.apply(_score_in_worker, (spec, x_freq, x_segment))
    np.testing.assert_array_equal(remote, _score_in_worker(spec, x_freq, x_segment))


def test_predict_frame_matches_the_score_functions():
    gen = np.random.default_rng(3)
    riders = pd.DataFrame({"rider_id": np.arange(50, dtype=np.int32), "x_freq": gen.gamma(2.0, 1.0, 50).astype(np.float32),
                           "x_segment": gen.integers(0, 3, 50).astype(np.int8), "x_tenure": np.ones(50, np.float32)})
    sessions = pd.DataFrame({"session_id": np.arange(200, dtype=np.int64) * 7,
                             "rider_id": gen.integers(0, 50, 200).astype(np.int32), "in_window": gen.random(200) < 0.9})
    pred = scores.predict_frame(sessions, riders)
    assert list(pred.columns) == ["session_id", "rider_id", "in_window", *scores.SCORE_FUNCTIONS]
    np.testing.assert_array_equal(pred["session_id"], sessions["session_id"])
    rider = riders.set_index("rider_id").loc[sessions["rider_id"]]
    batch = dataclasses.replace(make_batch(200, 37), x_freq=rider["x_freq"].to_numpy(),
                                x_segment=rider["x_segment"].to_numpy())
    for name, fn in scores.SCORE_FUNCTIONS.items():
        np.testing.assert_array_equal(pred[name].to_numpy(), score_batch(fn, batch, np.full(200, np.nan)))
    pd.testing.assert_frame_equal(pred, scores.predict_frame(sessions, riders))           # deterministic
    with pytest.raises(ValueError, match="missing"):
        scores.predict_frame(sessions.assign(rider_id=999), riders)


def test_predict_cli_writes_parquet_without_hidden_columns(tiny_run, tmp_path):
    from sim.state import HIDDEN_COLUMNS

    run_dir, out = tiny_run[0], tmp_path / "b8" / "pred.parquet"
    assert scores.main(["scores", "predict", str(run_dir), str(out)]) == 0
    pred = pd.read_parquet(out)
    sessions = pd.read_parquet(run_dir / "observed" / "sessions.parquet")
    assert len(pred) == len(sessions) and pred["session_id"].is_unique
    assert not set(pred.columns) & set(HIDDEN_COLUMNS)
    assert scores.main(["scores", "predict", str(run_dir)]) == 2


# --- A4 on the runs/s8/w0 data (informational) -----------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not (S8_W0 / "switchback_c7_28d").exists(),
                    reason='runs/s8/w0 not generated (README.md, section "Tái lập kết quả")')
def test_a4_effect_by_slack_quartile_on_cluster_switchback():
    frame = experiment_frame(S8_W0 / "switchback_c7_28d")
    table = effect_by_bin(frame, "cell_lag", quantiles=4)              # the A4 moderator
    assert len(table) == 4 and (table["n_on"] > 10_000).all() and (table["n_off"] > 10_000).all()
    assert np.isfinite(table[["effect", "ci_lo", "ci_hi"]].to_numpy()).all()
    assert sign_changes(table["effect"]) >= 0                          # reported, not gated
