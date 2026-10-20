"""M13 Runner: run ids, many seeds in parallel, results tables, run modes.

Seeds run in separate processes started with ``spawn`` (the only start method on
Windows), so a job carries picklable data only: the ``Config`` and scalars. The
worker rebuilds the world from ``world_seed``, builds the policy from
``cfg.policy.name`` (with the job's theta / kappa) and imports the engine from
its ``"module:function"`` string (``tests.fakes:fake_run`` in tests). Results come
back in job order.

Budget B (VND per budget period) comes from :func:`calibrate_budget`: an all_on
pilot without budget on seed ``run_seed + pilot_seed_offset``, unless
``budget.mode = fixed``. kappa-auto is a
fixed point: a threshold pilot without budget at ``kappa = -inf`` gives the
smallest score whose expected spend fits ``B x n_periods``; the pilot is rerun at
that kappa, and kappa is raised again, until the pilot's spend fits. Every theta
uses the same pilot seed, and the pilots of all thetas run in parallel, round by
round.
"""

from __future__ import annotations

import dataclasses
import math
import multiprocessing
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from sim.budget import resolve_budget_vnd
from sim.config import Config, config_hash, to_dict
from sim.engine import RunResult
from sim.logger import git_sha, write_metadata, write_results, write_run
from sim.policies import hidden_view, make_policy
from sim.policies.scores import import_callable
from sim.population import build_world
from sim.rng import Rng
from sim.state import Clock

NAN = float("nan")
DEFAULT_ENGINE = "sim.engine:run"
MODES = ("generate", "evaluate", "sweep_theta", "gte", "calibrate_budget", "throughput_curve")


@dataclass(frozen=True)
class Job:
    """One engine run. ``cfg.meta.run_seed`` is replaced by ``seed`` in the worker."""

    cfg: Config
    seed: int
    theta: float = NAN                 # ThresholdPolicy theta; NaN = not applicable / config value
    kappa: float | None = None         # ThresholdPolicy kappa; None = config value (-inf when "auto")
    budget_vnd: float | None = None
    enforce_budget: bool | None = None
    log_level: str = "minimal"
    profile: bool = False
    kappa_pilots: int = 0              # pilot runs kappa-auto used to set ``kappa`` (run_metadata only)
    budget_pilot_seed: int = -1        # seed of the all_on pilot that set B; -1 = no pilot (run_metadata)
    budget_pilot_spent_vnd: float = NAN  # voucher spend of that pilot on the base window; NaN = no pilot

    @property
    def policy_name(self) -> str:
        return self.cfg.policy.name


# ---------------------------------------------------------------------------
# Ids, seeds, config helpers
# ---------------------------------------------------------------------------


def run_id(mode: str, cfg: Config, policy: str, theta: float, seed: int) -> str:
    """``<mode>-<config_hash>-<policy>-<theta>-<seed>`` (docs/schema.md, meta/run_metadata)."""
    theta_text = "nan" if theta is None or math.isnan(theta) else f"{theta:g}"
    return f"{mode}-{config_hash(cfg)}-{policy}-{theta_text}-{int(seed)}"


def seeds_for(cfg: Config, n_seeds: int) -> list[int]:
    """``run_seed, run_seed + 1, ...`` (config/default.yaml: the runner adds the seed index)."""
    if n_seeds < 1:
        raise ValueError("n_seeds must be >= 1")
    return [cfg.meta.run_seed + i for i in range(int(n_seeds))]


def with_seed(cfg: Config, seed: int) -> Config:
    return dataclasses.replace(cfg, meta=dataclasses.replace(cfg.meta, run_seed=int(seed)))


def with_policy(cfg: Config, name: str) -> Config:
    return dataclasses.replace(cfg, policy=dataclasses.replace(cfg.policy, name=name))


def pilot_seed(cfg: Config) -> int:
    """Seed of every pilot run, for B and for kappa-auto at any theta: ``run_seed + budget.pilot_seed_offset``."""
    return cfg.meta.run_seed + cfg.budget.pilot_seed_offset


# ---------------------------------------------------------------------------
# Running jobs
# ---------------------------------------------------------------------------


