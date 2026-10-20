"""M3 PricingAndPromotion: base fare, voucher value, and the voucher layer of step 5.

Step 5 has one voucher layer for every policy. ``start_slot``
asks the policy for its cell decision when the slot changes; ``quote`` prices
the sessions of the tick (fare from ``T[pu, do, h]``, quoted ETA from the M5
search rule without holding a driver), builds the observed ``SessionBatch``,
and ``decide`` asks the policy for offers and then applies the budget in
``session_id`` order, so all policies are budgeted the same way. The outcome is
written back to the session columns of step "quote" (docs/schema.md).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sim import experiment
from sim.budget import BudgetLedger, DONG_PER_VND
from sim.config import Config
from sim.policies.base import CellDecision, OfferDecision, Policy, SessionBatch, SnapshotView
from sim.shocks import travel_hour


def base_fare_vnd(cfg: Config, trip_min, trip_km) -> np.ndarray:
    """Quoted fare ``p_s``, vectorized, float32.

    ``p_s = open_fare_vnd + per_km_vnd * max(0, km - open_km) + per_min_vnd * T_after + platform_fee_vnd``
    with ``T_after = T * max(0, km - open_km) / km`` the minutes driven after the opening ``open_km``
    (GrabCar Hanoi: 28.473đ for 2 km, 9.818đ/km and 442đ/min after, 10.000đ platform fee). With
    ``open_km = per_km_vnd = platform_fee_vnd = 0`` this is ``open_fare_vnd + per_min_vnd * T``, the
    earlier tariff.
    """
    p = cfg.pricing
    trip_min = np.asarray(trip_min, dtype=np.float64)
    km = np.asarray(trip_km, dtype=np.float64)
    beyond = np.maximum(0.0, km - p.open_km)
    share = np.divide(beyond, km, out=np.zeros_like(km), where=km > 0)
    fare = p.open_fare_vnd + p.per_km_vnd * beyond + p.per_min_vnd * trip_min * share + p.platform_fee_vnd
    return fare.astype(np.float32)


def voucher_dong(cfg: Config, fare_vnd) -> np.ndarray:
    """``v_s = pct_of_fare * p_s`` capped by ``max_vnd``, in integer dong (int64)."""
    fare_vnd = np.asarray(fare_vnd, dtype=np.float64)
    value = cfg.voucher.pct_of_fare * fare_vnd
    if cfg.voucher.max_vnd is not None:
        value = np.minimum(value, cfg.voucher.max_vnd)
    return np.rint(value * DONG_PER_VND).astype(np.int64)


@dataclass
class VoucherOutcome:
    """What the voucher layer writes back for one batch (observed columns of docs/schema.md)."""

    decision: OfferDecision        # policy output (propensity, mechanism, score, propensity_true)
    voucher_dong: np.ndarray      # int64 [n], 0 when not granted
    arm: np.ndarray                # int8 [n], 1 = voucher granted
    budget_blocked: np.ndarray     # bool [n], offered but the period budget was exhausted
    budget_period: np.ndarray      # int16 [n]
    promo_on_cell: np.ndarray      # bool [n]
    cell_propensity: np.ndarray    # float32 [n]
    slack_hat: np.ndarray          # float32 [n]
    lag_slot_same_block: np.ndarray  # bool [n]: slot k-1 in the same switchback block, False in slot 0

    def __len__(self) -> int:
        return len(self.arm)


class VoucherLayer:
    """The single seam between the engine and the policy."""

    def __init__(self, cfg: Config, policy: Policy, ledger: BudgetLedger, n_cells: int) -> None:
        self.cfg = cfg
        self.policy = policy
        self.ledger = ledger
        self.n_cells = int(n_cells)
        self.cell_dec: CellDecision | None = None

    def start_slot(self, slot: int, snapshots: SnapshotView) -> CellDecision:
        """Called by the engine when the slot changes (before step 5 of its first tick)."""
        dec = self.policy.cell_state(slot, snapshots)
        if dec.n_cells != self.n_cells or dec.slot != slot:
            raise ValueError("policy returned a CellDecision for the wrong slot or grid")
        self.cell_dec = dec
        return dec

    def decide(self, batch: SessionBatch) -> VoucherOutcome:
        """Policy offers, then budget in ``session_id`` order."""
        if self.cell_dec is None:
            raise RuntimeError("start_slot must be called before decide")
        n = len(batch)
        dec = self.policy.offer(batch, self.cell_dec, self.ledger)
        if len(dec) != n:
            raise ValueError("policy returned an OfferDecision of the wrong length")
        dong = voucher_dong(self.cfg, batch.quoted_fare_vnd)
        granted = np.zeros(n, dtype=np.int64)
        blocked = np.zeros(n, dtype=bool)
        periods = np.zeros(n, dtype=np.int16)
        for i in np.argsort(batch.session_id, kind="stable"):
            periods[i] = self.ledger.period_of(float(batch.open_time_s[i]))
            if not dec.offer[i]:
                continue
            c = int(dong[i])
            if c > 0 and not self.ledger.reserve(int(batch.session_id[i]), float(batch.open_time_s[i]), c):
                blocked[i] = True
                continue
            granted[i] = c
        arm = (dec.offer & ~blocked).astype(np.int8)
        cd = self.cell_dec
        return VoucherOutcome(
            decision=dec,
            voucher_dong=granted,
            arm=arm,
            budget_blocked=blocked,
            budget_period=periods,
            promo_on_cell=cd.promo_on[batch.pu_cell],
            cell_propensity=cd.cell_propensity[batch.pu_cell],
            slack_hat=cd.s_hat[batch.pu_cell],
            lag_slot_same_block=np.full(n, bool(cd.lag_same_block and cd.slot > 0)),
        )

    def quote(self, ctx, t: float) -> None:
        """Engine step 5: price this tick's sessions, ask the policy, apply the budget, write the columns."""
        start, stop = ctx.tick_sessions
        if stop <= start:
            return
        if self.cell_dec is None:
            raise RuntimeError("start_slot must be called before quote")
        s, space, riders = ctx.sessions, ctx.world.space, ctx.world.riders
        rows = slice(start, stop)
        pu, do, hour = s.pu_cell[rows], s.do_cell[rows], s.hour[rows]

        t_hour = travel_hour(ctx, t, hour)                   # + 24 while it rains
        trip_min = space.T[pu, do, t_hour]
        trip_km = trip_min.astype(np.float64) / 60.0 * space.speed_kmh[t_hour]   # road km, as orders.trip_km
        fare = base_fare_vnd(self.cfg, trip_min, trip_km)
        eta, no_supply = quote_eta_by_cell(space, pu, int(t_hour[0]), ctx.cells.idle)
        s.quoted_fare_vnd[rows] = fare
        s.quoted_eta_min[rows] = eta
        s.no_supply[rows] = no_supply

        rider = s.rider_id[rows]
        batch = SessionBatch(
            session_id=s.session_id[rows], rider_id=rider, open_time_s=s.open_time_s[rows],
            day=s.day[rows], hour=hour, slot=s.slot[rows], slot_of_day=s.slot_of_day[rows],
            pu_cell=pu, do_cell=do,
            x_freq=riders.x_freq[rider], x_tenure=riders.x_tenure[rider], x_segment=riders.x_segment[rider],
            quoted_fare_vnd=fare, quoted_eta_min=eta, no_supply=no_supply,
            u_target=s.u_target[rows], u_explore=s.u_explore[rows], u_explore_arm=s.u_explore_arm[rows],
            u_score=s.u_score[rows],
        )
        out = self.decide(batch)
        cd, dec = self.cell_dec, out.decision

        s.voucher_dong[rows] = out.voucher_dong
        s.voucher_value_vnd[rows] = (out.voucher_dong / DONG_PER_VND).astype(np.float32)
        s.arm[rows] = out.arm
        s.budget_blocked[rows] = out.budget_blocked
        s.budget_period[rows] = out.budget_period
        s.promo_on_cell[rows] = out.promo_on_cell
        s.cell_propensity[rows] = out.cell_propensity
        s.slack_hat[rows] = out.slack_hat
        s.lag_slot_same_block[rows] = out.lag_slot_same_block
        s.assign_mechanism[rows] = dec.mechanism
        s.propensity[rows] = dec.propensity
        s.score[rows] = dec.score
        s.propensity_true[rows] = dec.propensity_true
        s.cluster_id[rows] = cd.cluster_id[pu]
        s.block[rows] = cd.block
        s.in_burnin[rows] = experiment.in_burnin(self.cfg, s.open_time_s[rows]) if cd.block >= 0 else False
        ctx.monitor.on_offers(ctx.slot_counters, pu[out.arm == 1])


def quote_eta_by_cell(space, pu_cell: np.ndarray, hour: int, idle_count: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Quoted ETA (float32 [n]) and ``no_supply`` (bool [n]) of sessions, one M5 search per distinct cell."""
    n = len(pu_cell)
    eta = np.empty(n, dtype=np.float32)
    no_supply = np.empty(n, dtype=bool)
    for cell in np.unique(pu_cell):
        e, ns = space.quote_eta(int(cell), hour, idle_count)
        mask = pu_cell == cell
        eta[mask] = e
        no_supply[mask] = ns
    return eta, no_supply
