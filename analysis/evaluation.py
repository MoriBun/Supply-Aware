"""Policy-evaluation helpers: read the outputs of policy tables and theta sweeps and report.

- :func:`gap_closed`: share of the gap from ``all_on_B`` to the oracle that a policy closes, paired by seed,
  with a seed-bootstrap interval; the oracle is only the upper bound of per-rider policies, so
  cell-tier policies can exceed 100%.
- :func:`split_seed_regret`: pick the best theta on one half of the seeds, measure the regret of that choice on
  the other half (:func:`analysis.metrics.sweep_regret`), both ways, and the share of the cell
  tier's maximum gain (over theta = 0) that the chosen theta recovers on the held-out half.
- :func:`switch_table`: switches per cell per day of each policy row and the minimum hold time that would cap them.
- :func:`stack_tables`: one frame out of several ``policy_table`` directories (robustness grid,
  sensitivity variants) with the directory's settings as columns.
- :func:`shock_split_table`: second condition of hypothesis H2 (in W1 the supply-tension policy beats the hour-off
  rule, 95% CI excluding 0, with the gain concentrated in slots with shocks): split ``N(a) - N(b)`` of a
  full-log policy table (``--log-level full``) into the slots that overlap a shock of ``meta/shocks`` (rain,
  event) and the quiet slots, per seed; :func:`shock_split_summary` averages over seeds.

CLI::

    python -m analysis.evaluation gap    --table runs/s10/w0/policy_table
    python -m analysis.evaluation sweep  --sweep runs/s10/w0/sweep_ring1_dr --theta-hat 2.3
    python -m analysis.evaluation switch --table runs/s10/w0/policy_table
    python -m analysis.evaluation stack  --dirs runs/s10/w1/robust_* [--labels tier_dr_kvnd pi_hourB_ring1]
    python -m analysis.evaluation shocks --table runs/s10/w1/h2_slots_full --a pi_hourB_ring1 --b hour_off_dr
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from analysis import metrics

REFERENCE, ORACLE = "all_on_B", "oracle"


def _load_runs(table_dir: Path) -> pd.DataFrame:
    """Runs of a policy table with their ``label`` (joined from ``results/policy_labels``)."""
    from analysis.qini_vs_value import policy_runs

    return policy_runs(Path(table_dir))


def _wide(runs: pd.DataFrame, metric: str = "N_completed") -> pd.DataFrame:
    return runs.pivot(index="seed", columns="label", values=metric).astype(float)


# ---------------------------------------------------------------------------
# Gap all_on -> oracle closed
# ---------------------------------------------------------------------------


def gap_closed(runs: pd.DataFrame, label: str, *, reference: str = REFERENCE, oracle: str = ORACLE,
               metric: str = "N_completed", n_boot: int = 2000, seed: int = 0, alpha: float = 0.05) -> dict:
    """``(N(label) - N(ref)) / (N(oracle) - N(ref))`` from seed means, with a seed-bootstrap interval."""
    w = _wide(runs, metric)
    d_lab, d_or = (w[label] - w[reference]).to_numpy(), (w[oracle] - w[reference]).to_numpy()
    ok = np.isfinite(d_lab) & np.isfinite(d_or)
    d_lab, d_or = d_lab[ok], d_or[ok]
    n = len(d_lab)
    share = float(d_lab.mean() / d_or.mean()) if d_or.mean() != 0 else math.nan
    gen = np.random.default_rng(seed)
    idx = gen.integers(0, n, size=(n_boot, n))
    with np.errstate(divide="ignore", invalid="ignore"):
        draws = d_lab[idx].mean(axis=1) / d_or[idx].mean(axis=1)
    lo, hi = np.nanquantile(draws, [alpha / 2, 1 - alpha / 2])
    return {"label": label, "n_seeds": n, "dN_vs_ref": float(d_lab.mean()), "dN_oracle_vs_ref": float(d_or.mean()),
            "gap_closed": share, "ci_lo": float(lo), "ci_hi": float(hi)}


def gap_table(runs: pd.DataFrame, labels: Sequence[str] | None = None, **kw) -> pd.DataFrame:
    labels = [l for l in dict.fromkeys(runs["label"]) if l not in (REFERENCE,)] if labels is None else list(labels)
    return pd.DataFrame([gap_closed(runs, l, **kw) for l in labels])


# ---------------------------------------------------------------------------
# Split-seed regret and recovered gain
# ---------------------------------------------------------------------------


def _sweep_wide(policy_results: pd.DataFrame, metric: str = "N_completed") -> pd.DataFrame:
    return policy_results.pivot(index="seed", columns="theta", values=metric).astype(float)


def gain_recovered(policy_results: pd.DataFrame, theta_hat: float, *, metric: str = "N_completed") -> dict:
    """Share of the cell tier's maximum gain over theta = 0 that ``theta_hat`` recovers: ``(N(θ̂) − N(0)) / (max_θ N(θ) − N(0))``."""
    w = _sweep_wide(policy_results, metric)
    thetas = w.columns.to_numpy(dtype=float)
    if 0.0 not in thetas:
        raise ValueError("the sweep must contain theta = 0")
    means = w.mean()
    best = float(means.idxmax())
    at_hat = float(np.mean([np.interp(theta_hat, thetas, row) for row in w.to_numpy()]))
    base, top = float(means[0.0]), float(means[best])
    return {"theta_hat": float(theta_hat), "theta_best": best, "N_0": base, "N_best": top, "N_theta_hat": at_hat,
            "max_gain": top - base, "gain_recovered": (at_hat - base) / (top - base) if top > base else math.nan}