def execute_job(engine_spec: str, job: Job) -> RunResult:
    """Run one job in the current process (also the worker body of the pool)."""
    engine = import_callable(engine_spec)
    cfg = with_seed(job.cfg, job.seed)
    rng = Rng.from_config(cfg)
    world = build_world(cfg, rng)
    theta = None if math.isnan(job.theta) else float(job.theta)
    policy = make_policy(cfg, world, rng=rng, theta=theta, kappa=job.kappa)
    return engine(cfg, world, policy, rng, log_level=job.log_level, profile=job.profile,
                  budget_vnd=job.budget_vnd, enforce_budget=job.enforce_budget)


def run_jobs(jobs: list[Job], *, engine: str = DEFAULT_ENGINE, n_procs: int | None = None) -> list[RunResult]:
    """Run jobs, ``n_procs`` at a time (None = ``runner.n_procs`` of the first job's config, then CPU count)."""
    if not jobs:
        return []
    if n_procs is None:
        n_procs = jobs[0].cfg.runner.n_procs
    if n_procs is None:
        n_procs = os.cpu_count() or 1
    n_procs = max(1, min(int(n_procs), len(jobs)))
    import_callable(engine)  # fail fast in the parent on a bad engine spec
    if n_procs == 1:
        return [execute_job(engine, job) for job in jobs]
    with multiprocessing.get_context("spawn").Pool(n_procs) as pool:
        return pool.starmap(execute_job, [(engine, job) for job in jobs])


# ---------------------------------------------------------------------------
# Tables (docs/schema.md: results/, meta/)
# ---------------------------------------------------------------------------


def policy_results_table(mode: str, jobs: list[Job], results: list[RunResult]) -> pd.DataFrame:
    rows = []
    for job, res in zip(jobs, results, strict=True):
        rows.append({
            "run_id": run_id(mode, job.cfg, res.policy, job.theta, job.seed),
            "policy": res.policy, "theta": job.theta, "seed": job.seed, "world_seed": job.cfg.meta.world_seed,
            "N_completed": res.N_completed, "V_profit_vnd": res.V_profit_vnd,
            "voucher_spent_vnd": res.voucher_spent_vnd, "budget_B_vnd": res.budget_B_vnd,
            "n_sessions": res.n_sessions, "n_requests": res.n_requests,
            "n_abandoned": res.n_abandoned, "n_cancelled": res.n_cancelled,
            "mean_pickup_eta_min": res.mean_pickup_eta_min, "share_cells_off": res.share_cells_off,
            "n_switches_per_cell_day": res.n_switches_per_cell_day, "runtime_s": res.runtime_s,
        })
    return pd.DataFrame(rows)


def throughput_curve_table(jobs: list[Job], results: list[RunResult]) -> pd.DataFrame:
    rows = []
    for job, res in zip(jobs, results, strict=True):
        rows.append({
            "run_id": run_id("throughput_curve", job.cfg, res.policy, job.theta, job.seed),
            "demand_scale": job.cfg.demand.demand_scale, "fleet_size": job.cfg.supply.fleet_size, "seed": job.seed,
            "completed_per_h": res.completed_per_h, "requests_per_h": res.requests_per_h,
            "mean_pickup_eta_min": res.mean_pickup_eta_min, "mean_slack": res.mean_slack,
            "abandon_rate": res.abandon_rate, "cancel_rate": res.cancel_rate,
        })
    return pd.DataFrame(rows)


def theta_sweep_table(results: pd.DataFrame) -> pd.DataFrame:
    """Aggregate of policy_results over seeds per theta (docs/schema.md results/theta_sweep)."""
    g = results.groupby("theta", sort=True)
    n = g.size()
    root_n = np.sqrt(n.to_numpy(dtype=float))
    out = pd.DataFrame({
        "theta": n.index.to_numpy(dtype=float),
        "N_mean": g["N_completed"].mean().to_numpy(dtype=float),
        "N_se": g["N_completed"].std(ddof=1).to_numpy(dtype=float) / root_n,
        "V_mean": g["V_profit_vnd"].mean().to_numpy(dtype=float),
        "V_se": g["V_profit_vnd"].std(ddof=1).to_numpy(dtype=float) / root_n,
        "spent_mean": g["voucher_spent_vnd"].mean().to_numpy(dtype=float),
        "n_seeds": n.to_numpy(),
    })
    is_argmax = np.zeros(len(out), dtype=bool)
    if len(out):
        is_argmax[int(out["N_mean"].to_numpy().argmax())] = True   # ties: the smallest theta
    out["is_argmax"] = is_argmax
    return out


