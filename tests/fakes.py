"""Fakes and builders shared by the tests.

Everything here is deterministic and free of randomness, so a test that uses a
fake never depends on the module the fake stands in for.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

from sim.budget import BudgetLedger, vnd_to_dong
from sim.engine import RunResult
from sim.policies.base import SNAPSHOT_FIELDS, SessionBatch, SnapshotStore
from sim.policies.fixed import FixedPolicy
from sim.population import World, build_world
from sim.rng import Rng, Stream
from sim.state import Clock, SimContext

NAN = float("nan")


class ForbiddenAccess:
    """Stand-in for a ``SimContext`` field a step must not touch (hard rule 5).

    Any attribute access raises, so ``reposition.step`` reading ``ctx.monitor``
    fails loudly in a test.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str) -> None:
        raise AssertionError(f"ctx.{self._name} must not be used here (accessed .{item})")


def make_context(cfg, *, on: bool = False, budget_vnd: float | None = None,
                 enforce_budget: bool | None = False) -> SimContext:
    """A fresh ``SimContext`` on the stub world with a FixedPolicy; use it to call one step in a test."""
    from sim.engine import build_context  # local import: engine imports every module

    world = stub_world(cfg)
    return build_context(cfg, world, FixedPolicy(on, world.n_cells), Rng.from_config(cfg),
                         budget_vnd=budget_vnd, enforce_budget=enforce_budget)


def with_forbidden(ctx: SimContext, *fields: str) -> SimContext:
    """Copy of ``ctx`` where the named fields raise on any use."""
    return dataclasses.replace(ctx, **{f: ForbiddenAccess(f) for f in fields})


