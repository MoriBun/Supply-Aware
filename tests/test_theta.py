"""analysis/theta.py: theta-hat without and with the budget."""

import numpy as np
import pandas as pd
import pytest

from analysis import theta


def switchback_frame(tau_by_hour, *, units_per_hour=200, per_unit=40, base=0.3, voucher=4.0, seed=0):
    """Global switchback sessions: every (hour, unit) is on or off; the effect depends on the hour only."""
    g = np.random.default_rng(seed)
    rows = []
    for h, tau in enumerate(tau_by_hour):
        arm = g.integers(0, 2, units_per_hour)
        for u in range(units_per_hour):
            y = g.random(per_unit) < base + arm[u] * tau
            rows.append(pd.DataFrame({"hour": h, "unit": h * 10_000 + u, "arm": arm[u], "completed": y.astype(np.int8),
                                      "in_burnin": False, "voucher_value_vnd": voucher * arm[u]}))
    return pd.concat(rows, ignore_index=True)


def test_hour_effects_recover_the_effect_and_cost_by_hour():
    tau = np.r_[np.full(6, 0.08), np.full(3, -0.05), np.full(15, 0.06)]
    eff = theta.hour_effects(switchback_frame(tau, per_unit=100), n_boot=50)          # se ~0.0065 per hour
    np.testing.assert_allclose(eff["tau"], tau, atol=0.025)
    np.testing.assert_allclose(eff["cost"], 4.0 * (0.3 + tau), atol=0.08)       # paid only when completed
    assert eff["tau_draws"].shape == eff["cost_draws"].shape == (50, 24)
    lo, hi = np.quantile(eff["tau_draws"][:, 7], [0.025, 0.975])
    assert hi < 0                                                                # the harmful hour is detected


def test_gain_curves_by_hand():
    # Two hours: hour 0 harmful (tau -0.1, cost 1), hour 1 helpful (tau 0.2, cost 2). One session each per day.
    tau, cost = np.zeros(24), np.ones(24)
    tau[0], tau[1], cost[1] = -0.1, 0.2, 2.0
    x, hour = np.array([0.1, 1.0]), np.array([0, 1])
    c = theta.gain_curves(x, hour, 1.0, tau, cost, budget_per_day=1.5, grid=[0.0, 0.5, 2.0]).set_index("theta")
    assert c.loc[0.0, "gain_A"] == pytest.approx(0.1) and c.loc[0.5, "gain_A"] == pytest.approx(0.2)
    assert c.loc[2.0, "gain_A"] == 0.0 and c.loc[2.0, "share_cut"] == 1.0
    assert c.loc[0.0, "gain_B"] == pytest.approx(1.5 * 0.1 / 3.0)               # min(B, spend 3) x rate
    assert c.loc[0.5, "gain_B"] == pytest.approx(1.5 * 0.2 / 2.0)
    nan_kept = theta.gain_curves(np.array([np.nan, 1.0]), hour, 1.0, tau, cost, 1.5, [0.5])
    assert nan_kept["share_cut"].iloc[0] == 0.0                                  # NaN keeps the cell on


def test_theta_hat_cuts_the_harmful_hours_only():
    tau = np.r_[np.full(6, 0.08), np.full(3, -0.05), np.full(15, 0.06)]
    eff = theta.hour_effects(switchback_frame(tau), n_boot=40)
    g = np.random.default_rng(1)
    hour = np.repeat(np.arange(24), 100)
    tight = (hour >= 6) & (hour <= 8)
    x = np.where(tight, g.uniform(0.0, 0.3, len(hour)), g.uniform(0.5, 5.0, len(hour)))
    grid = np.round(np.arange(0, 1.01, 0.05), 2)
    r = theta.theta_hat(x, hour, 1.0, eff, budget_per_day=1e9, grid=grid)
    assert 0.3 <= r["theta_hat_A"] <= 0.5                                        # all tight sessions, nothing else
    assert r["ci_A"][0] >= 0.3
    assert r["theta_hat_B"] == r["theta_hat_A"]                                  # budget never binds: same rule


# --- harm by hour: voucher for everyone against voucher for no one --------------------------------


def test_hour_table_gives_the_bootstrap_interval():
    f = switchback_frame([0.05] * 3 + [0.0] * 21, units_per_hour=60, per_unit=20)
    t = theta.hour_table(theta.hour_effects(f, n_boot=200))
    assert list(t.columns) == ["hour", "tau", "lo", "hi", "cost"] and len(t) == 24
    assert ((t["lo"] <= t["tau"]) & (t["tau"] <= t["hi"])).all()


def test_hourly_outcomes_counts_in_window_sessions_and_their_orders():
    sessions = pd.DataFrame({"session_id": [1, 2, 3, 4], "hour": [5, 5, 7, 7], "in_window": [True, True, True, False],
                             "quoted_eta_min": [4.0, 6.0, 3.0, 9.0]})
    orders = pd.DataFrame({"session_id": [1, 2, 3, 4], "status": ["Completed", "Cancelled", "Abandoned", "Completed"]})
    t = theta.hourly_outcomes(sessions, orders)
    assert len(t) == 24 and t["sessions"].sum() == 3                      # session 4 is outside the window
    assert t.loc[5, ["sessions", "requests", "completed", "lost"]].tolist() == [2, 2, 1, 1]
    assert t.loc[7, ["completed", "lost"]].tolist() == [0, 1] and t.loc[5, "quoted_eta_min"] == 5.0
    assert t.loc[0, "requests"] == 0 and np.isnan(t.loc[0, "quoted_eta_min"])


def test_on_off_by_hour_pairs_seeds():
    def rows(completed_by_seed):
        return pd.DataFrame([{"seed": s, "hour": h, "sessions": 10.0, "requests": 8.0, "completed": c[h], "lost": 1.0,
                              "quoted_eta_min": 5.0} for s, c in completed_by_seed.items() for h in range(2)])
    off = rows({0: [5, 6], 1: [7, 6]})
    on = rows({0: [4, 8], 1: [6, 9]})                                     # hour 0: -1 both seeds; hour 1: +2, +3
    t = theta.on_off_by_hour(on, off).set_index("hour")
    assert t.loc[0, "completed_diff"] == -1 and t.loc[0, "completed_diff_se"] == 0
    assert t.loc[1, "completed_diff"] == 2.5 and t.loc[1, "completed_diff_se"] == pytest.approx(0.5)
    assert t.loc[1, "completed_off"] == 6 and t.loc[0, "lost_rate_off"] == pytest.approx(1 / 8)
    with pytest.raises(ValueError, match="same seeds"):
        theta.on_off_by_hour(on[on["seed"] == 0], off)
