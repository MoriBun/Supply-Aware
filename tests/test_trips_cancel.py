"""Trips and cancellation: pickup, dropoff, payments, abandonment, en-route cancellation."""

import math

import numpy as np
import pytest

from sim.budget import DONG_PER_VND
from sim import cancel, matching, trips
from sim.budget import LedgerState
from sim.cancel import cancel_hazard_per_min
from sim.config import load_config
from sim.state import CancelReason, DriverStatus, OrderStatus
from tests.conftest import ROOT
from tests.fakes import make_context, with_forbidden

DEFAULT = ROOT / "config" / "default.yaml"
IDLE, EN_ROUTE, ON_TRIP = int(DriverStatus.IDLE), int(DriverStatus.EN_ROUTE), int(DriverStatus.ON_TRIP)
REPOSITIONING = int(DriverStatus.REPOSITIONING)
WAITING, MATCHED, O_ON_TRIP = int(OrderStatus.WAITING), int(OrderStatus.MATCHED), int(OrderStatus.ON_TRIP)
COMPLETED, ABANDONED, CANCELLED = (int(OrderStatus.COMPLETED), int(OrderStatus.ABANDONED),
                                   int(OrderStatus.CANCELLED))
T0 = 12 * 3600.0


def idle_driver(ctx, driver_id, cell, idle_since=0.0):
    d = ctx.drivers
    d.status[driver_id], d.cell[driver_id], d.idle_since[driver_id] = IDLE, cell, idle_since
    d.shift_end_s[driver_id] = np.inf
    ctx.cells.idle[cell] += 1
    return driver_id


def booked_order(ctx, pu_cell, do_cell, *, request_time_s=T0, rider_id=0, fare_vnd=2000.0, voucher_dong=0,
                 trip_noise=1.0, e_cancel=1e9):
    """A booked session and its Waiting order, as steps 4-6 leave them (voucher reserved and committed)."""
    sid = 5000 + ctx.sessions.n
    s_idx = ctx.sessions.append(session_id=sid, rider_id=rider_id, pu_cell=pu_cell, do_cell=do_cell,
                                open_time_s=request_time_s, trip_noise=trip_noise, e_cancel=e_cancel,
                                requested=True, voucher_dong=voucher_dong)
    o_idx = ctx.orders.append(order_id=sid, session_id=sid, rider_id=rider_id, pu_cell=pu_cell, do_cell=do_cell,
                              request_time_s=request_time_s, gross_fare_vnd=fare_vnd,
                              voucher_value_vnd=voucher_dong / DONG_PER_VND, net_fare_vnd=fare_vnd - voucher_dong / DONG_PER_VND,
                              session_idx=s_idx, voucher_dong=voucher_dong, in_window=True)
    ctx.sessions.order_idx[s_idx] = o_idx
    ctx.cells.waiting[pu_cell] += 1
    if voucher_dong:
        assert ctx.ledger.reserve(sid, request_time_s, voucher_dong)
        ctx.ledger.commit(sid)
    return o_idx


def run_until(ctx, t_from, t_to, *, cancellation=True):
    """Steps 1, 3 and 8 of every tick in [t_from, t_to]."""
    t = t_from
    while t <= t_to:
        trips.advance(ctx, t)
        cancel.expire_waiting(ctx, t)
        if cancellation:
            cancel.en_route(ctx, t)
        t += ctx.clock.tick_s


def counters_ok(ctx):
    d, o = ctx.drivers, ctx.orders
    by = lambda mask, cell: np.bincount(cell[mask], minlength=ctx.n_cells)  # noqa: E731
    np.testing.assert_array_equal(ctx.cells.idle, by(d.status == IDLE, d.cell))
    st, pu = o.col("status"), o.col("pu_cell")
    np.testing.assert_array_equal(ctx.cells.waiting, by(st == WAITING, pu))
    np.testing.assert_array_equal(ctx.cells.enroute, by(st == MATCHED, pu))
    np.testing.assert_array_equal(ctx.cells.ontrip, by(st == O_ON_TRIP, pu))


@pytest.fixture
def cfg():
    # Unit tests on the old grid (37 cells, 0.75 km): the hand-built pickups stay within reach.
    return load_config(DEFAULT, ["space.grid_radius=3", "space.edge_km=0.75"])


@pytest.fixture
def ctx(cfg):
    return make_context(cfg)


# --- pickup and dropoff ----------------------------------------------------------