def split_seed_regret(policy_results: pd.DataFrame, *, metric: str = "N_completed", alpha: float = 0.05) -> pd.DataFrame:
    """Best theta chosen on half A (even-ranked seeds), regret and recovered gain measured on half B, and the
    other way round. The regret of a theta chosen on the same seeds would be optimistic (winner's curse)."""
    seeds = np.sort(policy_results["seed"].unique())
    halves = {"A": seeds[::2], "B": seeds[1::2]}
    rows = []
    for pick, test in (("A", "B"), ("B", "A")):
        chosen_on = policy_results[policy_results["seed"].isin(halves[pick])]
        held = policy_results[policy_results["seed"].isin(halves[test])]
        best_pick = float(_sweep_wide(chosen_on, metric).mean().idxmax())
        r = metrics.sweep_regret(held, best_pick, metric=metric, alpha=alpha)
        g = gain_recovered(held, best_pick, metric=metric)
        rows.append({"chosen_on": pick, "tested_on": test, "theta_chosen": best_pick, "theta_best_heldout": r["theta_best"],
                     "regret": r["regret"], "regret_se": r["se"], "regret_lo": r["lo"], "regret_hi": r["hi"],
                     "regret_relative": r["relative"], "gain_recovered": g["gain_recovered"], "max_gain_heldout": g["max_gain"],
                     "n_seeds_heldout": r["n_seeds"]})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Switches per cell per day and a hold time
# ---------------------------------------------------------------------------


def switch_table(runs: pd.DataFrame, *, targets: Sequence[float] = (2.0, 4.0, 8.0)) -> pd.DataFrame:
    """Per label: mean / median / p90 of ``n_switches_per_cell_day`` over seeds and the minimum hold time (minutes)
    that would cap a cell at each target number of switches per day (``1440 / target``; only rows that switch)."""
    rows = []
    for label, g in runs.groupby("label", sort=False):
        s = g["n_switches_per_cell_day"].astype(float)
        if not np.isfinite(s).any():
            continue
        row = {"label": label, "theta": float(g["theta"].iloc[0]), "switches_mean": float(s.mean()),
               "switches_median": float(s.median()), "switches_p90": float(s.quantile(0.9)),
               "share_cells_off": float(g["share_cells_off"].mean())}
        for t in targets:
            row[f"hold_min_for_{t:g}_per_day"] = 1440.0 / t if s.mean() > t else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Stack several policy tables
