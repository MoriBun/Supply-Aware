"""Engine skeleton: the tick loop runs end to end with stubs."""

import dataclasses
import inspect

import numpy as np
import pytest

from sim import cancel, choice, demand, matching, reposition, supply, trips
from sim.engine import STEP_NAMES, RunResult, build_context, run
from sim.policies.fixed import FixedPolicy
from sim.pricing import VoucherLayer
from sim.rng import Rng
from sim.state import Clock, SimContext
from tests.fakes import ForbiddenAccess, make_context, stub_world, with_forbidden


# --- contract: every step is step(ctx, t) --------------------------------------------

STEPS = [trips.advance, supply.update, cancel.expire_waiting, demand.spawn, VoucherLayer.quote, choice.decide,
         matching.match, cancel.en_route, reposition.step]


@pytest.mark.parametrize("fn", STEPS, ids=lambda f: f.__qualname__)
def test_step_signature(fn):
    params = list(inspect.signature(fn).parameters)
    assert params[-2:] == ["ctx", "t"], f"{fn.__qualname__} must be step(ctx, t)"


def test_init_drivers_signature():
    assert list(inspect.signature(supply.init_drivers).parameters) == ["ctx"]


def test_build_context_and_fakes(tiny_cfg):
    ctx = make_context(tiny_cfg, on=True)
    assert isinstance(ctx, SimContext) and ctx.n_cells == 7 and ctx.policy.name == "all_on"
    assert ctx.sessions.n == 0 and ctx.orders.n == 0 and ctx.drivers.n == 10 and ctx.tick_sessions == (0, 0)
    assert ctx.ledger.enforce is False and ctx.layer.policy is ctx.policy
    # Hard rule 5: a repositioning step that reads demand or snapshots fails in this harness.
    guarded = with_forbidden(ctx, "monitor", "sessions", "orders")
    reposition.step(guarded, 0.0)                     # stub touches nothing
    with pytest.raises(AssertionError, match="ctx.monitor"):
        guarded.monitor.view(0)
    assert isinstance(guarded.monitor, ForbiddenAccess) and ctx.monitor is not guarded.monitor
    real = build_context(tiny_cfg, stub_world(tiny_cfg), FixedPolicy(False, 7), Rng.from_config(tiny_cfg),
                         budget_vnd=1000.0, enforce_budget=True)
    assert real.ledger.enforce and real.ledger.limit_dong(0) == 1000


def _no_demand(cfg):
    # These tests check the loop itself (window, slots, snapshots, aggregates of an empty market),
    # so they run without demand. Runs with real sessions are in tests/test_integration.py.
    return dataclasses.replace(cfg, demand=dataclasses.replace(cfg.demand, demand_scale=0.0))


def _run(cfg, on=False, **kw):
    cfg = _no_demand(cfg)
    world = stub_world(cfg)
    return run(cfg, world, FixedPolicy(on, world.n_cells), Rng.from_config(cfg), enforce_budget=False, **kw)


def test_all_off_skeleton_runs(tiny_cfg):
    res = _run(tiny_cfg, on=False)
    clock = Clock.from_config(tiny_cfg)
    assert isinstance(res, RunResult) and res.policy == "all_off"
    assert res.N_completed == 0 and res.V_profit_vnd == 0.0 and res.voucher_spent_vnd == 0.0
    assert res.n_sessions == res.n_requests == res.n_abandoned == res.n_cancelled == 0
    assert np.isnan(res.mean_pickup_eta_min) and np.isnan(res.abandon_rate) and np.isnan(res.budget_B_vnd)
    assert res.window_start_s == clock.window_start_s and res.window_end_s == clock.window_end_s
    assert res.sim_end_s == clock.window_end_s          # no open orders -> no cool-down
    assert res.n_truncated_orders == 0 and res.runtime_s > 0
    assert res.completed_per_h == 0.0 and res.requests_per_h == 0.0
    assert np.isinf(res.mean_slack)                     # no en-route driver-ticks
    assert res.promo_on.shape == (len(clock.window_slots), 7) and not res.promo_on.any()
    assert res.share_cells_off == 1.0 and res.n_switches_per_cell_day == 0.0
    np.testing.assert_array_equal(res.spent_by_period_vnd, [0.0])
    assert len(res.offer_score) == 0 and res.profile == {}
    assert res.sessions is None and res.orders is None and res.snapshots is None


def test_all_on_and_profile(tiny_cfg):
    res = _run(tiny_cfg, on=True, profile=True, log_level="full")
    assert res.policy == "all_on" and res.promo_on.all() and res.share_cells_off == 0.0
    assert set(res.profile) == set(STEP_NAMES) and all(v >= 0 for v in res.profile.values())
    clock = Clock.from_config(tiny_cfg)
    assert len(res.snapshots) == clock.window_end_s // clock.slot_s      # warm-up + window slots all published
    assert res.snapshots[0]["published_at_s"][0] == clock.slot_s
    assert res.sessions is not None and res.sessions.n == 0 and res.orders.n == 0


def test_same_seed_same_result(tiny_cfg):
    a, b = _run(tiny_cfg), _run(tiny_cfg)
    for f in dataclasses.fields(RunResult):
        if f.name in ("runtime_s", "profile", "sessions", "orders", "snapshots"):
            continue
        va, vb = getattr(a, f.name), getattr(b, f.name)
        if isinstance(va, np.ndarray):
            np.testing.assert_array_equal(va, vb)
        else:
            assert va == vb or (isinstance(va, float) and np.isnan(va) and np.isnan(vb)), f.name


def test_budget_arguments(tiny_cfg):
    tiny_cfg = _no_demand(tiny_cfg)
    world = stub_world(tiny_cfg)
    policy = FixedPolicy(True, world.n_cells)
    rng = Rng.from_config(tiny_cfg)
    with pytest.raises(ValueError):
        run(tiny_cfg, world, policy, rng)                       # enforce true (config) but no B
    res = run(tiny_cfg, world, policy, rng, budget_vnd=5000.0)    # enforced with B
    assert res.budget_B_vnd == 5000.0
    with pytest.raises(ValueError):
        run(tiny_cfg, world, policy, rng, log_level="verbose", enforce_budget=False)


def test_default_config_skeleton_is_fast(default_yaml):
    # 1 day of ticks with stubs must stay far below the 30 s target: the loop itself is cheap.
    from sim.config import load_config
    cfg = load_config(default_yaml)
    res = _run(cfg)
    assert res.sim_end_s == 90000 and res.promo_on.shape == (96, 61)    # 61 cells
    assert res.runtime_s < 5.0


def test_every_model_option_key_is_implemented():
    # The engine refused config keys whose behaviour was not implemented yet (ring_tiebreak, exact_time,
    # shocks, the Hanoi tariff); all of them are implemented now, so the list is empty.
    from sim.engine import _NOT_YET
    assert _NOT_YET == ()