def metadata_table(mode: str, jobs: list[Job], results: list[RunResult], *,
                   budget_B_vnd: float | None = None) -> pd.DataFrame:
    """One meta/run_metadata row per run. ``budget_B_vnd`` overrides the engine's value (pilot runs)."""
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sha = git_sha()
    rows = []
    for job, res in zip(jobs, results, strict=True):
        rows.append({
            "run_id": run_id(mode, job.cfg, res.policy, job.theta, job.seed), "mode": mode,
            "config_hash": config_hash(job.cfg),
            "config_yaml": yaml.safe_dump(to_dict(with_seed(job.cfg, job.seed)), sort_keys=True),
            "git_sha": sha, "policy": res.policy, "hidden_view": hidden_view(job.cfg), "theta": job.theta,
            "seed": job.seed, "world_seed": job.cfg.meta.world_seed,
            "budget_B_vnd": res.budget_B_vnd if budget_B_vnd is None else budget_B_vnd,
            "budget_pilot_seed": int(job.budget_pilot_seed), "budget_pilot_spent_vnd": float(job.budget_pilot_spent_vnd),
            "kappa": NAN if job.kappa is None else float(job.kappa), "kappa_pilots": int(job.kappa_pilots),
            "window_start_s": res.window_start_s, "window_end_s": res.window_end_s, "sim_end_s": res.sim_end_s,
            "n_truncated_orders": res.n_truncated_orders, "runtime_s": res.runtime_s, "created_at": created,
        })
    return pd.DataFrame(rows)


def write_full_runs(out_dir: Path, mode: str, jobs: list[Job], results: list[RunResult]) -> list[Path]:
    """Per-run tables of the full-log runs (``log_level = "full"``), each under ``out_dir/runs/<run_id>``.

    A single-run mode (``generate``) writes straight into ``out_dir`` instead. The
    world only depends on ``world_seed``, so it is built once for all runs.
    """
    full = [(job, res) for job, res in zip(jobs, results, strict=True) if res.sessions is not None]
    if not full:
        return []
    cfg0 = full[0][0].cfg
    riders = build_world(cfg0, Rng.from_config(cfg0)).riders
    written = []
    for job, res in full:
        rid = run_id(mode, job.cfg, res.policy, job.theta, job.seed)
        target = Path(out_dir) if len(jobs) == 1 else Path(out_dir) / "runs" / rid
        written += list(write_run(target, res, riders, rid, job.seed).values())
        if target != Path(out_dir):
            # One-row results and metadata so the run directory is self-contained: analysis.animation and
            # analysis.check_dataset read one run at a time; the table-level files keep every run.
            written.append(write_results(target, "policy_results", policy_results_table(mode, [job], [res])))
            written.append(write_metadata(target, metadata_table(mode, [job], [res])))
    return written


def _finish(mode: str, out_dir: Path, jobs: list[Job], results: list[RunResult]) -> pd.DataFrame:
    table = policy_results_table(mode, jobs, results)
    write_results(out_dir, "policy_results", table)
    write_metadata(out_dir, metadata_table(mode, jobs, results))
    write_full_runs(out_dir, mode, jobs, results)
    return table


# ---------------------------------------------------------------------------
# Budget B and kappa
# ---------------------------------------------------------------------------


def calibrate_budget(cfg: Config, *, engine: str = DEFAULT_ENGINE) -> tuple[float, RunResult | None]:
    """B in VND per budget period: ``budget.fixed_vnd``, or ``fraction`` x mean spend per period of an all_on pilot."""
    if cfg.budget.mode == "fixed":
        return resolve_budget_vnd(cfg), None
    job = Job(cfg=with_policy(cfg, "all_on"), seed=pilot_seed(cfg), enforce_budget=False)
    pilot = run_jobs([job], engine=engine, n_procs=1)[0]
    return resolve_budget_vnd(cfg, pilot.spent_by_period_vnd), pilot


