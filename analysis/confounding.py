"""How the legacy targeting confounds the voucher effect (notebook 05).

The legacy rule offers a voucher with ``P = sigma(g0 + g_freq * z_freq + g_u * u_latent)``
(``policy.legacy``), and ``u_latent`` also raises the chance that a rider completes a trip
(``alpha_u``). ``u_latent`` is hidden (docs/schema.md), so no estimator on observed data
can adjust for it. :func:`latent_profile` shows the mechanism directly: it reads ``u_latent``
from ``hidden/`` **for diagnosis only**; nothing here feeds a model or a policy.

The estimates themselves (naive, adjusted on X, explore slice) are
:func:`analysis.interference.confounding_estimates`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from analysis.io import completed_outcome, load_hidden, load_run


def latent_profile_frame(sessions: pd.DataFrame, orders: pd.DataFrame, u_latent: pd.Series, *,
                         n_bins: int) -> pd.DataFrame:
    """Per bin of ``u_latent`` (equal numbers of riders): offer share and completion rates of a legacy run.

    ``u_latent`` is indexed by ``rider_id``. Sessions are the in-window ones not blocked by the
    budget (as in :func:`analysis.uplift.uplift_frame`). Columns: ``bin, u_mid, sessions,
    share_offered`` (legacy rule, explore slice left out), ``rate_not_offered, rate_offered,
    naive`` (their difference, legacy rule) and ``explore_effect`` (on minus off in the explore
    slice of the bin, randomized).
    """
    s = sessions[sessions["in_window"] & ~sessions["budget_blocked"]].reset_index(drop=True)
    y = completed_outcome(s, orders).astype(np.float64)
    t = s["arm"].to_numpy() == 1
    explore = (s["assign_mechanism"] == "explore").to_numpy()
    edges = np.quantile(u_latent.to_numpy(np.float64), np.linspace(0, 1, n_bins + 1)[1:-1])
    u = u_latent.reindex(s["rider_id"].to_numpy()).to_numpy(np.float64)
    if np.isnan(u).any():
        raise ValueError("u_latent is missing for some riders of the sessions")
    code = np.searchsorted(edges, u, side="right")
    legacy_all = ~explore
    rows = []
    for b in range(n_bins):
        m = code == b
        legacy, exp = m & ~explore, m & explore
        rate_off = y[legacy & ~t].mean() if (legacy & ~t).any() else np.nan
        rate_on = y[legacy & t].mean() if (legacy & t).any() else np.nan
        exp_on = y[exp & t].mean() if (exp & t).any() else np.nan
        exp_off = y[exp & ~t].mean() if (exp & ~t).any() else np.nan
        rows.append({"bin": b, "u_mid": float(np.median(u[m])) if m.any() else np.nan, "sessions": int(m.sum()),
                     "share_offered": float(t[legacy].mean()) if legacy.any() else np.nan,
                     "rate_not_offered": rate_off, "rate_offered": rate_on, "naive": rate_on - rate_off,
                     "explore_effect": exp_on - exp_off})
    out = pd.DataFrame(rows)
    out.attrs["naive"] = float(y[legacy_all & t].mean() - y[legacy_all & ~t].mean())   # the whole legacy slice
    return out


def decompose_naive(profile: pd.DataFrame) -> pd.DataFrame:
    """Split the naive difference of a :func:`latent_profile_frame` into its parts (effect on completion).

    - ``ate``: mean effect over all sessions (explore effect of each bin, weighted by sessions);
    - ``att``: mean effect over the sessions the legacy rule offered (same, weighted by offers):
      the rule targets riders whose effect is larger, so ``att - ate`` is targeting, not bias;
    - ``naive_within_u``: naive difference inside each ``u_latent`` bin, weighted by offers, i.e. what
      adjusting for ``u_latent`` would give (impossible on observed data, the column is hidden);
    - ``naive``: the plain difference. ``naive - naive_within_u`` is the selection on ``u_latent``:
      offered riders would complete more often even without a voucher.
    """
    offers = profile["sessions"] * profile["share_offered"]
    ate = float(np.average(profile["explore_effect"], weights=profile["sessions"]))
    att = float(np.average(profile["explore_effect"], weights=offers))
    within = float(np.average(profile["naive"], weights=offers))
    naive = float(profile.attrs["naive"])
    return pd.DataFrame({"part": ["ate", "targeting (att - ate)", "within-bin residual (naive_within_u - att)",
                                  "selection on u_latent (naive - naive_within_u)", "naive"],
                         "value": [ate, att - ate, within - att, naive - within, naive]})


def latent_profile(run_dir: Path | str, *, n_bins: int = 10) -> pd.DataFrame:
    """:func:`latent_profile_frame` of a legacy run directory (reads ``hidden/riders_hidden`` for ``u_latent``)."""
    run = load_run(Path(run_dir))
    hidden = load_hidden(Path(run_dir))["riders_hidden"]
    return latent_profile_frame(run["sessions"], run["orders"], hidden.set_index("rider_id")["u_latent"], n_bins=n_bins)
