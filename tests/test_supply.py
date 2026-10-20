"""Supply (sim/supply.py) and repositioning (sim/reposition.py; hard rule 5)."""

import dataclasses

import numpy as np
import pytest

from sim import matching, reposition, supply, trips
from sim.config import load_config
from sim.population import describe_world, on_shift, online_by_hour
from sim.state import DriverStatus
from tests.conftest import ROOT
from tests.fakes import make_context, with_forbidden

DEFAULT = ROOT / "config" / "default.yaml"
OFFLINE, IDLE, ON_TRIP = int(DriverStatus.OFFLINE), int(DriverStatus.IDLE), int(DriverStatus.ON_TRIP)
REPOSITIONING = int(DriverStatus.REPOSITIONING)


def started(cfg):
    ctx = make_context(cfg)
    supply.init_drivers(ctx)
    return ctx


def idle_by_cell(ctx):
    d = ctx.drivers
    return np.bincount(d.cell[d.status == IDLE], minlength=ctx.n_cells)


@pytest.fixture(scope="module")
def cfg():
    return load_config(DEFAULT)


# --- the periodic shift rule ----------------------------------------------------


def test_on_shift_wraps_around_midnight(cfg):
    sched = dataclasses.replace(make_context(cfg).world.drivers,
                                shift_start_h=np.array([6.0, 20.0, 0.0]), shift_len_h=np.array([8.0, 8.0, 5.0]))
    at = lambda h: on_shift(sched, h * 3600.0).tolist()  # noqa: E731
    assert at(0) == [False, True, True]
    assert at(3.99) == [False, True, True] and at(4) == [False, False, True] and at(5) == [False, False, False]
    assert at(6) == [True, False, False] and at(13.99) == [True, False, False] and at(14) == [False, False, False]
    assert at(20) == [False, True, False] and at(24 + 3) == [False, True, True] and at(48 + 6) == [True, False, False]


def test_no_cold_start_at_midnight(cfg):
    # Drivers whose shift covers t = 0 start IDLE at their origin cell.
    ctx = started(cfg)
    d, sched = ctx.drivers, ctx.world.drivers
    online = on_shift(sched, 0.0)
    assert 0 < online.sum() < d.n
    np.testing.assert_array_equal(d.status == IDLE, online)
    np.testing.assert_array_equal(d.status == OFFLINE, ~online)
    np.testing.assert_array_equal(d.cell[online], sched.origin_cell[online])
    assert (d.cell[~online] == -1).all()
    assert (d.idle_since[online] == 0.0).all() and np.isnan(d.idle_since[~online]).all()
    # Their shift began yesterday and ends at start + len - 24 hours of day 0.
    np.testing.assert_allclose(d.shift_end_s[online], (sched.shift_start_h + sched.shift_len_h - 24.0)[online] * 3600)
    assert (d.shift_start_s[online] <= 0).all() and (d.shift_end_s[online] > 0).all()
    np.testing.assert_allclose(d.shift_start_s[~online], sched.shift_start_h[~online] * 3600)
    np.testing.assert_array_equal(ctx.cells.idle, idle_by_cell(ctx))


# --- a quiet fleet follows its schedule ------------------------------------------


def test_idle_fleet_follows_the_schedule_for_two_days(cfg):
    ctx = started(cfg)
    d, sched = ctx.drivers, ctx.world.drivers
    for tick in range(2 * 1440 + 1):
        t = tick * 60.0
        supply.update(ctx, t)
        if tick % 7 == 0:
            np.testing.assert_array_equal(d.status == IDLE, on_shift(sched, t), err_msg=f"tick {tick}")
            np.testing.assert_array_equal(ctx.cells.idle, idle_by_cell(ctx))
    assert set(np.unique(d.status)) <= {OFFLINE, IDLE}
    assert ctx.cells.idle.sum() == (d.status == IDLE).sum()


def test_shift_start_puts_the_driver_at_the_origin_cell(cfg):
    ctx = started(cfg)
    d, sched = ctx.drivers, ctx.world.drivers
    i = int(np.flatnonzero(d.status == OFFLINE)[0])
    start_s = sched.shift_start_h[i] * 3600
    first_tick = int(np.ceil(start_s / 60.0))
    for tick in range(first_tick + 1):
        supply.update(ctx, tick * 60.0)
        if tick < first_tick:
            assert d.status[i] == OFFLINE
    assert d.status[i] == IDLE and d.cell[i] == sched.origin_cell[i] and d.idle_since[i] == first_tick * 60.0


