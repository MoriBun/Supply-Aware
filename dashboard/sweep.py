"""theta sweep launched from the dashboard: the jobs of ``sim.runner.sweep_theta``, run with live progress.

The search for theta* is a sweep experiment: the threshold policy
is run for every theta of a grid, on the same seeds and under the same budget B, and
theta* = argmax_theta N(pi_theta). This module builds exactly the jobs that
:func:`sim.runner.sweep_theta` builds (B from the all_on pilot, kappa-auto per theta,
``seeds_for``), but runs them through its own pool so that every finished seed can be
reported to the page while the sweep is going. The tables written are the ones of
mode ``sweep_theta`` (docs/schema.md), so the sweep also shows up on the results page.

The theta* *set* (which thetas are statistically as good as the best one, paired by
seed) comes from :func:`analysis.metrics.theta_star_set`, the same
function the notebooks use.
"""

from __future__ import annotations

import dataclasses
import math
import multiprocessing
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.metrics import theta_star_interval, theta_star_set
from sim.config import Config
from sim.engine import RunResult
from sim.logger import write_metadata, write_results
from sim.runner import (
    DEFAULT_ENGINE, Job, KappaAuto, budget_for, execute_job, metadata_table, policy_results_table, resolve_kappas,
    seeds_for, theta_sweep_table, with_policy,
)

MODE = "sweep_theta"
NAN = float("nan")

ROW_FIELDS = ("theta", "seed", "N_completed", "V_profit_vnd", "voucher_spent_vnd", "budget_B_vnd", "share_cells_off",
              "mean_pickup_eta_min", "n_sessions", "n_requests", "n_abandoned", "n_cancelled", "n_switches_per_cell_day",
              "kappa", "runtime_s")


def _f(x) -> float | None:
    x = float(x)
    return x if math.isfinite(x) else None


def sweep_config(cfg: Config, n_procs: int | None) -> Config:
    """The threshold policy with the pool size the sweep asked for (``runner.n_procs``)."""
    base = with_policy(cfg, "threshold")
    if n_procs is not None:
        base = dataclasses.replace(base, runner=dataclasses.replace(base.runner, n_procs=int(n_procs)))
    return base


def build_jobs(base: Config, grid: list[float], n_seeds: int, budget: float | None,
               kappas: list[KappaAuto]) -> list[Job]:
    """One job per (theta, seed), in theta-major order, as ``runner.sweep_theta`` builds them."""
    jobs: list[Job] = []
    for theta, ka in zip(grid, kappas, strict=True):
        jobs += [Job(cfg=base, seed=s, theta=float(theta), kappa=ka.kappa, budget_vnd=budget,
                     enforce_budget=base.budget.enforce, kappa_pilots=ka.n_pilots)
                 for s in seeds_for(base, n_seeds)]
    return jobs


def _indexed_job(args) -> tuple[int, RunResult]:
    i, engine, job = args
    return i, execute_job(engine, job)


ResultFn = Callable[[int, Job, RunResult], None]


def run_jobs_live(jobs: list[Job], *, n_procs: int, on_result: ResultFn,
                  engine: str = DEFAULT_ENGINE) -> list[RunResult]:
    """Run the jobs ``n_procs`` at a time and call ``on_result`` as each one finishes; results in job order."""
    results: list[RunResult | None] = [None] * len(jobs)
    n_procs = max(1, min(int(n_procs), len(jobs)))
    if n_procs == 1:
        for i, job in enumerate(jobs):
            results[i] = execute_job(engine, job)
            on_result(i, job, results[i])
    else:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(n_procs) as pool:
            for i, res in pool.imap_unordered(_indexed_job, [(i, engine, job) for i, job in enumerate(jobs)]):
                results[i] = res
                on_result(i, jobs[i], res)
    return results  # type: ignore[return-value]