def test_pickup_is_match_plus_eta_and_dropoff_is_pickup_plus_trip_time(ctx):
    space = ctx.world.space
    source = int(space.rings[3][1][0])
    order = booked_order(ctx, 3, 20, trip_noise=1.2)
    driver = idle_driver(ctx, 7, source)
    matching.match(ctx, T0)
    o, d = ctx.orders, ctx.drivers
    eta, trip = float(o.pickup_eta_min[order]), float(o.trip_time_min[order])
    pickup_at, dropoff_at = T0 + eta * 60, T0 + eta * 60 + trip * 60

    run_until(ctx, T0 + 60, pickup_at - 1, cancellation=False)
    assert o.status[order] == MATCHED and d.status[driver] == EN_ROUTE and np.isnan(o.pickup_time_s[order])
    run_until(ctx, math.ceil(pickup_at / 60) * 60.0, dropoff_at - 1, cancellation=False)
    assert o.status[order] == O_ON_TRIP and d.status[driver] == ON_TRIP
    assert o.pickup_time_s[order] == pytest.approx(pickup_at)             # exact, not rounded to the tick
    assert d.busy_until[driver] == pytest.approx(dropoff_at) and d.cell[driver] == 20 and d.origin_cell[driver] == 3
    assert ctx.cells.enroute[3] == 0 and ctx.cells.ontrip[3] == 1
    counters_ok(ctx)

    done_tick = math.ceil(dropoff_at / 60) * 60.0
    run_until(ctx, done_tick, done_tick, cancellation=False)
    assert o.status[order] == COMPLETED and o.dropoff_time_s[order] == pytest.approx(dropoff_at)
    assert o.dropoff_time_s[order] - o.pickup_time_s[order] == pytest.approx(trip * 60)
    assert d.status[driver] == IDLE and d.cell[driver] == 20 and d.idle_since[driver] == pytest.approx(dropoff_at)
    assert np.isinf(d.busy_until[driver]) and d.order[driver] == -1
    assert ctx.cells.ontrip[3] == 0 and ctx.cells.idle[20] == 1 and ctx.orders.count_open() == 0
    assert ctx.slot_counters.n_completed[3] == 1 and ctx.slot_counters.n_completed.sum() == 1
    counters_ok(ctx)


def test_payments_at_completion(cfg, ctx):
    # The platform fee goes to the platform, the commission applies to the rest.
    order = booked_order(ctx, 4, 9, fare_vnd=85000.0, voucher_dong=20000)
    driver = idle_driver(ctx, 0, 4)
    matching.match(ctx, T0)
    o = ctx.orders
    assert o.driver_pay_vnd[order] == 0.0 and o.platform_profit_vnd[order] == 0.0     # nothing before completion
    run_until(ctx, T0 + 60, T0 + 3600, cancellation=False)
    assert o.status[order] == COMPLETED
    assert cfg.pricing.commission_rate == 0.25 and cfg.pricing.platform_fee_vnd == 10000
    assert o.driver_pay_vnd[order] == pytest.approx(0.75 * (85000.0 - 10000.0))        # 75% of the fare after the fee
    assert o.net_fare_vnd[order] == pytest.approx(65000.0)
    assert o.platform_profit_vnd[order] == pytest.approx(10000.0 + 0.25 * 75000.0 - 20000.0)   # fee + commission - voucher
    assert ctx.drivers.earnings_vnd[driver] == pytest.approx(0.75 * 75000.0)
    # Voucher: committed -> spent, counted in the pickup cell at dropoff time.
    sid = int(o.session_id[order])
    assert ctx.ledger.entry(sid) == (ctx.ledger.period_of(T0), 20000, LedgerState.SPENT)
    assert ctx.slot_counters.voucher_spent_dong[4] == 20000
    ctx.ledger.check_invariant()


def test_order_without_voucher_never_touches_the_ledger(ctx):
    booked_order(ctx, 4, 9)
    idle_driver(ctx, 0, 4)
    matching.match(ctx, T0)
    run_until(ctx, T0 + 60, T0 + 3600)
    assert ctx.orders.status[0] == COMPLETED and ctx.ledger.n_entries == 0
    assert ctx.slot_counters.voucher_spent_dong.sum() == 0


