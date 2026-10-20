"""FastAPI application of the dashboard: JSON API under ``/api``, static front end at ``/``.

Start with ``python -m dashboard`` (uvicorn). The app object is ``dashboard.server:app``;
:func:`create_app` builds one bound to another repository root (tests).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sim.config import ConfigError, load_config, to_dict
from sim.policies.scores import BUILTIN_SCORES
from sim.runner import MODES

from dashboard import results as res
from dashboard import summary as summ
from dashboard.jobs import DashRun, RunManager, RunSpec, SweepSpec
from dashboard.sweep import aggregate as sweep_aggregate
from dashboard.sweep import default_n_procs
from dashboard.trace import EVENT_TYPES

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"

# Score functions offered in the form: the built-ins, the S5 baselines and the S9 models per 1.000đ
# (analysis.uplift, decisions T-45; the ``_w1`` models are fitted on W1 data) and the S9 hour-off rules.
SCORE_FUNCTIONS: tuple[str, ...] = tuple(BUILTIN_SCORES) + (
    "analysis.score_ties:tau_x_baseline", "analysis.score_ties:tau_per_dollar_baseline",
    "analysis.uplift:tau_x_dr_all_per_kvnd", "analysis.uplift:tau_xs_dr_all_per_kvnd",
    "analysis.uplift:tau_x_dr_per_kvnd", "analysis.uplift:tau_xs_dr_per_kvnd",
    "analysis.uplift:tau_x_dr_all", "analysis.uplift:tau_xs_dr_all",
    "analysis.uplift:tau_x_dr_all_w1_per_kvnd", "analysis.uplift:tau_xs_dr_all_w1_per_kvnd",
    "analysis.hour_rule:off_hours_dr_w0", "analysis.hour_rule:off_hours_dr_w1",
    "analysis.hour_rule:off_hours_random_w0", "analysis.hour_rule:off_hours_random_w1",
)

# Worlds of S8 (decisions T-42, T-43; W1 = config/world_w1.yaml): 61 cells (radius 4, H7.1), Hanoi scale, VND.
PRESETS: tuple[dict, ...] = (
    {"key": "default", "label": "W0 Hà Nội: 61 ô, 1.150 xe, 1 ngày", "layers": [], "overrides": []},
    {"key": "w1", "label": "W1 Hà Nội + biến động: 61 ô, 1.278 xe, mưa / sự kiện", "layers": ["config/world_w1.yaml"],
     "overrides": []},
    {"key": "demo4h", "label": "Demo nhanh: W0, cửa sổ 4 giờ", "layers": [], "overrides": ["time.window_min=240"]},
    {"key": "tiny", "label": "Tiny (test): 7 ô, 10 xe, 2 giờ", "layers": ["tests/fixtures/tiny.yaml"], "overrides": []},
)


class RunRequest(BaseModel):
    name: str = Field(default="run", max_length=80)
    overrides: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    budget_vnd: float | None = None
    kappa: float | None = None


class SweepRequest(BaseModel):
    name: str = Field(default="sweep", max_length=80)
    overrides: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    theta_grid: list[float] = Field(default_factory=list, max_length=64)
    n_seeds: int = Field(default=3, ge=1, le=100)
    n_procs: int | None = Field(default=None, ge=1, le=64)


def _finite(x: Any) -> Any:
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def create_app(root: Path = ROOT, runs_dir: Path | None = None) -> FastAPI:
    root = Path(root)
    manager = RunManager(root, runs_dir)
    app = FastAPI(title="Supply-Aware Promotion dashboard", version="0.1.0")
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.state.manager = manager
    app.state.root = root

    @app.middleware("http")
    async def revalidate_frontend(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # --- config and presets --------------------------------------------------------

    @app.get("/api/config/defaults")
    def config_defaults() -> dict:
        cfg = load_config(root / "config" / "default.yaml")
        return {"config": to_dict(cfg), "score_functions": list(SCORE_FUNCTIONS), "presets": list(PRESETS),
                "modes": list(MODES), "event_types": list(EVENT_TYPES), "cpu_count": default_n_procs()}

    @app.post("/api/config/validate")
    def config_validate(req: RunRequest) -> dict:
        layers = [root / "config" / "default.yaml"] + [root / p for p in req.layers]
        try:
            cfg = load_config(layers, req.overrides)
        except (ConfigError, FileNotFoundError) as exc:
            return {"ok": False, "error": str(exc)}
        from sim.config import config_hash, eval_window_min
        return {"ok": True, "config_hash": config_hash(cfg), "window_min": eval_window_min(cfg),
                "n_cells": 3 * cfg.space.grid_radius ** 2 + 3 * cfg.space.grid_radius + 1,
                "fleet_size": cfg.supply.fleet_size, "policy": cfg.policy.name}

    # --- runs ----------------------------------------------------------------------

    @app.get("/api/runs")
    def list_runs() -> dict:
        return {"runs": manager.list()}

    @app.post("/api/runs", status_code=202)
    def start_run(req: RunRequest) -> dict:
        for layer in req.layers:
            if not (root / layer).exists():
                raise HTTPException(400, f"lớp config không tồn tại: {layer}")
        spec = RunSpec(name=req.name or "run", overrides=tuple(req.overrides), layers=tuple(req.layers),
                       budget_vnd=req.budget_vnd, kappa=req.kappa)
        run = manager.submit(spec)
        return run.public()

    @app.post("/api/sweeps", status_code=202)
    def start_sweep(req: SweepRequest) -> dict:
        for layer in req.layers:
            if not (root / layer).exists():
                raise HTTPException(400, f"lớp config không tồn tại: {layer}")
        grid = sorted({float(t) for t in req.theta_grid})
        if not grid or any(t < 0 for t in grid):
            raise HTTPException(400, "theta_grid cần ít nhất một θ ≥ 0")
        spec = SweepSpec(name=req.name or "sweep", overrides=tuple(req.overrides), layers=tuple(req.layers),
                         theta_grid=tuple(grid), n_seeds=req.n_seeds, n_procs=req.n_procs)
        return manager.submit(spec).public()

    def _run(run_key: str) -> DashRun:
        run = manager.get(run_key)
        if run is None:
            raise HTTPException(404, f"không có lượt chạy {run_key}")
        return manager.ensure_loaded(run)

    @app.get("/api/runs/{run_key}/sweep")
    def get_sweep(run_key: str) -> dict:
        run = _run(run_key)
        if run.kind != "sweep" or run.sweep is None:
            raise HTTPException(404, "lượt này không phải quét θ")
        rows = list(run.sweep.get("rows", []))
        agg = (run.summary or {}).get("sweep") if run.status == "done" else None
        if agg is None:
            agg = sweep_aggregate(rows, run.sweep["theta_grid"])
        return {"status": run.status, "kind": "sweep", "theta_grid": run.sweep["theta_grid"],
                "n_seeds": run.sweep["n_seeds"], "n_procs": run.sweep.get("n_procs"), "done": len(rows),
                "total": run.sweep.get("total"), "budget_vnd": run.sweep.get("budget_vnd"),
                "kappas": run.sweep.get("kappas"), "rows": rows, "aggregate": agg, "scope": run.scope,
                "score_fn": run.score_fn, "config_hash": run.config_hash}

    @app.get("/api/runs/{run_key}")
    def get_run(run_key: str) -> dict:
        run = _run(run_key)
        out = run.public()
        out["geometry"] = run.geometry
        out["config"] = run.config
        return out

    @app.delete("/api/runs/{run_key}")
    def delete_run(run_key: str) -> dict:
        if not manager.delete(run_key):
            raise HTTPException(409, "chỉ bỏ được lượt đã xong hoặc lỗi (file trên đĩa giữ nguyên)")
        return {"ok": True}

    @app.get("/api/runs/{run_key}/frames")
    def get_frames(run_key: str, start: int = Query(0, ge=0), count: int = Query(240, ge=1, le=2000)) -> dict:
        run = _run(run_key)
        tr = run.trace
        if tr is None:
            return {"start": start, "frames": 0, "n_frames": 0, "done": run.status in ("done", "error")}
        n = tr.n_frames
        stop = min(n, start + count)
        if stop <= start:
            return {"start": start, "frames": 0, "n_frames": n, "done": run.status in ("done", "error")}
        sl = slice(start, stop)
        busy = np.stack(tr.busy_until[sl]).astype(np.float64)
        busy = np.where(np.isfinite(busy), busy, -1.0)
        return {
            "start": start, "frames": stop - start, "n_frames": n, "done": run.status in ("done", "error"),
            "t": [float(x) for x in tr.t[sl]],
            "status": np.stack(tr.status[sl]).tolist(), "cell": np.stack(tr.cell[sl]).tolist(),
            "origin": np.stack(tr.origin[sl]).tolist(),
            "move_start": np.stack(tr.move_start[sl]).astype(np.float64).tolist(), "busy_until": busy.tolist(),
            "idle": np.stack(tr.idle[sl]).tolist(), "enroute": np.stack(tr.enroute[sl]).tolist(),
            "ontrip": np.stack(tr.ontrip[sl]).tolist(), "waiting": np.stack(tr.waiting[sl]).tolist(),
            "order_counts": np.stack(tr.order_counts[sl]).tolist(),
            "session_counts": np.stack(tr.session_counts[sl]).tolist(), "ledger": np.stack(tr.ledger[sl]).tolist(),
            "events": [e.tolist() for e in tr.events[sl]],
        }

    @app.get("/api/runs/{run_key}/slots")
    def get_slots(run_key: str) -> dict:
        run = _run(run_key)
        if run.trace is None:
            return {"n_slots": 0, "slots": []}
        out = summ.cell_matrix(run.trace)
        out["theta"] = run.theta
        out["budget_vnd"] = run.budget_vnd
        return out

    @app.get("/api/runs/{run_key}/compare")
    def compare_runs(run_key: str, ref: str = Query(..., description="id of the reference run (same seed)")) -> dict:
        a, b = _run(run_key), _run(ref)
        if a.kind == "sweep" or b.kind == "sweep" or a.status != "done" or b.status != "done":
            raise HTTPException(400, "cần hai lượt một θ đã xong")
        try:
            return summ.compare({"id": a.id, "name": a.name, "summary": a.summary, "config": a.config},
                                {"id": b.id, "name": b.name, "summary": b.summary, "config": b.config})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{run_key}/summary")
    def get_summary(run_key: str) -> dict:
        run = _run(run_key)
        if run.summary is not None:
            return {"status": run.status, **run.summary}
        if run.trace is None or run.cfg is None:
            return {"status": run.status, "kpis": None, "slot_series": None, "switch_events": [], "distribution": None}
        from sim.state import Clock
        clock = Clock.from_config(run.cfg)
        return {"status": run.status, "kpis": None, "slot_series": summ.slot_series(run.trace, clock),
                "switch_events": summ.switch_events(run.trace, clock, run.theta), "distribution": None}

    # --- finished experiments under runs/ --------------------------------------------

    runs_root = root / "runs"

    @app.get("/api/results/sweeps")
    def results_sweeps() -> dict:
        return {"sweeps": res.discover_sweeps(runs_root) if runs_root.exists() else []}

    @app.get("/api/results/policy_tables")
    def results_policy_tables() -> dict:
        return {"tables": res.policy_tables(runs_root) if runs_root.exists() else []}

    @app.get("/api/results/throughput")
    def results_throughput() -> dict:
        return {"curves": res.throughput_curves(runs_root) if runs_root.exists() else []}

    @app.get("/api/results/gte")
    def results_gte() -> dict:
        return {"gte": res.gte_tables(runs_root) if runs_root.exists() else []}

    @app.get("/api/results/evaluations")
    def results_evaluations() -> dict:
        return {"tables": res.evaluate_tables(runs_root) if runs_root.exists() else []}

    # --- front end -------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
