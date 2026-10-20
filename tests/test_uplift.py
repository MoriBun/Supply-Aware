"""analysis/uplift.py (DR-learner, score functions)."""

import dataclasses
import json
import multiprocessing

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm")

from analysis import uplift  # noqa: E402
from sim.policies.scores import load_score_fn, score_batch  # noqa: E402
from tests.fakes import make_batch  # noqa: E402

FAST = {**uplift.DEFAULTS, "k_folds": 3,
        "nuisance": {**uplift.DEFAULTS["nuisance"], "num_boost_round": 60},
        "final": {**uplift.DEFAULTS["final"], "min_data_in_leaf": 300, "num_boost_round": 60},
        "cost": {**uplift.DEFAULTS["cost"], "num_boost_round": 30}}


def confounded_frame(n=40_000, seed=4) -> pd.DataFrame:
    """Sessions where the offer depends on an observed confounder and the effect on the segment.

    ``x_freq`` raises both the chance of an offer and the chance of completing, so the naive
    difference is biased; the true effect is 0.02 / 0.08 / 0.14 by ``x_segment``. A 10% explore
    slice is randomized with propensity 0.5.
    """
    g = np.random.default_rng(seed)
    seg = g.integers(0, 3, n)
    xf = g.gamma(2.0, 1.0, n)
    explore = g.random(n) < 0.1
    e_true = 1 / (1 + np.exp(-(-2.0 + 1.0 * xf)))
    t = np.where(explore, g.random(n) < 0.5, g.random(n) < e_true).astype(np.int8)
    tau = np.array([0.02, 0.08, 0.14])[seg]
    p0 = np.clip(0.05 + 0.06 * xf, 0, 0.8)
    y = (g.random(n) < p0 + t * tau).astype(np.int8)
    fare = g.uniform(10, 30, n)
    return pd.DataFrame({
        "rider_id": g.integers(0, 5000, n), "x_freq": xf, "x_tenure": g.uniform(0, 24, n), "x_segment": seg,
        "slack_lag_slot": g.uniform(0, 3, n), "slack_lag_day": g.uniform(0, 3, n), "hour": g.integers(0, 24, n),
        "quoted_fare_vnd": fare, "quoted_eta_min": g.uniform(1, 10, n), "no_supply": False, "promo_on_cell": True,
        "assign_mechanism": np.where(explore, "explore", "legacy_rule"), "T": t, "Y": y,
        "known_e": np.where(explore, 0.5, np.nan), "voucher_value_vnd": np.where(t == 1, 0.2 * fare, 0.0),
    }), tau


def test_pseudo_outcome_formula():
    phi = uplift.pseudo_outcome([1, 0], [1, 0], np.array([0.2, 0.2]), np.array([0.5, 0.5]), [0.5, 0.5], 0.02)
    np.testing.assert_allclose(phi, [0.3 + 0.5 / 0.5, 0.3 - (-0.2) / 0.5])
    clipped = uplift.pseudo_outcome([1], [1], np.array([0.0]), np.array([0.0]), [0.0], 0.05)
    assert clipped[0] == pytest.approx(1 / 0.05)


def test_rider_folds_keep_a_rider_in_one_fold():
    rid = np.repeat(np.arange(500), 4)
    fold = uplift.rider_folds(rid, 5)
    assert set(np.unique(fold)) == set(range(5))
    assert (pd.Series(fold).groupby(rid).nunique() == 1).all()


def test_dr_removes_observed_confounding_that_biases_the_naive_difference():
    f, tau = confounded_frame()
    fit = uplift.fit_dr(f, sample="all", params=FAST)
    sm = fit["summary"]
    assert sm["ate_naive"] - tau.mean() > 0.05                          # the naive difference is far off
    assert sm["ate_dr"] == pytest.approx(tau.mean(), abs=3 * sm["ate_dr_se"] + 0.005)
    pred = fit["boosters"]["tau_x"].predict(f[uplift.X_COLS].to_numpy(np.float64))
    by_seg = pd.Series(pred).groupby(f["x_segment"]).mean().to_numpy()
    np.testing.assert_allclose(by_seg, [0.02, 0.08, 0.14], atol=0.03)


