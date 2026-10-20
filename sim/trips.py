"""M6 TripExecution: pickup and dropoff events, payments.

Contract: ``advance`` processes drivers whose ``busy_until <= t``: pickups
(EN_ROUTE -> ON_TRIP, order OnTrip), dropoffs (ON_TRIP -> IDLE at the dropoff
cell, order Completed with payments, ``ctx.ledger.settle`` when the order held a
voucher, slot counters n_completed / voucher_spent) and arrivals of
repositioning drivers.

Event times are exact (``pickup_at``, ``dropoff_at``), not rounded to the tick
that processes them; the trip duration was fixed at match time (matching.py).
With ``cancel.exact_time`` a due pickup whose rider crossed the cancellation
threshold first (``t_cancel < pickup_at``) is a cancellation instead.
"""

from __future__ import annotations

import numpy as np

from sim import cancel
from sim.monitor import MarketMonitor
from sim.state import DriverStatus, OrderStatus, SimContext

_IDLE, _EN_ROUTE = int(DriverStatus.IDLE), int(DriverStatus.EN_ROUTE)
_ON_TRIP, _REPOSITIONING = int(DriverStatus.ON_TRIP), int(DriverStatus.REPOSITIONING)


def advance(ctx: SimContext, t: float) -> None:
    """Engine step 1: pickups, dropoffs and repositioning arrivals due by ``t``."""
    drivers = ctx.drivers
    due = np.flatnonzero(drivers.busy_until <= t)
    if len(due) == 0:
        return
    status = drivers.status[due]
    pickups = due[status == _EN_ROUTE]
    if ctx.cfg.cancel.exact_time and len(pickups):
        t_cancel = cancel.cancel_time_s(ctx, drivers.order[pickups])
        first = t_cancel < drivers.busy_until[pickups]          # the rider gave up before the driver arrived
        cancel.cancel_drivers(ctx, pickups[first], t_cancel[first])
        pickups = pickups[~first]
    _pick_up(ctx, pickups)
    # A trip shorter than a tick can start and end in the same step, so look again.
    on_trip = np.flatnonzero((drivers.status == _ON_TRIP) & (drivers.busy_until <= t))
    _drop_off(ctx, on_trip)
    _arrive(ctx, due[status == _REPOSITIONING])


def _pick_up(ctx: SimContext, d_idx: np.ndarray) -> None:
    if len(d_idx) == 0:
        return
    drivers, orders, cells = ctx.drivers, ctx.orders, ctx.cells
    o_idx = drivers.order[d_idx]
    pickup_at = drivers.busy_until[d_idx]
    pu_cell = orders.pu_cell[o_idx]

    orders.status[o_idx] = int(OrderStatus.ON_TRIP)
    orders.pickup_time_s[o_idx] = pickup_at

    drivers.status[d_idx] = _ON_TRIP
    drivers.origin_cell[d_idx] = pu_cell
    drivers.cell[d_idx] = orders.do_cell[o_idx]            # target cell while moving
    drivers.busy_until[d_idx] = pickup_at + orders.trip_time_min[o_idx].astype(np.float64) * 60.0

    np.subtract.at(cells.enroute, pu_cell, 1)
    np.add.at(cells.ontrip, pu_cell, 1)


def _drop_off(ctx: SimContext, d_idx: np.ndarray) -> None:
    if len(d_idx) == 0:
        return
    drivers, orders, cells = ctx.drivers, ctx.orders, ctx.cells
    o_idx = drivers.order[d_idx]
    dropoff_at = drivers.busy_until[d_idx]
    pu_cell, do_cell = orders.pu_cell[o_idx], orders.do_cell[o_idx]

    # Payments at completion: the platform fee goes to the platform, the
    # commission applies to the rest; profit = fee + commission x (gross - fee) - voucher.
    gross = orders.gross_fare_vnd[o_idx].astype(np.float64)
    driver_pay = (1.0 - ctx.cfg.pricing.commission_rate) * (gross - ctx.cfg.pricing.platform_fee_vnd)
    orders.status[o_idx] = int(OrderStatus.COMPLETED)
    orders.dropoff_time_s[o_idx] = dropoff_at
    orders.driver_pay_vnd[o_idx] = driver_pay
    orders.platform_profit_vnd[o_idx] = orders.net_fare_vnd[o_idx].astype(np.float64) - driver_pay

    voucher = orders.voucher_dong[o_idx]
    session_id = orders.session_id[o_idx]
    for i in np.flatnonzero(voucher > 0):
        ctx.ledger.settle(int(session_id[i]))              # committed -> spent

    drivers.status[d_idx] = _IDLE
    drivers.cell[d_idx] = do_cell
    drivers.idle_since[d_idx] = dropoff_at
    drivers.busy_until[d_idx] = np.inf
    drivers.order[d_idx] = -1
    drivers.earnings_vnd[d_idx] += driver_pay

    np.subtract.at(cells.ontrip, pu_cell, 1)
    np.add.at(cells.idle, do_cell, 1)
    MarketMonitor.on_completed(ctx.slot_counters, pu_cell, voucher)


def _arrive(ctx: SimContext, d_idx: np.ndarray) -> None:
    """A repositioning driver reaches the target cell and becomes idle there."""
    if len(d_idx) == 0:
        return
    drivers = ctx.drivers
    drivers.status[d_idx] = _IDLE
    drivers.idle_since[d_idx] = drivers.busy_until[d_idx]
    drivers.busy_until[d_idx] = np.inf
    np.add.at(ctx.cells.idle, drivers.cell[d_idx], 1)
