"""Natural shocks of world W1 (sim/shocks.py)."""

import ast
from pathlib import Path

import numpy as np
import pytest

from sim import shocks as sk
from sim.config import load_config
from sim.engine import build_context, run
from sim.policies import make_policy
from sim.policies.base import SessionBatch
from sim.policies.fixed import FixedPolicy
from sim.population import build_world
from sim.rng import Rng
from sim.space import build_space
from sim.state import Clock, DriverStatus
from tests.conftest import ROOT

DEFAULT = ROOT / "config" / "default.yaml"
TINY = [DEFAULT, ROOT / "tests" / "fixtures" / "tiny.yaml"]
NEUTRAL = ["shocks.enabled=true", "shocks.day_demand.sigma=0", "shocks.rain.p_per_day=0",
           "shocks.events.venues=[[1, 0.0, 21.0, 23.0, 60.0, 2.0, 1.3]]", "shocks.attendance.p_work=1",
           "shocks.shift_jitter.sd_min=0"]


def _schedule(cfg):
    world = build_world(cfg, Rng.from_config(cfg))
    return sk.build_schedule(cfg, world, Rng.from_config(cfg), Clock.from_config(cfg)), world


# --- off / neutral = old model --------------------------------------------------------------------


def _day(layers, overrides, policy_on=False):
    cfg = load_config(layers, overrides)
    world = build_world(cfg, Rng.from_config(cfg))
    return run(cfg, world, FixedPolicy(policy_on, world.n_cells), Rng.from_config(cfg), log_level="full",
               enforce_budget=False)


def test_neutral_shocks_reproduce_the_model_without_shocks_number_for_number():
    # Shocks on but every effect neutral: same sessions, orders and aggregates as shocks off.
    off = _day(TINY, ["time.window_min=240"])
    neutral = _day(TINY, ["time.window_min=240", *NEUTRAL])
    for name in ("N_completed", "n_sessions", "n_requests", "n_cancelled", "V_profit_vnd"):
        assert getattr(off, name) == getattr(neutral, name), name
    for col in off.orders.names():
        x, y = off.orders.col(col), neutral.orders.col(col)
        assert np.array_equal(x, y, equal_nan=x.dtype.kind == "f"), col


def test_shocks_off_builds_nothing_and_keeps_24_travel_hours():
    cfg = load_config(DEFAULT)
    assert not cfg.shocks.enabled
    world = build_world(cfg, Rng.from_config(cfg))
    ctx = build_context(cfg, world, FixedPolicy(False, world.n_cells), Rng.from_config(cfg), enforce_budget=False)
    assert ctx.shocks is None and world.space.T.shape[2] == 24
    assert sk.travel_hour(ctx, 3600.0 * 15, 15) == 15


# --- same shocks under every policy (CRN) ---------------------------------------------------------


def test_every_policy_meets_the_same_shocks_and_the_same_sessions():
    cfg = load_config(TINY, ["shocks.enabled=true", "time.window_min=240", "shocks.rain.p_per_day=1",
                             "shocks.rain.start_hour_range=[1.5, 2.0]"])
    world = build_world(cfg, Rng.from_config(cfg))
    tables, sessions = [], []
    for name in ("all_off", "all_on", "threshold"):
        pol = make_policy(load_config(TINY, ["shocks.enabled=true", "time.window_min=240", "shocks.rain.p_per_day=1",
                                             "shocks.rain.start_hour_range=[1.5, 2.0]", f"policy.name={name}"]), world)
        ctx = build_context(cfg, world, pol, Rng.from_config(cfg), enforce_budget=False)
        tables.append(ctx.shocks.table())
        res = run(cfg, world, pol, Rng.from_config(cfg), log_level="full", enforce_budget=False, budget_vnd=None)
        sessions.append(res.sessions)
    for t in tables[1:]:
        for col in tables[0]:
            assert np.array_equal(tables[0][col], t[col]), col
    for s in sessions[1:]:
        for col in ("session_id", "pu_cell", "do_cell", "open_time_s"):
            np.testing.assert_array_equal(sessions[0].col(col), s.col(col), err_msg=col)


def test_schedule_depends_on_run_seed_and_venues_on_world_seed_only():
    a, _ = _schedule(load_config(DEFAULT, ["shocks.enabled=true", "meta.run_seed=0"]))
    b, _ = _schedule(load_config(DEFAULT, ["shocks.enabled=true", "meta.run_seed=1"]))
    c, _ = _schedule(load_config(DEFAULT, ["shocks.enabled=true", "meta.world_seed=7"]))
    np.testing.assert_array_equal(a.venue_cells, b.venue_cells)          # world property
    assert not np.array_equal(a.day_mult, b.day_mult)                    # a new seed is a new sample of days
    assert not np.array_equal(a.venue_cells, c.venue_cells)


def test_policies_cannot_read_the_shock_schedule():
    # Hard rule 3 spirit: the schedule is engine state. No policy module imports sim.shocks and the
    # observed batch carries no shock field.
    for path in (ROOT / "sim" / "policies").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom | ast.Import) for a in n.names}
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "sim.shocks" not in mods and "shocks" not in names, path.name
    assert not any("shock" in f or "rain" in f or "event" in f for f in SessionBatch.__dataclass_fields__)


