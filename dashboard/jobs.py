"""Background runs of the dashboard: one worker thread, a queue, and the files under ``runs/dashboard/``.

A dashboard run goes through the same preparation as ``sim.runner.evaluate`` for
one seed: the all_on pilot that sets B (``runner.calibrate_budget``), kappa-auto
for a threshold policy (``runner.kappa_auto``), then the traced engine loop
(:func:`dashboard.trace.run_traced`). The run's tables are written with the
simulator's own logger (observed/, market/, hidden/, results/policy_results,
meta/run_metadata, ``mode = "dashboard"``), plus ``dashboard/`` with the frames
(``trace.npz``), the slot records (``slots.npz``) and the page summaries (JSON), so
a finished run reloads without the engine objects.
"""

from __future__ import annotations

import dataclasses
import json
import math
import queue
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from sim.config import Config, ConfigError, config_hash, load_config, to_dict
from sim.engine import RunResult
from sim.logger import write_metadata, write_results, write_run
from sim.policies import make_policy
from sim.population import build_world
from sim.rng import Rng
from sim.runner import (
    Job, KappaAuto, budget_for, calibrate_budget, kappa_auto, metadata_table, policy_results_table, resolve_kappas,
    run_id,
)
from sim.state import Clock

from dashboard import summary as summ
from dashboard.geometry import world_geometry
from dashboard.trace import SLOT_SNAPSHOT_FIELDS, Trace, run_traced

NAN = float("nan")
MODE = "dashboard"
STAGES = ("queued", "loading", "budget", "kappa", "simulating", "writing", "done", "error")