def test_nobody_leaves_before_shift_end(cfg):
    # early_exit_enabled = false -> no driver leaves before shift_end.
    assert cfg.supply.early_exit_enabled is False
    ctx = started(cfg)
    d = ctx.drivers
    n_left = 0
    for tick in range(1441):
        t = tick * 60.0
        before, end_before = d.status.copy(), d.shift_end_s.copy()
        supply.update(ctx, t)
        left = (before == IDLE) & (d.status == OFFLINE)
        assert (end_before[left] <= t).all()
        assert (end_before[left] > t - 60.0).all()            # ... and no later than the first tick after it
        n_left += int(left.sum())
    assert n_left >= d.n * 0.9                                 # nearly every driver ended a shift that day


# --- busy drivers ---------------------------------------------------------------


def test_busy_driver_leaves_only_after_dropoff(cfg):
    ctx = started(cfg)
    d, sched = ctx.drivers, ctx.world.drivers
    i = int(np.flatnonzero(d.status == IDLE)[0])
    end_s = float(d.shift_end_s[i])
    # Put the driver on a trip that ends 20 minutes after the shift does.
    ctx.cells.idle[d.cell[i]] -= 1
    d.status[i] = ON_TRIP
    dropoff_cell = (int(sched.origin_cell[i]) + 1) % ctx.n_cells
    dropoff_tick = int(np.ceil(end_s / 60.0)) + 20
    for tick in range(dropoff_tick):
        supply.update(ctx, tick * 60.0)
        assert d.status[i] == ON_TRIP                          # shift over, still busy: stays
    # trips.advance (step 1) makes the driver idle at the dropoff cell ...
    t = dropoff_tick * 60.0
    d.status[i], d.cell[i], d.idle_since[i] = IDLE, dropoff_cell, t
    ctx.cells.idle[dropoff_cell] += 1
    # ... and supply.update (step 2 of the same tick) takes them offline.
    supply.update(ctx, t)
    assert d.status[i] == OFFLINE and d.cell[i] == -1
    np.testing.assert_array_equal(ctx.cells.idle, idle_by_cell(ctx))
    # The next shift is tomorrow's, at the usual hours, back at the origin cell.
    whole_days = (d.shift_start_s[i] - sched.shift_start_h[i] * 3600) / 86400
    assert whole_days == pytest.approx(round(whole_days)) and d.shift_start_s[i] - t < 86400
    assert d.shift_end_s[i] - d.shift_start_s[i] == pytest.approx(sched.shift_len_h[i] * 3600)
    assert d.shift_start_s[i] > t
    for tick in range(dropoff_tick, int(np.ceil(d.shift_start_s[i] / 60.0)) + 1):
        supply.update(ctx, tick * 60.0)
    assert d.status[i] == IDLE and d.cell[i] == sched.origin_cell[i]


def test_busy_drivers_are_never_touched(cfg):
    ctx = started(cfg)
    d = ctx.drivers
    busy = np.flatnonzero(d.status == IDLE)[:5]
    np.subtract.at(ctx.cells.idle, d.cell[busy], 1)
    d.status[busy] = int(DriverStatus.EN_ROUTE)
    cells = d.cell[busy].copy()
    for tick in range(1441):
        supply.update(ctx, tick * 60.0)
    assert (d.status[busy] == int(DriverStatus.EN_ROUTE)).all()
    np.testing.assert_array_equal(d.cell[busy], cells)
    np.testing.assert_array_equal(ctx.cells.idle, idle_by_cell(ctx))


# --- always_on -------------------------------------------------------------------


def test_always_on_keeps_every_driver_online(cfg):
    ctx = started(load_config(DEFAULT, ["supply.shift_mode=always_on"]))
    d, sched = ctx.drivers, ctx.world.drivers
    assert (d.status == IDLE).all() and np.isinf(d.shift_end_s).all()
    np.testing.assert_array_equal(d.cell, sched.origin_cell)
    for tick in range(0, 2 * 1440, 5):
        supply.update(ctx, tick * 60.0)
    assert (d.status == IDLE).all() and ctx.cells.idle.sum() == d.n
    np.testing.assert_array_equal(ctx.cells.idle, np.bincount(sched.origin_cell, minlength=ctx.n_cells))


# --- contract and hard rule 5 ---------------------------------------------------