@dataclass(frozen=True)
class BudgetPilot:
    """B and the all_on pilot that produced it (``meta/run_metadata`` ``budget_pilot_*``)."""
    budget_vnd: float | None
    pilot_seed: int = -1
    pilot_spent_vnd: float = NAN

    @property
    def job_fields(self) -> dict:
        return {"budget_pilot_seed": self.pilot_seed, "budget_pilot_spent_vnd": self.pilot_spent_vnd}


def budget_and_pilot(cfg: Config, *, enforce: bool | None = None, engine: str = DEFAULT_ENGINE) -> BudgetPilot:
    """B when the budget is enforced (default ``budget.enforce``), with the pilot seed and spend; no pilot: -1, NaN."""
    if not (cfg.budget.enforce if enforce is None else enforce):
        return BudgetPilot(None)
    budget, pilot = calibrate_budget(cfg, engine=engine)
    if pilot is None:
        return BudgetPilot(budget)
    return BudgetPilot(budget, pilot_seed(cfg), float(pilot.voucher_spent_vnd))


def budget_for(cfg: Config, *, engine: str = DEFAULT_ENGINE) -> float | None:
    """B when ``budget.enforce`` is on, else None."""
    return budget_and_pilot(cfg, engine=engine).budget_vnd


def kappa_from_pilot(score, voucher_vnd, completed, budget_total_vnd: float) -> float:
    """Smallest score ``kappa`` such that the sessions with ``score >= kappa`` fit the budget.

    Expected spend of an offered session is its voucher when the order completed, 0
    otherwise. Sessions with equal scores are kept or dropped together, because the
    policy offers on ``score >= kappa`` (one rider's sessions share a score under
    ``heuristic_low_freq``). Sessions without a finite score are
    never kept. ``+inf`` when not even the top score fits, ``-inf`` when every
    session fits.
    """
    score = np.asarray(score, dtype=np.float64)
    spend = np.where(np.asarray(completed, dtype=bool), np.asarray(voucher_vnd, dtype=np.float64), 0.0)
    keep = np.isfinite(score)
    score, spend = score[keep], spend[keep]
    if len(score) == 0:
        return -math.inf
    order = np.argsort(-score, kind="stable")
    score, cum = score[order], np.cumsum(spend[order])
    group_end = np.flatnonzero(np.append(score[1:] != score[:-1], True))   # last index of each tie group
    fits = group_end[cum[group_end] <= float(budget_total_vnd)]           # a prefix: cum is non-decreasing
    if len(fits) == 0:
        return math.inf
    if fits[-1] == len(score) - 1:
        return -math.inf
    return float(score[fits[-1]])


def pilot_spend_vnd(pilot: RunResult) -> float:
    """Expected spend of a pilot: vouchers of the offered sessions (window) whose order completed."""
    voucher = np.asarray(pilot.offer_voucher_vnd, dtype=np.float64)
    return float(voucher[np.asarray(pilot.offer_completed, dtype=bool)].sum())


@dataclass(frozen=True)
class KappaAuto:
    """kappa of a threshold run and how it was found."""

    kappa: float
    n_pilots: int = 0                         # pilot runs used, the kappa = -inf one included; 0 = no kappa-auto
    pilot_kappas: tuple[float, ...] = ()      # kappa each pilot ran at (-inf first)
    spend_ratios: tuple[float, ...] = ()      # each pilot's expected spend / (B x n_periods)


