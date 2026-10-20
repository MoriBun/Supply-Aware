"""LegacyPolicy: rule on lagged slack with an epsilon coin, rider targeting via LegacyHiddenView, explore slice.

The operator of the past: a cell is on when last slot's slack was at least
``slack_on``, except that with probability ``epsilon_cell`` the state is a coin
with ``epsilon_p_on`` (CELLSLOT stream, key ``(LEGACY_EPS, 0, cell, slot)``).
In an on cell a rider is targeted with ``sigma(g0 + g_freq*zf + g_u*u_latent)``,
which uses the hidden ``u_latent`` and so creates confounding; the analyst sees
``propensity = NaN`` for those sessions. An explore slice of ``explore_frac`` is
randomized with ``explore_p`` whatever the cell state. All randomness comes from
the pre-drawn session uniforms and the CELLSLOT stream (hard rule 4).
"""

from __future__ import annotations

import numpy as np

from sim.budget import BudgetLedger
from sim.config import Config
from sim.policies.base import CellDecision, LegacyHiddenView, OfferDecision, SessionBatch, SnapshotView
from sim.rng import CellSlotKind, Rng, Stream
from sim.state import Mechanism

NAN = float("nan")


def sigmoid(x: np.ndarray) -> np.ndarray:
    return np.exp(-np.logaddexp(0.0, -np.asarray(x, dtype=np.float64)))


class LegacyPolicy:
    name = "legacy"

    def __init__(self, cfg: Config, hidden: LegacyHiddenView, rng: Rng, n_cells: int) -> None:
        self.p = cfg.policy.legacy
        self.slack_cap = float(cfg.monitor.slack_cap)
        self.hidden = hidden
        self.rng = rng
        self.n_cells = int(n_cells)

    def cell_state(self, slot: int, snapshots: SnapshotView) -> CellDecision:
        p = self.p
        lag = snapshots.lag("slack", 1)                       # NaN before the first publish, inf when E = 0
        rule_on = lag >= p.slack_on                            # NaN -> off, inf -> on
        u = np.array([self.rng.rng_for(Stream.CELLSLOT, CellSlotKind.LEGACY_EPS, 0, c, int(slot)).random(2)
                      for c in range(self.n_cells)]).reshape(self.n_cells, 2)
        is_eps = u[:, 0] < p.epsilon_cell
        coin = u[:, 1] < p.epsilon_p_on
        promo_on = np.where(is_eps, coin, rule_on)
        mechanism = np.where(is_eps, int(Mechanism.LEGACY_EPS), int(Mechanism.LEGACY_RULE)).astype(np.int8)
        cell_prop = ((1.0 - p.epsilon_cell) * rule_on + p.epsilon_cell * p.epsilon_p_on).astype(np.float32)
        return CellDecision(
            slot=int(slot), promo_on=promo_on, cell_propensity=cell_prop, mechanism=mechanism,
            s_hat=np.minimum(lag, self.slack_cap).astype(np.float32),   # slot k-1, capped
            cluster_id=np.full(self.n_cells, -1, dtype=np.int16),
        )

    def offer(self, batch: SessionBatch, cell_dec: CellDecision, ledger: BudgetLedger) -> OfferDecision:
        p = self.p
        n = len(batch)
        zf = self.hidden.zf_of(batch.rider_id).astype(np.float64)
        u_latent = self.hidden.u_latent_of(batch.rider_id).astype(np.float64)
        p_target = sigmoid(p.target_g0 + p.target_g_freq * zf + p.target_g_u * u_latent)

        on_cell = cell_dec.promo_on[batch.pu_cell].astype(bool)
        explore = batch.u_explore < p.explore_frac
        targeted = on_cell & ~explore & (batch.u_target < p_target)
        explore_offer = explore & (batch.u_explore_arm < p.explore_p)

        dec = OfferDecision.blank(n, mechanism=Mechanism.LEGACY_RULE)
        dec.offer = targeted | explore_offer
        dec.mechanism = np.where(explore, int(Mechanism.EXPLORE), cell_dec.mechanism[batch.pu_cell]).astype(np.int8)
        # Observed: known only for the randomized slice and for cells that are off.
        dec.propensity = np.where(explore, p.explore_p, np.where(on_cell, NAN, 0.0)).astype(np.float32)
        # Hidden truth: the real offer probability of the session.
        dec.propensity_true = np.where(explore, p.explore_p, np.where(on_cell, p_target, 0.0)).astype(np.float32)
        return dec
