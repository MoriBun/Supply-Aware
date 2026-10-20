"""Keyed random streams for common random numbers.

Every random draw goes through ``rng.rng_for(stream, *key)``. A generator is a
pure function of (seed, stream, key), so switching policy cannot shift the
random numbers of any session, order, driver or (cell, slot).

This module also fixes the CRN contract pieces that several modules share:
the stable ``session_id``, the order in which a
session's pre-drawn numbers are taken from its generator and
the integer ``kind`` codes of the CELLSLOT and SHOCK streams.
"""

from __future__ import annotations

import enum
import hashlib
import operator
import struct
from typing import NamedTuple

import numpy as np


class Stream(enum.IntEnum):
    """Random streams. The integer codes are part of the CRN contract: never renumber."""

    WORLD = 1          # cell weights, riders, drivers; seeded by world_seed, not run_seed
    DEMAND = 2         # key (day, tick_of_day, cell): Poisson session counts
    SESSION = 3        # key (session_id): rider, destination, pre-drawn numbers
    CELLSLOT = 4       # key (kind, level_code, cell_or_cluster, slot_or_block): legacy epsilon, switchback
    RIDER = 5          # key (rider_id): arm of rider-level A/B
    DRIVER = 6         # key (driver_id, counter): repositioning
    SESSION_BATCH = 7  # key (day, tick_of_day, cell): optional batched SESSION draws
    SHOCK = 8          # key (kind, day, ...): day-level shock schedule, same under every policy


class CellSlotKind(enum.IntEnum):
    """``kind`` codes for the CELLSLOT stream (part of the CRN contract: never renumber)."""

    LEGACY_EPS = 1     # epsilon coin of LegacyPolicy, key (kind, 0, cell, slot)
    SWITCHBACK = 2     # on/off of a (cluster, block), key (kind, level_code, cluster_id, block)


class ShockKind(enum.IntEnum):
    """``kind`` codes for the SHOCK stream (part of the CRN contract: never renumber).

    Keys hold the day and, for per-venue or per-driver draws, an index; never anything
    that depends on the policy, so every policy meets the same shocks.
    """

    DAY_DEMAND = 1     # key (kind, day): city-wide demand multiplier of the day
    EVENT = 2          # key (kind, day, venue_idx): event or not, end time
    RAIN = 3           # key (kind, day): rain or not, start, duration
    ATTEND = 4         # key (kind, day, driver_id): works today or not, off during rain or not
    JITTER = 5         # key (kind, day, driver_id): shift-start offset of the day


def cluster_level_code(level) -> int:
    """Integer code of ``experiment.cluster_level`` (1, 7 or "all") for CELLSLOT keys."""
    if level == "all":
        return 0
    if level in (1, 7):
        return int(level)
    raise ValueError(f"unknown cluster_level {level!r}")


_INT64 = struct.Struct("<q")


def hash64(*values: int) -> int:
    """Stable 64-bit hash of a tuple of integers (same on every process and platform)."""
    try:
        packed = b"".join(_INT64.pack(operator.index(v)) for v in values)
    except struct.error as exc:
        raise ValueError(f"key values must fit in int64: {values!r}") from exc
    return int.from_bytes(hashlib.blake2b(packed, digest_size=8).digest(), "little")


class Rng:
    """Factory of keyed generators for one run.

    ``WORLD`` streams are seeded by ``world_seed`` so the world (cell weights,
    riders, drivers) is identical across run seeds; every other stream is
    seeded by ``run_seed``.
    """

    def __init__(self, run_seed: int, world_seed: int) -> None:
        self.run_seed = operator.index(run_seed)
        self.world_seed = operator.index(world_seed)

    @classmethod
    def from_config(cls, cfg) -> Rng:
        return cls(run_seed=cfg.meta.run_seed, world_seed=cfg.meta.world_seed)

    def rng_for(self, stream: Stream, *key: int) -> np.random.Generator:
        """Independent generator for ``(stream, *key)``; keys must be integers."""
        stream = Stream(stream)
        if not key:
            raise ValueError(f"{stream.name}: a stream needs a non-empty key")
        seed = self.world_seed if stream is Stream.WORLD else self.run_seed
        return np.random.Generator(np.random.Philox(key=hash64(seed, stream.value, *key)))