# ---------------------------------------------------------------------------


def _settings_of(table_dir: Path) -> dict:
    """Settings that differ from the base config, from the stored metadata of the reference row."""
    import yaml

    meta = pd.read_parquet(Path(table_dir) / "meta" / "run_metadata.parquet").iloc[0]
    cfg = yaml.safe_load(meta["config_yaml"])
    return {"dir": Path(table_dir).name, "budget_fraction": cfg["budget"]["fraction"], "fleet_size": cfg["supply"]["fleet_size"],
            "world_seed": cfg["meta"]["world_seed"], "dest_decay_rings": cfg["demand"]["dest_decay_rings"],
            "cancel_base_per_min": cfg["cancel"]["base_per_min"], "hour_profile_peak": max(cfg["demand"]["hour_profile"]),
            "shocks": cfg["shocks"]["enabled"]}


def stack_tables(table_dirs: Sequence[Path], *, labels: Sequence[str] | None = None, reference: str = REFERENCE) -> pd.DataFrame:
    """One row per (table, label): N mean/SE, ``dN_vs_ref`` paired by seed, plus the table's settings."""
    from analysis.policy_table import summarize

    rows = []
    for d in table_dirs:
        runs = _load_runs(d)
        summary = summarize(runs, reference=reference)
        if labels is not None:
            summary = summary[summary["label"].isin(labels)]
        settings = _settings_of(d)
        for r in summary.to_dict("records"):
            rows.append({**settings, **{k: r[k] for k in ("label", "theta", "N_mean", "N_se", "dN_vs_ref", "dN_vs_ref_se",
                                                            "spent_mean", "share_cells_off", "n_seeds")}})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# H2: is the gain of the tension policy on W1 concentrated in the slots with a shock?
# ---------------------------------------------------------------------------

SHOCK_KINDS = ("rain", "event")      # slot-level shocks; day_demand and attendance cover whole days
COMPLETED = "Completed"


def shock_slot_mask(shocks: pd.DataFrame, slot_s: float, n_slots: int, kinds: Sequence[str] = SHOCK_KINDS) -> np.ndarray:
    """``True`` for every slot (``floor(t / slot_s)``) that overlaps a ``meta/shocks`` window of one of ``kinds``."""
    mask = np.zeros(int(n_slots), dtype=bool)
    for r in shocks[shocks["kind"].isin(list(kinds))].itertuples(index=False):
        lo, hi = int(math.floor(float(r.start_s) / slot_s)), int(math.ceil(float(r.end_s) / slot_s))
        mask[max(lo, 0):max(min(hi, n_slots), 0)] = True
    return mask


def completed_by_slot(orders: pd.DataFrame, slot_s: float, n_slots: int) -> np.ndarray:
    """Completed orders in the evaluation window, counted at their request slot (sum = ``N_completed``)."""
    o = orders[(orders["status"] == COMPLETED) & orders["in_window"].to_numpy().astype(bool)]
    slot = np.floor(o["request_time_s"].to_numpy(dtype=np.float64) / float(slot_s)).astype(np.int64)
    slot = slot[(slot >= 0) & (slot < n_slots)]
    return np.bincount(slot, minlength=int(n_slots)).astype(np.int64)


def gain_by_shock_slots(delta_by_slot: np.ndarray, mask: np.ndarray) -> dict:
    """One seed: the per-slot difference ``N(a) - N(b)`` summed over shock slots and quiet slots, with the
    share of the gain and of the slots that are shock slots and the gain per slot of each group."""
    d = np.asarray(delta_by_slot, dtype=float)
    m = np.asarray(mask, dtype=bool)
    n_shock, n_quiet = int(m.sum()), int((~m).sum())
    g_shock, g_quiet = float(d[m].sum()), float(d[~m].sum())
    total = g_shock + g_quiet
    return {"n_slots_shock": n_shock, "n_slots_quiet": n_quiet, "share_slots_shock": n_shock / max(len(m), 1),
            "gain_shock": g_shock, "gain_quiet": g_quiet, "gain_total": total,
            "share_gain_shock": g_shock / total if total != 0 else math.nan,
            "gain_per_shock_slot": g_shock / n_shock if n_shock else math.nan,
            "gain_per_quiet_slot": g_quiet / n_quiet if n_quiet else math.nan}


