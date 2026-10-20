"""Tick loop: the 10 ordered steps, warm-up / window / cool-down, and ``RunResult``.

Contract: ``run(cfg, world, policy, rng, ...) -> RunResult``; N(pi) and
V(pi) are computed here and nowhere else; each step is ``fn(ctx, t)``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from sim import cancel, choice, demand, matching, reposition, supply, trips
from sim.budget import DONG_PER_VND, BudgetLedger, LedgerState, vnd_to_dong
from sim.config import Config
from sim.monitor import MarketMonitor
from sim.policies.base import Policy
from sim.population import World
from sim.shocks import build_schedule
from sim.pricing import VoucherLayer
from sim.rng import Rng
from sim.state import (
    CellCounters, Clock, DriverState, OrderBuffer, OrderStatus, SessionBuffer, SimContext, SlotCounters,
    OPEN_ORDER_STATUSES,
)

NAN = float("nan")

STEP_NAMES = ("advance", "supply", "expire", "spawn", "quote", "decide", "match", "en_route", "reposition", "monitor")


@dataclass
class RunResult:
    """Outcome of one run. Fields cover results/policy_results, theta_sweep, throughput_curve and kappa-auto."""

    policy: str
    N_completed: int
    V_profit_vnd: float
    voucher_spent_vnd: float
    budget_B_vnd: float
    n_sessions: int
    n_requests: int
    n_abandoned: int
    n_cancelled: int
    mean_pickup_eta_min: float
    share_cells_off: float
    n_switches_per_cell_day: float
    mean_slack: float               # sum(idle x tick) / sum(enroute x tick) over the window
    completed_per_h: float
    requests_per_h: float
    abandon_rate: float
    cancel_rate: float
    window_start_s: float
    window_end_s: float
    sim_end_s: float
    n_truncated_orders: int
    runtime_s: float
    kappa: float = NAN
    promo_on: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), dtype=bool))  # [n_slots, N]
    spent_by_period_vnd: np.ndarray = field(default_factory=lambda: np.zeros(0))
    offer_score: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    offer_voucher_vnd: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    offer_completed: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    profile: dict[str, float] = field(default_factory=dict)
    sessions: SessionBuffer | None = None      # log_level = "full" only
    orders: OrderBuffer | None = None          # log_level = "full" only
    snapshots: list | None = None              # log_level = "full" only
    shocks: dict | None = None                 # rows of meta/shocks (sim.shocks.ShockSchedule.table)


# Config keys of the contract whose behaviour is not implemented yet: refuse rather than silently run
# the old model. Each entry is removed once its key is implemented; currently empty.
_NOT_YET: tuple = ()


def _check_contract_keys(cfg: Config) -> None:
    for pending, what in _NOT_YET:
        if pending(cfg):
            raise NotImplementedError(f"{what} is part of the config contract but not implemented yet")


def build_context(cfg: Config, world: World, policy: Policy, rng: Rng, *, budget_vnd: float | None = None,
                  enforce_budget: bool | None = None) -> SimContext:
    """Fresh state for a run (also used by unit tests to call a single step)."""
    _check_contract_keys(cfg)
    clock = Clock.from_config(cfg)
    n_cells = world.n_cells
    enforce = cfg.budget.enforce if enforce_budget is None else bool(enforce_budget)
    ledger = BudgetLedger(
        enforce=enforce,
        budget_dong=None if budget_vnd is None else vnd_to_dong(budget_vnd),
        window_start_s=clock.window_start_s, period_s=clock.period_s,
        n_periods=clock.n_periods, warmup_s=clock.warmup_s,
    )
    return SimContext(
        cfg=cfg, clock=clock, world=world, rng=rng, policy=policy,
        drivers=DriverState(cfg.supply.fleet_size), sessions=SessionBuffer(), orders=OrderBuffer(),
        cells=CellCounters(n_cells), slot_counters=SlotCounters(n_cells),
        ledger=ledger, layer=VoucherLayer(cfg, policy, ledger, n_cells), monitor=MarketMonitor(cfg, clock, n_cells),
        profile={name: 0.0 for name in STEP_NAMES},
        shocks=build_schedule(cfg, world, rng, clock) if cfg.shocks.enabled else None,
    )


def run(cfg: Config, world: World, policy: Policy, rng: Rng, *, log_level: str = "minimal",
        profile: bool = False, budget_vnd: float | None = None, enforce_budget: bool | None = None) -> RunResult:
    """Simulate one run: warm-up, evaluation window, cool-down; return the aggregates."""
    if log_level not in ("minimal", "full"):
        raise ValueError("log_level must be 'minimal' or 'full'")
    t_wall = time.perf_counter()
    ctx = build_context(cfg, world, policy, rng, budget_vnd=budget_vnd, enforce_budget=enforce_budget)
    clock, ledger, layer, monitor = ctx.clock, ctx.ledger, ctx.layer, ctx.monitor
    supply.init_drivers(ctx)

    sum_idle_ticks = 0.0
    sum_enroute_ticks = 0.0
    tick = 0
    clk = time.perf_counter if profile else None
    while True:
        t = tick * clock.tick_s
        if t >= clock.window_end_s and (ctx.orders.count_open() == 0 or t >= clock.window_end_s + clock.cooldown_max_s):
            break
        slot = clock.slot_of(t)
        if slot != ctx.current_slot:
            ctx.current_slot = slot
            layer.start_slot(slot, monitor.view(slot))

        s0 = clk() if clk else 0.0
        trips.advance(ctx, t)
        s1 = clk() if clk else 0.0
        supply.update(ctx, t)
        s2 = clk() if clk else 0.0
        cancel.expire_waiting(ctx, t)
        s3 = clk() if clk else 0.0
        if t < clock.window_end_s:
            demand.spawn(ctx, t)
        else:
            ctx.tick_sessions = (ctx.sessions.n, ctx.sessions.n)
        s4 = clk() if clk else 0.0
        layer.quote(ctx, t)
        s5 = clk() if clk else 0.0
        choice.decide(ctx, t)
        s6 = clk() if clk else 0.0
        matching.match(ctx, t)
        s7 = clk() if clk else 0.0
        cancel.en_route(ctx, t)
        s8 = clk() if clk else 0.0
        reposition.step(ctx, t)
        s9 = clk() if clk else 0.0
        monitor.accumulate(ctx.cells, ctx.slot_counters)
        if clock.in_window(t):
            sum_idle_ticks += float(ctx.cells.idle.sum())
            sum_enroute_ticks += float(ctx.cells.enroute.sum())
        t_next = t + clock.tick_s
        if t_next % clock.slot_s == 0:
            monitor.publish(slot, t_next, ctx.slot_counters, layer.cell_dec)
        s10 = clk() if clk else 0.0
        if clk:
            for name, dt in zip(STEP_NAMES, (s1 - s0, s2 - s1, s3 - s2, s4 - s3, s5 - s4, s6 - s5, s7 - s6,
                                             s8 - s7, s9 - s8, s10 - s9)):
                ctx.profile[name] += dt
        tick += 1
    sim_end_s = float(tick * clock.tick_s)

    n_truncated = _truncate_open_orders(ctx)
    ledger.check_invariant()
    result = _aggregate(ctx, policy, sim_end_s, n_truncated, budget_vnd, sum_idle_ticks, sum_enroute_ticks)
    result.runtime_s = time.perf_counter() - t_wall
    result.profile = dict(ctx.profile) if profile else {}
    if log_level == "full":
        result.sessions = ctx.sessions
        result.orders = ctx.orders
        result.snapshots = monitor.store.records()
    return result


def _truncate_open_orders(ctx: SimContext) -> int:
    """Orders still open when cool-down ends are Truncated; their committed budget is released."""
    orders = ctx.orders
    status = orders.col("status")
    open_mask = np.isin(status, [int(s) for s in OPEN_ORDER_STATUSES])
    idx = np.flatnonzero(open_mask)
    for i in idx:
        status[i] = int(OrderStatus.TRUNCATED)
        sid = int(orders.col("session_id")[i])
        entry = ctx.ledger.entry(sid)
        if entry is not None and entry[2] == LedgerState.COMMITTED:
            ctx.ledger.release_committed(sid)
    return int(len(idx))


def _aggregate(ctx: SimContext, policy: Policy, sim_end_s: float, n_truncated: int, budget_vnd: float | None,
               sum_idle_ticks: float, sum_enroute_ticks: float) -> RunResult:
    clock = ctx.clock
    sessions, orders = ctx.sessions, ctx.orders
    s_in = sessions.col("in_window")
    o_in = orders.col("in_window")
    o_status = orders.col("status")
    completed = o_in & (o_status == int(OrderStatus.COMPLETED))
    n_completed = int(completed.sum())
    window_h = clock.window_s / 3600.0

    matched = o_in & ~np.isnan(orders.col("matched_time_s"))
    eta = orders.col("pickup_eta_min")[matched]
    n_requests = int((s_in & sessions.col("requested")).sum())

    store = ctx.monitor.store
    window_slots = [s for s in clock.window_slots if store.has(s)]
    if window_slots:
        promo = np.stack([store.get("promo_on", s).astype(bool) for s in window_slots])
        share_off = float((~promo).mean())
        if len(window_slots) > 1:
            switches = (promo[1:] != promo[:-1]).sum(axis=0)
            n_switches = float(switches.mean() / (clock.window_s / 86400.0))
        else:
            n_switches = 0.0
    else:
        promo = np.zeros((0, ctx.n_cells), dtype=bool)
        share_off, n_switches = NAN, NAN

    offered = s_in & (sessions.col("arm") == 1)
    order_idx = sessions.col("order_idx")[offered]
    offer_completed = np.zeros(int(offered.sum()), dtype=bool)
    has_order = order_idx >= 0
    offer_completed[has_order] = o_status[order_idx[has_order]] == int(OrderStatus.COMPLETED)

    return RunResult(
        policy=policy.name,
        N_completed=n_completed,
        V_profit_vnd=float(orders.col("platform_profit_vnd")[completed].sum()),
        voucher_spent_vnd=float(orders.col("voucher_value_vnd")[completed].sum()),
        budget_B_vnd=NAN if budget_vnd is None else float(budget_vnd),
        n_sessions=int(s_in.sum()),
        n_requests=n_requests,
        n_abandoned=int((o_in & (o_status == int(OrderStatus.ABANDONED))).sum()),
        n_cancelled=int((o_in & (o_status == int(OrderStatus.CANCELLED))).sum()),
        mean_pickup_eta_min=float(eta.mean()) if len(eta) else NAN,
        share_cells_off=share_off,
        n_switches_per_cell_day=n_switches,
        mean_slack=(sum_idle_ticks / sum_enroute_ticks) if sum_enroute_ticks > 0 else float("inf"),
        completed_per_h=n_completed / window_h,
        requests_per_h=n_requests / window_h,
        abandon_rate=_rate((o_in & (o_status == int(OrderStatus.ABANDONED))).sum(), n_requests),
        cancel_rate=_rate((o_in & (o_status == int(OrderStatus.CANCELLED))).sum(), n_requests),
        window_start_s=float(clock.window_start_s),
        window_end_s=float(clock.window_end_s),
        sim_end_s=sim_end_s,
        n_truncated_orders=n_truncated,
        runtime_s=NAN,
        promo_on=promo,
        spent_by_period_vnd=ctx.ledger.spent_by_period_vnd(),
        offer_score=sessions.col("score")[offered].astype(np.float32),
        offer_voucher_vnd=(sessions.col("voucher_dong")[offered] / DONG_PER_VND).astype(np.float32),
        offer_completed=offer_completed,
        shocks=None if ctx.shocks is None else ctx.shocks.table(),
    )


def _rate(num, den) -> float:
    return float(num) / float(den) if den > 0 else NAN
