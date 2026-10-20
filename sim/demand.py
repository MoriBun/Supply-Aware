"""M2 DemandGenerator: Poisson sessions per (cell, tick), rider and destination choice, pre-drawn numbers.

Contract: ``spawn`` appends this tick's sessions to ``ctx.sessions`` (columns of
step "spawn" in ``state.SESSION_COLUMNS``) and sets ``ctx.tick_sessions`` to
their row range. Session ids come from ``rng.session_ids_for_tick``; the SESSION
generator of each session is consumed in ``rng.SESSION_DRAW_ORDER``.

Nothing here reads the policy, the drivers or the snapshots, so the sessions of a
``(day, tick, cell)`` are identical under every policy (hard rule 4, CRN).
"""

from __future__ import annotations

import numpy as np

from sim.config import Config
from sim.rng import Stream, draw_session_scalars, session_ids_for_tick
from sim.state import SimContext

# Uniforms taken from a session's generator before the scalars of ``draw_session_scalars``
# ("rider" and "dest" in rng.SESSION_DRAW_ORDER): home-cell coin, rider choice, destination.
_N_CHOICE_UNIFORMS = 3
_SCALAR_COLUMNS = ("u_book", "u_target", "u_explore", "u_explore_arm", "trip_noise", "e_cancel", "u_score")


def session_rate(cfg: Config, cell_weight: np.ndarray, hour: int) -> np.ndarray:
    """Expected sessions per cell in one tick: ``base * demand_scale * w_z * hour_profile[h] * tick_s / 3600``."""
    d = cfg.demand
    return d.base_sessions_per_cell_h * d.demand_scale * cell_weight * d.hour_profile[hour] * cfg.time.tick_s / 3600.0


def spawn(ctx: SimContext, t: float) -> None:
    """Engine step 4: open this tick's sessions."""
    cfg, clock, world, rng = ctx.cfg, ctx.clock, ctx.world, ctx.rng
    n_cells = world.n_cells
    day, tick_of_day, hour = clock.day_of(t), clock.tick_of_day(t), clock.hour_of(t)

    # Session counts: one DEMAND generator per (day, tick_of_day, cell). A zero rate needs no draw.
    lam = session_rate(cfg, world.cell_weight, hour)
    if ctx.shocks is not None:
        lam = lam * ctx.shocks.demand_factor(t)     # day factor, rain, events; same for every policy
    counts = np.zeros(n_cells, dtype=np.int64)
    for cell in np.flatnonzero(lam > 0):
        counts[cell] = rng.rng_for(Stream.DEMAND, day, tick_of_day, int(cell)).poisson(lam[cell])
    total = int(counts.sum())
    if total == 0:
        ctx.tick_sessions = (ctx.sessions.n, ctx.sessions.n)
        return

    # One SESSION generator per session, consumed in rng.SESSION_DRAW_ORDER, always in full.
    session_id = np.empty(total, dtype=np.int64)
    pu_cell = np.repeat(np.arange(n_cells), counts)
    u_choice = np.empty((total, _N_CHOICE_UNIFORMS))
    scalars = np.empty((total, len(_SCALAR_COLUMNS)))
    sigma = cfg.demand.trip_time_noise_sigma
    row = 0
    for cell in np.flatnonzero(counts):
        ids = session_ids_for_tick(day, tick_of_day, int(cell), int(counts[cell]),
                                   n_cells=n_cells, ticks_per_day=clock.ticks_per_day)
        for sid in ids.tolist():
            gen = rng.rng_for(Stream.SESSION, sid)
            u_choice[row] = gen.random(_N_CHOICE_UNIFORMS)
            scalars[row] = draw_session_scalars(gen, sigma)
            session_id[row] = sid
            row += 1

    rider_id = _pick_riders(world, cfg.demand.home_cell_share, pu_cell, u_choice[:, 0], u_choice[:, 1])
    do_cell = _pick_destinations(world, pu_cell, u_choice[:, 2])

    sessions = ctx.sessions
    start, stop = sessions.reserve(total)
    rows = slice(start, stop)
    sessions.session_id[rows] = session_id
    sessions.rider_id[rows] = rider_id
    sessions.open_time_s[rows] = t
    sessions.day[rows] = day
    sessions.hour[rows] = hour
    sessions.slot[rows] = clock.slot_of(t)
    sessions.slot_of_day[rows] = clock.slot_of_day(t)
    sessions.pu_cell[rows] = pu_cell
    sessions.do_cell[rows] = do_cell
    sessions.in_window[rows] = clock.in_window(t)
    for j, name in enumerate(_SCALAR_COLUMNS):
        getattr(sessions, name)[rows] = scalars[:, j]

    ctx.slot_counters.n_sessions += counts.astype(ctx.slot_counters.n_sessions.dtype)  # by open time and cell
    ctx.tick_sessions = (start, stop)


def _pick_riders(world, home_cell_share: float, pu_cell: np.ndarray, u_home: np.ndarray,
                 u_rider: np.ndarray) -> np.ndarray:
    """Rider of each session.

    With probability ``home_cell_share`` a rider living in the pickup cell, otherwise
    any rider; inside the chosen group the probability is proportional to ``x_freq``.
    A cell without residents always draws from the whole population.
    """
    tb = world.tables
    lo, hi = tb.home_start[pu_cell], tb.home_start[pu_cell + 1]
    local = (u_home < home_cell_share) & (hi > lo)

    rider = np.searchsorted(tb.all_cdf, u_rider, side="right")
    np.minimum(rider, len(tb.all_cdf) - 1, out=rider)
    if local.any():
        pos = np.searchsorted(tb.home_cdf, pu_cell[local] + u_rider[local], side="right")
        pos = np.clip(pos, lo[local], hi[local] - 1)   # guards the float edge at the end of a cell's block
        rider[local] = tb.home_order[pos]
    return rider


def _pick_destinations(world, pu_cell: np.ndarray, u_dest: np.ndarray) -> np.ndarray:
    """Destination cell: ``P(d | o) ∝ w_d * exp(-D[o, d] / dest_decay_rings)``."""
    n_cells = world.n_cells
    pos = np.searchsorted(world.tables.dest_cdf, pu_cell + u_dest, side="right") - pu_cell * n_cells
    return np.clip(pos, 0, n_cells - 1)
