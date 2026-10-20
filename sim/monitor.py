"""M11 MarketMonitor: per-(cell, slot) I, E, O, W, slack, utilization; publishes snapshots after each slot.

``accumulate`` runs every tick from the live counters, ``publish`` closes a slot
(record fields = docs/schema.md) and ``view`` hands the policy of slot k the
snapshots of slots < k. The store is a queue: ``publish`` must be called at the
end of slot k with slot k-1 already published, and ``view(k)`` refuses to exist
while slot k is published or slot k-1 is missing (hard rule 2).

Event counters are recorded through the ``on_*`` methods at the
time of the event and in the pickup cell; several events in the same cell and
tick add up (``np.add.at``). ``n_sessions`` is the exception: ``demand.spawn``
adds it from its per-cell Poisson counts.
"""

from __future__ import annotations

import numpy as np

from sim.budget import DONG_PER_VND
from sim.config import Config
from sim.policies.base import CellDecision, LookAheadError, SnapshotStore, SnapshotView
from sim.state import CellCounters, Clock, SlotCounters

NAN = float("nan")


class MarketMonitor:
    def __init__(self, cfg: Config, clock: Clock, n_cells: int) -> None:
        self.cfg = cfg
        self.clock = clock
        self.n_cells = int(n_cells)
        self.store = SnapshotStore(n_cells, clock.slots_per_day)

    # --- per tick --------------------------------------------------------------

    def accumulate(self, cells: CellCounters, acc: SlotCounters) -> None:
        """Step 10 of every tick: add the live per-cell counts to the slot sums."""
        acc.sum_idle += cells.idle
        acc.sum_enroute += cells.enroute
        acc.sum_ontrip += cells.ontrip
        acc.sum_waiting += cells.waiting
        acc.n_ticks += 1

    # --- event counters: by event time, in the pickup cell ----------------------

    @staticmethod
    def on_offers(acc: SlotCounters, pu_cell) -> None:
        """Step 5: sessions granted a voucher (``arm = 1``), by ``open_time``."""
        np.add.at(acc.n_offers, pu_cell, 1)

    @staticmethod
    def on_requests(acc: SlotCounters, pu_cell) -> None:
        """Step 6: sessions that booked, by ``open_time``."""
        np.add.at(acc.n_requests, pu_cell, 1)

    @staticmethod
    def on_matched(acc: SlotCounters, pu_cell, pickup_eta_min) -> None:
        """Step 7: orders matched this tick with their quoted pickup ETA (by ``matched_time``)."""
        np.add.at(acc.n_matched, pu_cell, 1)
        np.add.at(acc.sum_pickup_eta_min, pu_cell, pickup_eta_min)
        np.add.at(acc.n_pickup_eta, pu_cell, 1)

    @staticmethod
    def on_abandoned(acc: SlotCounters, pu_cell) -> None:
        """Step 3: Waiting orders that gave up this tick."""
        np.add.at(acc.n_abandoned, pu_cell, 1)

    @staticmethod
    def on_cancelled(acc: SlotCounters, pu_cell) -> None:
        """Step 8: en-route cancellations this tick."""
        np.add.at(acc.n_cancelled, pu_cell, 1)

    @staticmethod
    def on_completed(acc: SlotCounters, pu_cell, voucher_dong) -> None:
        """Step 1: orders completed this tick with the voucher they carried (by ``dropoff_time``)."""
        np.add.at(acc.n_completed, pu_cell, 1)
        np.add.at(acc.voucher_spent_dong, pu_cell, voucher_dong)

    # --- per slot --------------------------------------------------------------

    def publish(self, slot: int, published_at_s: float, acc: SlotCounters,
                cell_dec: CellDecision | None) -> dict[str, np.ndarray]:
        """Close slot ``slot``: compute the snapshot, store it, reset the accumulators.

        Must be called at the end of the slot (``published_at_s = (slot + 1) * slot_s``);
        the store enforces slot order.
        """
        n = self.n_cells
        t_start = slot * self.clock.slot_s
        if published_at_s != t_start + self.clock.slot_s:
            raise ValueError(f"snapshot of slot {slot} must be published at its end "
                             f"({t_start + self.clock.slot_s} s), got {published_at_s}")
        full = lambda v, dt: np.full(n, v, dtype=dt)  # noqa: E731
        with np.errstate(divide="ignore", invalid="ignore"):
            ticks = acc.n_ticks if acc.n_ticks > 0 else NAN
            I = (acc.sum_idle / ticks).astype(np.float32)
            E = (acc.sum_enroute / ticks).astype(np.float32)
            O = (acc.sum_ontrip / ticks).astype(np.float32)
            W = (acc.sum_waiting / ticks).astype(np.float32)
            # slack = I/E. With E = 0: +inf if some driver idles there, 0 if the cell has no idle
            # driver at all, i.e. no supply to offer.
            slack = np.where(E > 0, I.astype(np.float64) / E, np.where(I > 0, np.inf, 0.0))
            slack = np.where(np.isnan(I), NAN, slack).astype(np.float64)
            busy = E.astype(np.float64) + O
            denom = I.astype(np.float64) + busy
            utilization = np.where(denom > 0, busy / denom, NAN).astype(np.float32)  # repositioning excluded
            eta = np.where(acc.n_pickup_eta > 0, acc.sum_pickup_eta_min / acc.n_pickup_eta, NAN).astype(np.float32)
        lag_slot = self._lag("slack", slot, 1)
        lag_day = self._lag("slack", slot, self.clock.slots_per_day)
        if cell_dec is None:
            promo_on = full(False, bool)
            cell_prop = full(NAN, np.float32)
            mech = full(-1, np.int8)
            cluster = full(-1, np.int16)
            block, burnin = -1, False
        else:
            promo_on = cell_dec.promo_on.astype(bool)
            cell_prop = cell_dec.cell_propensity.astype(np.float32)
            mech = cell_dec.mechanism.astype(np.int8)
            cluster = cell_dec.cluster_id.astype(np.int16)
            block, burnin = cell_dec.block, cell_dec.in_burnin
        record = {
            "cell": np.arange(n, dtype=np.int16),
            "slot": full(slot, np.int32),
            "day": full(self.clock.day_of(t_start), np.int16),
            "slot_of_day": full(self.clock.slot_of_day(t_start), np.int16),
            "hour": full(self.clock.hour_of(t_start), np.int16),
            "idle_avg": I, "enroute_avg": E, "ontrip_avg": O, "waiting_avg": W,
            "slack": slack, "utilization": utilization, "mean_pickup_eta_min": eta,
            "n_sessions": acc.n_sessions.copy(), "n_offers": acc.n_offers.copy(),
            "n_requests": acc.n_requests.copy(), "n_matched": acc.n_matched.copy(),
            "n_completed": acc.n_completed.copy(), "n_abandoned": acc.n_abandoned.copy(),
            "n_cancelled": acc.n_cancelled.copy(),
            "voucher_spent_vnd": (acc.voucher_spent_dong / DONG_PER_VND).astype(np.float32),
            "promo_on": promo_on, "cell_propensity": cell_prop, "assign_mechanism_cell": mech,
            "cluster_id": cluster, "block": full(block, np.int32), "in_burnin": full(burnin, bool),
            "slack_lag_slot": lag_slot, "slack_lag_day": lag_day,
            "published_at_s": full(float(published_at_s), np.float64),
        }
        self.store.publish(slot, record)
        acc.reset()
        return record

    def _lag(self, field: str, slot: int, k: int) -> np.ndarray:
        prev = slot - k
        if self.store.has(prev):
            return self.store.get(field, prev).astype(np.float64)
        return np.full(self.n_cells, NAN, dtype=np.float64)

    def view(self, current_slot: int) -> SnapshotView:
        """View for decisions taken in ``current_slot``: only earlier slots are readable.

        Queue discipline: slot ``current_slot - 1`` must be the last one
        published, and nothing readable may have been published after the slot started.
        """
        last = self.store.last_published_slot
        if last >= current_slot:
            raise LookAheadError(f"slot {last} is already published; decisions in slot {current_slot} "
                                 f"would see it")
        if last < current_slot - 1:
            raise RuntimeError(f"snapshot of slot {current_slot - 1} was not published before slot "
                               f"{current_slot} started (last published: {last})")
        if last >= 0:
            published_at = float(self.store.get("published_at_s", last)[0])
            if published_at > current_slot * self.clock.slot_s:
                raise LookAheadError(f"slot {last} was published at {published_at} s, after slot "
                                     f"{current_slot} started")
        return SnapshotView(self.store, current_slot)