def test_supply_ignores_demand_policy_and_market_state(cfg):
    ctx = make_context(cfg)
    guarded = with_forbidden(ctx, "policy", "sessions", "orders", "monitor", "ledger", "layer", "slot_counters", "rng")
    supply.init_drivers(guarded)
    for tick in range(600):
        supply.update(guarded, tick * 60.0)
    assert (guarded.drivers.status == IDLE).any()


def test_early_exit_is_not_available_yet():
    ctx = make_context(load_config(DEFAULT, ["supply.early_exit_enabled=true"]))
    with pytest.raises(NotImplementedError, match="early_exit"):
        supply.init_drivers(ctx)


def test_tiny_fixture(tiny_cfg):
    ctx = started(tiny_cfg)
    assert ctx.drivers.n == 10
    for tick in range(181):
        supply.update(ctx, tick * 60.0)
    np.testing.assert_array_equal(ctx.cells.idle, idle_by_cell(ctx))


# --- world summary --------------------------------------------------------------


def test_world_summary(cfg):
    world = make_context(cfg).world
    text = describe_world(cfg, world)
    assert f"cells            {world.n_cells}" in text and f"riders           {world.riders.n}" in text
    assert f"fleet            {world.drivers.n} drivers" in text
    online = online_by_hour(world.drivers)
    assert online.shape == (24,) and online[0] == on_shift(world.drivers, 0.0).sum()
    assert "on shift         " + " ".join(f"{n:>3d}" for n in online) in text


# --- repositioning --------------------------------------------------------------

T0 = 12 * 3600.0


def park(ctx, driver_id, cell, idle_since):
    """An idle, on-shift driver in ``cell`` since ``idle_since``."""
    d = ctx.drivers
    d.status[driver_id], d.cell[driver_id], d.idle_since[driver_id] = IDLE, cell, idle_since
    d.shift_end_s[driver_id] = np.inf
    ctx.cells.idle[cell] += 1


def test_repositioning_never_reads_demand_or_snapshots(cfg):
    # A monitor that raises on any use; the same for sessions, orders, policy, budget.
    ctx = make_context(cfg)
    for i in range(20):
        park(ctx, i, i, idle_since=0.0)
    guarded = with_forbidden(ctx, "monitor", "sessions", "orders", "policy", "ledger", "layer", "slot_counters")
    reposition.step(guarded, T0)
    assert (guarded.drivers.status[:20] == REPOSITIONING).all()


def test_only_long_idle_drivers_move_to_an_adjacent_cell(cfg):
    ctx = make_context(cfg)
    space, limit_s = ctx.world.space, cfg.reposition.max_idle_min * 60
    park(ctx, 0, 5, idle_since=T0 - limit_s)             # exactly at the limit: moves
    park(ctx, 1, 5, idle_since=T0 - limit_s + 60)        # one minute short: stays
    reposition.step(ctx, T0)
    d = ctx.drivers
    assert d.status[0] == REPOSITIONING and d.status[1] == IDLE and d.cell[1] == 5
    target = int(d.cell[0])
    assert target in space.neighbors[5] and d.origin_cell[0] == 5
    assert d.busy_until[0] == pytest.approx(T0 + float(space.T[5, target, 12]) * 60)
    assert np.isnan(d.idle_since[0]) and d.reposition_count[0] == 1 and d.reposition_count[1] == 0
    assert ctx.cells.idle[5] == 1 and ctx.cells.idle.sum() == 1          # the mover is counted nowhere
    np.testing.assert_array_equal(ctx.cells.idle, idle_by_cell(ctx))


def test_arrival_makes_the_driver_idle_at_the_target(cfg):
    ctx = make_context(cfg)
    park(ctx, 0, 5, idle_since=0.0)
    reposition.step(ctx, T0)
    d = ctx.drivers
    target, arrive_at = int(d.cell[0]), float(d.busy_until[0])
    t = T0
    while d.status[0] == REPOSITIONING:
        t += 60
        trips.advance(ctx, t)                             # step 1 of a later tick
    assert t - 60 < arrive_at <= t
    assert d.status[0] == IDLE and d.cell[0] == target and d.idle_since[0] == pytest.approx(arrive_at)
    assert ctx.cells.idle[target] == 1 and ctx.cells.idle.sum() == 1
    # Idle again from the arrival time: the next move comes max_idle_min later, with a new stream key.
    reposition.step(ctx, t)
    assert d.status[0] == IDLE
    reposition.step(ctx, arrive_at + cfg.reposition.max_idle_min * 60)
    assert d.status[0] == REPOSITIONING and d.reposition_count[0] == 2 and d.origin_cell[0] == target


