"""Estimated threshold theta-hat from experiment data, without and with the budget.

Inputs:

- the voucher effect ``tau(h)`` on the completion rate of a session, and the voucher dollars paid
  per offered session ``cost(h)``, by hour of day, from the **global** switchback
  (``switchback_all_28d``). Only a global design shows the harm in the tight hours: in a cluster
  design an "on" cluster borrows idle drivers from its "off" neighbours;
- the sessions a day of the policy would see, with the hour and the value of the indicator
  (``analysis.tension`` on ``all_off`` runs), e.g. ``ring1`` or ``cell_persistence``.

The policy cuts every session whose indicator is below theta. Each session is credited with the
effect and cost of its hour (the model's one assumption: within an hour the effect does not depend
on the indicator; the indicator only decides how much of each hour is cut). Per day:

- (A) harm only, no budget: ``gain_A(theta) = sum over kept sessions of tau(h)``.
  ``theta_hat_A`` = argmax; it cuts the hours where tau < 0 as far as the indicator separates them.
- (B) budget B per day spread over the kept sessions (rider tier = random score):
  ``gain_B(theta) = min(B, S(theta)) * r(theta)``, with ``S`` the spend if every kept session were
  offered and ``r = sum tau / sum cost`` over kept sessions (extra trips per dollar there).
  Cutting raises ``r`` while ``S >= B``; past that point money is left unspent.

Both are argmax over a grid; the interval comes from the unit bootstrap of ``tau`` and ``cost``.

The harm itself is shown two ways (notebook 04): :func:`hour_table` (``tau(h)`` with its
interval, any switchback) and :func:`on_off_by_hour` (voucher for everyone minus for no one, no budget,
paired by seed: completed, requests, riders who gave up, quoted ETA).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

LOST = ("Abandoned", "Cancelled")           # order statuses where the rider gave up waiting


def hour_table(effects: dict, *, alpha: float = 0.05) -> pd.DataFrame:
    """``hour, tau, lo, hi, cost`` from :func:`hour_effects` (percentile interval of the unit bootstrap)."""
    with np.errstate(all="ignore"):
        lo, hi = np.nanquantile(effects["tau_draws"], [alpha / 2, 1 - alpha / 2], axis=0)
    return pd.DataFrame({"hour": np.arange(24), "tau": effects["tau"], "lo": lo, "hi": hi, "cost": effects["cost"]})


def hourly_outcomes(sessions: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """Per hour of one run's evaluation window: sessions, requests, completed, lost (gave up), mean quoted ETA."""
    s = sessions[sessions["in_window"]]
    o = orders.merge(s[["session_id", "hour"]], on="session_id")     # orders of in-window sessions, with their hour
    out = pd.DataFrame({
        "sessions": s.groupby("hour").size(),
        "requests": o.groupby("hour").size(),
        "completed": o[o["status"] == "Completed"].groupby("hour").size(),
        "lost": o[o["status"].isin(LOST)].groupby("hour").size(),
        "quoted_eta_min": s.groupby("hour")["quoted_eta_min"].mean(),
    }).reindex(range(24))
    counts = ["sessions", "requests", "completed", "lost"]
    out[counts] = out[counts].fillna(0.0)
    out.index.name = "hour"
    return out


def _per_seed_hourly(out_dir: Path) -> pd.DataFrame:
    from analysis.io import load_run

    frames = []
    for run_dir in sorted((Path(out_dir) / "runs").iterdir()):
        run = load_run(run_dir)
        frames.append(hourly_outcomes(run["sessions"], run["orders"])
                      .assign(seed=int(run_dir.name.rsplit("-", 1)[1])).reset_index())   # run_id ends with the seed
    return pd.concat(frames, ignore_index=True)


def on_off_by_hour(on: pd.DataFrame, off: pd.DataFrame) -> pd.DataFrame:
    """Voucher for everyone minus voucher for no one, per hour, paired by seed (common random numbers).

    ``on`` and ``off`` are ``hourly_outcomes`` rows with ``hour`` and ``seed`` columns. Returns per
    hour the "off" level, the mean on - off difference of completed trips, requests and lost
    requests per day, its SE over seeds, and the quoted ETA off and its change.
    """
    keys = ["seed", "hour"]
    a, b = on.set_index(keys).sort_index(), off.set_index(keys).sort_index()
    if not a.index.equals(b.index):
        raise ValueError("on and off runs must cover the same seeds and hours")
    d = (a - b).groupby("hour")
    n_seeds = a.index.get_level_values("seed").nunique()
    lvl = b.groupby("hour")
    return pd.DataFrame({
        "completed_off": lvl["completed"].mean(),
        "completed_diff": d["completed"].mean(),
        "completed_diff_se": d["completed"].std(ddof=1) / np.sqrt(n_seeds),
        "requests_diff": d["requests"].mean(),
        "lost_diff": d["lost"].mean(),
        "lost_rate_off": lvl["lost"].sum() / lvl["requests"].sum(),
        "quoted_eta_off": lvl["quoted_eta_min"].mean(),
        "quoted_eta_diff": d["quoted_eta_min"].mean(),
    }).reset_index()


