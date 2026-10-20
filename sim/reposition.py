"""M9 Repositioning: static-weight moves of long-idle drivers; never reads demand or snapshots.

Contract: ``step`` uses only ``ctx.world.cell_weight``, ``ctx.world.space`` and
the DRIVER stream keyed by ``(driver_id, reposition_count)``. It must not touch
``ctx.monitor``, ``ctx.sessions`` or ``ctx.orders``.

The destination depends only on the driver, on how many times they have moved
and on the static cell weights, so supply stays independent of the policy (hard rule 5).
"""

from __future__ import annotations

import numpy as np

from sim.rng import Stream
from sim.shocks import travel_hour
from sim.state import DriverStatus, SimContext

_IDLE, _REPOSITIONING = int(DriverStatus.IDLE), int(DriverStatus.REPOSITIONING)


def step(ctx: SimContext, t: float) -> None:
    """Engine step 9: drivers idle for ``max_idle_min`` or more head for an adjacent cell."""
    rc = ctx.cfg.reposition
    if not rc.enabled or rc.mode == "stay":
        return
    drivers, space, weight = ctx.drivers, ctx.world.space, ctx.world.cell_weight
    moving = np.flatnonzero((drivers.status == _IDLE) & (t - drivers.idle_since >= rc.max_idle_min * 60.0))
    if len(moving) == 0:
        return

    hour = travel_hour(ctx, t, ctx.clock.hour_of(t))   # + 24 while it rains
    origin = drivers.cell[moving]
    target = np.empty(len(moving), dtype=drivers.cell.dtype)
    for j, d in enumerate(moving.tolist()):
        neighbors = space.neighbors[origin[j]]
        neighbors = neighbors[neighbors >= 0]                 # without torus, border cells have fewer
        cdf = np.cumsum(weight[neighbors])
        u = ctx.rng.rng_for(Stream.DRIVER, d, int(drivers.reposition_count[d])).random()
        target[j] = neighbors[min(int(np.searchsorted(cdf, u * cdf[-1], side="right")), len(neighbors) - 1)]

    drivers.status[moving] = _REPOSITIONING                   # not matchable until arrival (trips.advance)
    drivers.origin_cell[moving] = origin
    drivers.cell[moving] = target                             # target cell while moving
    drivers.busy_until[moving] = t + space.T[origin, target, hour].astype(np.float64) * 60.0
    drivers.idle_since[moving] = np.nan
    drivers.reposition_count[moving] += 1
    np.subtract.at(ctx.cells.idle, origin, 1)
