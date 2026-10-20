"""analysis/confounding.py: how the legacy targeting on u_latent confounds the effect."""

import numpy as np
import pandas as pd
import pytest

from analysis.confounding import latent_profile_frame


def legacy_sessions(n_riders=4000, per_rider=10, effect=0.06, seed=3):
    """Legacy-like data: offer and baseline completion both rise with u; the true effect is ``effect`` everywhere."""
    g = np.random.default_rng(seed)
    u = pd.Series(g.normal(size=n_riders), index=np.arange(n_riders))
    rider = np.repeat(np.arange(n_riders), per_rider)
    n = len(rider)
    explore = g.random(n) < 0.2
    p_offer = np.where(explore, 0.5, 1 / (1 + np.exp(-(-1.0 + 1.5 * u.to_numpy()[rider]))))
    t = g.random(n) < p_offer
    p_done = np.clip(0.3 + 0.12 * u.to_numpy()[rider] + effect * t, 0, 1)
    done = g.random(n) < p_done
    sessions = pd.DataFrame({"session_id": np.arange(n), "rider_id": rider, "in_window": True, "budget_blocked": False,
                             "arm": t.astype(np.int8), "assign_mechanism": np.where(explore, "explore", "legacy_rule")})
    sessions.loc[0, "in_window"] = False                                   # dropped
    orders = pd.DataFrame({"session_id": np.flatnonzero(done), "status": "Completed"})
    return sessions, orders, u


def test_profile_shows_offer_and_baseline_both_rising_with_u():
    sessions, orders, u = legacy_sessions()
    p = latent_profile_frame(sessions, orders, u, n_bins=5)
    assert len(p) == 5 and p["sessions"].sum() == len(sessions) - 1
    assert np.all(np.diff(p["share_offered"]) > 0) and np.all(np.diff(p["rate_not_offered"]) > 0)
    assert np.all(np.diff(p["u_mid"]) > 0)
    # Within a narrow u bin the confounding is small: naive and explore both near the true 0.06.
    np.testing.assert_allclose(p["explore_effect"], 0.06, atol=0.04)


def test_profile_rejects_riders_without_u():
    sessions, orders, u = legacy_sessions(n_riders=200)
    with pytest.raises(ValueError, match="missing"):
        latent_profile_frame(sessions, orders, u.iloc[:-1], n_bins=4)


def test_decomposition_adds_up_and_finds_the_selection():
    from analysis.confounding import decompose_naive

    sessions, orders, u = legacy_sessions(n_riders=8000)
    d = decompose_naive(latent_profile_frame(sessions, orders, u, n_bins=10)).set_index("part")["value"]
    assert d.iloc[:4].sum() == pytest.approx(d["naive"])
    assert d["ate"] == pytest.approx(0.06, abs=0.015)                     # homogeneous true effect
    assert abs(d["targeting (att - ate)"]) < 0.01                          # nothing to target here
    assert d["selection on u_latent (naive - naive_within_u)"] > 0.05      # offered riders complete more anyway