# ---------------------------------------------------------------------------
# Stable session ids
# ---------------------------------------------------------------------------

MAX_SESSIONS_PER_CELL_TICK = 1000  # k < 1000, otherwise raise


def ticks_per_day(tick_s: int) -> int:
    if tick_s <= 0 or 86400 % tick_s:
        raise ValueError(f"tick_s must divide 86400, got {tick_s}")
    return 86400 // tick_s


def make_session_id(day: int, tick_of_day: int, cell: int, k: int, *, n_cells: int, ticks_per_day: int) -> int:
    """``((day*ticks_per_day + tick_of_day)*N + cell)*1000 + k`` with range checks."""
    if not 0 <= k < MAX_SESSIONS_PER_CELL_TICK:
        raise ValueError(f"more than {MAX_SESSIONS_PER_CELL_TICK} sessions in one (cell, tick): k={k}")
    if not 0 <= cell < n_cells:
        raise ValueError(f"cell {cell} out of range 0..{n_cells - 1}")
    if not 0 <= tick_of_day < ticks_per_day:
        raise ValueError(f"tick_of_day {tick_of_day} out of range 0..{ticks_per_day - 1}")
    if day < 0:
        raise ValueError(f"day must be >= 0, got {day}")
    return ((day * ticks_per_day + tick_of_day) * n_cells + cell) * MAX_SESSIONS_PER_CELL_TICK + k


def session_ids_for_tick(day: int, tick_of_day: int, cell: int, n_new: int, *, n_cells: int,
                         ticks_per_day: int) -> np.ndarray:
    """Ids of the ``n_new`` sessions spawned in one (cell, tick), k = 0..n_new-1 (int64)."""
    if n_new == 0:
        return np.zeros(0, dtype=np.int64)
    first = make_session_id(day, tick_of_day, cell, 0, n_cells=n_cells, ticks_per_day=ticks_per_day)
    if n_new > MAX_SESSIONS_PER_CELL_TICK:
        raise ValueError(f"more than {MAX_SESSIONS_PER_CELL_TICK} sessions in one (cell, tick): {n_new}")
    return first + np.arange(n_new, dtype=np.int64)


def split_session_id(session_id: int, *, n_cells: int, ticks_per_day: int) -> tuple[int, int, int, int]:
    """Inverse of :func:`make_session_id`: ``(day, tick_of_day, cell, k)``."""
    sid = operator.index(session_id)
    sid, k = divmod(sid, MAX_SESSIONS_PER_CELL_TICK)
    sid, cell = divmod(sid, n_cells)
    day, tick_of_day = divmod(sid, ticks_per_day)
    return day, tick_of_day, cell, k


# ---------------------------------------------------------------------------
# Pre-drawn numbers of a session
# ---------------------------------------------------------------------------

# The SESSION generator of a session is consumed in exactly this order, always in
# full, whatever the policy. Changing it changes every random number of every run.
SESSION_DRAW_ORDER = (
    "rider",          # demand.py: home-cell coin, then rider choice
    "dest",           # demand.py: destination cell
    "u_book",         # M4 booking decision
    "u_target",       # LegacyPolicy rider targeting
    "u_explore",      # LegacyPolicy explore slice
    "u_explore_arm",  # LegacyPolicy explore arm
    "trip_noise",     # M6 trip duration multiplier
    "e_cancel",       # M7 cumulative-hazard threshold
    "u_score",        # scores.random
)


class SessionScalars(NamedTuple):
    u_book: float
    u_target: float
    u_explore: float
    u_explore_arm: float
    trip_noise: float
    e_cancel: float
    u_score: float


def draw_session_scalars(gen: np.random.Generator, trip_time_noise_sigma: float) -> SessionScalars:
    """Draw the scalar part of :data:`SESSION_DRAW_ORDER` (everything after ``dest``).

    Call it once per session, after rider and destination were chosen from the
    same generator, and never skip it.
    """
    u_book, u_target, u_explore, u_explore_arm = gen.random(4)
    trip_noise = float(gen.lognormal(0.0, trip_time_noise_sigma))
    e_cancel = float(gen.exponential(1.0))
    u_score = float(gen.random())
    return SessionScalars(float(u_book), float(u_target), float(u_explore), float(u_explore_arm),
                          trip_noise, e_cancel, u_score)