def test_explore_sample_uses_the_known_propensity_only():
    f, tau = confounded_frame()
    fit = uplift.fit_dr(f, sample="explore", params=FAST)
    assert fit["summary"]["n"] == int((f["assign_mechanism"] == "explore").sum())
    np.testing.assert_array_equal(fit["nuisances"]["e"], 0.5)
    with pytest.raises(ValueError, match="sample"):
        uplift.fit_dr(f, sample="legacy")


def test_fit_is_deterministic():
    f, _ = confounded_frame(n=8000)
    a = uplift.fit_dr(f, sample="all", params=FAST)
    b = uplift.fit_dr(f, sample="all", params=FAST)
    np.testing.assert_array_equal(a["phi"], b["phi"])
    X = f[uplift.X_COLS].to_numpy(np.float64)
    np.testing.assert_array_equal(a["boosters"]["tau_x"].predict(X), b["boosters"]["tau_x"].predict(X))


# --- saved models and score functions --------------------------------------------


@pytest.mark.parametrize("name", sorted(set(uplift.MODEL_SUFFIXES.values())))    # W0 and W1 models
def test_saved_models_are_well_formed(name):
    meta = json.loads((uplift.MODEL_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert meta["outcome"] == "completed" and meta["run_id"].startswith("generate-")
    assert meta["sample"] == name.removeprefix("dr_").removesuffix("_w1") and meta["slack_cap"] > 0
    assert meta["mean_paid_per_offer_vnd"] > 1000      # đồng, not the old dollars
    assert meta["x_cols"] == uplift.X_COLS and meta["s_cols"] == uplift.S_COLS
    for key in uplift.BOOSTER_COLS:
        assert (uplift.MODEL_DIR / f"{name}_{key}.txt").exists()


def realistic_batch(n=12):
    b = make_batch(n, 37)
    g = np.random.default_rng(0)
    return dataclasses.replace(b, x_freq=g.gamma(2.0, 1.0, n).astype(np.float32),
                               x_tenure=g.uniform(0, 24, n).astype(np.float32),
                               x_segment=np.tile([0, 1, 2], n // 3).astype(np.int8),
                               quoted_fare_vnd=g.uniform(10, 30, n).astype(np.float32),
                               quoted_eta_min=g.uniform(1, 10, n).astype(np.float32))


@pytest.mark.parametrize("fn_name", sorted(uplift.SCORE_FUNCTIONS))
def test_score_functions_read_only_batch_columns_and_are_finite(fn_name):
    fn = load_score_fn(f"analysis.uplift:{fn_name}")
    batch = realistic_batch()
    s_hat = np.array([0.1, 0.5, np.inf, np.nan] * 3)
    out = score_batch(fn, batch, s_hat)
    assert out.shape == (12,) and np.isfinite(out).all()
    np.testing.assert_array_equal(out, score_batch(fn, batch, s_hat))                 # deterministic
    other = dataclasses.replace(batch, pu_cell=batch.pu_cell[::-1].copy(), u_score=np.zeros(12),
                                session_id=batch.session_id + 1000)
    np.testing.assert_array_equal(out, score_batch(fn, other, s_hat))
    if "xs" not in fn_name:                                                            # tau(x) ignores s_hat
        np.testing.assert_array_equal(out, score_batch(fn, batch, np.zeros(12)))


def test_inf_slack_is_capped_like_the_threshold_policy():
    meta, _ = uplift.load_model("dr_explore")
    batch = realistic_batch()
    a = score_batch(uplift.tau_xs_dr, batch, np.full(12, np.inf))
    b = score_batch(uplift.tau_xs_dr, batch, np.full(12, meta["slack_cap"]))
    np.testing.assert_array_equal(a, b)


def _score_in_worker(spec: str) -> np.ndarray:
    return score_batch(load_score_fn(spec), realistic_batch(), np.full(12, 0.7))


def test_score_function_loads_in_a_spawned_worker():
    spec = "analysis.uplift:tau_xs_dr_per_dollar"
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        remote = pool.apply(_score_in_worker, (spec,))
    np.testing.assert_array_equal(remote, _score_in_worker(spec))
