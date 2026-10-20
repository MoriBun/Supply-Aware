"""M7 Cancellation: abandon after max_wait, en-route cancellation by cumulative hazard.

Contract: both steps move orders to a terminal status, release the driver
(en-route case), call ``ctx.ledger.release_committed`` when the order held a
voucher, and update ``ctx.cells`` / ``ctx.slot_counters``.

Both rules are deterministic given the session: ``max_wait_min`` belongs to the
rider and ``e_cancel`` was pre-drawn with the session, so the same session has
the same patience and the same cancellation threshold under every policy (CRN).

With ``cancel.exact_time`` the rider cancels at the exact
crossing time ``t_cancel = matched_time + 60 * e_cancel / h``: step 1
(``trips.advance``) cancels instead of picking up when ``t_cancel < pickup_at``
(the last minute before pickup used to be skipped), and both steps place the
driver and set ``idle_since`` at ``t_cancel``. No extra random number is drawn.
"""

from __future__ import annotations

import numpy as np

from sim.config import Config
from sim.monitor import MarketMonitor
from sim.state import CancelReason, DriverStatus, OrderStatus, SimContext

_IDLE, _EN_ROUTE = int(DriverStatus.IDLE), int(DriverStatus.EN_ROUTE)
_WAITING = int(OrderStatus.WAITING)


def cancel_hazard_per_min(cfg: Config, pickup_eta_min) -> np.ndarray:
    """``h = base_per_min + slope_per_min2 * max(0, pickup_eta_min - eta_free_min)``."""
    c = cfg.cancel
    eta = np.asarray(pickup_eta_min, dtype=np.float64)
    return c.base_per_min + c.slope_per_min2 * np.maximum(0.0, eta - c.eta_free_min)


def expire_waiting(ctx: SimContext, t: float) -> None:
    """Engine step 3: Waiting orders older than the rider's max_wait become Abandoned."""
    orders = ctx.orders
    waiting = np.flatnonzero(orders.col("status") == _WAITING)
    if len(waiting) == 0:
        return
    max_wait_s = ctx.world.riders.max_wait_min[orders.rider_id[waiting]].astype(np.float64) * 60.0
    gone = waiting[t - orders.request_time_s[waiting] >= max_wait_s]
    if len(gone) == 0:
        return
    orders.status[gone] = int(OrderStatus.ABANDONED)
    _release_vouchers(ctx, gone)
    pu_cell = orders.pu_cell[gone]
    np.subtract.at(ctx.cells.waiting, pu_cell, 1)
    MarketMonitor.on_abandoned(ctx.slot_counters, pu_cell)


def cancel_time_s(ctx: SimContext, o_idx: np.ndarray) -> np.ndarray:
    """``t_cancel = matched_time + 60 * e_cancel / h`` per order (inf when ``h = 0``)."""
    orders = ctx.orders
    h = cancel_hazard_per_min(ctx.cfg, orders.pickup_eta_min[o_idx].astype(np.float64))
    e = ctx.sessions.e_cancel[orders.session_idx[o_idx]].astype(np.float64)
    with np.errstate(divide="ignore"):
        wait_s = np.where(h > 0, 60.0 * e / np.where(h > 0, h, 1.0), np.inf)
    return orders.matched_time_s[o_idx] + wait_s


def en_route(ctx: SimContext, t: float) -> None:
    """Engine step 8: the rider cancels while the driver is on the way (cumulative hazard >= e_cancel)."""
    drivers, orders = ctx.drivers, ctx.orders
    d_idx = np.flatnonzero(drivers.status == _EN_ROUTE)      # pickup still ahead: step 1 handled the others
    if len(d_idx) == 0:
        return
    o_idx = drivers.order[d_idx]
    eta_min = orders.pickup_eta_min[o_idx].astype(np.float64)
    elapsed_s = t - orders.matched_time_s[o_idx]
    cumulative = cancel_hazard_per_min(ctx.cfg, eta_min) * elapsed_s / 60.0
    hit = cumulative >= ctx.sessions.e_cancel[orders.session_idx[o_idx]]
    if not hit.any():
        return
    d_idx, o_idx = d_idx[hit], o_idx[hit]
    if ctx.cfg.cancel.exact_time:
        cancel_drivers(ctx, d_idx, np.minimum(cancel_time_s(ctx, o_idx), t))   # <= t up to float rounding
    else:
        cancel_drivers(ctx, d_idx, np.full(len(d_idx), float(t)))


def cancel_drivers(ctx: SimContext, d_idx: np.ndarray, t_event: np.ndarray) -> None:
    """Cancel the orders of en-route drivers ``d_idx`` at times ``t_event`` (used by steps 1 and 8).

    The driver becomes idle where they stand at ``t_event``: still at the origin cell before half
    the pickup ETA, at the rider's cell after, with ``idle_since = t_event``.
    """
    if len(d_idx) == 0:
        return
    drivers, orders = ctx.drivers, ctx.orders
    o_idx = drivers.order[d_idx]
    eta_min = orders.pickup_eta_min[o_idx].astype(np.float64)
    t_event = np.asarray(t_event, dtype=np.float64)
    elapsed_s = t_event - orders.matched_time_s[o_idx]

    orders.status[o_idx] = int(OrderStatus.CANCELLED)
    orders.cancel_reason[o_idx] = int(CancelReason.RIDER_EN_ROUTE)
    _release_vouchers(ctx, o_idx)

    # The driver stops where they are: still near the origin before half the ETA, at the rider's cell after.
    pu_cell = orders.pu_cell[o_idx]
    stop_cell = np.where(elapsed_s < eta_min * 60.0 / 2.0, orders.driver_origin_cell[o_idx], pu_cell)
    drivers.status[d_idx] = _IDLE
    drivers.cell[d_idx] = stop_cell
    drivers.idle_since[d_idx] = t_event
    drivers.busy_until[d_idx] = np.inf
    drivers.order[d_idx] = -1

    np.subtract.at(ctx.cells.enroute, pu_cell, 1)
    np.add.at(ctx.cells.idle, stop_cell, 1)
    MarketMonitor.on_cancelled(ctx.slot_counters, pu_cell)


def _release_vouchers(ctx: SimContext, o_idx: np.ndarray) -> None:
    """committed -> released for the orders that held a voucher."""
    orders = ctx.orders
    voucher = orders.voucher_dong[o_idx]
    session_id = orders.session_id[o_idx]
    for i in np.flatnonzero(voucher > 0):
        ctx.ledger.release_committed(int(session_id[i]))
