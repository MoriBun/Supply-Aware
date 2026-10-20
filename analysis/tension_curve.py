"""Voucher effect by supply tension measured **before** each block, and the tension threshold.

The hour method (:mod:`analysis.theta`) estimates the effect of a voucher **by hour** and
uses tension only to decide which cells to cut first. Here, from a city-wide
switchback run (``experiment``, ``cluster_level = all``), blocks are grouped by the tension the city had in
the last slot before the block (``system_pre``; per-session ``ring_pre`` and ``cell_pre`` as well), so the
moderator is never moved by the voucher of the block it moderates. Outputs:

- the effect curve: completion rate on minus off per tension bin, unit (block) bootstrap interval
  (:func:`analysis.estimate.effect_by_bin`);
- the tension threshold ``theta_hat_tension``: zero crossing of the isotonic fit
  (:func:`analysis.estimate.theta_hat`), with interval;
- the hour method for comparison (``theta_hat_A`` without and ``theta_hat_B`` with the budget,
  :mod:`analysis.theta`), computed here from a (hour, tension level) count table so it runs on 4.2 M
  sessions (same numbers as :func:`analysis.theta.theta_hat`, see ``tests/test_tension_curve.py``);
- the harmful hours (``tau(h)`` upper interval < 0) for the hour-off rule of :mod:`analysis.hour_rule`;
- the distribution of the city tension without vouchers (``all_off``) and under policies (``all_on``,
  ``legacy``): if a policy lowers tension, a threshold must be read on tension measured under the policy.

CLI (one world at a time)::

    python -m analysis.tension_curve --switchback runs/s8/w0/switchback_call_28d --budget runs/s8/w0/calibrate_budget
        --all-off runs/s8/w0/all_off_5seeds_full --all-on runs/s8/w0/all_on_5seeds_full --legacy runs/s8/w0/legacy_28d
        --out runs/s9/w0 [--quantiles 10] [--n-boot 500]

Reads only ``observed/`` and ``market/`` tables (docs/schema.md); nothing here simulates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from analysis import estimate, theta
from analysis.estimate import ratio_slack
from analysis.io import load_run

MODERATORS = ("system_pre", "ring_pre", "cell_pre")
THETA_GRID = np.round(np.arange(0.0, 10.0001, 0.05), 2)      # same range as the runs/s5 sweeps (θ ≤ 10)
TENSION_EDGES = (0.0, 0.35, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, float("inf"))   # fixed bins; 0.35 = WGC-like cut


# ---------------------------------------------------------------------------
# Effect curve and tension threshold
# ---------------------------------------------------------------------------


def curve_by_tension(frame: pd.DataFrame, moderator: str, *, edges: Sequence[float] | None = TENSION_EDGES,
                     quantiles: int | None = None, n_boot: int = 500, seed: int = 0) -> pd.DataFrame:
    """Effect per tension bin (fixed ``edges`` by default, or ``quantiles`` equal-count bins) with bin bounds."""
    table = estimate.effect_by_bin(frame, moderator, edges=None if quantiles else list(edges), quantiles=quantiles,
                                   n_boot=n_boot, seed=seed)
    bounds = table["bin"].astype(str).str.strip("[)").str.split(", ", expand=True).astype(float)
    table.insert(1, "bin_lo", bounds[0].to_numpy())
    table.insert(2, "bin_hi", bounds[1].to_numpy())
    table["moderator"] = moderator
    table["sign_changes"] = estimate.sign_changes(table["effect"])
    return table


def tension_thresholds(frame: pd.DataFrame, moderators: Sequence[str] = MODERATORS, *, quantiles: int = 20,
                       n_boot: int = 500, seed: int = 0) -> pd.DataFrame:
    """``theta_hat_tension`` (zero crossing of the isotonic effect curve) per moderator, with interval."""
    return pd.DataFrame([estimate.theta_hat(frame, m, quantiles=quantiles, n_boot=n_boot, seed=seed)
                         for m in moderators])


# ---------------------------------------------------------------------------
# Hour method on a count table (same numbers as analysis.theta, feasible on millions of rows)
# ---------------------------------------------------------------------------


def _kept_counts(indicator: np.ndarray, hour: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """``kept[k, h]``: sessions of hour ``h`` a threshold ``grid[k]`` keeps (``not (x < theta)``: NaN and +inf kept)."""
    x = np.asarray(indicator, dtype=np.float64)
    h = np.asarray(hour, dtype=np.int64)
    always = ~np.isfinite(x) & ~(x == -np.inf)                       # NaN and +inf: never below any theta
    # Number of grid points theta with x < theta, i.e. the first k whose grid[k] > x; sessions with x < grid[k]
    # are cut at k. cut_at[i] = count of grid values <= x[i]  ->  kept for k <= cut_at[i] - 1 + ... see below.
    first_cut = np.searchsorted(grid, x[~always], side="right")     # smallest k with grid[k] > x
    counts = np.zeros((len(grid) + 1, 24))
    np.add.at(counts, (first_cut, h[~always]), 1.0)                  # cut from index first_cut onwards
    kept = np.cumsum(counts[::-1], axis=0)[::-1][1:]                 # kept[k] = sessions with first_cut > k
    kept = kept + np.bincount(h[always], minlength=24)[None, :]
    return kept


def gain_curves_fast(kept: np.ndarray, grid: np.ndarray, n_total: int, n_days: float, tau: np.ndarray,
                     cost: np.ndarray, budget_per_day: float) -> pd.DataFrame:
    """:func:`analysis.theta.gain_curves` from the ``kept`` count table."""
    t = np.nan_to_num(np.asarray(tau, dtype=np.float64))
    c = np.nan_to_num(np.asarray(cost, dtype=np.float64))
    sum_tau = kept @ t / n_days
    spend = kept @ c / n_days
    with np.errstate(divide="ignore", invalid="ignore"):
        rate = np.where(spend > 0, sum_tau / spend, 0.0)
    return pd.DataFrame({"theta": grid.astype(float), "share_cut": 1.0 - kept.sum(axis=1) / n_total,
                         "gain_A": sum_tau, "spend_all_on": spend, "gain_B": np.minimum(budget_per_day, spend) * rate})


def hour_theta_hat(frame: pd.DataFrame, indicator_col: str, effects: dict, *, n_days: float, budget_per_day: float,
                   grid: np.ndarray = THETA_GRID, alpha: float = 0.05, drop_burnin: bool = True) -> dict:
    """``theta_hat_A`` / ``theta_hat_B`` of :mod:`analysis.theta` for the indicator column, via count tables."""
    f = frame[~frame["in_burnin"]] if drop_burnin else frame
    grid = np.asarray(grid, dtype=np.float64)
    kept = _kept_counts(f[indicator_col].to_numpy(), f["hour"].to_numpy(), grid)
    n_total = len(f)
    curves = gain_curves_fast(kept, grid, n_total, n_days, effects["tau"], effects["cost"], budget_per_day)
    out = {"indicator": indicator_col, "curves": curves}
    draws = {"A": [], "B": []}
    for tau_b, cost_b in zip(effects["tau_draws"], effects["cost_draws"]):
        cb = gain_curves_fast(kept, grid, n_total, n_days, tau_b, cost_b, budget_per_day)
        draws["A"].append(grid[int(np.nanargmax(cb["gain_A"]))])
        draws["B"].append(grid[int(np.nanargmax(cb["gain_B"]))])
    for k in ("A", "B"):
        d = np.asarray(draws[k])
        lo, hi = np.quantile(d, [alpha / 2, 1 - alpha / 2])
        out[f"theta_hat_{k}"] = float(grid[int(np.nanargmax(curves[f"gain_{k}"]))])
        out[f"ci_{k}"] = (float(lo), float(hi))
    return out


def harmful_hours(effects: dict, *, alpha: float = 0.05) -> list[int]:
    """Hours whose effect on the completion rate is negative with the whole ``1 - alpha`` interval below 0."""
    table = theta.hour_table(effects, alpha=alpha)
    return [int(h) for h, hi in zip(table["hour"], table["hi"]) if np.isfinite(hi) and hi < 0]


# ---------------------------------------------------------------------------
# Tension distribution without vouchers vs under a policy
# ---------------------------------------------------------------------------


def _snapshot_dirs(d: Path) -> list[Path]:
    d = Path(d)
    if (d / "market" / "slot_snapshots.parquet").exists():
        return [d]
    return sorted(p.parent.parent for p in d.glob("runs/*/market/slot_snapshots.parquet"))


def city_tension_by_slot(run_dir: Path) -> pd.DataFrame:
    """Per published slot: city slack = sum(idle_avg) / sum(enroute_avg) over cells, window slots only.

    ``run_dir`` is a generate directory or one ``<mode dir>/runs/<run_id>`` of an evaluate run; the window and
    ``slot_min`` come from its ``run_metadata`` row (in the directory or in the mode directory above).
    """
    import yaml
    from analysis.validation import _run_metadata_row

    snaps = load_run(run_dir)["slot_snapshots"]
    meta = _run_metadata_row(Path(run_dir))
    slot_s = float(yaml.safe_load(meta["config_yaml"])["time"]["slot_min"]) * 60.0
    by_slot = snaps.groupby("slot", as_index=False).agg(idle=("idle_avg", "sum"), enroute=("enroute_avg", "sum"),
                                                        hour=("hour", "first"))
    by_slot["tension"] = ratio_slack(by_slot["idle"], by_slot["enroute"])
    start = by_slot["slot"].to_numpy(np.float64) * slot_s
    in_window = (start >= float(meta["window_start_s"])) & (start < float(meta["window_end_s"]))
    return by_slot.loc[in_window, ["slot", "hour", "tension"]].reset_index(drop=True)


def tension_distribution(run_dirs: dict[str, Path], *, cut: float = 0.35) -> pd.DataFrame:
    """Quantiles of the city tension per slot for each labelled directory (an evaluate dir with several seeds
    or one generate dir); ``share_below_cut`` is the share of window slots with tension < ``cut``."""
    rows = []
    for label, d in run_dirs.items():
        parts = [city_tension_by_slot(p) for p in _snapshot_dirs(Path(d))]
        if not parts:
            raise FileNotFoundError(f"{d}: no market/slot_snapshots.parquet")
        t = pd.concat(parts)["tension"].to_numpy(np.float64)
        finite = t[np.isfinite(t)]
        q = np.quantile(finite, [0.1, 0.25, 0.5, 0.75, 0.9]) if len(finite) else [np.nan] * 5
        rows.append({"label": label, "n_slots": int(len(t)), "share_below_cut": float(np.mean(t < cut)),
                     "share_inf": float(np.mean(np.isinf(t))), "q10": q[0], "q25": q[1], "median": q[2],
                     "q75": q[3], "q90": q[4], "mean_finite": float(finite.mean()) if len(finite) else np.nan})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# One world end to end
# ---------------------------------------------------------------------------


def budget_per_day_of(budget_dir: Path) -> float:
    meta = pd.read_parquet(Path(budget_dir) / "meta" / "run_metadata.parquet").iloc[0]
    return float(meta["budget_B_vnd"])        # one budget period per day (default.yaml)


def analyse_world(switchback_dir: Path, *, budget_per_day: float, n_boot: int = 500, quantiles: int = 20,
                  seed: int = 0, alpha: float = 0.05) -> dict:
    """Curves, thresholds, hour method and harmful hours of one city-wide switchback run."""
    frame = estimate.experiment_frame(switchback_dir)
    meta = pd.read_parquet(Path(switchback_dir) / "meta" / "run_metadata.parquet").iloc[0]
    n_days = (float(meta["window_end_s"]) - float(meta["window_start_s"])) / 86400.0
    curves = pd.concat([curve_by_tension(frame, m, n_boot=n_boot, seed=seed) for m in MODERATORS], ignore_index=True)
    thresholds = tension_thresholds(frame, quantiles=quantiles, n_boot=n_boot, seed=seed)
    effects = theta.hour_effects(frame, n_boot=n_boot, seed=seed)
    hours = theta.hour_table(effects, alpha=alpha)
    hour_method = {m: hour_theta_hat(frame, m, effects, n_days=n_days, budget_per_day=budget_per_day, alpha=alpha)
                   for m in MODERATORS}
    return {"frame_rows": int(len(frame)), "n_units": int(frame["unit"].nunique()), "n_days": float(n_days),
            "curves": curves, "thresholds": thresholds, "hour_effects": hours,
            "hour_method": hour_method, "harmful_hours": harmful_hours(effects, alpha=alpha)}


def summary_markdown(result: dict, distribution: pd.DataFrame | None, world: str) -> str:
    lines = [f"## {world}: tác dụng voucher theo độ căng trước khối (switchback toàn thành phố)", ""]
    lines.append(f"{result['frame_rows']:,} session trong cửa sổ, {result['n_units']} khối, {result['n_days']:.0f} ngày.")
    lines.append("")
    lines.append("### Đường tác dụng theo độ căng (tỷ lệ hoàn thành on − off, CI 95% bootstrap theo khối)")
    lines.append("")
    lines.append("| moderator | bin | n_off | n_on | effect | ci_lo | ci_hi |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in result["curves"].itertuples():
        lines.append(f"| {r.moderator} | {r.bin} | {r.n_off:,} | {r.n_on:,} | {r.effect:+.4f} | {r.ci_lo:+.4f} | {r.ci_hi:+.4f} |")
    lines.append("")
    lines.append("### Ngưỡng theo độ căng (zero crossing, isotonic) so với ngưỡng theo giờ")
    lines.append("")
    lines.append("| moderator | θ̂_tension [CI] | share_boot_zero | θ̂_A (giờ, không B) [CI] | θ̂_B (giờ, có B) [CI] |")
    lines.append("|---|---|---|---|---|")
    th = result["thresholds"].set_index("moderator")
    for m in MODERATORS:
        hm = result["hour_method"][m]
        lines.append(f"| {m} | {th.loc[m, 'theta_hat']:.2f} [{th.loc[m, 'ci_lo']:.2f}; {th.loc[m, 'ci_hi']:.2f}] | "
                     f"{th.loc[m, 'share_boot_zero']:.2f} | {hm['theta_hat_A']:.2f} [{hm['ci_A'][0]:.2f}; {hm['ci_A'][1]:.2f}] | "
                     f"{hm['theta_hat_B']:.2f} [{hm['ci_B'][0]:.2f}; {hm['ci_B'][1]:.2f}] |")
    lines.append("")
    lines.append(f"### Giờ có hại (τ̂(h) với cận trên CI < 0): {result['harmful_hours'] or 'không'}")
    lines.append("")
    lines.append("| giờ | τ̂(h) | lo | hi | chi/session on (đ) |")
    lines.append("|---|---|---|---|---|")
    for r in result["hour_effects"].itertuples():
        lines.append(f"| {r.hour} | {r.tau:+.4f} | {r.lo:+.4f} | {r.hi:+.4f} | {r.cost:,.0f} |")
    if distribution is not None:
        lines += ["", "### Phân bố độ căng toàn thành phố theo slot: không voucher so với dưới chính sách", "",
                  "| lượt | slot | share < 0,35 | share inf | q10 | q25 | median | q75 | q90 |", "|---|---|---|---|---|---|---|---|---|"]
        for r in distribution.itertuples():
            lines.append(f"| {r.label} | {r.n_slots} | {r.share_below_cut:.3f} | {r.share_inf:.3f} | {r.q10:.2f} | {r.q25:.2f} | "
                         f"{r.median:.2f} | {r.q75:.2f} | {r.q90:.2f} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Voucher effect by pre-block tension and the tension threshold")
    ap.add_argument("--switchback", type=Path, required=True, help="city-wide switchback generate dir")
    ap.add_argument("--budget", type=Path, default=None, help="calibrate_budget dir (B per day)")
    ap.add_argument("--budget-per-day", type=float, default=None, help="B per day in đồng, instead of --budget")
    ap.add_argument("--all-off", type=Path, default=None)
    ap.add_argument("--all-on", type=Path, default=None)
    ap.add_argument("--legacy", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--world", default=None)
    ap.add_argument("--quantiles", type=int, default=20)
    ap.add_argument("--n-boot", type=int, default=500)
    args = ap.parse_args(argv)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    world = args.world or out.name
    if (args.budget is None) == (args.budget_per_day is None):
        ap.error("give exactly one of --budget (calibrate_budget dir) or --budget-per-day")
    budget = budget_per_day_of(args.budget) if args.budget is not None else float(args.budget_per_day)
    result = analyse_world(args.switchback, budget_per_day=budget, n_boot=args.n_boot, quantiles=args.quantiles)
    dirs = {k: v for k, v in (("all_off", args.all_off), ("all_on", args.all_on), ("legacy", args.legacy)) if v}
    distribution = tension_distribution(dirs) if dirs else None

    result["curves"].to_parquet(out / "curve_by_tension.parquet", index=False)
    result["thresholds"].to_parquet(out / "thresholds.parquet", index=False)
    result["hour_effects"].to_parquet(out / "hour_effects.parquet", index=False)
    if distribution is not None:
        distribution.to_parquet(out / "tension_distribution.parquet", index=False)
    payload = {"world": world, "switchback": str(args.switchback), "frame_rows": result["frame_rows"],
               "n_units": result["n_units"], "n_days": result["n_days"], "harmful_hours": result["harmful_hours"],
               "theta_hat_tension": {r["moderator"]: {k: (float(v) if isinstance(v, (int, float, np.floating)) else v)
                                                       for k, v in r.items() if k != "moderator"}
                                     for r in result["thresholds"].to_dict("records")},
               "theta_hat_hour": {m: {k: v for k, v in hm.items() if k != "curves"} for m, hm in result["hour_method"].items()}}
    (out / "summary.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    text = summary_markdown(result, distribution, world)
    (out / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
