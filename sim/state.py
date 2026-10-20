"""Struct-of-arrays state shared by every module (docs/schema.md).

Contract pieces shared by the modules:
- status / mechanism codes (int8) and their strings in the logged tables;
- column lists of the session and order buffers with dtype, fill value and the
  engine step that first writes them;
- ``DriverState`` arrays, per-cell counters, per-slot accumulators;
- ``Clock`` (tick / slot / window / budget period arithmetic) and ``SimContext``,
  the one object every engine step receives.

``BudgetLedger`` lives in ``sim/budget.py`` and is re-exported here.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from sim.budget import BudgetLedger  # noqa: F401  (re-export)
from sim.config import Config, budget_period_min, eval_window_min
from sim.rng import Rng, ticks_per_day

if TYPE_CHECKING:  # avoid import cycles; these are only type names here
    from sim.monitor import MarketMonitor
    from sim.policies.base import Policy
    from sim.population import World
    from sim.pricing import VoucherLayer

NAN = float("nan")


# ---------------------------------------------------------------------------
# Codes (int8 in buffers) and their strings in the logged tables
# ---------------------------------------------------------------------------


class DriverStatus(enum.IntEnum):
    OFFLINE = 0
    IDLE = 1
    EN_ROUTE = 2
    ON_TRIP = 3
    REPOSITIONING = 4


class OrderStatus(enum.IntEnum):
    WAITING = 0
    MATCHED = 1
    ON_TRIP = 2
    COMPLETED = 3
    ABANDONED = 4
    CANCELLED = 5
    TRUNCATED = 6


ORDER_STATUS_NAMES = {
    OrderStatus.WAITING: "Waiting", OrderStatus.MATCHED: "Matched", OrderStatus.ON_TRIP: "OnTrip",
    OrderStatus.COMPLETED: "Completed", OrderStatus.ABANDONED: "Abandoned",
    OrderStatus.CANCELLED: "Cancelled", OrderStatus.TRUNCATED: "Truncated",
}
OPEN_ORDER_STATUSES = (OrderStatus.WAITING, OrderStatus.MATCHED, OrderStatus.ON_TRIP)
TERMINAL_ORDER_STATUSES = (OrderStatus.COMPLETED, OrderStatus.ABANDONED, OrderStatus.CANCELLED,
                           OrderStatus.TRUNCATED)


class Mechanism(enum.IntEnum):
    """``assign_mechanism`` codes (docs/schema.md)."""

    LEGACY_RULE = 0
    LEGACY_EPS = 1
    EXPLORE = 2
    EXPERIMENT = 3
    THRESHOLD = 4
    FIXED = 5


MECHANISM_NAMES = {
    Mechanism.LEGACY_RULE: "legacy_rule", Mechanism.LEGACY_EPS: "legacy_eps", Mechanism.EXPLORE: "explore",
    Mechanism.EXPERIMENT: "experiment", Mechanism.THRESHOLD: "threshold", Mechanism.FIXED: "fixed",
}


class CancelReason(enum.IntEnum):
    NONE = 0
    RIDER_EN_ROUTE = 1


CANCEL_REASON_NAMES = {CancelReason.NONE: "", CancelReason.RIDER_EN_ROUTE: "rider_en_route"}


# Columns that must never appear in observed/ or market/ tables (docs/schema.md).
HIDDEN_COLUMNS = frozenset({
    "u_latent", "alpha", "beta_price", "beta_eta", "delta_promo", "max_wait_min", "propensity_true",
    "p_request_treat", "p_request_control", "direct_request_effect_fixed_market",
    "u_book", "u_target", "u_explore", "u_explore_arm", "u_score", "trip_noise", "e_cancel",
})
# Pre-drawn numbers a policy may consume through SessionBatch. Everything
# else in HIDDEN_COLUMNS is forbidden in SessionBatch.
POLICY_UNIFORMS = frozenset({"u_target", "u_explore", "u_explore_arm", "u_score"})


# ---------------------------------------------------------------------------
# Column specs and the generic struct-of-arrays buffer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    dtype: str
    fill: Any
    step: str                       # engine step that first writes it
    hidden: bool = False            # logged to hidden/ only
    internal: bool = False          # engine bookkeeping, never logged
    codes: dict | None = None       # int8 code -> string in the logged table


def _col(name, dtype, fill, step, **kw) -> Column:
    return Column(name, dtype, fill, step, **kw)


def decode_codes(column: Column, values: np.ndarray) -> np.ndarray:
    """Strings of the logged table for an int8 code column; unknown codes (e.g. -1) become ``""``.

    The logger calls this for every column with ``codes``;
    buffers never hold strings.
    """
    if column.codes is None:
        raise ValueError(f"column {column.name} has no code map")
    width = max(len(s) for s in column.codes.values())
    table = np.full(256, "", dtype=f"U{max(width, 1)}")
    for code, text in column.codes.items():
        table[int(code) + 128] = text
    return table[np.asarray(values, dtype=np.int16) + 128]


# observed/sessions (docs/schema.md) + hidden/sessions_hidden + internal bookkeeping.
# Steps: spawn (4), quote (5), decide (6), match (7), advance (1), expire (3), en_route (8).
SESSION_COLUMNS: tuple[Column, ...] = (
    _col("session_id", "int64", -1, "spawn"),
    _col("rider_id", "int32", -1, "spawn"),
    _col("open_time_s", "float64", NAN, "spawn"),
    _col("day", "int16", -1, "spawn"),
    _col("hour", "int16", -1, "spawn"),
    _col("slot", "int16", -1, "spawn"),
    _col("slot_of_day", "int16", -1, "spawn"),
    _col("pu_cell", "int16", -1, "spawn"),
    _col("do_cell", "int16", -1, "spawn"),
    _col("quoted_fare_vnd", "float32", NAN, "quote"),
    _col("voucher_value_vnd", "float32", 0.0, "quote"),
    _col("quoted_eta_min", "float32", NAN, "quote"),
    _col("no_supply", "bool", False, "quote"),
    _col("promo_on_cell", "bool", False, "quote"),
    _col("arm", "int8", 0, "quote"),
    _col("assign_mechanism", "int8", -1, "quote", codes=MECHANISM_NAMES),
    _col("propensity", "float32", NAN, "quote"),
    _col("cell_propensity", "float32", NAN, "quote"),
    _col("cluster_id", "int16", -1, "quote"),
    _col("block", "int32", -1, "quote"),
    _col("in_burnin", "bool", False, "quote"),
    _col("budget_blocked", "bool", False, "quote"),
    _col("budget_period", "int16", -1, "quote"),
    _col("score", "float32", NAN, "quote"),
    _col("slack_hat", "float32", NAN, "quote"),
    _col("lag_slot_same_block", "bool", True, "quote"),      # slot k-1 in the same switchback block
    _col("requested", "bool", False, "decide"),
    _col("in_window", "bool", False, "spawn"),
    # hidden/sessions_hidden
    _col("propensity_true", "float32", NAN, "quote", hidden=True),
    _col("p_request_treat", "float32", NAN, "decide", hidden=True),
    _col("p_request_control", "float32", NAN, "decide", hidden=True),
    _col("direct_request_effect_fixed_market", "float32", NAN, "decide", hidden=True),
    _col("u_book", "float64", NAN, "spawn", hidden=True),
    _col("u_target", "float64", NAN, "spawn", hidden=True),
    _col("u_explore", "float64", NAN, "spawn", hidden=True),
    _col("u_explore_arm", "float64", NAN, "spawn", hidden=True),
    _col("u_score", "float64", NAN, "spawn", hidden=True),
    _col("trip_noise", "float64", NAN, "spawn", hidden=True),
    _col("e_cancel", "float64", NAN, "spawn", hidden=True),
    # internal
    _col("order_idx", "int64", -1, "decide", internal=True),
    _col("voucher_dong", "int64", 0, "quote", internal=True),
)

# observed/orders (docs/schema.md) + internal bookkeeping. order_id = session_id.
ORDER_COLUMNS: tuple[Column, ...] = (
    _col("order_id", "int64", -1, "decide"),
    _col("session_id", "int64", -1, "decide"),
    _col("rider_id", "int32", -1, "decide"),
    _col("driver_id", "int32", -1, "match"),
    _col("pu_cell", "int16", -1, "decide"),
    _col("do_cell", "int16", -1, "decide"),
    _col("driver_origin_cell", "int16", -1, "match"),
    _col("request_time_s", "float64", NAN, "decide"),
    _col("matched_time_s", "float64", NAN, "match"),
    _col("pickup_eta_min", "float32", NAN, "match"),
    _col("pickup_time_s", "float64", NAN, "advance"),
    _col("dropoff_time_s", "float64", NAN, "advance"),
    _col("trip_time_min", "float32", NAN, "match"),
    _col("trip_km", "float32", NAN, "match"),
    _col("gross_fare_vnd", "float32", NAN, "decide"),
    _col("voucher_value_vnd", "float32", 0.0, "decide"),
    _col("net_fare_vnd", "float32", NAN, "decide"),
    _col("driver_pay_vnd", "float32", 0.0, "advance"),
    _col("platform_profit_vnd", "float32", 0.0, "advance"),
    _col("status", "int8", int(OrderStatus.WAITING), "decide", codes=ORDER_STATUS_NAMES),
    _col("cancel_reason", "int8", int(CancelReason.NONE), "en_route", codes=CANCEL_REASON_NAMES),
    _col("in_window", "bool", False, "decide"),
    # internal
    _col("session_idx", "int64", -1, "decide", internal=True),
    _col("voucher_dong", "int64", 0, "decide", internal=True),
)


class SoABuffer:
    """Preallocated struct-of-arrays table that grows by doubling.

    Columns are exposed as attributes (``buf.session_id``) and are the *full*
    capacity arrays; use ``buf.col(name)`` for the filled prefix ``[:n]``. A
    ``grow`` reallocates every array, so never cache a column across an append.
    """

    columns: tuple[Column, ...] = ()

    def __init__(self, columns: tuple[Column, ...] | None = None, capacity: int = 1024) -> None:
        self.columns = tuple(columns) if columns is not None else type(self).columns
        if not self.columns:
            raise ValueError("a buffer needs at least one column")
        self.capacity = max(1, int(capacity))
        self.n = 0
        self._arrays: dict[str, np.ndarray] = {
            c.name: np.full(self.capacity, c.fill, dtype=c.dtype) for c in self.columns
        }

    def __getattr__(self, name: str) -> np.ndarray:
        arrays = self.__dict__.get("_arrays")
        if arrays is not None and name in arrays:
            return arrays[name]
        raise AttributeError(name)

    def __len__(self) -> int:
        return self.n

    def col(self, name: str) -> np.ndarray:
        """View of the filled prefix of a column."""
        return self._arrays[name][: self.n]

    def names(self, *, hidden: bool | None = None, internal: bool | None = None) -> list[str]:
        out = []
        for c in self.columns:
            if hidden is not None and c.hidden != hidden:
                continue
            if internal is not None and c.internal != internal:
                continue
            out.append(c.name)
        return out

    def reserve(self, k: int) -> tuple[int, int]:
        """Allocate ``k`` rows filled with defaults; returns ``(start, stop)``."""
        if k < 0:
            raise ValueError("k must be >= 0")
        while self.n + k > self.capacity:
            self.grow()
        start = self.n
        self.n += k
        return start, self.n

    def append(self, **values: Any) -> int:
        """Append one row (unspecified columns keep their fill value); returns its index."""
        unknown = set(values) - set(self._arrays)
        if unknown:
            raise KeyError(f"unknown column(s) {sorted(unknown)}")
        idx, _ = self.reserve(1)
        for name, value in values.items():
            self._arrays[name][idx] = value
        return idx

    def grow(self) -> None:
        new_capacity = self.capacity * 2
        for c in self.columns:
            old = self._arrays[c.name]
            new = np.full(new_capacity, c.fill, dtype=c.dtype)
            new[: self.n] = old[: self.n]
            self._arrays[c.name] = new
        self.capacity = new_capacity

    def to_dict(self, *, hidden: bool | None = None, internal: bool | None = False) -> dict[str, np.ndarray]:
        return {name: self.col(name) for name in self.names(hidden=hidden, internal=internal)}


class SessionBuffer(SoABuffer):
    columns = SESSION_COLUMNS


class OrderBuffer(SoABuffer):
    columns = ORDER_COLUMNS

    def count_open(self) -> int:
        status = self.col("status")
        return int(np.isin(status, [int(s) for s in OPEN_ORDER_STATUSES]).sum())


# ---------------------------------------------------------------------------
# Drivers and counters
# ---------------------------------------------------------------------------


class DriverState:
    """Fleet as parallel numpy arrays. Index = driver_id."""

    def __init__(self, n: int) -> None:
        self.n = int(n)
        self.status = np.full(n, int(DriverStatus.OFFLINE), dtype=np.int8)
        self.cell = np.full(n, -1, dtype=np.int16)          # current (or target, while moving) cell
        self.origin_cell = np.full(n, -1, dtype=np.int16)   # cell left at match / reposition time
        self.busy_until = np.full(n, np.inf, dtype=np.float64)
        self.idle_since = np.full(n, NAN, dtype=np.float64)
        self.order = np.full(n, -1, dtype=np.int64)         # index into OrderBuffer, -1 if none
        self.shift_start_s = np.full(n, NAN, dtype=np.float64)  # current/next shift, absolute seconds
        self.shift_end_s = np.full(n, NAN, dtype=np.float64)
        self.earnings_vnd = np.zeros(n, dtype=np.float64)
        self.reposition_count = np.zeros(n, dtype=np.int32)  # DRIVER stream counter

    def count_by_status(self) -> dict[DriverStatus, int]:
        counts = np.bincount(self.status.astype(np.int64), minlength=len(DriverStatus))
        return {s: int(counts[int(s)]) for s in DriverStatus}


class CellCounters:
    """Live per-cell counts maintained by the modules."""

    def __init__(self, n_cells: int) -> None:
        self.n_cells = int(n_cells)
        self.idle = np.zeros(n_cells, dtype=np.int32)      # idle drivers in the cell
        self.enroute = np.zeros(n_cells, dtype=np.int32)   # en-route drivers whose rider is in the cell
        self.ontrip = np.zeros(n_cells, dtype=np.int32)    # on-trip drivers picked up in the cell
        self.waiting = np.zeros(n_cells, dtype=np.int32)   # Waiting orders in the cell


class SlotCounters:
    """Accumulators of the current slot, reset by the monitor after publishing."""

    def __init__(self, n_cells: int) -> None:
        self.n_cells = int(n_cells)
        self.sum_idle = np.zeros(n_cells, dtype=np.float64)
        self.sum_enroute = np.zeros(n_cells, dtype=np.float64)
        self.sum_ontrip = np.zeros(n_cells, dtype=np.float64)
        self.sum_waiting = np.zeros(n_cells, dtype=np.float64)
        self.n_ticks = 0
        self.n_sessions = np.zeros(n_cells, dtype=np.int32)
        self.n_offers = np.zeros(n_cells, dtype=np.int32)
        self.n_requests = np.zeros(n_cells, dtype=np.int32)
        self.n_matched = np.zeros(n_cells, dtype=np.int32)
        self.n_completed = np.zeros(n_cells, dtype=np.int32)
        self.n_abandoned = np.zeros(n_cells, dtype=np.int32)
        self.n_cancelled = np.zeros(n_cells, dtype=np.int32)
        self.sum_pickup_eta_min = np.zeros(n_cells, dtype=np.float64)
        self.n_pickup_eta = np.zeros(n_cells, dtype=np.int32)
        self.voucher_spent_dong = np.zeros(n_cells, dtype=np.int64)

    def reset(self) -> None:
        for name, arr in vars(self).items():
            if isinstance(arr, np.ndarray):
                arr.fill(0)
        self.n_ticks = 0


# ---------------------------------------------------------------------------
# Clock and the per-run context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    """Tick / slot / window / budget-period arithmetic."""

    tick_s: int
    slot_s: int
    warmup_s: int
    window_s: int
    cooldown_max_s: int
    period_s: int
    ticks_per_day: int
    slots_per_day: int

    @classmethod
    def from_config(cls, cfg: Config) -> Clock:
        t = cfg.time
        return cls(
            tick_s=t.tick_s,
            slot_s=t.slot_min * 60,
            warmup_s=t.warmup_min * 60,
            window_s=eval_window_min(cfg) * 60,
            cooldown_max_s=t.cooldown_max_min * 60,
            period_s=budget_period_min(cfg) * 60,
            ticks_per_day=ticks_per_day(t.tick_s),
            slots_per_day=1440 // t.slot_min,
        )

    @property
    def window_start_s(self) -> int:
        return self.warmup_s

    @property
    def window_end_s(self) -> int:
        return self.warmup_s + self.window_s

    @property
    def n_periods(self) -> int:
        return math.ceil(self.window_s / self.period_s)

    @property
    def window_slots(self) -> range:
        return range(self.window_start_s // self.slot_s, self.window_end_s // self.slot_s)

    def tick_of(self, t: float) -> int:
        return int(t // self.tick_s)

    def tick_of_day(self, t: float) -> int:
        return int((t % 86400) // self.tick_s)

    def slot_of(self, t: float) -> int:
        return int(t // self.slot_s)

    def day_of(self, t: float) -> int:
        return int(t // 86400)

    def hour_of(self, t: float) -> int:
        return int((t % 86400) // 3600)

    def slot_of_day(self, t: float) -> int:
        return int((t % 86400) // self.slot_s)

    def in_window(self, t: float) -> bool:
        return self.window_start_s <= t < self.window_end_s

    def period_of(self, t: float) -> int:
        """Budget period of a time, -1 in warm-up."""
        if t < self.window_start_s:
            return -1
        return int((t - self.window_start_s) // self.period_s)


@dataclass
class SimContext:
    """Everything an engine step may touch. Steps have the signature ``step(ctx, t)``."""

    cfg: Config
    clock: Clock
    world: World
    rng: Rng
    policy: Policy
    drivers: DriverState
    sessions: SessionBuffer
    orders: OrderBuffer
    cells: CellCounters
    slot_counters: SlotCounters
    ledger: BudgetLedger
    layer: VoucherLayer
    monitor: MarketMonitor
    tick_sessions: tuple[int, int] = (0, 0)   # [start, stop) rows of sessions spawned this tick
    current_slot: int = -1
    profile: dict[str, float] = field(default_factory=dict)
    shocks: Any = None                         # sim.shocks.ShockSchedule when shocks.enabled, else None

    @property
    def n_cells(self) -> int:
        return self.cells.n_cells