def _slot_s_of(table_dir: Path) -> float:
    import yaml

    meta = pd.read_parquet(Path(table_dir) / "meta" / "run_metadata.parquet").iloc[0]
    return 60.0 * float(yaml.safe_load(meta["config_yaml"])["time"]["slot_min"])


def shock_split_table(table_dir: Path, a: str, b: str, *, kinds: Sequence[str] = SHOCK_KINDS) -> pd.DataFrame:
    """Per seed of a full-log policy table: :func:`gain_by_shock_slots` of ``N(a) - N(b)`` by request slot.

    Both runs of a seed share the shock schedule (CRN: the SHOCK stream does not depend on the policy); the
    function checks it. ``N`` of each run is the sum of its completed-by-slot vector, so the per-seed
    ``gain_total`` equals the paired difference of ``results/policy_results``.
    """
    table_dir = Path(table_dir)
    runs = _load_runs(table_dir)
    slot_s = _slot_s_of(table_dir)
    rows = []
    for seed in sorted(runs["seed"].unique()):
        ra = runs[(runs["label"] == a) & (runs["seed"] == seed)]
        rb = runs[(runs["label"] == b) & (runs["seed"] == seed)]
        if len(ra) != 1 or len(rb) != 1:
            raise ValueError(f"seed {seed}: expected one run of {a!r} and one of {b!r}")
        da, db = table_dir / "runs" / ra["run_id"].iloc[0], table_dir / "runs" / rb["run_id"].iloc[0]
        cols = ["request_time_s", "status", "in_window"]
        oa, ob = (pd.read_parquet(d / "observed" / "orders.parquet", columns=cols) for d in (da, db))
        sa, sb = (pd.read_parquet(d / "meta" / "shocks.parquet") for d in (da, db))
        keep = ["day", "kind", "cell", "start_s", "end_s"]
        if not sa[keep].reset_index(drop=True).equals(sb[keep].reset_index(drop=True)):
            raise ValueError(f"seed {seed}: the two runs have different shock schedules")
        n_slots = int(math.ceil(max(float(oa["request_time_s"].max()), float(ob["request_time_s"].max())) / slot_s)) + 1
        na, nb = completed_by_slot(oa, slot_s, n_slots), completed_by_slot(ob, slot_s, n_slots)
        row = {"seed": int(seed), "N_a": int(na.sum()), "N_b": int(nb.sum())}
        rows.append({**row, **gain_by_shock_slots(na - nb, shock_slot_mask(sa, slot_s, n_slots, kinds))})
    return pd.DataFrame(rows)