# --- magnitudes follow the parameters -------------------------------------------------------------


def test_drawn_frequencies_and_sizes_follow_the_parameters():
    cfg = load_config(DEFAULT, ["shocks.enabled=true", "time.days_per_run=28", "time.window_min=null"])
    sch, world = _schedule(cfg)
    sh = cfg.shocks
    n = sch.n_days
    assert n == 29                                                       # 28 days + cool-down day
    rainy = ~np.isnan(sch.rain_start_s)
    assert abs(rainy.mean() - sh.rain.p_per_day) < 3 * np.sqrt(sh.rain.p_per_day * (1 - sh.rain.p_per_day) / n)
    start_h = (sch.rain_start_s[rainy] % 86400) / 3600
    length_h = (sch.rain_end_s[rainy] - sch.rain_start_s[rainy]) / 3600
    assert (start_h >= sh.rain.start_hour_range[0]).all() and (start_h <= sh.rain.start_hour_range[1]).all()
    assert (length_h >= sh.rain.duration_h_range[0]).all() and (length_h <= sh.rain.duration_h_range[1]).all()
    assert abs(np.mean(sch.day_mult) - 1.0) < 4 * sh.day_demand.sigma / np.sqrt(n)
    assert np.std(np.log(sch.day_mult)) == pytest.approx(sh.day_demand.sigma, rel=0.4)
    n_drivers = cfg.supply.fleet_size
    assert sch.works.mean() == pytest.approx(sh.attendance.p_work, abs=0.01)
    assert sch.rain_off.mean() == pytest.approx(sh.rain.driver_off_share, abs=0.01)
    assert sch.jitter_s.std() == pytest.approx(sh.shift_jitter.sd_min * 60, rel=0.05)
    assert sch.works.shape == (n, n_drivers)
    # Events: per venue kind, the share of venue-days with an event follows its probability.
    venue_kind = np.repeat(np.arange(len(sh.events.venues)), [int(r[0]) for r in sh.events.venues])
    for k, row in enumerate(sh.events.venues):
        n_venues, p_day, end_lo, end_hi = int(row[0]), row[1], row[2], row[3]
        idx = np.flatnonzero(venue_kind == k)
        evs = [e for e in sch.events if e.venue_idx in idx]
        share = len(evs) / (n_venues * n)
        assert abs(share - p_day) < 4 * np.sqrt(p_day * (1 - p_day) / (n_venues * n)) + 1e-9
        for e in evs:
            end_h = (e.start_s - e.day * 86400) / 3600
            assert end_lo <= end_h <= end_hi and e.end_s - e.start_s == pytest.approx(row[4] * 60)
    assert len(set(sch.venue_cells.tolist())) == len(sch.venue_cells)   # distinct venue cells


def test_demand_factor_combines_day_rain_and_event():
    cfg = load_config(DEFAULT, ["shocks.enabled=true"])
    sch, world = _schedule(cfg)
    sch.day_mult[:] = 1.1
    sch.rain_start_s[0], sch.rain_end_s[0] = 15 * 3600.0, 16 * 3600.0
    ev = sk.Event(day=0, venue_idx=0, cell=5, ring1=world.space.neighbors[5].astype(np.int64),
                  start_s=15.5 * 3600, end_s=16.5 * 3600, mult_venue=20.0, mult_ring1=3.0)
    sch.events = [ev]
    f = sch.demand_factor(15.75 * 3600)                                  # rain and event
    assert f[5] == pytest.approx(1.1 * 1.25 * 20.0) and f[ev.ring1[0]] == pytest.approx(1.1 * 1.25 * 3.0)
    others = np.setdiff1d(np.arange(world.n_cells), np.r_[5, ev.ring1])
    assert np.allclose(f[others], 1.1 * 1.25)
    assert np.allclose(sch.demand_factor(14 * 3600), 1.1)                # neither
    assert sch.raining(15 * 3600) and not sch.raining(16 * 3600)         # [start, end)


def test_rain_slows_every_travel_time_by_the_speed_factor():
    cfg = load_config(DEFAULT, ["shocks.enabled=true"])
    space = build_space(cfg)
    assert space.T.shape[2] == 48
    np.testing.assert_allclose(space.T[:, :, 24:], space.T[:, :, :24] / cfg.shocks.rain.speed_mult, rtol=1e-6)
    np.testing.assert_allclose(space.speed_kmh[24:], space.speed_kmh[:24] * cfg.shocks.rain.speed_mult)
    world = build_world(cfg, Rng.from_config(cfg))
    ctx = build_context(cfg, world, FixedPolicy(False, world.n_cells), Rng.from_config(cfg), enforce_budget=False)
    ctx.shocks.rain_start_s[0], ctx.shocks.rain_end_s[0] = 15 * 3600.0, 17 * 3600.0
    assert sk.travel_hour(ctx, 16 * 3600.0, 16) == 40 and sk.travel_hour(ctx, 18 * 3600.0, 18) == 18
    np.testing.assert_array_equal(sk.travel_hour(ctx, np.array([14.5, 16.0]) * 3600, np.array([14, 16])), [14, 40])


