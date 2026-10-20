"""Estimation on experiment data: voucher effect by supply tension, sign changes (A4), threshold estimate.

Inputs are run directories written by ``generate`` with ``policy.name = experiment``
(README.md, section "Tái lập kết quả"; e.g. ``runs/b7a``). Only observed/ and market/ tables are
read; nothing here touches hidden/ (docs/schema.md).

The unit of randomization is a (cluster, block). Every confidence interval resamples those
units, never single sessions: sessions of one unit share the same arm and the same market.

Moderators (the tension measure a session is binned by):

- ``cell_lag``     slack of the session's cell in the previous slot (``slack_lag_slot``). This is
                   what acceptance check A4 names, but inside a block it is already affected by the
                   block's own arm (post-treatment), so on/off groups are not comparable.
- ``cell_pre``     slack of the cell in the last slot before the block began.
- ``ring_pre``     idle / en-route summed over the cell and its six neighbours, last slot before the
                   block (the s_hat chosen by ``analysis.tension``).
- ``cluster_pre``  idle / en-route summed over the session's cluster, last slot before the block.
- ``system_pre``   idle / en-route summed over all cells, last slot before the block.

The three ``*_pre`` moderators are fixed before the block's coin is tossed.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from analysis.io import completed_outcome, load_run

MODERATORS = ("cell_lag", "cell_pre", "ring_pre", "cluster_pre", "system_pre")
DEFAULT_EDGES = (0.0, 0.2, 0.35, 0.6, 1.0, 2.0, 5.0, np.inf)


# ---------------------------------------------------------------------------
# Session frame
# ---------------------------------------------------------------------------


def ratio_slack(idle, enroute) -> np.ndarray:
    """``idle / enroute`` with the rule of the simulator: E = 0 gives inf if I > 0, else 0."""
    idle = np.asarray(idle, dtype=np.float64)
    enroute = np.asarray(enroute, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(enroute > 0, idle / enroute, np.where(idle > 0, np.inf, 0.0))


def ring_slack(snaps: pd.DataFrame, neighbors: np.ndarray) -> pd.DataFrame:
    """``(slot, cell, ring1)``: idle / en-route summed over each cell and its neighbours (``-1`` = none)."""
    nb = np.asarray(neighbors)
    n = nb.shape[0]
    member = np.zeros((n, n))                                   # member[c, j] = 1 if j is c or a neighbour of c
    member[np.arange(n), np.arange(n)] = 1.0
    rows, cols = np.nonzero(nb >= 0)
    member[rows, nb[rows, cols]] = 1.0
    idle = snaps.pivot(index="slot", columns="cell", values="idle_avg").reindex(columns=range(n)).to_numpy()
    enr = snaps.pivot(index="slot", columns="cell", values="enroute_avg").reindex(columns=range(n)).to_numpy()
    slots = snaps["slot"].drop_duplicates().sort_values().to_numpy()
    ring = ratio_slack(idle @ member.T, enr @ member.T)
    return pd.DataFrame({"slot": np.repeat(slots, n), "cell": np.tile(np.arange(n), len(slots)), "ring1": ring.ravel()})


def run_config(run: dict[str, pd.DataFrame]) -> dict:
    """The config the run was made with (``meta/run_metadata.config_yaml``)."""
    return yaml.safe_load(run["run_metadata"]["config_yaml"].iloc[0])


def stored_config(raw: dict):
    """``Config`` of a stored run config; keys added to ``config/default.yaml`` after the run take their default.

    ``build_config`` rejects a stored config that lacks a key added later (e.g. ``runs/b7a``, generated
    before ``policy.threshold.scope``); every stored value still wins.
    """
    from sim.config import build_config, load_raw

    def merge(base: dict, overlay: dict) -> None:
        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value

    base = load_raw(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
    merge(base, raw)
    return build_config(base)


def experiment_frame(run_dir: Path | str) -> pd.DataFrame:
    """One row per in-window session of a switchback run: arm, outcome, unit and the moderators."""
    from sim.space import build_space

    run = load_run(Path(run_dir))
    cfg = run_config(run)
    neighbors = build_space(stored_config(cfg)).neighbors
    slots_per_block = cfg["experiment"]["block_min"] // cfg["time"]["slot_min"]
    sessions, orders, snaps = run["sessions"], run["orders"], run["slot_snapshots"]

    s = sessions.loc[sessions["in_window"], ["session_id", "slot", "pu_cell", "hour", "arm", "requested",
                                             "cluster_id", "block", "in_burnin", "voucher_value_vnd"]].copy()
    if (s["block"] < 0).any():
        raise ValueError(f"{run_dir}: not an experiment run (block = -1)")
    s["completed"] = completed_outcome(s, orders)
    s["unit"] = s["cluster_id"].astype(np.int64) * 1_000_000 + s["block"].astype(np.int64)

    cell = snaps[["slot", "cell", "slack", "slack_lag_slot", "idle_avg", "enroute_avg", "cluster_id"]]
    s = s.merge(cell[["slot", "cell", "slack_lag_slot"]].rename(columns={"cell": "pu_cell", "slack_lag_slot": "cell_lag"}),
                on=["slot", "pu_cell"], how="left")

    # State in the last slot before each block: slot = block * slots_per_block - 1.
    last = cell[(cell["slot"] + 1) % slots_per_block == 0].copy()
    last["block"] = (last["slot"] + 1) // slots_per_block
    s = s.merge(last[["block", "cell", "slack"]].rename(columns={"cell": "pu_cell", "slack": "cell_pre"}),
                on=["block", "pu_cell"], how="left")
    ring = ring_slack(last, neighbors).merge(last[["slot", "block"]].drop_duplicates(), on="slot")
    s = s.merge(ring[["block", "cell", "ring1"]].rename(columns={"cell": "pu_cell", "ring1": "ring_pre"}),
                on=["block", "pu_cell"], how="left")
    by_cluster = last.groupby(["block", "cluster_id"], as_index=False)[["idle_avg", "enroute_avg"]].sum()
    by_cluster["cluster_pre"] = ratio_slack(by_cluster["idle_avg"], by_cluster["enroute_avg"])
    s = s.merge(by_cluster[["block", "cluster_id", "cluster_pre"]], on=["block", "cluster_id"], how="left")
    by_system = last.groupby("block", as_index=False)[["idle_avg", "enroute_avg"]].sum()
    by_system["system_pre"] = ratio_slack(by_system["idle_avg"], by_system["enroute_avg"])
    s = s.merge(by_system[["block", "system_pre"]], on="block", how="left")
    return s


# ---------------------------------------------------------------------------
# Effect by bin, with unit-level bootstrap
# ---------------------------------------------------------------------------


def _unit_sums(frame: pd.DataFrame, outcome: str) -> pd.DataFrame:
    """Per (bin, unit): arm, number of sessions, sum of the outcome."""
    g = frame.groupby(["bin", "unit"], observed=True)
    return g.agg(arm=("arm", "first"), n=("arm", "size"), y=(outcome, "sum")).reset_index()


def _bootstrap_effects(units: pd.DataFrame, bins: list, n_boot: int, seed: int) -> np.ndarray:
    """``[n_boot, n_bins]`` effects (rate on - rate off), resampling units with replacement."""
    all_units = units["unit"].unique()
    index = {u: i for i, u in enumerate(all_units)}
    gen = np.random.default_rng(seed)
    weights = gen.multinomial(len(all_units), np.full(len(all_units), 1.0 / len(all_units)), size=n_boot).astype(np.float64)
    out = np.full((n_boot, len(bins)), np.nan)
    for j, b in enumerate(bins):
        part = units[units["bin"] == b]
        w = weights[:, part["unit"].map(index).to_numpy()]
        on = part["arm"].to_numpy() == 1
        y, n = part["y"].to_numpy(np.float64), part["n"].to_numpy(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[:, j] = (w[:, on] @ y[on]) / (w[:, on] @ n[on]) - (w[:, ~on] @ y[~on]) / (w[:, ~on] @ n[~on])
    return out


def assign_bins(frame: pd.DataFrame, moderator: str, *, edges=None, quantiles: int | None = None) -> pd.DataFrame:
    """Copy of ``frame`` with a ``bin`` column: fixed ``edges`` (left-closed) or ``quantiles`` equal-count bins.

    Sessions without a moderator value (first block of the run) are dropped. Quantile
    bins are cut on the distinct values, so ties (many cells sit exactly at 0) never split.
    """
    if moderator not in MODERATORS:
        raise ValueError(f"moderator must be one of {MODERATORS}, got {moderator!r}")
    f = frame[frame[moderator].notna()].copy()
    x = f[moderator].to_numpy(np.float64)
    if quantiles is not None:
        finite_max = np.nanmax(x[np.isfinite(x)]) if np.isfinite(x).any() else 0.0
        q = np.quantile(np.minimum(x, finite_max + 1.0), np.linspace(0, 1, quantiles + 1))
        edges = np.unique(q)
        edges[-1] = np.inf
        if len(edges) < 2:
            raise ValueError("moderator has a single value: cannot bin")
    edges = np.asarray(DEFAULT_EDGES if edges is None else edges, dtype=np.float64)
    labels = [f"[{a:g}, {b:g})" for a, b in zip(edges[:-1], edges[1:])]
    code = np.searchsorted(edges, x, side="right") - 1
    code = np.where(np.isinf(x), len(labels) - 1, code)            # +inf belongs to the last bin
    f = f[(code >= 0) & (code < len(labels))].copy()
    code = code[(code >= 0) & (code < len(labels))]
    f["bin"] = pd.Categorical.from_codes(code, categories=labels, ordered=True)
    return f


def effect_by_bin(frame: pd.DataFrame, moderator: str, *, edges=None, quantiles: int | None = None,
                  outcome: str = "completed", drop_burnin: bool = True, n_boot: int = 500, seed: int = 0,
                  alpha: float = 0.05) -> pd.DataFrame:
    """Difference in the outcome rate per session, on minus off, in each bin of the moderator.

    Columns: ``bin, n_off, n_on, rate_off, rate_on, effect, ci_lo, ci_hi, share_on``.
    ``share_on`` far from the design's ``p_on`` in a bin means the moderator is not balanced
    between arms there, i.e. it was moved by the treatment.
    """
    f = frame[~frame["in_burnin"]] if drop_burnin else frame
    f = assign_bins(f, moderator, edges=edges, quantiles=quantiles)
    units = _unit_sums(f, outcome)
    bins = [b for b in f["bin"].cat.categories if (units["bin"] == b).any()]
    boot = _bootstrap_effects(units, bins, n_boot, seed)
    rows = []
    for j, b in enumerate(bins):
        part = units[units["bin"] == b]
        on = part["arm"] == 1
        n_on, n_off = int(part.loc[on, "n"].sum()), int(part.loc[~on, "n"].sum())
        rate_on = part.loc[on, "y"].sum() / n_on if n_on else np.nan
        rate_off = part.loc[~on, "y"].sum() / n_off if n_off else np.nan
        lo, hi = np.nanquantile(boot[:, j], [alpha / 2, 1 - alpha / 2])
        rows.append({"bin": b, "n_off": n_off, "n_on": n_on, "rate_off": rate_off, "rate_on": rate_on,
                     "effect": rate_on - rate_off, "ci_lo": lo, "ci_hi": hi, "share_on": n_on / (n_on + n_off)})
    return pd.DataFrame(rows)


def sign_changes(effects) -> int:
    """Number of sign changes along the ordered bins (acceptance check A4); zeros and NaN are skipped."""
    signs = [np.sign(e) for e in np.asarray(effects, dtype=np.float64) if np.isfinite(e) and e != 0]
    return int(sum(a != b for a, b in zip(signs[:-1], signs[1:])))


# ---------------------------------------------------------------------------
# Threshold estimate
# ---------------------------------------------------------------------------


def isotonic_increasing(values, weights) -> np.ndarray:
    """Weighted least-squares non-decreasing fit (pool adjacent violators)."""
    v = [float(x) for x in values]
    w = [float(x) for x in weights]
    blocks = [[v[i], w[i], 1] for i in range(len(v))]                 # mean, weight, length
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            m0, w0, l0 = blocks[i]
            m1, w1, l1 = blocks[i + 1]
            blocks[i:i + 2] = [[(m0 * w0 + m1 * w1) / (w0 + w1), w0 + w1, l0 + l1]]
            i = max(i - 1, 0)
        else:
            i += 1
    return np.concatenate([np.full(length, mean) for mean, _, length in blocks])


def theta_hat(frame: pd.DataFrame, moderator: str, *, quantiles: int = 20, outcome: str = "completed",
              drop_burnin: bool = True, n_boot: int = 500, seed: int = 0, alpha: float = 0.05) -> dict:
    """Tension level below which a voucher no longer adds completed trips.

    The effect is estimated in ``quantiles`` equal-count bins of the moderator, made
    non-decreasing in the moderator (more slack, more effect), and ``theta_hat`` is the
    upper edge of the highest bin whose fitted effect is <= 0; 0.0 when the fitted effect
    is positive in every bin ("never cut"). The interval comes from the unit bootstrap.

    This is the zero crossing of the effect **without a budget constraint**. The optimum
    theta* of a budgeted sweep can sit higher, because a budget makes it worth cutting
    cells whose effect is positive but small (compared in ``analysis.theta``).
    """
    f = frame[~frame["in_burnin"]] if drop_burnin else frame
    f = assign_bins(f, moderator, quantiles=quantiles)
    units = _unit_sums(f, outcome)
    bins = [b for b in f["bin"].cat.categories if (units["bin"] == b).any()]
    upper = np.array([float(str(b).split(", ")[1].rstrip(")")) for b in bins])
    weight = np.array([units.loc[units["bin"] == b, "n"].sum() for b in bins], dtype=np.float64)

    def crossing(effects: np.ndarray) -> float:
        ok = np.isfinite(effects)
        if not ok.any():
            return np.nan
        fitted = isotonic_increasing(effects[ok], weight[ok])
        below = np.flatnonzero(fitted <= 0)
        return float(upper[ok][below[-1]]) if len(below) else 0.0

    point = effect_by_bin(frame, moderator, quantiles=quantiles, outcome=outcome, drop_burnin=drop_burnin,
                          n_boot=1, seed=seed)
    boot = _bootstrap_effects(units, bins, n_boot, seed)
    draws = np.array([crossing(row) for row in boot])
    lo, hi = np.nanquantile(draws, [alpha / 2, 1 - alpha / 2])
    return {"moderator": moderator, "theta_hat": crossing(point["effect"].to_numpy()), "ci_lo": float(lo),
            "ci_hi": float(hi), "share_boot_zero": float(np.mean(draws == 0.0)), "n_bins": len(bins),
            "n_units": int(units["unit"].nunique())}


# ---------------------------------------------------------------------------
# Design comparison
# ---------------------------------------------------------------------------


def naive_total_effect(frame: pd.DataFrame, *, n_days: float, outcome: str = "completed", n_boot: int = 500,
                       seed: int = 0, alpha: float = 0.05) -> dict:
    """Difference in rate (on - off) scaled to trips per day if every session were treated vs none.

    This is what one would report as the total effect from a session-level comparison. It
    ignores interference between on and off units, so it is biased against the GTE.
    """
    f = frame.assign(bin=pd.Categorical(["all"] * len(frame)))
    units = _unit_sums(f, outcome)
    boot = _bootstrap_effects(units, ["all"], n_boot, seed)[:, 0]
    on = units["arm"] == 1
    effect = units.loc[on, "y"].sum() / units.loc[on, "n"].sum() - units.loc[~on, "y"].sum() / units.loc[~on, "n"].sum()
    scale = len(frame) / n_days
    lo, hi = np.nanquantile(boot, [alpha / 2, 1 - alpha / 2])
    return {"per_day": float(effect * scale), "ci_lo": float(lo * scale), "ci_hi": float(hi * scale),
            "n_units": int(units["unit"].nunique())}
