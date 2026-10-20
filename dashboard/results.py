"""Read finished experiments under ``runs/`` (docs/schema.md tables) for the results page.

Only ``results/`` and ``meta/`` tables are read here; the hidden tables are never
touched (docs/schema.md). Directories are discovered by their files, so any new
sweep written by ``python -m sim run`` shows up without configuration.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from sim.runner import gte_summary, summarize_throughput


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def discover_sweeps(runs_dir: Path) -> list[dict]:
    """Every ``results/theta_sweep.parquet``: thetas, N and V with standard errors, the argmax, B."""
    out = []
    for path in sorted(runs_dir.glob("**/results/theta_sweep.parquet")):
        run_dir = path.parents[1]
        try:
            sweep = pd.read_parquet(path).sort_values("theta")
        except Exception:  # noqa: BLE001 - a half-written file must not break the page
            continue
        budget = None
        n_runs = None
        pr = run_dir / "results" / "policy_results.parquet"
        if pr.exists():
            try:
                res = pd.read_parquet(pr, columns=["budget_B_vnd", "N_completed"])
                budget = _f(res["budget_B_vnd"].dropna().iloc[0]) if res["budget_B_vnd"].notna().any() else None
                n_runs = int(len(res))
            except Exception:  # noqa: BLE001
                pass
        argmax = sweep.loc[sweep["is_argmax"], "theta"]
        out.append({
            "key": _rel(run_dir, runs_dir), "name": run_dir.name, "group": _rel(run_dir.parent, runs_dir),
            "theta": [_f(v) for v in sweep["theta"]], "N_mean": [_f(v) for v in sweep["N_mean"]],
            "N_se": [_f(v) for v in sweep["N_se"]], "V_mean": [_f(v) for v in sweep["V_mean"]],
            "V_se": [_f(v) for v in sweep["V_se"]], "spent_mean": [_f(v) for v in sweep["spent_mean"]],
            "n_seeds": int(sweep["n_seeds"].iloc[0]) if len(sweep) else 0,
            "argmax_theta": _f(argmax.iloc[0]) if len(argmax) else None, "budget_B_vnd": budget, "n_runs": n_runs,
        })
    return out


def policy_tables(runs_dir: Path) -> list[dict]:
    """Every ``results/policy_table.parquet`` (analysis.policy_table): N, V, spend per policy under one B."""
    out = []
    for path in sorted(runs_dir.glob("**/results/policy_table.parquet")):
        try:
            t = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
        rows = []
        for _, r in t.iterrows():
            rows.append({k: (_f(v) if isinstance(v, (float, np.floating)) else (int(v) if isinstance(v, (np.integer,)) else v))
                         for k, v in r.items()})
        out.append({"key": _rel(path.parents[1], runs_dir), "rows": rows})
    return out


def throughput_curves(runs_dir: Path) -> list[dict]:
    """Every ``results/throughput_curve.parquet`` averaged over seeds per demand level (test A1)."""
    out = []
    for path in sorted(runs_dir.glob("**/results/throughput_curve.parquet")):
        try:
            t = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
        s = summarize_throughput(t)
        out.append({"key": _rel(path.parents[1], runs_dir), "fleet_size": int(t["fleet_size"].iloc[0]) if len(t) else None,
                    "n_seeds": int(t.groupby("demand_scale").size().max()) if len(t) else 0,
                    "rows": [{k: _f(v) for k, v in r.items()} for _, r in s.iterrows()]})
    return out


def gte_tables(runs_dir: Path) -> list[dict]:
    """policy_results tables holding both all_on and all_off without budget: GTE paired by seed."""
    out = []
    for path in sorted(runs_dir.glob("**/results/policy_results.parquet")):
        try:
            t = pd.read_parquet(path, columns=["policy", "seed", "N_completed", "budget_B_vnd", "V_profit_vnd",
                                               "voucher_spent_vnd"])
        except Exception:  # noqa: BLE001
            continue
        # GTE is the unrestricted all-on / all-off contrast. Budgeted policy
        # comparisons may live in the same file and must not enter this estimand.
        t = t[t["budget_B_vnd"].isna() & t["policy"].isin(("all_on", "all_off"))]
        pol = set(t["policy"].unique())
        if not {"all_on", "all_off"} <= pol:
            continue
        if t.duplicated(["policy", "seed"]).any():
            continue  # Multiple scenarios per seed: no unambiguous paired contrast.
        paired = set(t.loc[t["policy"] == "all_on", "seed"]) & set(t.loc[t["policy"] == "all_off", "seed"])
        if not paired:
            continue
        t = t[t["seed"].isin(paired)]
        g = gte_summary(t)
        out.append({"key": _rel(path.parents[1], runs_dir), **{k: _f(v) for k, v in g.items()}})
    return out


EVAL_COLUMNS = {"policy", "theta", "N_completed", "V_profit_vnd", "voucher_spent_vnd", "budget_B_vnd", "share_cells_off",
                "mean_pickup_eta_min"}


def evaluate_tables(runs_dir: Path, limit: int = 60) -> list[dict]:
    """Per policy_results table: policies, N mean ± se over seeds (every mode, for the comparison table)."""
    out = []
    for path in sorted(runs_dir.glob("**/results/policy_results.parquet")):
        if "dashboard" in path.parts:
            continue
        try:
            t = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
        if not EVAL_COLUMNS <= set(t.columns):
            continue  # data set written before the VND columns of B9 (decisions H-33): not comparable, skip
        rows = []
        for (policy, theta), g in t.groupby(["policy", t["theta"].fillna(-1.0)], sort=True):
            n = len(g)
            rows.append({
                "policy": policy, "theta": None if theta < 0 else _f(theta), "n_seeds": int(n),
                "N_mean": _f(g["N_completed"].mean()), "N_se": _f(g["N_completed"].std(ddof=1) / math.sqrt(n)) if n > 1 else None,
                "V_mean": _f(g["V_profit_vnd"].mean()), "spent_mean": _f(g["voucher_spent_vnd"].mean()),
                "budget_B_vnd": _f(g["budget_B_vnd"].dropna().iloc[0]) if g["budget_B_vnd"].notna().any() else None,
                "share_cells_off": _f(g["share_cells_off"].mean()), "mean_pickup_eta_min": _f(g["mean_pickup_eta_min"].mean()),
            })
        out.append({"key": _rel(path.parents[1], runs_dir), "rows": rows})
        if len(out) >= limit:
            break
    return out