def on_off_by_hour_runs(on_dir: Path, off_dir: Path) -> pd.DataFrame:
    """:func:`on_off_by_hour` of two ``evaluate --log-level full`` directories (all_on, all_off; no budget)."""
    return on_off_by_hour(_per_seed_hourly(on_dir), _per_seed_hourly(off_dir))



def hour_effects(frame: pd.DataFrame, *, n_boot: int = 500, seed: int = 0, drop_burnin: bool = True) -> dict:
    """``tau`` and ``cost`` by hour (point and ``[n_boot, 24]`` draws) from an ``experiment_frame``.

    ``tau(h)`` = completion rate on minus off; ``cost(h)`` = mean voucher paid per **on** session
    (voucher value x completed). Draws resample the (cluster, block) units with one set of
    weights shared by ``tau`` and ``cost``.
    """
    f = frame[~frame["in_burnin"]] if drop_burnin else frame
    f = f.assign(paid=f["voucher_value_vnd"].to_numpy(np.float64) * f["completed"].to_numpy(np.float64))
    u = f.groupby(["hour", "unit"]).agg(arm=("arm", "first"), n=("arm", "size"), y=("completed", "sum"),
                                         paid=("paid", "sum")).reset_index()
    units = u["unit"].unique()
    col = pd.Series(np.arange(len(units)), index=units)[u["unit"]].to_numpy()
    gen = np.random.default_rng(seed)
    weights = np.vstack([np.ones(len(units)),
                         gen.multinomial(len(units), np.full(len(units), 1.0 / len(units)), size=n_boot)])
    w = weights[:, col]                                                     # [1 + n_boot, rows]
    hour, on = u["hour"].to_numpy(), u["arm"].to_numpy() == 1
    n, y, paid = (u[c].to_numpy(np.float64) for c in ("n", "y", "paid"))
    tau = np.full((len(weights), 24), np.nan)
    cost = np.full((len(weights), 24), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        for h in range(24):
            m1, m0 = (hour == h) & on, (hour == h) & ~on
            n1, n0 = w[:, m1] @ n[m1], w[:, m0] @ n[m0]
            tau[:, h] = (w[:, m1] @ y[m1]) / n1 - (w[:, m0] @ y[m0]) / n0
            cost[:, h] = (w[:, m1] @ paid[m1]) / n1
    return {"tau": tau[0], "cost": cost[0], "tau_draws": tau[1:], "cost_draws": cost[1:]}


def gain_curves(indicator: np.ndarray, hour: np.ndarray, n_days: float, tau: np.ndarray, cost: np.ndarray,
                budget_per_day: float, grid) -> pd.DataFrame:
    """``gain_A`` and ``gain_B`` (trips per day) for every theta of ``grid``."""
    x = np.asarray(indicator, dtype=np.float64)
    h = np.asarray(hour, dtype=np.int64)
    t, c = np.asarray(tau)[h], np.asarray(cost)[h]
    rows = []
    for theta in grid:
        keep = ~(x < theta)                                   # NaN keeps the cell on, as the policy does
        sum_tau, spend = t[keep].sum() / n_days, c[keep].sum() / n_days
        rate = sum_tau / spend if spend > 0 else 0.0
        rows.append({"theta": float(theta), "share_cut": float(1 - keep.mean()), "gain_A": sum_tau,
                     "spend_all_on": spend, "gain_B": min(budget_per_day, spend) * rate})
    return pd.DataFrame(rows)


def theta_hat(indicator, hour, n_days: float, effects: dict, budget_per_day: float, grid,
              alpha: float = 0.05) -> dict:
    """Point estimate and bootstrap interval of ``theta_hat_A`` and ``theta_hat_B``."""
    grid = np.asarray(grid, dtype=np.float64)
    curves = gain_curves(indicator, hour, n_days, effects["tau"], effects["cost"], budget_per_day, grid)
    out = {"curves": curves}
    draws = {"A": [], "B": []}
    for tau_b, cost_b in zip(effects["tau_draws"], effects["cost_draws"]):
        cb = gain_curves(indicator, hour, n_days, tau_b, cost_b, budget_per_day, grid)
        draws["A"].append(grid[int(np.nanargmax(cb["gain_A"]))])
        draws["B"].append(grid[int(np.nanargmax(cb["gain_B"]))])
    for k in ("A", "B"):
        d = np.asarray(draws[k])
        lo, hi = np.quantile(d, [alpha / 2, 1 - alpha / 2])
        out[f"theta_hat_{k}"] = float(grid[int(np.nanargmax(curves[f"gain_{k}"]))])
        out[f"ci_{k}"] = (float(lo), float(hi))
    return out
