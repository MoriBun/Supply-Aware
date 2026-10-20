"""M5 Matching: FIFO matching, same cell first, then expanding rings up to max_pickup_eta_min.

Contract: ``match`` moves Waiting orders to Matched (columns of step "match"),
drivers to EN_ROUTE, and keeps ``ctx.cells`` (idle / enroute / waiting) and
``ctx.slot_counters`` (n_matched, pickup ETA sums) consistent.

The search rule itself lives in ``SpaceTime.find_pickup`` so that the quoted ETA
(step 5) and the match (step 7) can never disagree. Nothing here
shortens the search or caps the pickup distance (hard rule 6).
"""

from __future__ import annotations

import numpy as np

from sim.monitor import MarketMonitor
from sim.shocks import travel_hour
from sim.state import DriverStatus, OrderStatus, SimContext

_IDLE, _EN_ROUTE = int(DriverStatus.IDLE), int(DriverStatus.EN_ROUTE)
_WAITING, _MATCHED = int(OrderStatus.WAITING), int(OrderStatus.MATCHED)


def match(ctx: SimContext, t: float) -> None:
    """Engine step 7: give each Waiting order, oldest first, the nearest idle on-shift driver."""
    orders, drivers, space, clock = ctx.orders, ctx.drivers, ctx.world.space, ctx.clock
    # Orders are appended in request order, so ascending index is FIFO by request_time.
    waiting = np.flatnonzero(orders.col("status") == _WAITING)
    if len(waiting) == 0:
        return

    # Only drivers that are idle and still on shift can be matched.
    eligible = np.flatnonzero((drivers.status == _IDLE) & (drivers.shift_end_s > t))
    if len(eligible) == 0:
        return
    n_cells = ctx.n_cells
    cell_of = drivers.cell[eligible]
    avail = np.bincount(cell_of, minlength=n_cells)
    # Queue of each cell: longest-idle driver first (ties: smaller driver id).
    queue = eligible[np.lexsort((eligible, drivers.idle_since[eligible], cell_of))]
    head = np.searchsorted(np.sort(cell_of), np.arange(n_cells))   # start of each cell's queue

    hour = travel_hour(ctx, t, clock.hour_of(t))        # + 24 while it rains
    pu_cell = orders.pu_cell
    unreachable = np.zeros(n_cells, dtype=bool)   # no pickup from this cell for the rest of the tick
    left = len(eligible)
    m_order, m_driver, m_source, m_eta = [], [], [], []
    for o in waiting.tolist():
        if left == 0:
            break
        z = int(pu_cell[o])
        if unreachable[z]:
            continue
        source, eta = space.find_pickup(z, hour, avail)   # ETA uses the count before this driver is taken
        if source < 0:
            unreachable[z] = True                         # supply only shrinks within a tick
            continue
        m_order.append(o)
        m_driver.append(int(queue[head[source]]))
        m_source.append(source)
        m_eta.append(eta)
        head[source] += 1
        avail[source] -= 1
        left -= 1
    if not m_order:
        return

    o_idx = np.asarray(m_order)
    d_idx = np.asarray(m_driver)
    source = np.asarray(m_source)
    eta_min = np.asarray(m_eta)
    rider_cell = pu_cell[o_idx]
    pickup_at = t + eta_min * 60.0

    # Trip duration is fixed at match time: T[pickup, dropoff, hour of pickup] * trip_noise.
    h_pickup = travel_hour(ctx, pickup_at, ((pickup_at % 86400) // 3600).astype(np.int64))
    base_min = space.T[rider_cell, orders.do_cell[o_idx], h_pickup].astype(np.float64)
    trip_noise = ctx.sessions.trip_noise[orders.session_idx[o_idx]]

    orders.status[o_idx] = _MATCHED
    orders.driver_id[o_idx] = d_idx
    orders.driver_origin_cell[o_idx] = source
    orders.matched_time_s[o_idx] = t
    orders.pickup_eta_min[o_idx] = eta_min
    orders.trip_time_min[o_idx] = base_min * trip_noise
    orders.trip_km[o_idx] = base_min / 60.0 * space.speed_kmh[h_pickup]   # road distance, without noise

    drivers.status[d_idx] = _EN_ROUTE
    drivers.origin_cell[d_idx] = source
    drivers.cell[d_idx] = rider_cell          # target cell while moving
    drivers.busy_until[d_idx] = pickup_at
    drivers.idle_since[d_idx] = np.nan
    drivers.order[d_idx] = o_idx

    cells = ctx.cells
    np.subtract.at(cells.idle, source, 1)
    np.add.at(cells.enroute, rider_cell, 1)
    np.subtract.at(cells.waiting, rider_cell, 1)
    MarketMonitor.on_matched(ctx.slot_counters, rider_cell, eta_min)
