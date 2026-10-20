"""FixedPolicy: all_on / all_off.

Every cell is on (or off) in every slot; in an "on" cell every session is
offered. The budget, when enforced, is applied by the voucher layer like for
any other policy.
"""

from __future__ import annotations

import numpy as np

from sim.budget import BudgetLedger
from sim.policies.base import CellDecision, OfferDecision, SessionBatch, SnapshotView
from sim.state import Mechanism


class FixedPolicy:
    def __init__(self, on: bool, n_cells: int) -> None:
        self.on = bool(on)
        self.n_cells = int(n_cells)
        self.name = "all_on" if self.on else "all_off"

    def cell_state(self, slot: int, snapshots: SnapshotView) -> CellDecision:
        return CellDecision.blank(slot, self.n_cells, promo_on=self.on, mechanism=Mechanism.FIXED)

    def offer(self, batch: SessionBatch, cell_dec: CellDecision, ledger: BudgetLedger) -> OfferDecision:
        dec = OfferDecision.blank(len(batch), mechanism=Mechanism.FIXED)
        dec.offer = cell_dec.promo_on[batch.pu_cell].astype(bool)
        dec.propensity = dec.offer.astype(np.float32)
        return dec