def test_driver_serves_one_order_after_another(ctx):
    first = booked_order(ctx, 4, 9)
    second = booked_order(ctx, 9, 4, request_time_s=T0 + 60)
    driver = idle_driver(ctx, 0, 4)
    t = T0
    while ctx.orders.count_open() and t < T0 + 7200:
        trips.advance(ctx, t)
        matching.match(ctx, t)
        t += 60
    o = ctx.orders
    assert o.status[[first, second]].tolist() == [COMPLETED, COMPLETED]
    assert o.driver_id[[first, second]].tolist() == [driver, driver]
    assert o.matched_time_s[second] >= o.dropoff_time_s[first]            # matched only once idle again
    assert o.driver_origin_cell[second] == 9                              # ... from the first dropoff cell
    counters_ok(ctx)


def test_repositioning_driver_becomes_idle_on_arrival(ctx):
    d = ctx.drivers
    d.status[3], d.cell[3], d.origin_cell[3], d.busy_until[3] = REPOSITIONING, 11, 10, T0 + 250.0
    trips.advance(ctx, T0 + 240)
    assert d.status[3] == REPOSITIONING and ctx.cells.idle[11] == 0
    trips.advance(ctx, T0 + 300)
    assert d.status[3] == IDLE and d.cell[3] == 11 and d.idle_since[3] == T0 + 250.0 and np.isinf(d.busy_until[3])
    assert ctx.cells.idle[11] == 1


# --- abandonment -----------------------------------------------------------------


def test_waiting_order_is_abandoned_after_max_wait(ctx):
    riders = ctx.world.riders
    rider = int(np.argmin(np.abs(riders.max_wait_min - 5.0)))
    max_wait_s = float(riders.max_wait_min[rider]) * 60
    order = booked_order(ctx, 6, 8, rider_id=rider, voucher_dong=300)
    last_ok = T0 + math.ceil(max_wait_s / 60) * 60 - 60
    run_until(ctx, T0, last_ok)
    assert ctx.orders.status[order] == WAITING and ctx.cells.waiting[6] == 1
    cancel.expire_waiting(ctx, last_ok + 60)                              # first tick with t - request >= max_wait
    assert last_ok + 60 - T0 >= max_wait_s > last_ok - T0
    assert ctx.orders.status[order] == ABANDONED and ctx.cells.waiting[6] == 0
    assert ctx.slot_counters.n_abandoned[6] == 1
    assert ctx.ledger.entry(int(ctx.orders.session_id[order]))[2] == LedgerState.RELEASED
    assert ctx.ledger.totals(ctx.ledger.period_of(T0)) == (0, 0, 0)
    assert ctx.orders.cancel_reason[order] == int(CancelReason.NONE)
    counters_ok(ctx)


def test_matched_orders_are_not_abandoned(ctx):
    order = booked_order(ctx, 6, 8)
    idle_driver(ctx, 0, int(ctx.world.space.rings[6][2][0]))
    matching.match(ctx, T0)
    cancel.expire_waiting(ctx, T0 + 10 * 3600)                            # far beyond any max_wait
    assert ctx.orders.status[order] == MATCHED


def test_each_rider_has_their_own_patience(ctx):
    riders = ctx.world.riders
    patient, hasty = int(np.argmax(riders.max_wait_min)), int(np.argmin(riders.max_wait_min))
    a = booked_order(ctx, 1, 2, rider_id=patient)
    b = booked_order(ctx, 1, 2, rider_id=hasty)
    cancel.expire_waiting(ctx, T0 + float(riders.max_wait_min[hasty]) * 60 + 60)
    assert ctx.orders.status[[a, b]].tolist() == [WAITING, ABANDONED] and ctx.cells.waiting[1] == 1


# --- en-route cancellation -------------------------------------------------------