def row_of(job: Job, res: RunResult) -> dict:
    return {
        "theta": float(job.theta), "seed": int(job.seed), "N_completed": int(res.N_completed),
        "V_profit_vnd": _f(res.V_profit_vnd), "voucher_spent_vnd": _f(res.voucher_spent_vnd),
        "budget_B_vnd": _f(res.budget_B_vnd), "share_cells_off": _f(res.share_cells_off),
        "mean_pickup_eta_min": _f(res.mean_pickup_eta_min), "n_sessions": int(res.n_sessions),
        "n_requests": int(res.n_requests), "n_abandoned": int(res.n_abandoned), "n_cancelled": int(res.n_cancelled),
        "n_switches_per_cell_day": _f(res.n_switches_per_cell_day),
        "kappa": None if job.kappa is None else _f(job.kappa), "runtime_s": _f(res.runtime_s),
    }


def aggregate(rows: list[dict], grid: list[float], *, alpha: float = 0.05) -> dict:
    """Per-theta means and SEs over the finished seeds, the argmax, and the theta* set when it can be computed.

    The set needs the same seeds for every theta (paired comparison), so while the
    sweep is running it is computed on the seeds that every theta has finished, and
    only once there are at least two of them.
    """
    grid = [float(t) for t in grid]
    if not rows:
        return {"per_theta": [{"theta": t, "n": 0} for t in grid], "argmax_theta": None, "star": None}
    df = pd.DataFrame(rows)
    per = []
    for t in grid:
        g = df[df["theta"] == t]
        n = int(len(g))
        rec = {"theta": t, "n": n}
        if n:
            for col, name in (("N_completed", "N"), ("V_profit_vnd", "V"), ("voucher_spent_vnd", "spent"),
                              ("share_cells_off", "share_off"), ("mean_pickup_eta_min", "eta")):
                v = g[col].astype(float)
                rec[f"{name}_mean"] = _f(v.mean())
                rec[f"{name}_se"] = _f(v.std(ddof=1) / math.sqrt(n)) if n > 1 else None
            rec["kappa"] = _f(g["kappa"].iloc[0]) if g["kappa"].notna().any() else None
        per.append(rec)
    done = [p for p in per if p["n"]]
    argmax = max(done, key=lambda p: (p["N_mean"], -p["theta"]))["theta"] if done else None
    star = None
    complete = df[df["theta"].isin(grid)]
    seeds_all = None
    for t in grid:
        s = set(complete.loc[complete["theta"] == t, "seed"])
        seeds_all = s if seeds_all is None else seeds_all & s
    if seeds_all and len(seeds_all) >= 2 and all(p["n"] for p in per):
        paired = complete[complete["seed"].isin(seeds_all)]
        st = theta_star_set(paired, alpha=alpha)
        best, lo, hi = theta_star_interval(st)
        star = {"best": best, "lo": lo, "hi": hi, "n_seeds": int(len(seeds_all)), "alpha": alpha,
                "rows": [{"theta": _f(r.theta), "gap_to_best": _f(r.gap_to_best), "gap_se": _f(r.gap_se),
                          "gap_lo": _f(r.gap_lo), "in_set": bool(r.in_set)} for r in st.itertuples()]}
    gain = None
    if argmax is not None and 0.0 in grid and argmax != 0.0:
        a = complete[complete["theta"] == argmax].set_index("seed")["N_completed"].astype(float)
        z = complete[complete["theta"] == 0.0].set_index("seed")["N_completed"].astype(float)
        d = (a - z).dropna()
        if len(d):
            gain = {"vs_theta": 0.0, "dN": _f(d.mean()), "dN_se": _f(d.std(ddof=1) / math.sqrt(len(d))) if len(d) > 1 else None,
                    "n_seeds": int(len(d))}
    return {"per_theta": per, "argmax_theta": argmax, "star": star, "gain_vs_zero": gain,
            "n_rows": int(len(df)), "budget_B_vnd": _f(df["budget_B_vnd"].dropna().iloc[0]) if df["budget_B_vnd"].notna().any() else None}


def write_tables(out_dir: Path, jobs: list[Job], results: list[RunResult]) -> pd.DataFrame:
    """``results/policy_results``, ``results/theta_sweep`` and ``meta/run_metadata`` of mode ``sweep_theta``."""
    table = policy_results_table(MODE, jobs, results)
    write_results(out_dir, "policy_results", table)
    write_metadata(out_dir, metadata_table(MODE, jobs, results))
    write_results(out_dir, "theta_sweep", theta_sweep_table(table))
    return table


def default_n_procs() -> int:
    return os.cpu_count() or 1