def kappa_auto(cfg: Config, thetas, budget_vnd: float, *, engine: str = DEFAULT_ENGINE) -> list[KappaAuto]:
    """kappa-auto for several thetas: fixed-point iteration, the pilots of one round in parallel.

    Round 0 runs every theta at ``kappa = -inf``. A pilot whose expected spend
    (:func:`pilot_spend_vnd`) exceeds ``B x n_periods`` raises its theta's kappa with
    :func:`kappa_from_pilot` on that pilot, and the next round reruns the pilot at
    the new kappa; a theta is done when its pilot fits. Because tie groups are kept
    whole, an overspending pilot always gives a strictly larger kappa, so kappa only
    rises. After ``policy.threshold.kappa_max_iter`` reruns the last kappa found is
    kept untested (0 reruns = a single pilot); the hard
    budget stop of the ledger still holds in the evaluation runs. Pilots: threshold
    policy, no budget, the same seed :func:`pilot_seed` for every theta and round.
    """
    th_cfg = with_policy(cfg, "threshold")
    total = float(budget_vnd) * Clock.from_config(cfg).n_periods
    max_iter = int(cfg.policy.threshold.kappa_max_iter)
    thetas = [float(t) for t in thetas]
    kappa = [-math.inf] * len(thetas)
    ran: list[list[float]] = [[] for _ in thetas]
    ratios: list[list[float]] = [[] for _ in thetas]
    final: list[float | None] = [None] * len(thetas)
    active = list(range(len(thetas)))
    while active:
        jobs = [Job(cfg=th_cfg, seed=pilot_seed(cfg), theta=thetas[i], kappa=kappa[i], enforce_budget=False)
                for i in active]
        still = []
        for i, pilot in zip(active, run_jobs(jobs, engine=engine), strict=True):
            spend = pilot_spend_vnd(pilot)
            ran[i].append(kappa[i])
            ratios[i].append(spend / total if total > 0 else (0.0 if spend == 0 else math.inf))
            if spend <= total:
                final[i] = kappa[i]
                continue
            new = kappa_from_pilot(pilot.offer_score, pilot.offer_voucher_vnd, pilot.offer_completed, total)
            if not new > kappa[i]:
                raise RuntimeError(f"kappa-auto did not rise at theta={thetas[i]}: {kappa[i]} -> {new}")
            kappa[i] = new
            if len(ran[i]) > max_iter:
                final[i] = new                 # reruns used up: keep the last kappa, untested
            else:
                still.append(i)
        active = still
    return [KappaAuto(kappa=float(final[i]), n_pilots=len(ran[i]), pilot_kappas=tuple(ran[i]),
                      spend_ratios=tuple(ratios[i])) for i in range(len(thetas))]


def resolve_kappas(cfg: Config, thetas, budget_vnd: float | None, *,
                   engine: str = DEFAULT_ENGINE) -> list[KappaAuto] | None:
    """kappa of a threshold run at each theta: the config value, or kappa-auto; None for other policies."""
    if cfg.policy.name != "threshold":
        return None
    k = cfg.policy.threshold.kappa
    if k != "auto":
        return [KappaAuto(kappa=float(k)) for _ in thetas]
    if budget_vnd is None:
        return [KappaAuto(kappa=-math.inf) for _ in thetas]    # nothing to ration without a budget
    return kappa_auto(cfg, thetas, budget_vnd, engine=engine)


def resolve_kappa(cfg: Config, theta: float, budget_vnd: float | None, *,
                  engine: str = DEFAULT_ENGINE) -> float | None:
    """kappa of a threshold run at one theta (see :func:`resolve_kappas`); None for other policies."""
    found = resolve_kappas(cfg, [theta], budget_vnd, engine=engine)
    return None if found is None else found[0].kappa


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def evaluate(cfg: Config, out_dir: Path, *, engine: str = DEFAULT_ENGINE, log_level: str = "minimal",
             profile: bool = False) -> pd.DataFrame:
    """``policy.name`` x ``sweep.n_seeds`` under B; writes policy_results and run_metadata."""
    bp = budget_and_pilot(cfg, engine=engine)
    budget = bp.budget_vnd
    theta = float(cfg.policy.threshold.theta) if cfg.policy.name == "threshold" else NAN
    found = resolve_kappas(cfg, [theta], budget, engine=engine)
    ka = KappaAuto(kappa=NAN) if found is None else found[0]
    jobs = [Job(cfg=cfg, seed=s, theta=theta, kappa=None if found is None else ka.kappa, budget_vnd=budget,
                enforce_budget=cfg.budget.enforce, log_level=log_level, profile=profile, kappa_pilots=ka.n_pilots,
                **bp.job_fields)
            for s in seeds_for(cfg, cfg.sweep.n_seeds)]
    return _finish("evaluate", out_dir, jobs, run_jobs(jobs, engine=engine))


