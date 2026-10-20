"""M8 SupplyModel: periodic shifts; drivers leave only when idle.

Contract: ``init_drivers`` sets the t = 0 state (drivers whose periodic shift
covers t = 0 start IDLE at their origin cell; ``shift_mode = always_on`` puts
every driver online for the whole run). ``update`` handles shift starts and
ends each tick and keeps ``ctx.cells.idle`` consistent.

Supply never looks at demand, policy or snapshots (hard rule 5). A driver who is
busy when the shift ends is left alone: ``trips.advance`` (step 1) makes them
IDLE at dropoff, and ``update`` (step 2 of the same tick) then takes them offline.

Shocks (W1, ``ctx.shocks``), schedule mode only: a shift that
starts on day k is skipped when the driver does not work on day k (the next working
day's shift is taken) and starts ``jitter_s[k]`` later; while it rains, drivers
flagged for that day stop as soon as they are idle (OFFLINE, ``paused``) and come
back at the same cell when the rain ends if their shift is not over. The shocks
were drawn before the run, so supply stays independent of the policy.
"""

from __future__ import annotations

import numpy as np

from sim.population import on_shift
from sim.state import DriverStatus, SimContext

_DAY_S = 86400.0
_OFFLINE, _IDLE = int(DriverStatus.OFFLINE), int(DriverStatus.IDLE)


def init_drivers(ctx: SimContext) -> None:
    """Called once before the first tick: put the drivers whose shift covers t = 0 online."""
    cfg, drivers, schedule = ctx.cfg, ctx.drivers, ctx.world.drivers
    if cfg.supply.early_exit_enabled:
        raise NotImplementedError("supply.early_exit_enabled: sensitivity analysis only")

    if cfg.supply.shift_mode == "always_on":
        drivers.shift_start_s[:] = 0.0
        drivers.shift_end_s[:] = np.inf
        online = np.ones(drivers.n, dtype=bool)
    else:
        start_s = schedule.shift_start_h * 3600.0
        len_s = schedule.shift_len_h * 3600.0
        online = on_shift(schedule, 0.0)
        # A shift that covers t = 0 began yesterday (or right now, if it starts at 00:00).
        began = np.where(online & (start_s > 0), start_s - _DAY_S, start_s)
        drivers.shift_start_s[:] = began
        drivers.shift_end_s[:] = began + len_s
        if ctx.shocks is not None:
            # Shifts already running at t = 0 began the day before the run: no shock applies to them.
            later = np.flatnonzero(~online)
            _schedule_from_day(ctx, later, np.zeros(len(later), dtype=np.int64))

    _go_online(ctx, np.flatnonzero(online), 0.0)


def update(ctx: SimContext, t: float) -> None:
    """Engine step 2: shift ends (idle drivers only), then shift starts."""
    drivers = ctx.drivers
    status = drivers.status

    leaving = np.flatnonzero((status == _IDLE) & (drivers.shift_end_s <= t))
    if len(leaving):
        np.subtract.at(ctx.cells.idle, drivers.cell[leaving], 1)
        status[leaving] = _OFFLINE
        drivers.cell[leaving] = -1
        drivers.idle_since[leaving] = np.nan
        # Next shift: same hours on the first day whose shift has not ended yet (late leavers skip nothing).
        if ctx.shocks is None:
            days = np.floor((t - drivers.shift_end_s[leaving]) / _DAY_S) + 1.0
            drivers.shift_start_s[leaving] += days * _DAY_S
            drivers.shift_end_s[leaving] += days * _DAY_S
        else:
            _schedule_next(ctx, leaving, t)

    # The rain step runs first: it may bring paused drivers back, who must not start a second time.
    blocked = None
    if ctx.shocks is not None and ctx.cfg.supply.shift_mode == "schedule":
        blocked = _rain_step(ctx, t)
    can_start = (status == _OFFLINE) & (drivers.shift_start_s <= t)
    if blocked is not None:
        can_start &= ~blocked
    starting = np.flatnonzero(can_start)
    if len(starting):
        _go_online(ctx, starting, t)


