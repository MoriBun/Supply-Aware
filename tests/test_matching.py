"""Matching (sim/matching.py): FIFO, same cell first, nearest ring, pickup limit, tie-breaks."""

import numpy as np
import pytest

from sim import matching
from sim.config import load_config
from sim.state import DriverStatus, OrderStatus
from tests.conftest import ROOT
from tests.fakes import make_context, with_forbidden

DEFAULT = ROOT / "config" / "default.yaml"
IDLE, EN_ROUTE, OFFLINE = int(DriverStatus.IDLE), int(DriverStatus.EN_ROUTE), int(DriverStatus.OFFLINE)
WAITING, MATCHED = int(OrderStatus.WAITING), int(OrderStatus.MATCHED)
T0 = 18 * 3600.0          # 18:00, the slowest hour


def idle_driver(ctx, driver_id, cell, idle_since=0.0, shift_end_s=np.inf):
    d = ctx.drivers
    d.status[driver_id], d.cell[driver_id] = IDLE, cell
    d.idle_since[driver_id], d.shift_end_s[driver_id] = idle_since, shift_end_s
    ctx.cells.idle[cell] += 1
    return driver_id


def waiting_order(ctx, pu_cell, do_cell=None, request_time_s=T0, trip_noise=1.0):
    """A booked session and its Waiting order, as steps 4-6 would leave them."""
    do_cell = pu_cell if do_cell is None else do_cell
    sid = 1000 + ctx.sessions.n
    s_idx = ctx.sessions.append(session_id=sid, pu_cell=pu_cell, do_cell=do_cell, open_time_s=request_time_s,
                                trip_noise=trip_noise, requested=True)
    o_idx = ctx.orders.append(order_id=sid, session_id=sid, pu_cell=pu_cell, do_cell=do_cell,
                              request_time_s=request_time_s, session_idx=s_idx)
    ctx.sessions.order_idx[s_idx] = o_idx
    ctx.cells.waiting[pu_cell] += 1
    return o_idx


def status(ctx):
    return ctx.orders.col("status").tolist()


@pytest.fixture
def ctx():
    # Unit tests on the old grid (37 cells, 0.75 km), where every cell reaches every cell within
    # max_pickup_eta_min; the Hanoi grid of default.yaml is covered by the integration tests.
    return make_context(load_config(DEFAULT, ["space.grid_radius=3", "space.edge_km=0.75"]))


@pytest.fixture
def space(ctx):
    return ctx.world.space


# --- FIFO, nearest ring, pickup limit, tie-breaks --------------------------------


def test_fifo_older_order_gets_the_only_driver(ctx):
    old = waiting_order(ctx, 4, request_time_s=T0 - 120)
    new = waiting_order(ctx, 4, request_time_s=T0 - 60)
    driver = idle_driver(ctx, 0, 4)
    matching.match(ctx, T0)
    assert status(ctx) == [MATCHED, WAITING]
    assert ctx.orders.driver_id[old] == driver and ctx.orders.driver_id[new] == -1
    assert ctx.cells.waiting[4] == 1


def test_fifo_holds_across_cells_competing_for_one_driver(ctx, space):
    a, b = int(space.neighbors[0, 0]), int(space.neighbors[0, 1])     # two cells next to cell 0
    first = waiting_order(ctx, a, request_time_s=T0 - 120)
    second = waiting_order(ctx, b, request_time_s=T0 - 60)
    idle_driver(ctx, 0, 0)
    matching.match(ctx, T0)
    assert ctx.orders.status[first] == MATCHED and ctx.orders.status[second] == WAITING


def test_driver_in_the_same_cell_beats_an_adjacent_one(ctx, space):
    order = waiting_order(ctx, 0)
    near = idle_driver(ctx, 0, int(space.neighbors[0, 0]), idle_since=0.0)     # idle for longer, but next door
    here = idle_driver(ctx, 1, 0, idle_since=T0 - 1)
    matching.match(ctx, T0)
    assert ctx.orders.driver_id[order] == here and ctx.orders.driver_origin_cell[order] == 0
    assert ctx.orders.pickup_eta_min[order] == pytest.approx(space.eta_in(1, 18))
    assert ctx.drivers.status[near] == IDLE


def test_empty_cell_takes_the_nearest_ring_never_skipping_one(ctx, space):
    ring1, ring2 = space.rings[0][0], space.rings[0][1]
    order = waiting_order(ctx, 0)
    far = idle_driver(ctx, 0, int(ring2[0]), idle_since=0.0)
    close = idle_driver(ctx, 1, int(ring1[-1]), idle_since=T0 - 1)
    matching.match(ctx, T0)
    assert ctx.orders.driver_id[order] == close and ctx.orders.driver_origin_cell[order] == ring1[-1]
    assert ctx.orders.pickup_eta_min[order] == pytest.approx(float(space.T[ring1[-1], 0, 18]))
    assert ctx.drivers.status[far] == IDLE
    # With ring 1 empty the search goes on to ring 2.
    second = waiting_order(ctx, 0)
    matching.match(ctx, T0 + 60)
    assert ctx.orders.driver_id[second] == far and ctx.orders.driver_origin_cell[second] == ring2[0]


