"""ThresholdPolicy pi_theta: cell layer (forecast slack vs theta, hysteresis) and rider layer (score >= kappa).

Cell layer: forecast ``s_hat`` of each cell's slack from the published snapshots
(``persistence`` = last slot; ``ar`` = weighted lag slot and lag day), with inf
capped at ``monitor.slack_cap`` in **every** branch
(a score function fitted on the capped feature must never see inf; the ``slack``
column of ``market/`` keeps inf). Then ``promo_on = not (s_hat < theta)``, so an
unknown (NaN) forecast leaves the cell on, ``theta = 0`` cuts nothing and a cell
with idle drivers and nobody en route counts as slack ``slack_cap``. With ``hysteresis_h > 0`` a cell that is off turns on only when
``s_hat > theta + h``. Rider layer: ``score = score_fn(batch, s_hat)`` and a
session is offered when its cell is on and ``score >= kappa``; the budget is
applied afterwards by the voucher layer. ``kappa = -inf`` is the pilot
setting of kappa-auto. Only ``indicator = slack`` is implemented.

Scope: ``cell`` reads the cell's own slack; ``ring1`` reads
idle / en-route summed over the cell and its neighbours in the same slot; ``city``
sums over every cell, so all cells share one forecast and one decision (the ``system``
indicator of ``analysis/tension.py``). Both sums follow the monitor's slack rule (no idle
driver: 0; idle but none en route: inf) with inf capped at ``monitor.slack_cap``. A slot
not yet published stays NaN (cell on).
"""

from __future__ import annotations

import math

import numpy as np

from sim.budget import BudgetLedger
from sim.config import Config
from sim.policies.base import CellDecision, OfferDecision, SessionBatch, SnapshotView
from sim.policies.scores import ScoreFn, load_score_fn, score_batch
from sim.state import Mechanism


class ThresholdPolicy:
    name = "threshold"

    def __init__(self, cfg: Config, n_cells: int, *, theta: float | None = None, kappa: float | None = None,
                 score_fn: ScoreFn | None = None, neighbors: np.ndarray | None = None) -> None:
        th = cfg.policy.threshold
        if th.indicator != "slack":
            raise NotImplementedError(f"policy.threshold.indicator={th.indicator!r}: only 'slack' is defined")
        self.n_cells = int(n_cells)
        self.theta = float(th.theta if theta is None else theta)
        if kappa is not None:
            self.kappa = float(kappa)
        elif th.kappa == "auto":
            self.kappa = -math.inf                     # pilot run of kappa-auto; the runner passes the real kappa
        else:
            self.kappa = float(th.kappa)
        self.forecast = th.forecast
        self.ar_weights = tuple(float(w) for w in th.ar_weights)
        self.hysteresis_h = float(th.hysteresis_h)
        self.slack_cap = float(cfg.monitor.slack_cap)
        self.score_fn = load_score_fn(th.score_fn) if score_fn is None else score_fn
        self._prev_on = np.ones(self.n_cells, dtype=bool)
        self.scope = th.scope
        self._ring = None
        if self.scope == "ring1":
            if neighbors is None:
                raise ValueError("policy.threshold.scope = ring1 needs the neighbour table of the grid")
            nb = np.asarray(neighbors)
            ring = np.eye(self.n_cells)                     # ring[c, j] = 1 if j is c or a neighbour of c
            rows, cols = np.nonzero(nb >= 0)
            ring[rows, nb[rows, cols]] = 1.0
            self._ring = ring
        elif self.scope == "city":
            self._ring = np.ones((self.n_cells, self.n_cells))    # every cell sees the city-wide ratio

    def _slack(self, read) -> np.ndarray:
        """Slack of every cell in one published slot; ``read(field)`` returns that slot's field (NaN if unknown)."""
        if self._ring is None:
            return read("slack")
        idle = self._ring @ read("idle_avg").astype(np.float64)
        enroute = self._ring @ read("enroute_avg").astype(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(enroute > 0, idle / enroute, np.where(idle > 0, np.inf, 0.0))
        ratio = np.where(np.isnan(idle) | np.isnan(enroute), np.nan, ratio)
        return np.minimum(ratio, self.slack_cap)            # NaN stays NaN

    def forecast_slack(self, snapshots: SnapshotView) -> np.ndarray:
        """``s_hat`` per cell (float64); NaN where nothing is published yet."""
        a = np.minimum(self._slack(lambda f: snapshots.lag(f, 1)), self.slack_cap)   # inf -> cap, NaN stays
        if self.forecast == "persistence":
            return a
        b = np.minimum(self._slack(snapshots.lag_day), self.slack_cap)
        w_slot, w_day = self.ar_weights
        ar = w_slot * a + w_day * b
        return np.where(np.isnan(b), a, ar)             # first day: lag day unknown -> persistence

    def cell_state(self, slot: int, snapshots: SnapshotView) -> CellDecision:
        s_hat = self.forecast_slack(snapshots)
        on = ~(s_hat < self.theta)
        if self.hysteresis_h > 0:
            on = np.where(self._prev_on, on, s_hat > self.theta + self.hysteresis_h)
        self._prev_on = on
        return CellDecision(
            slot=int(slot), promo_on=on, cell_propensity=on.astype(np.float32),
            mechanism=np.full(self.n_cells, int(Mechanism.THRESHOLD), dtype=np.int8),
            s_hat=s_hat.astype(np.float32), cluster_id=np.full(self.n_cells, -1, dtype=np.int16),
        )

    def offer(self, batch: SessionBatch, cell_dec: CellDecision, ledger: BudgetLedger) -> OfferDecision:
        dec = OfferDecision.blank(len(batch), mechanism=Mechanism.THRESHOLD)
        s_hat = cell_dec.s_hat[batch.pu_cell].astype(np.float64)
        dec.score = score_batch(self.score_fn, batch, s_hat)
        dec.offer = cell_dec.promo_on[batch.pu_cell].astype(bool) & (dec.score >= self.kappa)   # NaN score: no offer
        dec.propensity = dec.offer.astype(np.float32)
        return dec
