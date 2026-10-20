"""Policy interface and the data that crosses it.

Contract:
- ``SessionBatch``: observed columns of the sessions quoted in one tick plus the
  pre-drawn policy uniforms; never a hidden column.
- ``CellDecision`` (one per slot) and ``OfferDecision`` (one per batch).
- ``SnapshotStore`` / ``SnapshotView``: published market snapshots; a view for
  slot ``k`` refuses any slot ``>= k`` (hard rule 2) and returns NaN before the
  first publish.
- ``LegacyHiddenView``: the only door to ``u_latent`` (hard rule 3).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable

import numpy as np

from sim.budget import BudgetLedger
from sim.state import HIDDEN_COLUMNS, POLICY_UNIFORMS, Mechanism

NAN = float("nan")

# market/slot_snapshots columns (docs/schema.md) except run_id and seed, which the logger adds.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "cell", "slot", "day", "slot_of_day", "hour",
    "idle_avg", "enroute_avg", "ontrip_avg", "waiting_avg", "slack", "utilization", "mean_pickup_eta_min",
    "n_sessions", "n_offers", "n_requests", "n_matched", "n_completed", "n_abandoned", "n_cancelled",
    "voucher_spent_vnd", "promo_on", "cell_propensity", "assign_mechanism_cell", "cluster_id", "block",
    "in_burnin", "slack_lag_slot", "slack_lag_day", "published_at_s",
)


# ---------------------------------------------------------------------------
# Data crossing the policy boundary
# ---------------------------------------------------------------------------


@dataclass
class SessionBatch:
    """Sessions quoted in one tick, observed columns only (names as in docs/schema.md).

    ``u_target``, ``u_explore``, ``u_explore_arm`` and ``u_score`` are the
    pre-drawn numbers a policy may consume; a policy never
    draws from the SESSION stream itself.
    """

    session_id: np.ndarray      # int64
    rider_id: np.ndarray        # int32
    open_time_s: np.ndarray     # float64
    day: np.ndarray             # int16
    hour: np.ndarray            # int16
    slot: np.ndarray            # int16
    slot_of_day: np.ndarray     # int16
    pu_cell: np.ndarray         # int16
    do_cell: np.ndarray         # int16
    x_freq: np.ndarray          # float32
    x_tenure: np.ndarray        # float32
    x_segment: np.ndarray       # int8
    quoted_fare_vnd: np.ndarray  # float32
    quoted_eta_min: np.ndarray   # float32
    no_supply: np.ndarray        # bool
    u_target: np.ndarray         # float64
    u_explore: np.ndarray        # float64
    u_explore_arm: np.ndarray    # float64
    u_score: np.ndarray          # float64

    def __post_init__(self) -> None:
        n = len(self.session_id)
        for f in fields(self):
            arr = getattr(self, f.name)
            if not isinstance(arr, np.ndarray) or arr.ndim != 1 or len(arr) != n:
                raise ValueError(f"SessionBatch.{f.name} must be a 1-d array of length {n}")

    def __len__(self) -> int:
        return len(self.session_id)

    @property
    def n(self) -> int:
        return len(self.session_id)


FORBIDDEN_IN_BATCH = frozenset(HIDDEN_COLUMNS - POLICY_UNIFORMS)
_leak = FORBIDDEN_IN_BATCH & {f.name for f in dataclasses.fields(SessionBatch)}
assert not _leak, f"hidden columns in SessionBatch: {sorted(_leak)}"


@dataclass
class CellDecision:
    """Promotion state of every cell for one slot."""

    slot: int
    promo_on: np.ndarray          # bool [N]
    cell_propensity: np.ndarray   # float32 [N], NaN if unknown
    mechanism: np.ndarray         # int8 [N], Mechanism codes
    s_hat: np.ndarray             # float32 [N], forecast supply indicator, NaN if unused
    cluster_id: np.ndarray        # int16 [N], -1 if not an experiment
    block: int = -1               # -1 if not an experiment
    in_burnin: bool = False
    lag_same_block: bool = True   # slot k-1 in the same switchback block; True without blocks

    @classmethod
    def blank(cls, slot: int, n_cells: int, *, promo_on: bool = False,
              mechanism: Mechanism = Mechanism.FIXED) -> CellDecision:
        return cls(
            slot=int(slot),
            promo_on=np.full(n_cells, promo_on, dtype=bool),
            cell_propensity=np.full(n_cells, 1.0 if promo_on else 0.0, dtype=np.float32),
            mechanism=np.full(n_cells, int(mechanism), dtype=np.int8),
            s_hat=np.full(n_cells, NAN, dtype=np.float32),
            cluster_id=np.full(n_cells, -1, dtype=np.int16),
        )

    @property
    def n_cells(self) -> int:
        return len(self.promo_on)


@dataclass
class OfferDecision:
    """Policy output for one batch. Budget is applied afterwards by the voucher layer."""

    offer: np.ndarray            # bool [n]
    propensity: np.ndarray       # float32 [n], NaN if unknown
    mechanism: np.ndarray        # int8 [n]
    score: np.ndarray            # float32 [n], NaN if unused
    propensity_true: np.ndarray  # float32 [n], hidden; NaN unless LegacyPolicy

    @classmethod
    def blank(cls, n: int, *, mechanism: Mechanism = Mechanism.FIXED) -> OfferDecision:
        return cls(
            offer=np.zeros(n, dtype=bool),
            propensity=np.full(n, NAN, dtype=np.float32),
            mechanism=np.full(n, int(mechanism), dtype=np.int8),
            score=np.full(n, NAN, dtype=np.float32),
            propensity_true=np.full(n, NAN, dtype=np.float32),
        )

    def __len__(self) -> int:
        return len(self.offer)


# ---------------------------------------------------------------------------
# Published snapshots and the no-look-ahead view (hard rule 2)
# ---------------------------------------------------------------------------


class LookAheadError(RuntimeError):
    """A policy asked for a snapshot of the current or a future slot."""


class SnapshotStore:
    """Snapshots published by the monitor, in slot order without gaps."""

    def __init__(self, n_cells: int, slots_per_day: int) -> None:
        self.n_cells = int(n_cells)
        self.slots_per_day = int(slots_per_day)
        self._records: list[dict[str, np.ndarray]] = []

    @property
    def last_published_slot(self) -> int:
        return len(self._records) - 1

    def has(self, slot: int) -> bool:
        return 0 <= slot <= self.last_published_slot

    def publish(self, slot: int, record: dict[str, np.ndarray]) -> None:
        if slot != len(self._records):
            raise ValueError(f"snapshots must be published in order: expected slot {len(self._records)}, got {slot}")
        missing = set(SNAPSHOT_FIELDS) - set(record)
        extra = set(record) - set(SNAPSHOT_FIELDS)
        if missing or extra:
            raise ValueError(f"snapshot record fields: missing {sorted(missing)}, unexpected {sorted(extra)}")
        for name, arr in record.items():
            if not isinstance(arr, np.ndarray) or arr.shape != (self.n_cells,):
                raise ValueError(f"snapshot field {name} must be an array of shape ({self.n_cells},)")
        self._records.append(record)

    def get(self, field: str, slot: int) -> np.ndarray:
        if not self.has(slot):
            raise KeyError(f"slot {slot} not published")
        return self._records[slot][field]

    def records(self) -> list[dict[str, np.ndarray]]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)


class SnapshotView:
    """Read access to snapshots of slots ``< current_slot`` only."""

    def __init__(self, store: SnapshotStore, current_slot: int) -> None:
        self._store = store
        self.current_slot = int(current_slot)

    @property
    def n_cells(self) -> int:
        return self._store.n_cells

    @property
    def slots_per_day(self) -> int:
        return self._store.slots_per_day

    def available(self, slot: int) -> bool:
        return slot < self.current_slot and self._store.has(slot)

    def get(self, field: str, slot: int) -> np.ndarray:
        """Field of a published slot as float64 [N]; NaN before the first publish; error if slot >= current."""
        if field not in SNAPSHOT_FIELDS:
            raise KeyError(f"unknown snapshot field {field!r}")
        if slot >= self.current_slot:
            raise LookAheadError(f"slot {slot} is not published for decisions in slot {self.current_slot}")
        if not self._store.has(slot):
            return np.full(self._store.n_cells, NAN, dtype=np.float64)
        return self._store.get(field, slot).astype(np.float64)

    def lag(self, field: str, k: int = 1) -> np.ndarray:
        """Field of slot ``current_slot - k`` (k >= 1)."""
        if k < 1:
            raise LookAheadError("lag must be >= 1")
        return self.get(field, self.current_slot - k)

    def lag_day(self, field: str) -> np.ndarray:
        return self.lag(field, self._store.slots_per_day)


# ---------------------------------------------------------------------------
# Hidden view for LegacyPolicy only (hard rule 3)
# ---------------------------------------------------------------------------


class LegacyHiddenView:
    """``u_latent`` and ``zf`` by rider id. Constructed only for LegacyPolicy."""

    def __init__(self, u_latent: np.ndarray, zf: np.ndarray) -> None:
        if len(u_latent) != len(zf):
            raise ValueError("u_latent and zf must have the same length")
        self._u_latent = np.asarray(u_latent, dtype=np.float32)
        self._zf = np.asarray(zf, dtype=np.float32)

    @classmethod
    def from_world(cls, world) -> LegacyHiddenView:
        return cls(world.riders.u_latent, world.riders.zf)

    def u_latent_of(self, rider_id: np.ndarray) -> np.ndarray:
        return self._u_latent[np.asarray(rider_id)]

    def zf_of(self, rider_id: np.ndarray) -> np.ndarray:
        return self._zf[np.asarray(rider_id)]


class OracleHiddenView:
    """Booking-model coefficients (``alpha``, ``beta_price``, ``beta_eta``, ``delta_promo``) by rider id.

    Second gate to hidden variables after :class:`LegacyHiddenView` (hard rule 3).
    Built only by ``make_policy`` for ``policy.name = threshold`` with ``score_fn = oracle``; the run is
    recorded as ``hidden_view = "oracle"`` in ``meta/run_metadata``. Never reaches ``SessionBatch`` or the logs.
    """

    def __init__(self, alpha: np.ndarray, beta_price: np.ndarray, beta_eta: np.ndarray, delta_promo: np.ndarray) -> None:
        arrays = [np.asarray(a, dtype=np.float64) for a in (alpha, beta_price, beta_eta, delta_promo)]
        if len({len(a) for a in arrays}) != 1:
            raise ValueError("coefficient arrays must have the same length")
        self._alpha, self._beta_price, self._beta_eta, self._delta_promo = arrays

    @classmethod
    def from_world(cls, world) -> OracleHiddenView:
        r = world.riders
        return cls(r.alpha, r.beta_price, r.beta_eta, r.delta_promo)

    def coefficients_of(self, rider_id: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """``(alpha, beta_price, beta_eta, delta_promo)`` of the given riders, float64."""
        idx = np.asarray(rider_id)
        return self._alpha[idx], self._beta_price[idx], self._beta_eta[idx], self._delta_promo[idx]


# ---------------------------------------------------------------------------
# Policy protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Policy(Protocol):
    name: str

    def cell_state(self, slot: int, snapshots: SnapshotView) -> CellDecision:
        """Called at the start of every slot with a view of the slots before it."""

    def offer(self, batch: SessionBatch, cell_dec: CellDecision, ledger: BudgetLedger) -> OfferDecision:
        """Called in step 5 for the sessions of one tick. ``ledger`` is read-only here;
        the voucher layer applies the budget."""