def test_repositioning_driver_takes_no_order(cfg):
    ctx = make_context(cfg)
    park(ctx, 0, 5, idle_since=0.0)
    reposition.step(ctx, T0)
    sid = 1
    s_idx = ctx.sessions.append(session_id=sid, pu_cell=5, do_cell=6, trip_noise=1.0)
    ctx.orders.append(order_id=sid, session_id=sid, pu_cell=5, do_cell=6, request_time_s=T0, session_idx=s_idx)
    ctx.cells.waiting[5] += 1
    matching.match(ctx, T0 + 60)
    assert ctx.orders.driver_id[0] == -1 and ctx.drivers.status[0] == REPOSITIONING


def test_target_follows_the_static_cell_weights(cfg):
    big = load_config(DEFAULT, ["supply.fleet_size=6000"])
    ctx = make_context(big)
    space, w = ctx.world.space, ctx.world.cell_weight
    for i in range(6000):
        park(ctx, i, 8, idle_since=0.0)
    reposition.step(ctx, T0)
    neighbors = space.neighbors[8]
    assert set(np.unique(ctx.drivers.cell)) <= set(neighbors.tolist())
    share = np.array([(ctx.drivers.cell == c).mean() for c in neighbors])
    np.testing.assert_allclose(share, w[neighbors] / w[neighbors].sum(), atol=0.02)


def test_move_depends_only_on_driver_and_move_count(cfg):
    # Stream DRIVER keyed by (driver_id, reposition_count): unaffected by the other drivers, the time or the policy.
    alone = make_context(cfg, on=False)
    park(alone, 7, 8, idle_since=0.0)
    reposition.step(alone, T0)
    crowd = make_context(cfg, on=True)
    for i in range(40):
        park(crowd, i, 8, idle_since=0.0)
    reposition.step(crowd, T0 + 5 * 3600)
    assert alone.drivers.cell[7] == crowd.drivers.cell[7]
    assert len(set(crowd.drivers.cell[:40].tolist())) > 1               # drivers do not all go the same way


def test_stay_mode_and_disabled_do_nothing():
    for override in ("reposition.mode=stay", "reposition.enabled=false"):
        ctx = make_context(load_config(DEFAULT, [override]))
        park(ctx, 0, 5, idle_since=0.0)
        reposition.step(ctx, T0)
        assert ctx.drivers.status[0] == IDLE and ctx.cells.idle[5] == 1


def test_busy_and_offline_drivers_are_not_repositioned(cfg):
    ctx = make_context(cfg)
    d = ctx.drivers
    d.status[0], d.cell[0], d.idle_since[0] = ON_TRIP, 5, 0.0
    reposition.step(ctx, T0)
    assert d.status[0] == ON_TRIP and (d.status[1:] == OFFLINE).all() and d.reposition_count.sum() == 0


def test_without_torus_border_drivers_stay_on_the_grid():
    ctx = make_context(load_config(DEFAULT, ["space.torus=false"]))
    space = ctx.world.space
    border = np.flatnonzero((space.neighbors < 0).any(axis=1))
    for i, cell in enumerate(border[:20]):
        park(ctx, i, int(cell), idle_since=0.0)
    reposition.step(ctx, T0)
    moved = ctx.drivers.cell[: min(20, len(border))]
    assert (moved >= 0).all() and (space.D[border[: len(moved)], moved] == 1).all()


def test_fleet_with_supply_and_repositioning_keeps_counters_consistent(cfg):
    # Steps 1, 2 and 9 over a quiet day: every driver is offline, idle or on the move, and counts agree.
    ctx = started(cfg)
    d = ctx.drivers
    for tick in range(1441):
        t = tick * 60.0
        trips.advance(ctx, t)
        supply.update(ctx, t)
        reposition.step(ctx, t)
        if tick % 20 == 0:
            np.testing.assert_array_equal(ctx.cells.idle, idle_by_cell(ctx))
            assert set(np.unique(d.status)) <= {OFFLINE, IDLE, REPOSITIONING}
            # A driver past shift end is offline or still on the way (leaves on arrival).
            assert not ((d.status == IDLE) & (d.shift_end_s <= t)).any()
    n_moves = int(d.reposition_count.sum())
    assert n_moves > d.n                                   # idle drivers keep moving all day

