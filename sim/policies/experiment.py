"""ExperimentPolicy: applies the M10 assignment.

Switchback designs: a cell is on when its cluster is on in the current block and
every session of an on cell is offered. ``rider_ab``: every cell is on and a
session is offered when its rider's arm is 1. The observed propensity of every
session is the design probability ``p_on``.
"""

from __future__ import annotations

import numpy as np

from sim import experiment
from sim.budget import BudgetLedger
from sim.config import Config
from sim.policies.base import CellDecision, OfferDecision, SessionBatch, SnapshotView
from sim.rng import Rng
from sim.state import Mechanism



class ExperimentPolicy:
    name = "experiment"

    def __init__(self, cfg: Config, world, rng: Rng) -> None:
        self.cfg = cfg
        self.rng = rng
        self.design = cfg.experiment.design
        self.p_on = float(cfg.experiment.p_on)
        self.n_cells = world.n_cells
        self.slot_s = cfg.time.slot_min * 60
        self.level = experiment.effective_level(cfg)
        if self.design == "rider_ab":
            self.clusters = np.full(self.n_cells, -1, dtype=np.int16)
        else:
            self.clusters = experiment.cluster_ids(world.space, self.level)

    def cell_state(self, slot: int, snapshots: SnapshotView) -> CellDecision:
        t0 = slot * self.slot_s
        block = experiment.block_of(self.cfg, t0)
        if self.design == "rider_ab":
            on = np.ones(self.n_cells, dtype=bool)
            cell_prop = np.ones(self.n_cells, dtype=np.float32)
            same_block = True                                 # no blocks: slot k-1 saw the same design
        else:
            on = experiment.cluster_on(self.rng, self.cfg, self.clusters, self.level, block)
            cell_prop = np.full(self.n_cells, self.p_on, dtype=np.float32)
            same_block = slot > 0 and experiment.block_of(self.cfg, t0 - self.slot_s) == block
        # Published slack of slot k-1, capped like every s_hat; not used to decide.
        lag = np.minimum(snapshots.lag("slack", 1), self.cfg.monitor.slack_cap).astype(np.float32)
        return CellDecision(
            slot=int(slot), promo_on=on, cell_propensity=cell_prop,
            mechanism=np.full(self.n_cells, int(Mechanism.EXPERIMENT), dtype=np.int8),
            s_hat=lag, cluster_id=self.clusters.copy(),
            block=int(block), in_burnin=bool(experiment.in_burnin(self.cfg, t0)), lag_same_block=bool(same_block),
        )

    def offer(self, batch: SessionBatch, cell_dec: CellDecision, ledger: BudgetLedger) -> OfferDecision:
        dec = OfferDecision.blank(len(batch), mechanism=Mechanism.EXPERIMENT)
        if self.design == "rider_ab":
            dec.offer = experiment.rider_arm(self.rng, self.cfg, batch.rider_id)
        else:
            dec.offer = cell_dec.promo_on[batch.pu_cell].astype(bool)
        dec.propensity[:] = self.p_on
        dec.propensity_true[:] = self.p_on
        return dec
