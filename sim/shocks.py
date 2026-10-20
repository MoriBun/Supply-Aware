"""Natural shocks of world W1: day-level demand, events at fixed venues, rain, driver attendance, shift jitter.

Values and their sources: ``config/default.yaml``, section ``shocks``.

The whole schedule is drawn before the run, from the SHOCK stream (seeded by ``run_seed``, keyed by
day and, for per-driver draws, ``driver_id``) and, for the venue cells, from the WORLD stream
(``WorldPart.VENUES``, seeded by ``world_seed``). No key depends on the policy, so every policy of a
seed meets the same shocks (CRN). The schedule lives on ``SimContext.shocks`` and is read only by the
engine steps (demand, supply, travel times); policies never see it (``SessionBatch`` and
``SnapshotView`` carry no shock field). With ``shocks.enabled = false`` nothing here is built and the
simulator runs exactly as before.

Effects:
- demand: the Poisson rate of every cell is multiplied by the day factor, by ``rain.demand_mult``
  while it rains, and, during the surge after an event, by ``mult_venue`` at the venue cell and
  ``mult_ring1`` at its six neighbours (overlapping events multiply);
- travel times: while it rains every travel time is divided by ``rain.speed_mult`` (the space holds a
  second, slower travel-time layer at hour index ``h + 24``, see :func:`travel_hour`);
- supply: a driver who does not work on a day skips that day's shift; shift starts move by the day's
  jitter; while it rains, drivers flagged for the day stop when idle and come back when it ends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from sim.config import Config
from sim.rng import Rng, ShockKind, Stream

_DAY_S = 86400.0


@dataclass
class Event:
    day: int
    venue_idx: int
    cell: int
    ring1: np.ndarray            # int, the six neighbours of the venue cell
    start_s: float               # surge window [start_s, end_s)
    end_s: float
    mult_venue: float
    mult_ring1: float


@dataclass
class ShockSchedule:
    """Shocks of one run (all days of the run, warm-up and cool-down included)."""

    n_days: int
    n_cells: int
    day_mult: np.ndarray                     # float64 [n_days]
    rain_start_s: np.ndarray                 # float64 [n_days], NaN = no rain that day
    rain_end_s: np.ndarray                   # float64 [n_days]
    rain_demand_mult: float
    rain_speed_mult: float
    events: list[Event]
    venue_cells: np.ndarray                  # int [n_venues]
    works: np.ndarray                        # bool [n_days, n_drivers]
    rain_off: np.ndarray                     # bool [n_days, n_drivers]: stops while it rains that day
    jitter_s: np.ndarray                     # float64 [n_days, n_drivers]
    # Run state of the supply side (mutated by sim.supply).
    paused: np.ndarray = field(default=None)       # bool [n_drivers]: offline because of rain
    pause_cell: np.ndarray = field(default=None)   # int16 [n_drivers]

    # --- queries -------------------------------------------------------------------------

    def day_of(self, t: float) -> int:
        return min(int(t // _DAY_S), self.n_days - 1)

    def raining(self, t: float) -> bool:
        d = self.day_of(t)
        return bool(self.rain_start_s[d] <= t < self.rain_end_s[d])   # NaN compares false

    def demand_factor(self, t: float) -> np.ndarray:
        """Multiplier of every cell's session rate at time ``t`` (float64 [n_cells])."""
        f = np.full(self.n_cells, self.day_mult[self.day_of(t)])
        if self.raining(t):
            f *= self.rain_demand_mult
        for ev in self.events:
            if ev.start_s <= t < ev.end_s:
                f[ev.cell] *= ev.mult_venue
                f[ev.ring1] *= ev.mult_ring1
        return f

    def table(self) -> dict[str, np.ndarray]:
        """Rows of ``meta/shocks`` (docs/schema.md): one per day factor, event, rain and attendance day."""
        rows: list[tuple] = []
        for d in range(self.n_days):
            rows.append((d, "day_demand", -1, d * _DAY_S, (d + 1) * _DAY_S, self.day_mult[d], 1.0, 1.0, 0))
            rows.append((d, "attendance", -1, d * _DAY_S, (d + 1) * _DAY_S, 1.0, 1.0, 1.0,
                         int((~self.works[d]).sum())))
            if not math.isnan(self.rain_start_s[d]):
                n_off = int((self.works[d] & self.rain_off[d]).sum())
                rows.append((d, "rain", -1, self.rain_start_s[d], self.rain_end_s[d], self.rain_demand_mult,
                             1.0, self.rain_speed_mult, n_off))
        for ev in self.events:
            rows.append((ev.day, "event", ev.cell, ev.start_s, ev.end_s, ev.mult_venue, ev.mult_ring1, 1.0, 0))
        rows.sort(key=lambda r: (r[3], r[1], r[2]))
        cols = list(zip(*rows)) if rows else [[]] * 9
        return {
            "day": np.asarray(cols[0], dtype=np.int16), "kind": np.asarray(cols[1], dtype=object),
            "cell": np.asarray(cols[2], dtype=np.int16),
            "start_s": np.asarray(cols[3], dtype=np.float64), "end_s": np.asarray(cols[4], dtype=np.float64),
            "demand_mult": np.asarray(cols[5], dtype=np.float32), "ring1_mult": np.asarray(cols[6], dtype=np.float32),
            "speed_mult": np.asarray(cols[7], dtype=np.float32), "n_drivers_off": np.asarray(cols[8], dtype=np.int32),
        }