def gte(cfg: Config, out_dir: Path, *, engine: str = DEFAULT_ENGINE, log_level: str = "minimal",
        profile: bool = False) -> pd.DataFrame:
    """all_on and all_off x ``gte.n_seeds``, no budget (D10)."""
    jobs = [Job(cfg=with_policy(cfg, name), seed=s, enforce_budget=False, log_level=log_level, profile=profile)
            for name in ("all_on", "all_off") for s in seeds_for(cfg, cfg.gte.n_seeds)]
    return _finish("gte", out_dir, jobs, run_jobs(jobs, engine=engine))


def gte_summary(table: pd.DataFrame) -> dict[str, float]:
    """GTE = mean over seeds of N(all_on) - N(all_off), paired by seed (CRN)."""
    on = table[table["policy"] == "all_on"].set_index("seed")["N_completed"].astype(float)
    off = table[table["policy"] == "all_off"].set_index("seed")["N_completed"].astype(float)
    diff = (on - off).dropna()
    n = int(len(diff))
    return {"N_on": float(on.mean()), "N_off": float(off.mean()), "GTE": float(diff.mean()),
            "GTE_se": float(diff.std(ddof=1) / math.sqrt(n)) if n > 1 else NAN, "n_seeds": n}


def sweep_theta(cfg: Config, out_dir: Path, *, engine: str = DEFAULT_ENGINE, log_level: str = "minimal",
                profile: bool = False) -> pd.DataFrame:
    """Threshold policy for every theta of ``sweep.theta_grid`` x ``sweep.n_seeds``, same B; returns theta_sweep."""
    base = with_policy(cfg, "threshold")
    bp = budget_and_pilot(base, engine=engine)
    budget = bp.budget_vnd
    grid = [float(t) for t in cfg.sweep.theta_grid]
    jobs: list[Job] = []
    for theta, ka in zip(grid, resolve_kappas(base, grid, budget, engine=engine), strict=True):
        jobs += [Job(cfg=base, seed=s, theta=theta, kappa=ka.kappa, budget_vnd=budget,
                     enforce_budget=base.budget.enforce, log_level=log_level, profile=profile,
                     kappa_pilots=ka.n_pilots, **bp.job_fields)
                 for s in seeds_for(base, cfg.sweep.n_seeds)]
    table = _finish("sweep_theta", out_dir, jobs, run_jobs(jobs, engine=engine))
    sweep = theta_sweep_table(table)
    write_results(out_dir, "theta_sweep", sweep)
    return sweep


def generate(cfg: Config, out_dir: Path, *, engine: str = DEFAULT_ENGINE, log_level: str = "full",
             profile: bool = False) -> tuple[pd.DataFrame, RunResult]:
    """``generate.days`` continuous days with ``policy.name`` legacy or experiment, one seed, full log.

    The window is ``generate.days`` x 1440 min (``time.window_min`` is ignored). B is
    per budget period, so the all_on pilot runs on the base window. Writes
    observed/, market/, hidden/ (docs/schema.md), results/policy_results and
    meta/run_metadata under ``out_dir`` and returns the engine result.
    """
    if cfg.policy.name not in ("legacy", "experiment"):
        raise ValueError(f"generate needs policy.name legacy or experiment, got {cfg.policy.name!r}")
    enforce = cfg.experiment.budget_enforce if cfg.policy.name == "experiment" else cfg.budget.enforce
    bp = budget_and_pilot(cfg, enforce=enforce, engine=engine)
    budget = bp.budget_vnd
    gen_cfg = dataclasses.replace(cfg, time=dataclasses.replace(cfg.time, days_per_run=cfg.generate.days,
                                                                window_min=None))
    job = Job(cfg=gen_cfg, seed=cfg.meta.run_seed, budget_vnd=budget, enforce_budget=enforce,
              log_level=log_level, profile=profile, **bp.job_fields)
    result = run_jobs([job], engine=engine, n_procs=1)[0]
    return _finish("generate", out_dir, [job], [result]), result