def test_no_match_beyond_max_pickup_eta_order_waits_for_a_later_tick():
    base = make_context(load_config(DEFAULT)).world.space
    t1, t2 = float(base.T[base.rings[0][0][0], 0, 18]), float(base.T[base.rings[0][1][0], 0, 18])
    ctx = make_context(load_config(DEFAULT, [f"matching.max_pickup_eta_min={(t1 + t2) / 2}"]))
    space = ctx.world.space
    order = waiting_order(ctx, 0)
    idle_driver(ctx, 0, int(space.rings[0][1][0]))                   # ring 2: beyond the limit
    matching.match(ctx, T0)
    assert ctx.orders.status[order] == WAITING and ctx.drivers.status[0] == IDLE and ctx.cells.waiting[0] == 1
    idle_driver(ctx, 1, int(space.rings[0][0][0]))                   # a driver appears within reach
    matching.match(ctx, T0 + 60)
    assert ctx.orders.status[order] == MATCHED and ctx.orders.driver_id[order] == 1
    assert ctx.orders.matched_time_s[order] == T0 + 60


def test_only_idle_on_shift_drivers_are_matched(ctx):
    order = waiting_order(ctx, 0)
    d = ctx.drivers
    d.status[0], d.cell[0] = EN_ROUTE, 0                              # busy
    d.status[1], d.cell[1] = OFFLINE, 0                               # not working
    off_shift = idle_driver(ctx, 2, 0, shift_end_s=T0)                # idle, but the shift is over
    matching.match(ctx, T0)
    assert ctx.orders.status[order] == WAITING
    assert d.status[off_shift] == IDLE and d.status[0] == EN_ROUTE and d.status[1] == OFFLINE
    on_shift = idle_driver(ctx, 3, 0, shift_end_s=T0 + 1)
    matching.match(ctx, T0)
    assert ctx.orders.driver_id[order] == on_shift


def test_tie_goes_to_the_longest_idle_driver_then_smallest_id(ctx):
    orders = [waiting_order(ctx, 0) for _ in range(3)]
    idle_driver(ctx, 5, 0, idle_since=300.0)
    idle_driver(ctx, 9, 0, idle_since=100.0)
    idle_driver(ctx, 2, 0, idle_since=300.0)
    matching.match(ctx, T0)
    assert ctx.orders.driver_id[orders].tolist() == [9, 2, 5]


# --- ETA and bookkeeping ---------------------------------------------------------


def test_eta_in_uses_the_idle_count_before_each_driver_is_taken(ctx, space):
    orders = [waiting_order(ctx, 7) for _ in range(3)]
    for i in range(3):
        idle_driver(ctx, i, 7)
    matching.match(ctx, T0)
    np.testing.assert_allclose(ctx.orders.pickup_eta_min[orders], [space.eta_in(k, 18) for k in (3, 2, 1)], rtol=1e-6)
    assert ctx.cells.idle[7] == 0 and ctx.cells.enroute[7] == 3 and ctx.cells.waiting[7] == 0


def test_ring_tie_goes_to_the_smallest_cell_id(ctx, space):
    ring1 = space.rings[0][0]
    order = waiting_order(ctx, 0)
    idle_driver(ctx, 0, int(ring1[3]))
    idle_driver(ctx, 1, int(ring1[1]))
    matching.match(ctx, T0)
    assert ctx.orders.driver_origin_cell[order] == ring1[1] and ctx.orders.driver_id[order] == 1


def test_match_writes_order_driver_and_counters(ctx, space):
    source, rider_cell, dest = int(space.rings[3][0][0]), 3, 20
    order = waiting_order(ctx, rider_cell, do_cell=dest, trip_noise=1.25)
    driver = idle_driver(ctx, 4, source, idle_since=50.0)
    matching.match(ctx, T0)
    o, d = ctx.orders, ctx.drivers
    eta = float(space.T[source, rider_cell, 18])
    assert o.status[order] == MATCHED and o.driver_id[order] == driver and o.driver_origin_cell[order] == source
    assert o.matched_time_s[order] == T0 and o.pickup_eta_min[order] == pytest.approx(eta)
    assert o.trip_time_min[order] == pytest.approx(float(space.T[rider_cell, dest, 18]) * 1.25, rel=1e-6)
    assert o.trip_km[order] == pytest.approx(float(space.T[rider_cell, dest, 18]) / 60 * space.speed_kmh[18], rel=1e-6)
    assert np.isnan(o.pickup_time_s[order]) and np.isnan(o.dropoff_time_s[order])     # written by trips.advance
    assert d.status[driver] == EN_ROUTE and d.origin_cell[driver] == source and d.cell[driver] == rider_cell
    assert d.busy_until[driver] == pytest.approx(T0 + eta * 60) and d.order[driver] == order
    assert np.isnan(d.idle_since[driver])
    assert ctx.cells.idle[source] == 0 and ctx.cells.enroute[rider_cell] == 1 and ctx.cells.waiting[rider_cell] == 0
    acc = ctx.slot_counters
    assert acc.n_matched[rider_cell] == 1 and acc.n_pickup_eta[rider_cell] == 1
    assert acc.sum_pickup_eta_min[rider_cell] == pytest.approx(eta)
    assert acc.n_matched.sum() == 1