def n_run_days(clock) -> int:
    """Days touched by a run: warm-up, window and the longest cool-down."""
    return int(math.ceil((clock.window_end_s + clock.cooldown_max_s) / _DAY_S))


def venue_cells(cfg: Config, rng: Rng, n_cells: int) -> np.ndarray:
    """Cells of every venue, in the order of ``shocks.events.venues`` rows; distinct cells, fixed by ``world_seed``."""
    from sim.population import WorldPart          # local import: sim.population imports sim.space only

    n_total = int(sum(int(row[0]) for row in cfg.shocks.events.venues))
    if n_total > n_cells:
        raise ValueError(f"shocks.events.venues asks for {n_total} venues on {n_cells} cells")
    gen = rng.rng_for(Stream.WORLD, WorldPart.VENUES, 0)
    return gen.choice(n_cells, size=n_total, replace=False).astype(np.int64)


def build_schedule(cfg: Config, world, rng: Rng, clock) -> ShockSchedule:
    """Draw the shocks of every day of the run."""
    sh = cfg.shocks
    n_days, n_cells, n_drivers = n_run_days(clock), world.n_cells, cfg.supply.fleet_size

    # Day factor: LogNormal with mean 1.
    sigma = sh.day_demand.sigma
    day_mult = np.array([
        float(np.exp(rng.rng_for(Stream.SHOCK, ShockKind.DAY_DEMAND, d).normal(-0.5 * sigma * sigma, sigma)))
        if sigma > 0 else 1.0 for d in range(n_days)])

    # Rain: one spell at most per day.
    rn = sh.rain
    rain_start = np.full(n_days, np.nan)
    rain_end = np.full(n_days, np.nan)
    for d in range(n_days):
        u_rain, u_start, u_len = rng.rng_for(Stream.SHOCK, ShockKind.RAIN, d).random(3)
        if u_rain < rn.p_per_day:
            start_h = rn.start_hour_range[0] + u_start * (rn.start_hour_range[1] - rn.start_hour_range[0])
            len_h = rn.duration_h_range[0] + u_len * (rn.duration_h_range[1] - rn.duration_h_range[0])
            rain_start[d] = d * _DAY_S + start_h * 3600.0
            rain_end[d] = rain_start[d] + len_h * 3600.0

    # Events: venue cells fixed by the world; each venue may host one event a day.
    cells = venue_cells(cfg, rng, n_cells)
    events: list[Event] = []
    v = 0
    for row in sh.events.venues:
        n, p_day, end_lo, end_hi, surge_min, mult_v, mult_r = row
        for _ in range(int(n)):
            cell = int(cells[v])
            ring1 = world.space.neighbors[cell]
            ring1 = ring1[ring1 >= 0].astype(np.int64)
            for d in range(n_days):
                u_event, u_end = rng.rng_for(Stream.SHOCK, ShockKind.EVENT, d, v).random(2)
                if u_event < p_day:
                    end_s = d * _DAY_S + (end_lo + u_end * (end_hi - end_lo)) * 3600.0
                    events.append(Event(day=d, venue_idx=v, cell=cell, ring1=ring1, start_s=end_s,
                                        end_s=end_s + surge_min * 60.0, mult_venue=float(mult_v),
                                        mult_ring1=float(mult_r)))
            v += 1

    # Drivers: attendance and rain stop (ATTEND), shift-start jitter (JITTER); one generator per (day, driver).
    works = np.ones((n_days, n_drivers), dtype=bool)
    rain_off = np.zeros((n_days, n_drivers), dtype=bool)
    jitter = np.zeros((n_days, n_drivers))
    sd_s = sh.shift_jitter.sd_min * 60.0
    for d in range(n_days):
        for i in range(n_drivers):
            u_work, u_off = rng.rng_for(Stream.SHOCK, ShockKind.ATTEND, d, i).random(2)
            works[d, i] = u_work < sh.attendance.p_work
            rain_off[d, i] = u_off < rn.driver_off_share
            if sd_s > 0:
                jitter[d, i] = rng.rng_for(Stream.SHOCK, ShockKind.JITTER, d, i).normal(0.0, sd_s)

    return ShockSchedule(
        n_days=n_days, n_cells=n_cells, day_mult=day_mult, rain_start_s=rain_start, rain_end_s=rain_end,
        rain_demand_mult=float(rn.demand_mult), rain_speed_mult=float(rn.speed_mult), events=events,
        venue_cells=cells, works=works, rain_off=rain_off, jitter_s=jitter,
        paused=np.zeros(n_drivers, dtype=bool), pause_cell=np.full(n_drivers, -1, dtype=np.int16))


def travel_hour(ctx, t: float, hour: int | np.ndarray) -> int | np.ndarray:
    """Hour index into ``space.T`` / ``space.speed_kmh``: ``hour + 24`` while it rains (slower layer)."""
    shocks = getattr(ctx, "shocks", None)
    if shocks is None:
        return hour
    if np.ndim(t) == 0:
        return hour + 24 if shocks.raining(float(t)) else hour
    rain = np.array([shocks.raining(float(x)) for x in np.asarray(t)])
    return np.asarray(hour) + 24 * rain