# --- supply side ----------------------------------------------------------------------------------


def test_absent_drivers_skip_the_day_and_jitter_moves_the_start():
    cfg = load_config(DEFAULT, ["shocks.enabled=true", "shocks.attendance.p_work=0.5"])
    world = build_world(cfg, Rng.from_config(cfg))
    ctx = build_context(cfg, world, FixedPolicy(False, world.n_cells), Rng.from_config(cfg), enforce_budget=False)
    from sim import supply
    supply.init_drivers(ctx)
    sch, sched, d = ctx.shocks, world.drivers, ctx.drivers
    later = np.flatnonzero((d.status == int(DriverStatus.OFFLINE)) & np.isfinite(d.shift_start_s))
    for i in later[:200].tolist():
        day = int(np.floor((d.shift_start_s[i] - sched.shift_start_h[i] * 3600 + 43200) / 86400))
        assert sch.works[day, i] and all(not sch.works[k, i] for k in range(day))   # first working day
        nominal = sched.shift_start_h[i] * 3600 + day * 86400
        assert d.shift_start_s[i] == pytest.approx(nominal + sch.jitter_s[day, i])
        assert d.shift_end_s[i] - d.shift_start_s[i] == pytest.approx(sched.shift_len_h[i] * 3600)


def test_flagged_drivers_stop_while_it_rains_and_come_back_after():
    cfg = load_config(TINY, ["shocks.enabled=true", "shocks.rain.driver_off_share=1", "time.window_min=240",
                             "supply.fleet_size=30", "shocks.attendance.p_work=1", "shocks.shift_jitter.sd_min=0"])
    world = build_world(cfg, Rng.from_config(cfg))
    ctx = build_context(cfg, world, FixedPolicy(False, world.n_cells), Rng.from_config(cfg), enforce_budget=False)
    from sim import supply
    supply.init_drivers(ctx)
    sch = ctx.shocks
    sch.rain_start_s[0], sch.rain_end_s[0] = 600.0, 1200.0
    supply.update(ctx, 300.0)
    online_before = int((ctx.drivers.status == int(DriverStatus.IDLE)).sum())
    assert online_before > 0
    supply.update(ctx, 600.0)                                           # rain: every idle driver stops
    assert (ctx.drivers.status == int(DriverStatus.IDLE)).sum() == 0 and ctx.cells.idle.sum() == 0
    assert sch.paused.sum() == online_before
    supply.update(ctx, 1200.0)                                          # rain over: back where they were
    assert (ctx.drivers.status == int(DriverStatus.IDLE)).sum() == online_before and not sch.paused.any()
    assert ctx.cells.idle.sum() == online_before


def test_a_rainy_w1_day_runs_and_keeps_the_invariants():
    cfg = load_config(TINY, ["shocks.enabled=true", "time.window_min=240", "shocks.rain.p_per_day=1",
                             "shocks.rain.start_hour_range=[1.5, 2.0]", "shocks.rain.driver_off_share=0.5"])
    world = build_world(cfg, Rng.from_config(cfg))
    res = run(cfg, world, FixedPolicy(True, world.n_cells), Rng.from_config(cfg), log_level="full", budget_vnd=5000.0,
              enforce_budget=True)
    assert res.N_completed > 0
    o = res.orders
    done = o.col("status") == 2
    assert (o.col("trip_km")[done] > 0).all()


# --- meta/shocks -------------------------------------------------------------------------------------


def test_full_log_runs_write_meta_shocks(tmp_path):
    import pandas as pd

    from sim import runner
    w1 = load_config(TINY, ["shocks.enabled=true", "policy.name=experiment", "generate.days=2", "time.window_min=null",
                            "time.days_per_run=1", "shocks.rain.p_per_day=1"])
    runner.generate(w1, tmp_path / "w1", log_level="full")
    table = pd.read_parquet(tmp_path / "w1" / "meta" / "shocks.parquet")
    sch, _ = _schedule(load_config(TINY, ["shocks.enabled=true", "generate.days=2", "time.window_min=null",
                                          "time.days_per_run=2", "shocks.rain.p_per_day=1"]))
    expected = sch.table()
    assert len(table) == len(expected["day"]) > 0
    for col in ("day", "cell", "start_s", "end_s", "n_drivers_off"):
        np.testing.assert_array_equal(table[col].to_numpy(), expected[col], err_msg=col)
    assert set(table["kind"]) >= {"day_demand", "attendance", "rain"}
    assert (table["run_id"] == pd.read_parquet(tmp_path / "w1" / "meta" / "run_metadata.parquet")["run_id"][0]).all()
    w0 = load_config(TINY, ["policy.name=experiment", "generate.days=1"])
    runner.generate(w0, tmp_path / "w0", log_level="full")
    empty = pd.read_parquet(tmp_path / "w0" / "meta" / "shocks.parquet")
    assert len(empty) == 0 and list(empty.columns)[:2] == ["run_id", "seed"]