def make_batch(n: int, n_cells: int, *, fare_vnd: float = 2000.0, open_time_s: float = 3600.0,
               slot: int = 4, session_ids=None, pu_cell=None, x_freq=None) -> SessionBatch:
    """A batch of ``n`` quoted sessions with simple deterministic values."""
    ids = np.arange(n, dtype=np.int64) if session_ids is None else np.asarray(session_ids, dtype=np.int64)
    pu = (np.arange(n) % n_cells).astype(np.int16) if pu_cell is None else np.asarray(pu_cell, dtype=np.int16)
    freq = np.ones(n, dtype=np.float32) if x_freq is None else np.asarray(x_freq, dtype=np.float32)
    u = (np.arange(n, dtype=np.float64) + 0.5) / max(n, 1)
    return SessionBatch(
        session_id=ids,
        rider_id=np.arange(n, dtype=np.int32),
        open_time_s=np.full(n, float(open_time_s)),
        day=np.zeros(n, dtype=np.int16),
        hour=np.full(n, int(open_time_s // 3600) % 24, dtype=np.int16),
        slot=np.full(n, slot, dtype=np.int16),
        slot_of_day=np.full(n, slot, dtype=np.int16),
        pu_cell=pu,
        do_cell=((np.arange(n) + 1) % n_cells).astype(np.int16),
        x_freq=freq,
        x_tenure=np.full(n, 12.0, dtype=np.float32),
        x_segment=np.zeros(n, dtype=np.int8),
        quoted_fare_vnd=np.full(n, fare_vnd, dtype=np.float32),
        quoted_eta_min=np.full(n, 3.0, dtype=np.float32),
        no_supply=np.zeros(n, dtype=bool),
        u_target=u.copy(), u_explore=u.copy(), u_explore_arm=u.copy(), u_score=u.copy(),
    )


def stub_world(cfg) -> World:
    return build_world(cfg, Rng.from_config(cfg))


def make_ledger(*, enforce: bool = True, budget_vnd: float | None = 10000.0, window_start_s: float = 3600.0,
                period_s: float = 86400.0, n_periods: int = 1, warmup_s: float = 3600.0) -> BudgetLedger:
    return BudgetLedger(
        enforce=enforce,
        budget_dong=None if budget_vnd is None else vnd_to_dong(budget_vnd),
        window_start_s=window_start_s, period_s=period_s, n_periods=n_periods, warmup_s=warmup_s,
    )


def blank_record(n_cells: int, slot: int, **overrides) -> dict[str, np.ndarray]:
    """A snapshot record with every field of docs/schema.md; numbers default to slot + 0.1*cell."""
    base = slot + 0.1 * np.arange(n_cells)
    rec: dict[str, np.ndarray] = {}
    for f in SNAPSHOT_FIELDS:
        if f == "cell":
            rec[f] = np.arange(n_cells, dtype=np.int16)
        elif f in ("promo_on", "in_burnin"):
            rec[f] = np.zeros(n_cells, dtype=bool)
        elif f in ("slot", "block"):
            rec[f] = np.full(n_cells, slot, dtype=np.int32)
        elif f in ("day", "slot_of_day", "hour", "cluster_id", "assign_mechanism_cell"):
            rec[f] = np.zeros(n_cells, dtype=np.int16)
        elif f.startswith("n_"):
            rec[f] = np.zeros(n_cells, dtype=np.int32)
        else:
            rec[f] = base.astype(np.float64)
    for k, v in overrides.items():
        rec[k] = np.asarray(v)
    return rec


def published_store(n_cells: int, n_slots: int, slots_per_day: int = 96) -> SnapshotStore:
    """A store with slots 0..n_slots-1 published (values from :func:`blank_record`)."""
    store = SnapshotStore(n_cells, slots_per_day)
    for s in range(n_slots):
        store.publish(s, blank_record(n_cells, s))
    return store


# ---------------------------------------------------------------------------
# Score function and engine stand-ins, importable as "tests.fakes:<name>"
# ---------------------------------------------------------------------------


def fake_score(batch: SessionBatch, s_hat: np.ndarray) -> np.ndarray:
    """A 'module:function' score for the loader tests: tenure plus the forecast (NaN counts as 0)."""
    return batch.x_tenure.astype(np.float64) + np.nan_to_num(s_hat, nan=0.0)


def make_run_result(**overrides) -> RunResult:
    """A ``RunResult`` with every field defaulted to an empty all_off run."""
    base = dict(
        policy="all_off", N_completed=0, V_profit_vnd=0.0, voucher_spent_vnd=0.0, budget_B_vnd=NAN,
        n_sessions=0, n_requests=0, n_abandoned=0, n_cancelled=0, mean_pickup_eta_min=NAN,
        share_cells_off=1.0, n_switches_per_cell_day=0.0, mean_slack=float("inf"),
        completed_per_h=0.0, requests_per_h=0.0, abandon_rate=NAN, cancel_rate=NAN,
        window_start_s=3600.0, window_end_s=90000.0, sim_end_s=90000.0, n_truncated_orders=0, runtime_s=0.0,
    )
    base.update(overrides)
    return RunResult(**base)


FAKE_VOUCHER_VND = 3.5   # voucher value of every offered session in the fake engine


def fake_offered_share(cfg, policy) -> tuple[float, float]:
    """``(share of sessions offered, share of cells off)`` the fake engine attributes to a policy.

    Threshold: cells on fall linearly from all (theta = 0) to half (theta >= 2); the
    rider tier keeps the share ``1 - kappa`` of a Uniform(0, 1) score.
    """
    name = policy.name
    if name == "all_on":
        return 1.0, 0.0
    if name == "all_off":
        return 0.0, 1.0
    if name == "experiment":
        return float(cfg.experiment.p_on), 0.0
    if name == "threshold":
        theta, kappa = float(policy.theta), float(policy.kappa)
        share_on = 1.0 - min(max(theta, 0.0), 2.0) / 4.0
        if math.isfinite(kappa):
            share_score = min(max(1.0 - kappa, 0.0), 1.0)
        else:
            share_score = 1.0 if kappa < 0 else 0.0
        return share_on * share_score, 1.0 - share_on
    return 0.3, 0.5   # legacy


def fake_run(cfg, world, policy, rng, *, log_level: str = "minimal", profile: bool = False,
             budget_vnd: float | None = None, enforce_budget: bool | None = None) -> RunResult:
    """Deterministic stand-in for ``engine.run`` with a WGC-like throughput hump.

    Completed trips per hour rise with demand, peak when requests match the fleet's
    capacity and fall beyond it; slack falls and pickup ETA rises with demand. The
    seed jitter is drawn from the DEMAND stream, so it is identical for every
    policy (CRN). Vouchers raise the booking rate in proportion to the share of
    sessions offered (:func:`fake_offered_share`); a threshold policy also gets a
    mild optimum at theta = 0.5 and the ``offer_*`` arrays kappa-auto needs
    (scores Uniform on [max(kappa, 0), 1), every other offered order completed).
    ``profile`` records what the engine was given, so runner tests can check the
    config transforms.
    """
    clock = Clock.from_config(cfg)
    window_h = clock.window_s / 3600.0
    d, sp = cfg.demand, cfg.supply
    offered, share_off = fake_offered_share(cfg, policy)
    theta = float(getattr(policy, "theta", NAN))
    kappa = float(getattr(policy, "kappa", NAN))

    sessions_per_h = d.base_sessions_per_cell_h * d.demand_scale * world.n_cells * float(np.mean(d.hour_profile))
    p_book = 0.15 * (1.0 + 0.5 * offered)
    requests_per_h = p_book * sessions_per_h
    capacity_per_h = 3.0 * sp.fleet_size
    x = requests_per_h / capacity_per_h
    jitter = 1.0 + 0.01 * float(rng.rng_for(Stream.DEMAND, 0, 0, 0).standard_normal())
    completed_per_h = capacity_per_h * 2.0 * x / (1.0 + x * x) * jitter
    if policy.name == "threshold":
        completed_per_h *= 1.0 + 0.05 * (1.0 - min(abs(theta - 0.5), 1.0))

    n_sessions = int(round(sessions_per_h * window_h))
    n_requests = int(round(requests_per_h * window_h))
    n_completed = min(n_requests, int(round(completed_per_h * window_h)))
    n_abandoned = n_requests - n_completed
    voucher = FAKE_VOUCHER_VND * n_completed * offered
    enforce = cfg.budget.enforce if enforce_budget is None else bool(enforce_budget)
    if enforce and budget_vnd is not None:
        voucher = min(voucher, float(budget_vnd) * clock.n_periods)

    n_offered = int(round(n_sessions * offered))
    lo = max(kappa, 0.0) if math.isfinite(kappa) else 0.0
    offer_score = np.linspace(lo, 1.0, n_offered, endpoint=False, dtype=np.float32)
    markers = {
        "demand_scale": float(d.demand_scale),
        "always_on": float(sp.shift_mode == "always_on"),
        "hour_profile_const": float(len(set(d.hour_profile)) == 1),
        "speed_const": float(len(set(cfg.space.speed_factor_by_hour)) == 1),
        "enforce": float(enforce),
        "run_seed": float(cfg.meta.run_seed),
        "theta": theta, "kappa": kappa, "offered": offered,
    }
    return make_run_result(
        policy=policy.name, N_completed=n_completed, V_profit_vnd=5.0 * n_completed - voucher,
        voucher_spent_vnd=voucher, budget_B_vnd=NAN if budget_vnd is None else float(budget_vnd),
        n_sessions=n_sessions, n_requests=n_requests, n_abandoned=n_abandoned, n_cancelled=0,
        mean_pickup_eta_min=2.0 + 6.0 * x, share_cells_off=share_off,
        mean_slack=(1.0 / x) if x > 0 else float("inf"),
        completed_per_h=n_completed / window_h, requests_per_h=n_requests / window_h,
        abandon_rate=(n_abandoned / n_requests) if n_requests else NAN, cancel_rate=0.0 if n_requests else NAN,
        window_start_s=float(clock.window_start_s), window_end_s=float(clock.window_end_s),
        sim_end_s=float(clock.window_end_s), runtime_s=0.001,
        spent_by_period_vnd=np.full(clock.n_periods, voucher / clock.n_periods), profile=markers,
        offer_score=offer_score, offer_voucher_vnd=np.full(n_offered, FAKE_VOUCHER_VND, dtype=np.float32),
        offer_completed=(np.arange(n_offered) % 2 == 0),
    )


def fake_run_congested(cfg, world, policy, rng, **kwargs) -> RunResult:
    """:func:`fake_run` whose offered orders complete less often the more sessions are offered.

    The completed share of offered sessions is ``0.2 + 0.7 x (1 - offered share)``,
    spread evenly over the score order. A kappa-auto pilot at ``kappa = -inf``
    offers to everyone and so under-predicts the spend of a run at a higher kappa,
    which is the congestion effect behind the kappa-auto reruns.
    """
    res = fake_run(cfg, world, policy, rng, **kwargs)
    share = 0.2 + 0.7 * (1.0 - res.profile["offered"])
    i = np.arange(len(res.offer_score))
    completed = np.floor((i + 1) * share) > np.floor(i * share)
    return dataclasses.replace(res, offer_completed=completed)