def _nominal_start_s(ctx: SimContext, idx: np.ndarray, day: np.ndarray) -> np.ndarray:
    return ctx.world.drivers.shift_start_h[idx] * 3600.0 + day * _DAY_S


def _schedule_from_day(ctx: SimContext, idx: np.ndarray, day: np.ndarray) -> None:
    """Set the shift of ``idx`` to the first working day >= ``day`` (attendance, then jitter)."""
    sh, drivers = ctx.shocks, ctx.drivers
    day = np.asarray(day, dtype=np.int64).copy()
    for j, i in enumerate(idx.tolist()):
        while day[j] < sh.n_days and not sh.works[day[j], i]:
            day[j] += 1
    in_run = day < sh.n_days
    start = _nominal_start_s(ctx, idx, day)
    start = start + np.where(in_run, sh.jitter_s[np.minimum(day, sh.n_days - 1), idx], 0.0)
    start = np.where(in_run, start, np.inf)                 # no working day left in the run
    drivers.shift_start_s[idx] = start
    drivers.shift_end_s[idx] = start + ctx.world.drivers.shift_len_h[idx] * 3600.0


def _schedule_next(ctx: SimContext, idx: np.ndarray, t: float) -> None:
    """Next shift after the one that just ended: the first day whose nominal shift has not ended by ``t``."""
    sched = ctx.world.drivers
    end_of_day0 = sched.shift_start_h[idx] * 3600.0 + sched.shift_len_h[idx] * 3600.0
    day = np.maximum(np.floor((t - end_of_day0) / _DAY_S).astype(np.int64) + 1, 0)
    # Never the day of the shift that just ended (a negative jitter can end it before its nominal end).
    current = np.floor((ctx.drivers.shift_start_s[idx] - sched.shift_start_h[idx] * 3600.0 + _DAY_S / 2) / _DAY_S)
    day = np.maximum(day, current.astype(np.int64) + 1)
    _schedule_from_day(ctx, idx, day)


def _rain_step(ctx: SimContext, t: float) -> np.ndarray:
    """Stop flagged idle drivers while it rains, bring them back after; return who may not start now."""
    sh, drivers, status = ctx.shocks, ctx.drivers, ctx.drivers.status
    d = sh.day_of(t)
    if sh.raining(t):
        stop = np.flatnonzero((status == _IDLE) & sh.rain_off[d] & ~sh.paused & (drivers.shift_end_s > t))
        if len(stop):
            np.subtract.at(ctx.cells.idle, drivers.cell[stop], 1)
            sh.pause_cell[stop] = drivers.cell[stop]
            sh.paused[stop] = True
            status[stop] = _OFFLINE
            drivers.cell[stop] = -1
            drivers.idle_since[stop] = np.nan
        return sh.rain_off[d] | sh.paused
    back = np.flatnonzero(sh.paused)
    if len(back):
        sh.paused[back] = False
        resume = back[drivers.shift_end_s[back] > t]
        if len(resume):
            cell = sh.pause_cell[resume]
            status[resume] = _IDLE
            drivers.cell[resume] = cell
            drivers.idle_since[resume] = t
            np.add.at(ctx.cells.idle, cell, 1)
        over = back[drivers.shift_end_s[back] <= t]          # the shift ended during the rain
        if len(over):
            _schedule_next(ctx, over, t)
    return np.zeros(drivers.n, dtype=bool)


def _go_online(ctx: SimContext, idx: np.ndarray, t: float) -> None:
    """OFFLINE -> IDLE at the driver's origin cell."""
    drivers = ctx.drivers
    cell = ctx.world.drivers.origin_cell[idx]
    drivers.status[idx] = _IDLE
    drivers.cell[idx] = cell
    drivers.idle_since[idx] = t
    np.add.at(ctx.cells.idle, cell, 1)
