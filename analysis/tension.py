"""Which supply-tension indicator should s_hat be?

Rule: pick the indicator that best forecasts the **service level** a session gets,
on ``all_off`` data, without looking at N. Every candidate only uses snapshots published before
the session's slot (slot k-1, or the same slot one day earlier), as a policy would:

- ``cell_persistence``  slack of the pickup cell in slot k-1 (the current ``forecast: persistence``)
- ``cell_ar``           ``ar_weights . [slot k-1, same slot a day earlier]`` (``forecast: ar``)
- ``cell_idle``         idle drivers of the pickup cell in slot k-1
- ``ring1``             idle / en-route summed over the cell and its six neighbours, slot k-1
- ``cluster7``          idle / en-route summed over the cell's fixed 7-cell cluster, slot k-1
- ``system``            idle / en-route summed over all cells, slot k-1
- ``hour``              hour of day only: the reference a tension indicator must beat

Slack ``inf`` is capped at ``monitor.slack_cap``. Two scores per candidate, on in-window sessions:

- ``eta_r2``: share of the variance of ``quoted_eta_min`` explained by the mean ETA of ``n_bins``
  quantile bins of the candidate (hour: one bin per hour);
- ``bad_capture``: share of "badly served" sessions (``no_supply`` or ETA >= ``bad_eta_min``) found
  when the same share of sessions as the tight ones of the policy is flagged, lowest values first.
  Ties at the cut are split pro rata, so a coarse indicator is not favoured.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from analysis.estimate import ratio_slack, ring_slack

CANDIDATES = ("cell_persistence", "cell_ar", "cell_idle", "ring1", "cluster7", "system", "hour")


def tension_frame(sessions: pd.DataFrame, snaps: pd.DataFrame, cluster_of_cell: np.ndarray, *, slack_cap: float,
                  ar_weights: tuple[float, float], neighbors: np.ndarray) -> pd.DataFrame:
    """In-window sessions with every candidate indicator and the service-level columns."""
    s = sessions.loc[sessions["in_window"], ["session_id", "slot", "hour", "pu_cell", "quoted_eta_min",
                                             "no_supply"]].copy()
    sn = snaps[["slot", "cell", "idle_avg", "enroute_avg", "slack_lag_slot", "slack_lag_day"]].copy()
    sn["cluster"] = np.asarray(cluster_of_cell)[sn["cell"].to_numpy()]
    prev = sn[["slot", "cell", "idle_avg"]].assign(slot=sn["slot"] + 1)          # published at the start of k
    clu = sn.groupby(["slot", "cluster"], as_index=False)[["idle_avg", "enroute_avg"]].sum()
    clu["cluster7"] = ratio_slack(clu["idle_avg"], clu["enroute_avg"])
    sysw = sn.groupby("slot", as_index=False)[["idle_avg", "enroute_avg"]].sum()
    sysw["system"] = ratio_slack(sysw["idle_avg"], sysw["enroute_avg"])

    s = s.merge(sn[["slot", "cell", "slack_lag_slot", "slack_lag_day", "cluster"]]
                .rename(columns={"cell": "pu_cell"}), on=["slot", "pu_cell"], how="left", validate="many_to_one")
    s = s.merge(prev.rename(columns={"cell": "pu_cell", "idle_avg": "cell_idle"}), on=["slot", "pu_cell"], how="left")
    s = s.merge(clu[["slot", "cluster", "cluster7"]].assign(slot=clu["slot"] + 1), on=["slot", "cluster"], how="left")
    s = s.merge(sysw[["slot", "system"]].assign(slot=sysw["slot"] + 1), on="slot", how="left")
    ring = ring_slack(sn, neighbors)
    s = s.merge(ring.assign(slot=ring["slot"] + 1).rename(columns={"cell": "pu_cell"}), on=["slot", "pu_cell"],
                how="left")

    lag = np.minimum(s["slack_lag_slot"].to_numpy(np.float64), slack_cap)
    day = np.minimum(s["slack_lag_day"].to_numpy(np.float64), slack_cap)
    w_slot, w_day = ar_weights
    s["cell_persistence"] = lag
    s["cell_ar"] = np.where(np.isnan(day), lag, w_slot * lag + w_day * day)       # same fallback as the policy
    for c in ("ring1", "cluster7", "system"):
        s[c] = np.minimum(s[c].to_numpy(np.float64), slack_cap)
    return s.drop(columns=["slack_lag_slot", "slack_lag_day", "cluster"]).reset_index(drop=True)


def eta_r2(indicator, eta, *, n_bins: int, categorical: bool = False) -> float:
    """Share of the variance of ``eta`` explained by the bin means of ``indicator`` (NaN rows dropped)."""
    x, y = np.asarray(indicator, dtype=np.float64), np.asarray(eta, dtype=np.float64)
    ok = ~np.isnan(x) & ~np.isnan(y)
    x, y = x[ok], y[ok]
    if categorical:
        groups = x
    else:
        # Rank-based bins so ties never straddle two bins.
        ranks = pd.Series(x).rank(method="min").to_numpy()
        groups = np.minimum((ranks - 1) * n_bins // len(x), n_bins - 1)
    means = pd.Series(y).groupby(groups).transform("mean").to_numpy()
    total = float(((y - y.mean()) ** 2).sum())
    return float(((means - y.mean()) ** 2).sum() / total) if total > 0 else float("nan")


def bad_capture(indicator, bad, share: float) -> float:
    """Share of ``bad`` rows among the ``share`` lowest values of ``indicator``; ties at the cut pro rata."""
    x, b = np.asarray(indicator, dtype=np.float64), np.asarray(bad, dtype=bool)
    ok = ~np.isnan(x)
    x, b = x[ok], b[ok]
    k = share * len(x)
    order = pd.DataFrame({"x": x, "b": b}).groupby("x")["b"].agg(["size", "sum"]).sort_index()
    before = order["size"].cumsum().shift(fill_value=0).to_numpy()
    take = np.clip(k - before, 0, order["size"].to_numpy())
    caught = float((order["sum"].to_numpy() * take / order["size"].to_numpy()).sum())
    return caught / b.sum() if b.sum() else float("nan")


def score_indicators(frame: pd.DataFrame, *, n_bins: int, bad_eta_min: float, share: float) -> pd.DataFrame:
    """One row per candidate: ``eta_r2``, ``bad_capture`` and the share of sessions with a value."""
    bad = frame["no_supply"].to_numpy(bool) | (frame["quoted_eta_min"].to_numpy() >= bad_eta_min)
    rows = []
    for c in CANDIDATES:
        x = frame[c].to_numpy(np.float64)
        rows.append({"indicator": c,
                     "eta_r2": eta_r2(x, frame["quoted_eta_min"], n_bins=n_bins, categorical=(c == "hour")),
                     # "hour" ranks by mean ETA of the hour: the best a pure time-of-day rule can do.
                     "bad_capture": bad_capture(-_hour_eta(frame) if c == "hour" else x, bad, share),
                     "coverage": float(np.mean(~np.isnan(x)))})
    return pd.DataFrame(rows)


def _hour_eta(frame: pd.DataFrame) -> np.ndarray:
    return frame.groupby("hour")["quoted_eta_min"].transform("mean").to_numpy(np.float64)


def run_frames(out_dir: Path, cfg) -> pd.DataFrame:
    """``tension_frame`` of every per-seed run under ``<out_dir>/runs`` (``evaluate --log-level full``)."""
    from analysis.io import load_run
    from sim.experiment import cluster_ids
    from sim.space import build_space

    space = build_space(cfg)
    clusters = cluster_ids(space, 7)
    frames = []
    for run_dir in sorted((Path(out_dir) / "runs").iterdir()):
        run = load_run(run_dir)
        f = tension_frame(run["sessions"], run["slot_snapshots"], clusters, slack_cap=float(cfg.monitor.slack_cap),
                          ar_weights=tuple(cfg.policy.threshold.ar_weights), neighbors=space.neighbors)
        frames.append(f.assign(seed=int(run_dir.name.rsplit("-", 1)[1])))      # run_id ends with the seed
    return pd.concat(frames, ignore_index=True)


def main(argv: list[str]) -> int:
    """``python -m analysis.tension <all_off evaluate dir> [config.yaml]``: print the indicator table."""
    from sim.config import load_config

    if len(argv) < 2:
        print("usage: python -m analysis.tension <all_off evaluate dir, --log-level full> [config.yaml ...]")
        return 2
    # Several config layers merge in order (e.g. default.yaml + world_w1.yaml).
    cfg = load_config(argv[2:] or [Path(__file__).resolve().parents[1] / "config" / "default.yaml"])
    frame = run_frames(Path(argv[1]), cfg)
    share = float(np.mean(frame["cell_persistence"] < cfg.policy.threshold.theta))
    print(f"{len(frame)} in-window sessions, {frame['seed'].nunique()} seeds; flagged share = "
          f"share of sessions with cell_persistence < theta ({cfg.policy.threshold.theta}) = {share:.3f}")
    table = score_indicators(frame, n_bins=10, bad_eta_min=8.0, share=share)
    by_seed = pd.concat([score_indicators(g, n_bins=10, bad_eta_min=8.0, share=share).assign(seed=s)
                         for s, g in frame.groupby("seed")])
    se = by_seed.groupby("indicator")[["eta_r2", "bad_capture"]].std(ddof=1) / np.sqrt(frame["seed"].nunique())
    table = table.join(se, on="indicator", rsuffix="_se")
    print(table.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
