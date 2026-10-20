"""analysis/tension_curve.py: effect by pre-block tension, tension threshold, hour method on count tables."""

import dataclasses
import json

import numpy as np
import pandas as pd
import pytest

from analysis import tension_curve as tc
from analysis import theta
from sim.config import load_config
from sim.runner import generate, with_policy
from tests.conftest import ROOT
from tests.test_theta import switchback_frame

GRID = np.round(np.arange(0.0, 1.01, 0.05), 2)


def frame_with_indicator(seed=1):
    tau = np.r_[np.full(6, 0.08), np.full(3, -0.10), np.full(15, 0.06)]
    f = switchback_frame(tau, units_per_hour=120, per_unit=30, seed=seed)
    g = np.random.default_rng(seed)
    tight = (f["hour"] >= 6) & (f["hour"] <= 8)
    x = np.where(tight, g.uniform(0.0, 0.3, len(f)), g.uniform(0.5, 5.0, len(f)))
    x[: 50] = np.nan                       # no previous slot: kept on, as the policy does
    x[50:80] = np.inf                      # no en-route driver: capped slack, kept on
    f["system_pre"] = x
    return f


@pytest.mark.parametrize("budget", [1e12, 1500.0])      # never binding, binding
def test_hour_theta_hat_matches_the_theta_module_number_for_number(budget):
    f = frame_with_indicator()
    eff = theta.hour_effects(f, n_boot=30)
    ours = tc.hour_theta_hat(f, "system_pre", eff, n_days=1.0, budget_per_day=budget, grid=GRID)
    ref = theta.theta_hat(f["system_pre"].to_numpy(), f["hour"].to_numpy(), 1.0, eff, budget, GRID)
    for k in ("theta_hat_A", "theta_hat_B", "ci_A", "ci_B"):
        assert ours[k] == ref[k], k
    for col in ("share_cut", "gain_A", "spend_all_on", "gain_B"):
        np.testing.assert_allclose(ours["curves"][col].to_numpy(), ref["curves"][col].to_numpy(), rtol=1e-10, atol=1e-9)
    assert 0.3 <= ours["theta_hat_A"] <= 0.5


def test_harmful_hours_are_the_hours_with_a_negative_interval():
    f = frame_with_indicator()
    eff = theta.hour_effects(f, n_boot=100)
    assert tc.harmful_hours(eff) == [6, 7, 8]


def test_kept_counts_follow_the_policy_rule_for_nan_and_inf():
    x = np.array([np.nan, np.inf, 0.0, 0.3, 0.3, 1.0])
    h = np.array([0, 0, 1, 1, 2, 2])
    grid = np.array([0.0, 0.3, 0.5])
    kept = tc._kept_counts(x, h, grid)
    # theta = 0: everything kept; theta = 0.3: x = 0 cut; theta = 0.5: x = 0 and 0.3 cut; NaN and inf always kept.
    assert kept[0, :3].tolist() == [2, 2, 2]
    assert kept[1, :3].tolist() == [2, 1, 2]
    assert kept[2, :3].tolist() == [2, 0, 1]


@pytest.fixture(scope="module")
def city_switchback(tmp_path_factory):
    out = tmp_path_factory.mktemp("switchback_all")
    cfg = load_config([ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"],
                      ["demand.demand_scale=4", "supply.shift_mode=always_on", "experiment.cluster_level=all",
                       "runner.n_procs=1"])
    cfg = dataclasses.replace(with_policy(cfg, "experiment"), generate=dataclasses.replace(cfg.generate, days=1))
    generate(cfg, out)
    return out


def test_analyse_world_on_a_generated_city_switchback(city_switchback):
    r = tc.analyse_world(city_switchback, budget_per_day=1e12, n_boot=20, quantiles=3)
    assert r["frame_rows"] > 500 and r["n_units"] > 10 and r["n_days"] == pytest.approx(1.0)
    curves = r["curves"]
    assert set(curves["moderator"]) == set(tc.MODERATORS)
    both = (curves["n_on"] > 0) & (curves["n_off"] > 0)                 # a tiny day leaves some fixed bins one-armed
    assert both.sum() >= 3 and (curves.loc[both, "ci_lo"] <= curves.loc[both, "ci_hi"]).all()
    assert np.isfinite(curves.loc[both, "effect"]).all()
    assert list(r["thresholds"]["moderator"]) == list(tc.MODERATORS)
    assert (r["thresholds"]["theta_hat"] >= 0).all()
    for m in tc.MODERATORS:
        hm = r["hour_method"][m]
        assert hm["theta_hat_A"] in tc.THETA_GRID and hm["theta_hat_B"] in tc.THETA_GRID
        assert hm["ci_A"][0] <= hm["ci_A"][1] and hm["ci_B"][0] <= hm["ci_B"][1]
        assert len(hm["curves"]) == len(tc.THETA_GRID)
    assert all(isinstance(h, int) and 0 <= h < 24 for h in r["harmful_hours"])
    text = tc.summary_markdown(r, None, "tiny")
    assert "tiny: " in text and "system_pre" in text


def test_tension_distribution_and_cli(city_switchback, tmp_path):
    dist = tc.tension_distribution({"switchback": city_switchback})
    assert len(dist) == 1 and dist["n_slots"][0] > 0 and 0.0 <= dist["share_below_cut"][0] <= 1.0
    out = tmp_path / "s9"
    rc = tc.main(["--switchback", str(city_switchback), "--budget-per-day", "1e12", "--legacy", str(city_switchback),
                  "--out", str(out), "--n-boot", "10", "--quantiles", "3"])
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert set(payload["theta_hat_tension"]) == set(tc.MODERATORS) and "harmful_hours" in payload
    assert (out / "curve_by_tension.parquet").exists() and (out / "summary.md").exists()
    assert len(pd.read_parquet(out / "tension_distribution.parquet")) == 1
