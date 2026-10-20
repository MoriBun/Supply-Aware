"""Dashboard (dashboard/): the traced loop equals the engine, frames are consistent, the API serves a run.

Fast tests on the tiny configuration. The API tests need ``fastapi`` (extras ``[dashboard]``)
and are skipped without it; the simulator itself is never imported from ``dashboard`` in ``sim/``.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pytest

from sim import engine
from sim.config import Config
from sim.policies import make_policy
from sim.population import build_world
from sim.rng import Rng
from sim.state import DriverStatus, OrderStatus

from dashboard.geometry import torus_display_vectors, world_geometry
from dashboard.trace import EVENT_CODE, Trace, run_traced

ROOT = Path(__file__).resolve().parents[1]
COMPARED = ("N_completed", "V_profit_vnd", "voucher_spent_vnd", "n_sessions", "n_requests", "n_abandoned", "n_cancelled",
            "mean_pickup_eta_min", "share_cells_off", "n_switches_per_cell_day", "mean_slack", "sim_end_s",
            "n_truncated_orders")


def _setup(cfg: Config, theta: float = 0.5):
    rng = Rng.from_config(cfg)
    world = build_world(cfg, rng)
    policy = make_policy(cfg, world, rng=rng, theta=theta, kappa=-math.inf)
    return world, policy, rng


def test_traced_run_matches_engine(tiny_cfg: Config) -> None:
    """Recording frames must not change a single number of the run (same steps, same order)."""
    world, policy, rng = _setup(tiny_cfg)
    expected = engine.run(tiny_cfg, world, policy, rng, log_level="full", budget_vnd=50.0, enforce_budget=True)
    world, policy, rng = _setup(tiny_cfg)
    result, trace = run_traced(tiny_cfg, world, policy, rng, budget_vnd=50.0, enforce_budget=True)
    for name in COMPARED:
        a, b = getattr(expected, name), getattr(result, name)
        assert a == b or (isinstance(a, float) and math.isnan(a) and math.isnan(b)), name
    for col in expected.sessions.names():
        x, y = expected.sessions.col(col), result.sessions.col(col)
        assert np.array_equal(x, y, equal_nan=x.dtype.kind == "f"), col
    for col in expected.orders.names():
        x, y = expected.orders.col(col), result.orders.col(col)
        assert np.array_equal(x, y, equal_nan=x.dtype.kind == "f"), col
    # Every published slot has a snapshot; a run may end in cool-down inside a slot that started but was
    # never published (longer trips on the Hanoi grid, H7.1), so the trace can hold one more slot.
    published = sum(rec["published"] for rec in trace.slots)
    assert len(expected.snapshots) == len(result.snapshots) == published
    assert len(trace.slots) - published in (0, 1) and all(rec["published"] for rec in trace.slots[:published])
    assert trace.n_frames == int(result.sim_end_s // tiny_cfg.time.tick_s)


def test_frames_consistent_with_counters(tiny_cfg: Config) -> None:
    """Per-cell idle / en-route counts of a frame equal the driver statuses of that frame; counters are monotone."""
    world, policy, rng = _setup(tiny_cfg)
    _, trace = run_traced(tiny_cfg, world, policy, rng, budget_vnd=50.0, enforce_budget=True)
    a = trace.frame_arrays()
    idle, enroute = int(DriverStatus.IDLE), int(DriverStatus.EN_ROUTE)
    for k in range(trace.n_frames):
        assert (a["status"][k] == idle).sum() == a["idle"][k].sum()
        assert (a["status"][k] == enroute).sum() == a["enroute"][k].sum()
        assert (a["cell"][k][a["status"][k] != int(DriverStatus.OFFLINE)] >= 0).all()
    assert (np.diff(a["session_counts"], axis=0) >= 0).all()
    assert (np.diff(a["order_counts"][:, int(OrderStatus.COMPLETED)]) >= 0).all()
    # ledger: used <= limit whenever a limit exists (hard rule 8, seen tick by tick)
    led = a["ledger"]
    has_limit = led[:, 4] >= 0
    assert (led[has_limit, 1:4].sum(axis=1) <= led[has_limit, 4]).all()
    # events: completions in the frames equal the completed orders
    ev = a["events"]
    assert (ev[:, 0] == EVENT_CODE["complete"]).sum() == a["order_counts"][-1][int(OrderStatus.COMPLETED)]
    assert (ev[:, 0] == EVENT_CODE["request"]).sum() == a["order_counts"][-1].sum()
    # a moving driver always has a leg that ends after it starts
    moving = a["status"] >= enroute
    assert (a["busy_until"][moving] >= a["move_start"][moving]).all()
    # round trip through the stacked arrays
    rebuilt = Trace.from_arrays(tiny_cfg, trace_clock(tiny_cfg), a, trace.slots)
    assert rebuilt.n_frames == trace.n_frames and len(rebuilt.events) == trace.n_frames
    assert all(np.array_equal(x, y) for x, y in zip(rebuilt.events, trace.events))


def trace_clock(cfg: Config):
    from sim.state import Clock
    return Clock.from_config(cfg)


def test_gte_discovery_excludes_budgeted_and_unpaired_runs(tmp_path: Path) -> None:
    """The unrestricted GTE cannot include budgeted all-on or unmatched seeds."""
    import pandas as pd
    from dashboard.results import gte_tables

    target = tmp_path / "mixed" / "results"
    target.mkdir(parents=True)
    rows = [
        ("all_on", 0, 110, None), ("all_off", 0, 100, None),
        ("all_on", 1, 130, None), ("all_off", 1, 100, None),
        ("all_on", 2, 9999, None),
        ("all_on", 0, 500, 50), ("all_off", 0, 100, 50),
    ]
    table = pd.DataFrame(rows, columns=["policy", "seed", "N_completed", "budget_B_vnd"])
    table["V_profit_vnd"] = 0.0
    table["voucher_spent_vnd"] = 0.0
    table.to_parquet(target / "policy_results.parquet", index=False)
    got = gte_tables(tmp_path)
    assert len(got) == 1
    assert got[0]["GTE"] == 20.0 and got[0]["GTE_se"] == 10.0
    assert got[0]["N_on"] == 120.0 and got[0]["N_off"] == 100.0
    assert got[0]["n_seeds"] == 2
    table.loc[:, "budget_B_vnd"] = 50.0
    table.to_parquet(target / "policy_results.parquet", index=False)
    assert gte_tables(tmp_path) == []


def test_torus_display_vectors_are_shortest_images(tiny_cfg: Config) -> None:
    world, _, _ = _setup(tiny_cfg)
    space = world.space
    disp = torus_display_vectors(space.cell_q, space.cell_r, space.radius, space.torus)
    assert disp.shape == (space.n_cells, space.n_cells, 2)
    assert np.allclose(disp[np.arange(space.n_cells), np.arange(space.n_cells)], 0.0)
    # neighbours are exactly one centre-to-centre distance (sqrt(3) in hex units) away on screen
    for c in range(space.n_cells):
        for nb in space.neighbors[c]:
            if nb >= 0:
                assert math.isclose(math.hypot(*disp[c, nb]), math.sqrt(3.0), rel_tol=1e-6)
    geo = world_geometry(tiny_cfg, world)
    assert len(geo["cells"]) == space.n_cells and geo["n_drivers"] == tiny_cfg.supply.fleet_size


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_api_runs_a_tiny_run(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard.server import create_app

    app = create_app(ROOT, runs_dir=tmp_path)
    client = TestClient(app)
    assert client.get("/api/config/defaults").status_code == 200
    bad = client.post("/api/config/validate", json={"overrides": ["policy.threshold.nope=1"]}).json()
    assert bad["ok"] is False
    body = {"name": "tiny", "layers": ["tests/fixtures/tiny.yaml"], "overrides": ["policy.threshold.theta=0.5"], "kappa": -1.0}
    run = client.post("/api/runs", json=body).json()
    rid = run["id"]
    for _ in range(600):
        info = client.get(f"/api/runs/{rid}").json()
        if info["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert info["status"] == "done", info.get("error")
    assert info["kappa"] == -1.0 and info["kappa_pilots"] == 0
    assert info["budget_vnd"] > 0 and info["n_frames"] > 0 and len(info["geometry"]["cells"]) == 7
    frames = client.get(f"/api/runs/{rid}/frames", params={"start": 0, "count": 1000}).json()
    assert frames["frames"] == info["n_frames"] and frames["done"] is True
    assert len(frames["status"][0]) == 10 and len(frames["events"]) == frames["frames"]
    slots = client.get(f"/api/runs/{rid}/slots").json()
    assert slots["n_slots"] == info["n_slots"] and len(slots["promo_on"][0]) == 7
    summary = client.get(f"/api/runs/{rid}/summary").json()
    assert summary["kpis"]["N_completed"] >= 0 and summary["distribution"]["budget_periods"]
    rev = summary["revenue"]["total"]                                       # money of the window's completed trips
    assert rev["trips"] == summary["kpis"]["N_completed"]
    assert rev["platform_profit_vnd"] == pytest.approx(summary["kpis"]["V_profit_vnd"], rel=1e-6)
    assert rev["voucher_value_vnd"] == pytest.approx(summary["kpis"]["voucher_spent_vnd"], rel=1e-6)
    assert rev["net_fare_vnd"] == pytest.approx(rev["gross_fare_vnd"] - rev["voucher_value_vnd"], rel=1e-5)
    assert rev["platform_profit_vnd"] == pytest.approx(rev["net_fare_vnd"] - rev["driver_pay_vnd"], rel=1e-5)
    arms = {r["arm"]: r for r in summary["revenue"]["by_arm"]}
    assert arms["voucher"]["trips"] + arms["no_voucher"]["trips"] == rev["trips"] and arms["no_voucher"]["voucher_value_vnd"] == 0
    assert sum(r["trips"] for r in summary["revenue"]["by_hour"]) == rev["trips"]
    # paired comparison with an all_off run of the same seed (CRN): the deltas are plain differences
    off = client.post("/api/runs", json={"name": "tiny off", "layers": ["tests/fixtures/tiny.yaml"],
                                         "overrides": ["policy.name=all_off", "budget.enforce=false"]}).json()
    for _ in range(600):
        info_off = client.get(f"/api/runs/{off['id']}").json()
        if info_off["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert info_off["status"] == "done", info_off.get("error")
    cmp_ = client.get(f"/api/runs/{rid}/compare", params={"ref": off["id"]}).json()
    assert cmp_["same_seed"] and cmp_["same_world"] and not cmp_["same_budget"]
    assert cmp_["delta"]["N_completed"] == summary["kpis"]["N_completed"] - info_off["kpis"]["N_completed"]
    assert cmp_["delta"]["voucher_value_vnd"] == pytest.approx(rev["voucher_value_vnd"], rel=1e-6)
    assert client.get(f"/api/runs/{rid}/compare", params={"ref": "nope"}).status_code == 404
    assert (tmp_path / rid / "results" / "policy_results.parquet").exists()
    assert (tmp_path / rid / "observed" / "sessions.parquet").exists()
    assert (tmp_path / rid / "dashboard" / "trace.npz").exists()
    # a fresh app reloads the saved run with its frames and summary
    client2 = TestClient(create_app(ROOT, runs_dir=tmp_path))
    listed = client2.get("/api/runs").json()["runs"]
    assert {r["id"] for r in listed} == {rid, off["id"]}
    assert next(r for r in listed if r["id"] == rid)["n_frames"] == info["n_frames"] and all(r["has_revenue"] for r in listed)
    again = client2.get(f"/api/runs/{rid}/frames", params={"start": 0, "count": 5}).json()
    assert again["t"] == frames["t"][:5] and again["events"][0] == frames["events"][0]
    assert client2.get(f"/api/runs/{rid}/summary").json()["kpis"]["N_completed"] == summary["kpis"]["N_completed"]
    assert client2.get("/api/results/sweeps").status_code == 200


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_api_sweep_finds_theta_star(tmp_path: Path) -> None:
    """A sweep from the API builds the jobs of runner.sweep_theta, runs them in a pool and reports theta*."""
    pytest.importorskip("fastapi")
    import pandas as pd
    from fastapi.testclient import TestClient

    from dashboard.server import create_app
    from dashboard.sweep import aggregate

    client = TestClient(create_app(ROOT, runs_dir=tmp_path))
    body = {"name": "tiny sweep", "layers": ["tests/fixtures/tiny.yaml"], "theta_grid": [0.0, 0.5, 1.0], "n_seeds": 2,
            "n_procs": 2}
    run = client.post("/api/sweeps", json=body).json()
    assert run["kind"] == "sweep" and run["sweep"]["total"] == 6
    rid = run["id"]
    for _ in range(1500):
        info = client.get(f"/api/runs/{rid}").json()
        if info["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert info["status"] == "done", info.get("error")
    sweep = client.get(f"/api/runs/{rid}/sweep").json()
    assert len(sweep["rows"]) == 6 and sweep["done"] == 6
    agg = sweep["aggregate"]
    assert agg["argmax_theta"] in (0.0, 0.5, 1.0) and agg["star"]["n_seeds"] == 2
    assert all(p["n"] == 2 for p in agg["per_theta"])
    assert len(sweep["kappas"]) == 3 and all(k["n_pilots"] >= 1 for k in sweep["kappas"])
    # same B for every theta, and the on-disk tables are the ones of mode sweep_theta
    assert len({r["budget_B_vnd"] for r in sweep["rows"]}) == 1
    table = pd.read_parquet(tmp_path / rid / "results" / "policy_results.parquet")
    assert len(table) == 6 and set(table["theta"]) == {0.0, 0.5, 1.0} and table["run_id"].str.startswith("sweep_theta-").all()
    star = pd.read_parquet(tmp_path / rid / "results" / "theta_sweep.parquet")
    assert float(star.loc[star["is_argmax"], "theta"].iloc[0]) == agg["argmax_theta"]
    # the aggregate recomputed from the rows matches what the run reported
    again = aggregate(sweep["rows"], sweep["theta_grid"])
    assert again["argmax_theta"] == agg["argmax_theta"] and again["star"]["lo"] == agg["star"]["lo"]
    # reload from disk: listed as a sweep with its brief, rows readable
    client2 = TestClient(create_app(ROOT, runs_dir=tmp_path))
    listed = client2.get("/api/runs").json()["runs"][0]
    assert listed["kind"] == "sweep" and listed["sweep_summary"]["argmax_theta"] == agg["argmax_theta"]
    assert len(client2.get(f"/api/runs/{rid}/sweep").json()["rows"]) == 6


def test_api_defaults_offer_both_worlds_and_no_figure_endpoint(tmp_path: Path) -> None:
    """S10 (T-49): W0/W1 presets with existing config layers, scope city accepted, the figure gallery is gone."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard.server import create_app

    client = TestClient(create_app(ROOT, runs_dir=tmp_path))
    d = client.get("/api/config/defaults").json()
    presets = {p["key"]: p for p in d["presets"]}
    assert {"default", "w1", "tiny"} <= set(presets)
    for p in presets.values():
        assert all((ROOT / layer).exists() for layer in p["layers"])
    assert "analysis.uplift:tau_x_dr_all_per_kvnd" in d["score_functions"]
    ok = client.post("/api/config/validate", json={"overrides": ["policy.threshold.scope=city"], "layers": presets["w1"]["layers"]}).json()
    assert ok["ok"] and ok["n_cells"] == 61 and ok["fleet_size"] == 1278
    assert client.get("/api/results/figures").status_code == 404