@pytest.mark.parametrize("cancel_after_min, at_origin", [(2, True), (12, False)])
def test_cancelled_driver_is_idle_where_they_stand(cfg, ctx, cancel_after_min, at_origin):
    space = ctx.world.space
    source = int(space.rings[0][2][0])                                    # 3 rings away: a long pickup
    eta = float(space.T[source, 0, 12])
    assert eta / 2 > 2 and eta > 12 > eta / 2
    # e_cancel chosen so that the cumulative hazard reaches it exactly after `cancel_after_min` minutes.
    threshold = float(cancel_hazard_per_min(cfg, eta)) * cancel_after_min
    order = booked_order(ctx, 0, 5, e_cancel=threshold, voucher_dong=400)
    other = booked_order(ctx, 0, 5, request_time_s=T0 + 60)
    driver = idle_driver(ctx, 2, source)
    matching.match(ctx, T0)
    assert ctx.orders.driver_id[order] == driver and ctx.orders.status[other] == WAITING

    t_cancel = T0 + cancel_after_min * 60
    for t in np.arange(T0, t_cancel, 60.0):
        trips.advance(ctx, t)
        cancel.en_route(ctx, t)
    assert ctx.orders.status[order] == MATCHED                            # not before the threshold
    # The tick of the cancellation, in engine order: step 7 (match) runs before step 8 (cancel).
    trips.advance(ctx, t_cancel)
    matching.match(ctx, t_cancel)
    cancel.en_route(ctx, t_cancel)
    o, d = ctx.orders, ctx.drivers
    assert o.status[order] == CANCELLED and o.cancel_reason[order] == int(CancelReason.RIDER_EN_ROUTE)
    assert d.status[driver] == IDLE and d.idle_since[driver] == t_cancel and d.order[driver] == -1
    assert d.cell[driver] == (source if at_origin else 0)
    assert np.isinf(d.busy_until[driver])
    assert o.status[other] == WAITING                                     # the freed driver is not re-matched this tick
    assert ctx.cells.enroute[0] == 0 and ctx.cells.idle[source if at_origin else 0] == 1
    assert ctx.slot_counters.n_cancelled[0] == 1
    assert ctx.ledger.entry(int(o.session_id[order]))[2] == LedgerState.RELEASED
    counters_ok(ctx)
    # Next tick the driver takes the order that was waiting.
    matching.match(ctx, t_cancel + 60)
    assert o.status[other] == MATCHED and o.driver_id[other] == driver


def test_no_cancellation_once_the_rider_is_picked_up(cfg, ctx):
    eta = float(ctx.world.space.eta_in(1, 12))
    order = booked_order(ctx, 4, 30, e_cancel=float(cancel_hazard_per_min(cfg, eta)) * 20)   # would hit at minute 20
    idle_driver(ctx, 0, 4)
    matching.match(ctx, T0)
    run_until(ctx, T0 + 60, T0 + 3 * 3600)
    assert ctx.orders.status[order] == COMPLETED


def test_hazard_formula(cfg):
    c = cfg.cancel
    np.testing.assert_allclose(cancel_hazard_per_min(cfg, [0.0, c.eta_free_min, c.eta_free_min + 7]),
                               [c.base_per_min, c.base_per_min, c.base_per_min + 7 * c.slope_per_min2])


def cancel_rate(eta_min: float, n: int = 10_000, exact_time: bool = True) -> float:
    """Share of ``n`` matched orders cancelled before pickup at a given pickup ETA."""
    cfg = load_config(DEFAULT, [f"supply.fleet_size={n}", f"cancel.exact_time={str(exact_time).lower()}"])
    ctx = make_context(cfg)
    e_cancel = np.random.default_rng(int(eta_min * 10)).exponential(1.0, n)
    s0, s1 = ctx.sessions.reserve(n)
    o0, o1 = ctx.orders.reserve(n)
    idx = np.arange(n)
    ctx.sessions.e_cancel[s0:s1] = e_cancel
    o, d = ctx.orders, ctx.drivers
    o.session_idx[o0:o1], o.session_id[o0:o1] = idx, idx
    o.pu_cell[o0:o1], o.do_cell[o0:o1], o.driver_origin_cell[o0:o1] = 0, 1, 2
    o.status[o0:o1], o.matched_time_s[o0:o1], o.pickup_eta_min[o0:o1] = MATCHED, 0.0, eta_min
    o.trip_time_min[o0:o1], o.driver_id[o0:o1] = 10.0, idx
    d.status[:], d.order[:], d.busy_until[:], d.cell[:], d.origin_cell[:] = EN_ROUTE, idx, eta_min * 60, 0, 2
    ctx.cells.enroute[0] = n
    for tick in range(int(eta_min) + 2):
        trips.advance(ctx, tick * 60.0)
        cancel.en_route(ctx, tick * 60.0)
    assert (o.col("status") != MATCHED).all()
    counters_ok(ctx)
    return float((o.col("status") == CANCELLED).mean())