@dataclass(frozen=True)
class RunSpec:
    """What the form sends: a name, config layers, ``key=value`` overrides and the two shortcuts."""

    name: str
    overrides: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()               # extra YAML layers after config/default.yaml (repo-relative)
    budget_vnd: float | None = None            # explicit B per period (skips the all_on pilot)
    kappa: float | None = None                 # explicit kappa (skips kappa-auto)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class SweepSpec:
    """A theta sweep: the grid, seeds per theta and the pool size, on top of the config layers / overrides."""

    name: str
    overrides: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()
    theta_grid: tuple[float, ...] = ()
    n_seeds: int = 3
    n_procs: int | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class DashRun:
    id: str
    name: str
    spec: RunSpec | SweepSpec
    created_at: str
    out_dir: Path
    kind: str = "single"              # single | sweep
    sweep: dict | None = None         # sweep progress and rows (kind = sweep)
    status: str = "queued"
    progress: float = 0.0
    tick: int = 0
    t_s: float = 0.0
    ticks_max: int = 0
    message: str = ""
    log: list[str] = field(default_factory=list)
    error: str | None = None
    stage_started: float = field(default_factory=time.time)
    # filled while running / after loading
    cfg: Config | None = None
    config: dict | None = None
    config_hash: str | None = None
    clock: dict | None = None
    policy: str | None = None
    theta: float | None = None
    scope: str | None = None
    score_fn: str | None = None
    budget_vnd: float | None = None
    kappa: float | None = None
    kappa_pilots: int = 0
    kappa_detail: dict | None = None
    geometry: dict | None = None
    trace: Trace | None = None
    result: RunResult | None = None
    summary: dict | None = None
    runtime: dict = field(default_factory=dict)
    n_frames_saved: int = 0          # from meta.json, until the frames are loaded
    n_slots_saved: int = 0

    def note(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {text}")
        self.message = text

    def set_stage(self, stage: str, text: str | None = None) -> None:
        self.status = stage
        self.stage_started = time.time()
        if text:
            self.note(text)

    def public(self) -> dict:
        """JSON view without the heavy parts (frames, slots, summary, sweep rows)."""
        sweep = None
        if self.sweep is not None:
            sweep = {k: v for k, v in self.sweep.items() if k != "rows"}
            sweep["n_rows"] = len(self.sweep.get("rows", []))
        return {
            "id": self.id, "name": self.name, "created_at": self.created_at, "status": self.status, "kind": self.kind,
            "sweep": sweep, "sweep_summary": (self.summary or {}).get("sweep_brief"),
            "progress": self.progress, "tick": self.tick, "t_s": self.t_s, "ticks_max": self.ticks_max,
            "message": self.message, "log": self.log[-40:], "error": self.error,
            "stage_elapsed_s": time.time() - self.stage_started if self.status not in ("done", "error") else None,
            "spec": self.spec.to_dict(), "config_hash": self.config_hash, "clock": self.clock,
            "policy": self.policy, "theta": self.theta, "scope": self.scope, "score_fn": self.score_fn,
            "budget_vnd": self.budget_vnd, "kappa": _json_float(self.kappa), "kappa_pilots": self.kappa_pilots,
            "kappa_detail": self.kappa_detail,
            "n_frames": self.trace.n_frames if self.trace else self.n_frames_saved,
            "n_slots": len(self.trace.slots) if self.trace else self.n_slots_saved, "runtime": self.runtime,
            "kpis": (self.summary or {}).get("kpis"), "out_dir": self.out_dir.as_posix(),
            "seed": ((self.config or {}).get("meta") or {}).get("run_seed"),
            "has_revenue": bool((self.summary or {}).get("revenue")),
        }


def _json_float(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip()).strip("-").lower()
    return text[:40] or "run"


class RunManager:
    """Owns the runs (in memory and under ``runs_dir``) and the single worker thread."""

    def __init__(self, root: Path, runs_dir: Path | None = None) -> None:
        self.root = Path(root)
        self.runs_dir = Path(runs_dir) if runs_dir is not None else self.root / "runs" / "dashboard"
        self.runs: dict[str, DashRun] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.load_saved()

    # --- public ------------------------------------------------------------------

    def submit(self, spec: RunSpec | SweepSpec) -> DashRun:
        kind = "sweep" if isinstance(spec, SweepSpec) else "single"
        run_key = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{'sweep-' if kind == 'sweep' else ''}{_slug(spec.name)}"
        with self._lock:
            n = 2
            base = run_key
            while run_key in self.runs:
                run_key = f"{base}-{n}"
                n += 1
            run = DashRun(id=run_key, name=spec.name, spec=spec, kind=kind,
                          created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          out_dir=self.runs_dir / run_key)
            if kind == "sweep":
                run.sweep = {"theta_grid": [float(t) for t in spec.theta_grid], "n_seeds": int(spec.n_seeds),
                             "n_procs": spec.n_procs, "done": 0, "total": len(spec.theta_grid) * int(spec.n_seeds),
                             "budget_vnd": None, "kappas": None, "rows": []}
            self.runs[run_key] = run
        run.note("Đã xếp hàng")
        self._queue.put(run_key)
        self._ensure_worker()
        return run

    def get(self, run_key: str) -> DashRun | None:
        return self.runs.get(run_key)

    def list(self) -> list[dict]:
        return [r.public() for r in sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)]

    def delete(self, run_key: str) -> bool:
        run = self.runs.get(run_key)
        if run is None or run.status not in ("done", "error"):
            return False
        with self._lock:
            self.runs.pop(run_key, None)
        return True

    # --- worker ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="dashboard-runs", daemon=True)
            self._worker.start()

    def _loop(self) -> None:
        while True:
            run_key = self._queue.get()
            run = self.runs.get(run_key)
            if run is None:
                continue
            try:
                if run.kind == "sweep":
                    self._execute_sweep(run)
                else:
                    self._execute(run)
            except Exception as exc:  # noqa: BLE001 - reported to the page
                run.error = f"{type(exc).__name__}: {exc}"
                run.note(f"Lỗi: {run.error}")
                run.log.append(traceback.format_exc()[-2000:])
                run.set_stage("error")

    def _execute(self, run: DashRun) -> None:
        spec = run.spec
        wall = time.perf_counter()
        run.set_stage("loading", "Nạp config")
        layers = [self.root / "config" / "default.yaml"] + [self.root / p for p in spec.layers]
        try:
            cfg = load_config(layers, list(spec.overrides))
        except ConfigError as exc:
            raise ConfigError(f"config: {exc}") from exc
        run.cfg = cfg
        run.config = to_dict(cfg)
        run.config_hash = config_hash(cfg)
        clock = Clock.from_config(cfg)
        run.clock = _clock_dict(clock)
        run.policy = cfg.policy.name
        is_threshold = cfg.policy.name == "threshold"
        run.theta = float(cfg.policy.threshold.theta) if is_threshold else None
        run.scope = cfg.policy.threshold.scope if is_threshold else None
        run.score_fn = cfg.policy.threshold.score_fn if is_threshold else None

        # 1. Budget B (spec §4.3): explicit, or the all_on pilot of runner.calibrate_budget.
        # M10 assigns treatment without a budget constraint so its propensity is
        # the configured experiment probability.  This mirrors sim.runner and
        # keeps the dashboard form's “Thí nghiệm không áp ngân sách” statement true.
        enforce = bool(cfg.experiment.budget_enforce if cfg.policy.name == "experiment" else cfg.budget.enforce)
        budget: float | None = None
        if enforce:
            if spec.budget_vnd is not None:
                budget = float(spec.budget_vnd)
                run.note(f"Ngân sách B = {budget:,.0f} đ/kỳ (nhập tay)")
            else:
                run.set_stage("budget", "Pilot all_on không ngân sách để lấy B (runner.calibrate_budget)")
                t0 = time.perf_counter()
                budget, pilot = calibrate_budget(cfg)
                run.runtime["budget_pilot_s"] = time.perf_counter() - t0
                spent = None if pilot is None else pilot.voucher_spent_vnd
                run.note(f"B = {budget:,.0f} đ/kỳ"
                         + (f" (pilot chi {spent:,.0f} đ, fraction {cfg.budget.fraction})" if spent is not None else ""))
        else:
            run.note("Không áp ngân sách (budget.enforce = false)")
        run.budget_vnd = budget

        # 2. kappa of the rider tier (spec §6, H-21).
        kappa: float | None = None
        ka: KappaAuto | None = None
        if is_threshold:
            if spec.kappa is not None:
                kappa = float(spec.kappa)
                run.note(f"κ = {kappa:g} (nhập tay)")
            elif cfg.policy.threshold.kappa != "auto":
                kappa = float(cfg.policy.threshold.kappa)
                run.note(f"κ = {kappa:g} (config)")
            elif budget is None:
                kappa = -math.inf
                run.note("κ = −∞ (không có ngân sách thì không hạn chế tầng rider, T-23)")
            else:
                run.set_stage("kappa", "κ auto: pilot threshold không ngân sách, lặp đến điểm bất động (H-21)")
                t0 = time.perf_counter()
                ka = kappa_auto(cfg, [run.theta], budget)[0]
                kappa = ka.kappa
                run.runtime["kappa_pilots_s"] = time.perf_counter() - t0
                run.kappa_pilots = ka.n_pilots
                run.kappa_detail = {"pilot_kappas": [_json_float(k) for k in ka.pilot_kappas],
                                    "spend_ratios": [_json_float(r) for r in ka.spend_ratios]}
                run.note(f"κ = {kappa:g} sau {ka.n_pilots} pilot")
        run.kappa = kappa

        # 3. World, policy, traced run.
        rng = Rng.from_config(cfg)
        world = build_world(cfg, rng)
        policy = make_policy(cfg, world, rng=rng, theta=run.theta, kappa=kappa)
        run.geometry = world_geometry(cfg, world)
        run.set_stage("simulating", f"Mô phỏng {cfg.supply.fleet_size} xe, {world.n_cells} ô, seed {cfg.meta.run_seed}")

        def on_progress(tick: int, t: float, ticks_max: int) -> None:
            run.tick, run.t_s, run.ticks_max = tick, t, ticks_max
            run.progress = min(1.0, tick / max(1, ticks_max))

        def on_trace(tr: Trace) -> None:
            run.trace = tr

        t0 = time.perf_counter()
        result, trace = run_traced(cfg, world, policy, rng, budget_vnd=budget, enforce_budget=enforce,
                                   on_progress=on_progress, on_trace=on_trace)
        run.runtime["simulate_s"] = time.perf_counter() - t0
        run.result, run.trace = result, trace
        run.progress = 1.0
        run.note(f"Xong mô phỏng: N = {result.N_completed}, chi {result.voucher_spent_vnd:,.0f} đ, "
                 f"{trace.n_frames} tick, {result.runtime_s:.1f} s")

        # 4. Summaries and files.
        run.set_stage("writing", "Tổng hợp và ghi bảng")
        run.summary = {
            "kpis": summ.kpis(result, cfg, theta=run.theta, kappa=kappa, kappa_pilots=run.kappa_pilots,
                              budget_vnd=budget, score_fn=run.score_fn, scope=run.scope),
            "slot_series": summ.slot_series(trace, clock),
            "switch_events": summ.switch_events(trace, clock, run.theta),
            "distribution": summ.distribution(result, trace, clock, kappa=kappa, x_segment=world.riders.x_segment),
            "revenue": summ.revenue(result),
        }
        self._write(run, cfg, world, result, trace, budget, kappa, enforce, ka)
        run.runtime["total_s"] = time.perf_counter() - wall
        run.set_stage("done", f"Hoàn tất trong {run.runtime['total_s']:.1f} s; ghi {run.out_dir.as_posix()}")
        run.result = None   # buffers are on disk now; keep the trace for the pages

    def _execute_sweep(self, run: DashRun) -> None:
        """theta sweep: B once, kappa-auto per theta, every (theta, seed) job, live rows, tables of mode sweep_theta."""
        from dashboard import sweep as sw

        spec: SweepSpec = run.spec  # type: ignore[assignment]
        wall = time.perf_counter()
        run.set_stage("loading", "Nạp config")
        layers = [self.root / "config" / "default.yaml"] + [self.root / p for p in spec.layers]
        cfg = load_config(layers, list(spec.overrides))
        n_procs = int(spec.n_procs) if spec.n_procs else sw.default_n_procs()
        base = sw.sweep_config(cfg, n_procs)
        run.cfg = base
        run.config = to_dict(base)
        run.config_hash = config_hash(base)
        clock = Clock.from_config(base)
        run.clock = _clock_dict(clock)
        run.policy = "threshold"
        run.scope = base.policy.threshold.scope
        run.score_fn = base.policy.threshold.score_fn
        grid = [float(t) for t in spec.theta_grid]
        if not grid:
            raise ValueError("theta_grid trống")
        state = run.sweep
        assert state is not None
        state["n_procs"] = n_procs

        run.set_stage("budget", "Pilot all_on không ngân sách để lấy B (chung cho mọi θ)")
        t0 = time.perf_counter()
        budget = budget_for(base)
        run.runtime["budget_pilot_s"] = time.perf_counter() - t0
        run.budget_vnd = budget
        state["budget_vnd"] = budget
        run.note("Không áp ngân sách" if budget is None else f"B = {budget:,.0f} đ/kỳ cho mọi θ")

        run.set_stage("kappa", f"κ auto cho {len(grid)} θ: pilot threshold không ngân sách, lặp đến điểm bất động, "
                               f"{n_procs} tiến trình (H-21)")
        t0 = time.perf_counter()
        kappas = resolve_kappas(base, grid, budget) or [KappaAuto(kappa=-math.inf) for _ in grid]
        run.runtime["kappa_pilots_s"] = time.perf_counter() - t0
        state["kappas"] = [{"theta": t, "kappa": _json_float(k.kappa), "n_pilots": k.n_pilots} for t, k in zip(grid, kappas)]
        run.note(f"κ auto xong: {sum(k.n_pilots for k in kappas)} pilot, {run.runtime['kappa_pilots_s']:.0f} s")

        jobs = sw.build_jobs(base, grid, int(spec.n_seeds), budget, kappas)
        state["total"] = len(jobs)
        run.set_stage("simulating", f"Chạy {len(jobs)} lượt ({len(grid)} θ × {spec.n_seeds} seed), {n_procs} tiến trình")
        t0 = time.perf_counter()

        def on_result(i: int, job: Job, res: RunResult) -> None:
            state["rows"].append(sw.row_of(job, res))
            state["done"] = len(state["rows"])
            run.progress = state["done"] / max(1, state["total"])
            run.message = (f"{state['done']} / {state['total']} lượt xong · θ = {job.theta:g}, seed {job.seed}: "
                           f"N = {res.N_completed}")

        results = sw.run_jobs_live(jobs, n_procs=n_procs, on_result=on_result)
        run.runtime["simulate_s"] = time.perf_counter() - t0
        run.progress = 1.0

        run.set_stage("writing", "Ghi bảng sweep_theta và tổng hợp θ*")
        run.out_dir.mkdir(parents=True, exist_ok=True)
        sw.write_tables(run.out_dir, jobs, results)
        agg = sw.aggregate(state["rows"], grid)
        run.summary = {"sweep": agg, "sweep_brief": _sweep_brief(agg)}
        self._write_sweep_meta(run)
        run.runtime["total_s"] = time.perf_counter() - wall
        star = agg.get("star")
        run.set_stage("done", f"θ* = {agg['argmax_theta']:g}"
                      + (f", tập θ* = [{star['lo']:g}; {star['hi']:g}] ({star['n_seeds']} seed)" if star else "")
                      + f" · {run.runtime['total_s']:.0f} s")

    def _write_sweep_meta(self, run: DashRun) -> None:
        d = run.out_dir / "dashboard"
        d.mkdir(parents=True, exist_ok=True)
        state = dict(run.sweep or {})
        rows = state.pop("rows", [])
        meta = {
            "id": run.id, "name": run.name, "kind": "sweep", "created_at": run.created_at, "spec": run.spec.to_dict(),
            "config": run.config, "config_hash": run.config_hash, "clock": run.clock, "policy": run.policy,
            "scope": run.scope, "score_fn": run.score_fn, "budget_vnd": run.budget_vnd, "runtime": run.runtime,
            "log": run.log, "sweep": state, "sweep_brief": (run.summary or {}).get("sweep_brief"),
        }
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "sweep.json").write_text(json.dumps({"rows": rows, "summary": run.summary}, ensure_ascii=False),
                                      encoding="utf-8")

    # --- files -------------------------------------------------------------------

    def _write(self, run: DashRun, cfg: Config, world, result: RunResult, trace: Trace, budget: float | None,
               kappa: float | None, enforce: bool, ka: KappaAuto | None) -> None:
        out = run.out_dir
        (out / "dashboard").mkdir(parents=True, exist_ok=True)
        theta = NAN if run.theta is None else float(run.theta)
        job = Job(cfg=cfg, seed=cfg.meta.run_seed, theta=theta, kappa=kappa, budget_vnd=budget,
                  enforce_budget=enforce, log_level="full", kappa_pilots=0 if ka is None else ka.n_pilots)
        rid = run_id(MODE, cfg, result.policy, theta, cfg.meta.run_seed)
        write_results(out, "policy_results", policy_results_table(MODE, [job], [result]))
        write_metadata(out, metadata_table(MODE, [job], [result]))
        write_run(out, result, world.riders, rid, cfg.meta.run_seed)
        np.savez_compressed(out / "dashboard" / "trace.npz", **trace.frame_arrays())
        np.savez_compressed(out / "dashboard" / "slots.npz", **_slots_arrays(trace))
        meta = {
            "id": run.id, "name": run.name, "created_at": run.created_at, "spec": run.spec.to_dict(),
            "config": run.config, "config_hash": run.config_hash, "clock": run.clock, "policy": run.policy,
            "theta": run.theta, "scope": run.scope, "score_fn": run.score_fn, "budget_vnd": budget,
            "kappa": _json_float(kappa), "kappa_pilots": run.kappa_pilots, "kappa_detail": run.kappa_detail,
            "runtime": run.runtime, "run_id": rid, "log": run.log, "n_frames": trace.n_frames,
            "n_slots": len(trace.slots),
        }
        (out / "dashboard" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "dashboard" / "geometry.json").write_text(json.dumps(run.geometry), encoding="utf-8")
        (out / "dashboard" / "summary.json").write_text(json.dumps(run.summary, ensure_ascii=False), encoding="utf-8")

    def load_saved(self) -> int:
        """Register the finished runs found under ``runs_dir`` (frames load lazily)."""
        n = 0
        if not self.runs_dir.exists():
            return 0
        for meta_path in sorted(self.runs_dir.glob("*/dashboard/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            run_key = meta.get("id") or meta_path.parents[1].name
            if run_key in self.runs:
                continue
            kind = meta.get("kind", "single")
            spec_cls = SweepSpec if kind == "sweep" else RunSpec
            try:
                spec = spec_cls(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in meta.get("spec", {}).items()})
            except TypeError:
                continue            # saved before a spec field was renamed (e.g. USD -> VND, decisions H-33): skip
            run = DashRun(id=run_key, name=meta.get("name", run_key), spec=spec, kind=kind,
                          created_at=meta.get("created_at", ""), out_dir=meta_path.parents[1], status="done",
                          progress=1.0)
            if kind == "sweep":
                run.sweep = {**meta.get("sweep", {}), "rows": []}
                if meta.get("sweep_brief"):
                    run.summary = {"sweep_brief": meta["sweep_brief"]}
            elif (meta_path.parent / "summary.json").exists():
                # KPIs are shown in the run list, so the (small) summary is read now; frames stay lazy.
                try:
                    run.summary = json.loads((meta_path.parent / "summary.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            for k in ("config", "config_hash", "clock", "policy", "theta", "scope", "score_fn", "budget_vnd", "kappa",
                      "kappa_pilots", "kappa_detail", "runtime"):
                if k in meta and meta[k] is not None:
                    setattr(run, k, meta[k])
            run.log = list(meta.get("log", []))
            run.n_frames_saved = int(meta.get("n_frames", 0))
            run.n_slots_saved = int(meta.get("n_slots", 0))
            run.message = "Đã nạp từ đĩa"
            self.runs[run_key] = run
            n += 1
        return n

    def ensure_loaded(self, run: DashRun) -> DashRun:
        """Load geometry, summary and frames of a saved run on first use."""
        if run.status != "done":
            return run
        d = run.out_dir / "dashboard"
        if run.kind == "sweep":
            if run.sweep is not None and not run.sweep.get("rows") and (d / "sweep.json").exists():
                data = json.loads((d / "sweep.json").read_text(encoding="utf-8"))
                run.sweep["rows"] = data.get("rows", [])
                run.sweep["done"] = len(run.sweep["rows"])
                run.summary = data.get("summary") or run.summary
            return run
        if run.geometry is None and (d / "geometry.json").exists():
            run.geometry = json.loads((d / "geometry.json").read_text(encoding="utf-8"))
        if run.summary is None and (d / "summary.json").exists():
            run.summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        if run.trace is None and (d / "trace.npz").exists():
            if run.cfg is None and run.config is not None:
                from sim.config import build_config
                run.cfg = build_config(run.config)
            clock = Clock.from_config(run.cfg)
            with np.load(d / "trace.npz") as z:
                arrays = {k: z[k] for k in z.files}
            slots = _slots_from_arrays(d / "slots.npz") if (d / "slots.npz").exists() else []
            run.trace = Trace.from_arrays(run.cfg, clock, arrays, slots)
        return run


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sweep_brief(agg: dict) -> dict:
    """The few numbers the run list shows for a sweep."""
    star = agg.get("star")
    best = next((p for p in agg["per_theta"] if p["theta"] == agg.get("argmax_theta")), None)
    return {"argmax_theta": agg.get("argmax_theta"), "N_best": best.get("N_mean") if best else None,
            "star_lo": star["lo"] if star else None, "star_hi": star["hi"] if star else None,
            "n_rows": agg.get("n_rows", 0), "gain_vs_zero": agg.get("gain_vs_zero")}


def _clock_dict(clock: Clock) -> dict:
    return {"tick_s": clock.tick_s, "slot_s": clock.slot_s, "warmup_s": clock.warmup_s, "window_s": clock.window_s,
            "window_start_s": clock.window_start_s, "window_end_s": clock.window_end_s,
            "cooldown_max_s": clock.cooldown_max_s, "period_s": clock.period_s, "n_periods": clock.n_periods,
            "slots_per_day": clock.slots_per_day, "ticks_per_day": clock.ticks_per_day}


def _slots_arrays(trace: Trace) -> dict[str, np.ndarray]:
    slots = trace.slots
    n, N = len(slots), trace.n_cells
    out: dict[str, np.ndarray] = {
        "slot": np.array([s["slot"] for s in slots], dtype=np.int32),
        "t_start": np.array([s["t_start"] for s in slots], dtype=np.float64),
        "published": np.array([s["published"] for s in slots], dtype=bool),
        "block": np.array([s["block"] for s in slots], dtype=np.int32),
        "in_burnin": np.array([s["in_burnin"] for s in slots], dtype=bool),
        "promo_on": np.stack([s["promo_on"] for s in slots]) if n else np.zeros((0, N), bool),
        "s_hat": np.stack([s["s_hat"] for s in slots]) if n else np.zeros((0, N), np.float32),
        "mechanism": np.stack([s["mechanism"] for s in slots]) if n else np.zeros((0, N), np.int8),
        "cluster_id": np.stack([s["cluster_id"] for s in slots]) if n else np.zeros((0, N), np.int16),
    }
    for name in SLOT_SNAPSHOT_FIELDS:
        rows = [np.asarray(s[name], dtype=np.float64) if s["published"] else np.full(N, np.nan) for s in slots]
        out[name] = np.stack(rows) if n else np.zeros((0, N), np.float64)
    return out


def _slots_from_arrays(path: Path) -> list[dict]:
    with np.load(path) as z:
        a = {k: z[k] for k in z.files}
    out = []
    for i in range(len(a["slot"])):
        rec: dict[str, Any] = {
            "slot": int(a["slot"][i]), "t_start": float(a["t_start"][i]), "published": bool(a["published"][i]),
            "block": int(a["block"][i]), "in_burnin": bool(a["in_burnin"][i]), "promo_on": a["promo_on"][i],
            "s_hat": a["s_hat"][i], "mechanism": a["mechanism"][i], "cluster_id": a["cluster_id"][i],
        }
        if rec["published"]:
            for name in SLOT_SNAPSHOT_FIELDS:
                rec[name] = a[name][i]
        out.append(rec)
    return out