def shock_split_summary(per_seed: pd.DataFrame) -> dict:
    """Means and SEs over seeds; the share of the gain in shock slots is the ratio of means; ``rate_diff`` is the
    paired mean of (gain per shock slot - gain per quiet slot) over the seeds that have shock slots."""
    def mean_se(x):
        x = np.asarray(x, dtype=float)
        x = x[np.isfinite(x)]
        se = float(x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 1 else math.nan
        return (float(x.mean()) if len(x) else math.nan), se, len(x)

    out = {"n_seeds": int(len(per_seed)), "n_seeds_with_shock": int((per_seed["n_slots_shock"] > 0).sum())}
    for c in ("gain_total", "gain_shock", "gain_quiet", "share_slots_shock", "gain_per_shock_slot", "gain_per_quiet_slot"):
        out[c], out[c + "_se"], _ = mean_se(per_seed[c])
    out["share_gain_shock"] = out["gain_shock"] / out["gain_total"] if out["gain_total"] else math.nan
    diff = (per_seed["gain_per_shock_slot"] - per_seed["gain_per_quiet_slot"]).to_numpy(dtype=float)
    out["rate_diff"], out["rate_diff_se"], n = mean_se(diff)
    if n > 1:
        crit = metrics.t_quantile(0.975, n - 1)
        out["rate_diff_lo"], out["rate_diff_hi"] = out["rate_diff"] - crit * out["rate_diff_se"], out["rate_diff"] + crit * out["rate_diff_se"]
    else:
        out["rate_diff_lo"] = out["rate_diff_hi"] = math.nan
    return out


def markdown(df: pd.DataFrame, cols: Sequence[str], digits: int = 1) -> str:
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        d = r._asdict()
        cells = []
        for c in cols:
            v = d[c]
            cells.append(f"{v:,.{digits}f}" if isinstance(v, (float, np.floating)) and np.isfinite(v) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Policy-evaluation helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gap"); g.add_argument("--table", type=Path, required=True); g.add_argument("--out", type=Path)
    s = sub.add_parser("sweep"); s.add_argument("--sweep", type=Path, required=True); s.add_argument("--theta-hat", type=float, nargs="*", default=[])
    s.add_argument("--out", type=Path)
    w = sub.add_parser("switch"); w.add_argument("--table", type=Path, required=True); w.add_argument("--out", type=Path)
    k = sub.add_parser("stack"); k.add_argument("--dirs", nargs="+", required=True); k.add_argument("--labels", nargs="*", default=None)
    k.add_argument("--out", type=Path)
    h = sub.add_parser("shocks"); h.add_argument("--table", type=Path, required=True); h.add_argument("--a", required=True)
    h.add_argument("--b", required=True); h.add_argument("--kinds", nargs="*", default=list(SHOCK_KINDS)); h.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "gap":
        runs = _load_runs(args.table)
        table = gap_table(runs)
        print(markdown(table, ["label", "n_seeds", "dN_vs_ref", "dN_oracle_vs_ref", "gap_closed", "ci_lo", "ci_hi"], 3))
    elif args.cmd == "sweep":
        pr = pd.read_parquet(Path(args.sweep) / "results" / "policy_results.parquet")
        by_theta = metrics.sweep_by_theta(pr)
        print(by_theta.round(3).to_string())
        split = split_seed_regret(pr)
        print("\nsplit-seed:\n" + split.round(3).to_string(index=False))
        fixed = pd.DataFrame([{**metrics.sweep_regret(pr, t), **{"gain_recovered": gain_recovered(pr, t)["gain_recovered"]}}
                              for t in args.theta_hat]) if args.theta_hat else None
        if fixed is not None:
            print("\nfixed theta_hat:\n" + fixed.round(3).to_string(index=False))
        table = split
        if args.out is not None:
            args.out.mkdir(parents=True, exist_ok=True)
            by_theta.to_parquet(args.out / "sweep_by_theta.parquet")
            if fixed is not None:
                fixed.to_parquet(args.out / "regret_fixed_theta.parquet", index=False)
    elif args.cmd == "switch":
        table = switch_table(_load_runs(args.table))
        print(markdown(table, list(table.columns), 2))
    elif args.cmd == "shocks":
        table = shock_split_table(args.table, args.a, args.b, kinds=args.kinds)
        print(markdown(table, list(table.columns), 2))
        summary = shock_split_summary(table)
        print(f"\nkinds = {args.kinds}: " + json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in summary.items()}))
        if args.out is not None:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / f"shocks_{'_'.join(args.kinds)}_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    else:
        dirs = [Path(p) for pattern in args.dirs for p in sorted(glob.glob(pattern))] or [Path(p) for p in args.dirs]
        table = stack_tables(dirs, labels=args.labels)
        print(markdown(table, ["dir", "budget_fraction", "fleet_size", "world_seed", "label", "theta", "N_mean", "N_se",
                               "dN_vs_ref", "dN_vs_ref_se", "share_cells_off"], 1))
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        table.to_parquet(args.out / f"{args.cmd}.parquet", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