@pytest.mark.parametrize("exact_time", [True, False])
def test_cancellation_rate_grows_with_pickup_eta(cfg, exact_time):
    # With 10 000 simulated orders the rate at ETA 10 min is above the rate at 3 min.
    short, long = cancel_rate(3.0, exact_time=exact_time), cancel_rate(10.0, exact_time=exact_time)
    assert long > short
    # exact_time: the exact hazard, P = 1 - exp(-h * eta). Without it the minute before pickup is
    # never checked: P = 1 - exp(-h * (eta - 1)).
    for rate, eta in ((short, 3.0), (long, 10.0)):
        exposure = eta if exact_time else eta - 1
        expected = 1 - math.exp(-float(cancel_hazard_per_min(cfg, eta)) * exposure)
        assert rate == pytest.approx(expected, abs=4 * math.sqrt(expected * (1 - expected) / 10_000))


def test_exact_cancellation_before_pickup_frees_the_driver_and_the_voucher_at_t_cancel(cfg):
    # The rider crosses the threshold between the last tick and a pickup due inside the next tick: with exact_time the
    # order is cancelled at t_cancel in step 1 (driver at the rider's cell, idle_since = t_cancel,
    # voucher released); without it the pickup happens.
    for exact in (True, False):
        c = load_config(DEFAULT, ["space.grid_radius=3", "space.edge_km=0.75", f"cancel.exact_time={str(exact).lower()}"])
        ctx = make_context(c)
        source = int(ctx.world.space.rings[0][1][0])
        eta = float(ctx.world.space.T[source, 0, 12])
        h = float(cancel_hazard_per_min(c, eta))
        last_tick = T0 + 60 * math.ceil(eta - 1)                             # last tick strictly before pickup
        t_cancel_offset = (last_tick + T0 + eta * 60) / 2 - T0               # between that tick and the pickup
        order = booked_order(ctx, 0, 5, e_cancel=h * t_cancel_offset / 60.0, voucher_dong=500)
        idle_driver(ctx, 0, source)
        matching.match(ctx, T0)
        pickup_at = T0 + eta * 60
        t = T0
        while t + 60 < pickup_at:
            t += 60
            trips.advance(ctx, t)
            cancel.en_route(ctx, t)
            assert ctx.orders.status[order] == MATCHED                       # not yet: H < e_cancel at every tick
        t += 60
        trips.advance(ctx, t)                                                # the tick that reaches pickup_at
        sid = int(ctx.orders.session_id[order])
        if exact:
            assert ctx.orders.status[order] == CANCELLED
            assert ctx.drivers.idle_since[0] == pytest.approx(T0 + t_cancel_offset)
            assert ctx.drivers.cell[0] == 0                                  # past half the ETA: at the rider's cell
            assert ctx.ledger.entry(sid)[2] == LedgerState.RELEASED
            assert ctx.slot_counters.n_cancelled[0] == 1
        else:
            assert ctx.orders.status[order] == O_ON_TRIP
        ctx.ledger.check_invariant()
        counters_ok(ctx)


def test_same_session_cancels_at_the_same_moment_whatever_the_policy(cfg):
    # CRN: the threshold is the session's pre-drawn e_cancel, so two runs agree to the tick.
    times = []
    for on in (False, True):
        ctx = make_context(cfg, on=on)
        source = int(ctx.world.space.rings[0][2][0])
        eta = float(ctx.world.space.T[source, 0, 12])
        order = booked_order(ctx, 0, 5, e_cancel=float(cancel_hazard_per_min(cfg, eta)) * 4.5)
        idle_driver(ctx, 0, source)
        matching.match(ctx, T0)
        t = T0
        while ctx.orders.status[order] == MATCHED:
            t += 60
            trips.advance(ctx, t)
            cancel.en_route(ctx, t)
        assert ctx.orders.status[order] == CANCELLED
        times.append(t)
    assert times[0] == times[1] == T0 + 5 * 60                            # first whole minute with H >= e_cancel


# --- step contract ---------------------------------------------------------------


def test_steps_do_nothing_on_an_empty_market(ctx):
    for t in (0.0, 60.0, 3600.0):
        trips.advance(ctx, t)
        cancel.expire_waiting(ctx, t)
        cancel.en_route(ctx, t)
    assert ctx.orders.n == 0 and ctx.cells.idle.sum() == 0


def test_steps_do_not_touch_policy_or_snapshots(ctx):
    booked_order(ctx, 4, 9)
    idle_driver(ctx, 0, 4)
    matching.match(ctx, T0)
    guarded = with_forbidden(ctx, "policy", "layer", "monitor", "rng")
    run_until(guarded, T0 + 60, T0 + 3600)
    assert guarded.orders.status[0] == COMPLETED