def calibrate_budget_table(cfg: Config, out_dir: Path, *, engine: str = DEFAULT_ENGINE) -> pd.DataFrame:
    """Mode ``calibrate_budget``: B and, for a pilot, its run_metadata row carrying B."""
    budget, pilot = calibrate_budget(cfg, engine=engine)
    if pilot is not None:
        job = Job(cfg=with_policy(cfg, "all_on"), seed=pilot_seed(cfg), enforce_budget=False)
        write_metadata(out_dir, metadata_table("calibrate_budget", [job], [pilot], budget_B_vnd=budget))
    return pd.DataFrame({"budget_B_vnd": [budget], "mode": [cfg.budget.mode], "fraction": [cfg.budget.fraction],
                         "pilot_seed": [-1 if pilot is None else pilot_seed(cfg)],
                         "pilot_spent_vnd": [NAN if pilot is None else pilot.voucher_spent_vnd]})


# ---------------------------------------------------------------------------
# Mode: throughput_curve (test A1)
# ---------------------------------------------------------------------------


def throughput_config(cfg: Config, demand_scale: float) -> Config:
    """all_off, every driver online all run, hour profile and speed frozen at ``throughput.reference_hour``."""
    h = cfg.throughput.reference_hour
    return dataclasses.replace(
        cfg,
        demand=dataclasses.replace(cfg.demand, demand_scale=float(demand_scale),
                                   hour_profile=(cfg.demand.hour_profile[h],) * 24),
        space=dataclasses.replace(cfg.space, speed_factor_by_hour=(cfg.space.speed_factor_by_hour[h],) * 24),
        supply=dataclasses.replace(cfg.supply, shift_mode="always_on"),
        policy=dataclasses.replace(cfg.policy, name="all_off"),
    )


def throughput_jobs(cfg: Config, *, profile: bool = False) -> list[Job]:
    """``throughput.demand_scale_grid`` x ``throughput.n_seeds``, no budget."""
    return [
        Job(cfg=throughput_config(cfg, ds), seed=seed, enforce_budget=False, profile=profile)
        for ds in cfg.throughput.demand_scale_grid
        for seed in seeds_for(cfg, cfg.throughput.n_seeds)
    ]


def run_throughput_curve(cfg: Config, out_dir: Path, *, engine: str = DEFAULT_ENGINE,
                         n_procs: int | None = None, profile: bool = False) -> pd.DataFrame:
    """Run the grid and write ``results/throughput_curve.parquet``; returns the table."""
    jobs = throughput_jobs(cfg, profile=profile)
    results = run_jobs(jobs, engine=engine, n_procs=n_procs)
    table = throughput_curve_table(jobs, results)
    write_results(out_dir, "throughput_curve", table)
    return table


def summarize_throughput(table: pd.DataFrame) -> pd.DataFrame:
    """Mean over seeds per demand level, for the console and the A1 throughput plot."""
    cols = ["completed_per_h", "requests_per_h", "mean_pickup_eta_min", "mean_slack", "abandon_rate", "cancel_rate"]
    return table.groupby("demand_scale", as_index=False)[cols].mean()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def run_mode(mode: str, cfg: Config, out_dir: Path, *, engine: str = DEFAULT_ENGINE, log_level: str = "minimal",
             profile: bool = False) -> pd.DataFrame:
    """Run a mode and return its headline table (also written under ``out_dir``)."""
    out_dir = Path(out_dir)
    if mode == "throughput_curve":
        return run_throughput_curve(cfg, out_dir, engine=engine, profile=profile)
    if mode == "evaluate":
        return evaluate(cfg, out_dir, engine=engine, log_level=log_level, profile=profile)
    if mode == "gte":
        return gte(cfg, out_dir, engine=engine, log_level=log_level, profile=profile)
    if mode == "sweep_theta":
        return sweep_theta(cfg, out_dir, engine=engine, log_level=log_level, profile=profile)
    if mode == "calibrate_budget":
        return calibrate_budget_table(cfg, out_dir, engine=engine)
    if mode == "generate":
        return generate(cfg, out_dir, engine=engine, log_level="full", profile=profile)[0]
    raise ValueError(f"unknown mode {mode!r}")