def test_trip_time_uses_the_hour_of_pickup(ctx, space):
    # Matched at 16:59 with a pickup after 17:00: the trip is timed with hour 17 speeds.
    t = 17 * 3600.0 - 60.0
    source = int(space.rings[3][-1][0])
    order = waiting_order(ctx, 3, do_cell=20, request_time_s=t)
    idle_driver(ctx, 0, source)
    matching.match(ctx, t)
    assert ctx.orders.pickup_eta_min[order] == pytest.approx(float(space.T[source, 3, 16]))
    assert t + ctx.orders.pickup_eta_min[order] * 60 >= 17 * 3600
    assert ctx.orders.trip_time_min[order] == pytest.approx(float(space.T[3, 20, 17]))
    assert space.T[3, 20, 17] != space.T[3, 20, 16]


def test_match_agrees_with_the_quoted_eta(ctx, space):
    # Same rule as the quote of step 5.
    idle_driver(ctx, 0, int(space.rings[11][1][2]))
    idle_driver(ctx, 1, int(space.rings[11][2][0]))
    quoted, no_supply = space.quote_eta(11, 18, ctx.cells.idle)
    order = waiting_order(ctx, 11)
    matching.match(ctx, T0)
    assert not no_supply and ctx.orders.pickup_eta_min[order] == pytest.approx(quoted)


def test_orders_without_reachable_supply_keep_waiting(ctx):
    orders = [waiting_order(ctx, c) for c in (0, 5, 5, 9)]
    matching.match(ctx, T0)                                   # no driver at all
    assert status(ctx) == [WAITING] * 4 and ctx.slot_counters.n_matched.sum() == 0
    idle_driver(ctx, 0, 5)
    idle_driver(ctx, 1, 5)
    matching.match(ctx, T0 + 60)                              # two drivers for four orders: the two oldest win
    assert ctx.orders.status[orders].tolist() == [MATCHED, MATCHED, WAITING, WAITING]
    assert ctx.cells.waiting.sum() == 2 and ctx.cells.idle.sum() == 0


def test_already_matched_orders_and_busy_drivers_are_left_alone(ctx):
    first = waiting_order(ctx, 2)
    idle_driver(ctx, 0, 2)
    matching.match(ctx, T0)
    busy_until = ctx.drivers.busy_until[0]
    second = waiting_order(ctx, 2)
    matching.match(ctx, T0 + 60)
    assert ctx.orders.status[first] == MATCHED and ctx.orders.matched_time_s[first] == T0
    assert ctx.orders.status[second] == WAITING and ctx.drivers.busy_until[0] == busy_until


def test_counters_stay_consistent_under_load(ctx):
    gen = np.random.default_rng(5)
    for i in range(60):
        idle_driver(ctx, i, int(gen.integers(ctx.n_cells)), idle_since=float(gen.integers(1000)))
    for _ in range(150):
        waiting_order(ctx, int(gen.integers(ctx.n_cells)), do_cell=int(gen.integers(ctx.n_cells)))
    matching.match(ctx, T0)
    o, d = ctx.orders, ctx.drivers
    matched = o.col("status") == MATCHED
    assert matched.sum() == 60 and (d.status[:60] == EN_ROUTE).all()          # every driver is within reach
    assert len(set(o.col("driver_id")[matched].tolist())) == 60                # one order per driver
    np.testing.assert_array_equal(ctx.cells.waiting, np.bincount(o.col("pu_cell")[~matched], minlength=ctx.n_cells))
    np.testing.assert_array_equal(ctx.cells.enroute, np.bincount(o.col("pu_cell")[matched], minlength=ctx.n_cells))
    assert ctx.cells.idle.sum() == 0 and (ctx.cells.idle >= 0).all()
    assert (o.col("pickup_eta_min")[matched] <= ctx.cfg.matching.max_pickup_eta_min).all()
    # Every driver can reach every cell here (max T < max_pickup_eta_min), so FIFO serves exactly the 60 oldest.
    assert matched[:60].all() and not matched[60:].any()


def test_match_does_not_touch_policy_budget_or_snapshots(ctx):
    waiting_order(ctx, 0)
    idle_driver(ctx, 0, 0)
    guarded = with_forbidden(ctx, "policy", "ledger", "layer", "monitor", "rng")
    matching.match(guarded, T0)
    assert guarded.orders.status[0] == MATCHED
