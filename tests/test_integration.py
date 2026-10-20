"""Integration: the real tick loop (engine.run) with every step."""

import numpy as np
import pytest

from sim import reposition
from sim.config import load_config
from sim.engine import run
from sim.policies.fixed import FixedPolicy
from sim.rng import Rng
from sim.state import Clock, DriverStatus, OrderStatus
from tests.conftest import ROOT
from tests.fakes import stub_world

DEFAULT = ROOT / "config" / "default.yaml"
TINY = [DEFAULT, ROOT / "tests" / "fixtures" / "tiny.yaml"]
OFFLINE, IDLE, EN_ROUTE, ON_TRIP, REPOSITIONING = (int(s) for s in DriverStatus)
WAITING, MATCHED, O_ON_TRIP, COMPLETED, ABANDONED, CANCELLED, TRUNCATED = (int(s) for s in OrderStatus)
POLICIES = ("all_off", "all_on")      # the other policies: tests/test_acceptance.py ("smoke day with every policy")


def simulate(cfg, policy, **kw):
    world = stub_world(cfg)
    return run(cfg, world, FixedPolicy(policy == "all_on", world.n_cells), Rng.from_config(cfg),
               enforce_budget=False, log_level="full", **kw)


@pytest.fixture(scope="module")
def day():
    """One simulated day of ``default.yaml`` under each policy (module-wide, about 3 s per run)."""
    cfg = load_config(DEFAULT)
    return cfg, {name: simulate(cfg, name) for name in POLICIES}


# --- test_smoke_day -------------------------------------------------------------


@pytest.mark.parametrize("policy", POLICIES)
def test_smoke_day(day, policy):
    cfg, results = day
    res = results[policy]
    assert res.policy == policy
    assert res.N_completed > 0 and res.n_requests > res.N_completed
    assert res.n_sessions > 20_000 and res.sessions.n >= res.n_sessions
    assert 0 < res.mean_pickup_eta_min <= cfg.matching.max_pickup_eta_min
    assert np.isfinite(res.mean_slack) and res.mean_slack > 0
    assert res.runtime_s < cfg.performance.max_seconds_per_sim_day          # a safety bound, not the A5 measure
    assert res.V_profit_vnd != 0.0
    if policy == "all_on":
        assert res.voucher_spent_vnd > 0 and res.promo_on.all() and res.offer_completed.sum() > 0
    else:
        assert res.voucher_spent_vnd == 0.0 and not res.promo_on.any() and len(res.offer_score) == 0


def test_voucher_raises_requests(day):
    _, results = day
    assert results["all_on"].n_requests > results["all_off"].n_requests
    assert results["all_on"].V_profit_vnd < results["all_off"].V_profit_vnd  # a 20% voucher eats the margin


# --- test_conservation ----------------------------------------------------------


@pytest.mark.parametrize("policy", POLICIES)
def test_conservation(day, policy):
    _, results = day
    res = results[policy]
    s, o = res.sessions, res.orders
    requested = s.col("requested")
    status = o.col("status")
    # sessions = requested + not requested; every request is exactly one order.
    assert requested.sum() + (~requested).sum() == s.n
    assert o.n == requested.sum()
    np.testing.assert_array_equal(np.sort(o.col("session_id")), np.sort(s.col("session_id")[requested]))
    np.testing.assert_array_equal(s.col("order_idx")[requested], np.arange(o.n))
    # requested = Completed + Abandoned + Cancelled + Truncated: no order is left open.
    counts = np.bincount(status, minlength=len(OrderStatus))
    assert counts[[WAITING, MATCHED, O_ON_TRIP]].sum() == 0
    assert counts[[COMPLETED, ABANDONED, CANCELLED, TRUNCATED]].sum() == o.n
    # ... and the same inside the evaluation window, against the aggregates of RunResult.
    in_w = o.col("in_window")
    assert res.n_sessions == s.col("in_window").sum()
    assert res.n_requests == in_w.sum() == (s.col("in_window") & requested).sum()
    assert res.N_completed == (in_w & (status == COMPLETED)).sum()
    assert res.n_abandoned == (in_w & (status == ABANDONED)).sum()
    assert res.n_cancelled == (in_w & (status == CANCELLED)).sum()
    assert res.n_requests == res.N_completed + res.n_abandoned + res.n_cancelled + (in_w & (status == TRUNCATED)).sum()
    # Money: completed orders carry payments, the others none.
    done = status == COMPLETED
    assert (o.col("driver_pay_vnd")[done] > 0).all() and (o.col("driver_pay_vnd")[~done] == 0).all()
    assert res.V_profit_vnd == pytest.approx(float(o.col("platform_profit_vnd")[in_w & done].sum()))
    assert res.voucher_spent_vnd == pytest.approx(float(o.col("voucher_value_vnd")[in_w & done].sum()))


@pytest.mark.parametrize("policy", POLICIES)
def test_order_timeline_is_ordered(day, policy):
    _, results = day
    o = results[policy].orders
    status = o.col("status")
    done = status == COMPLETED
    assert (o.col("matched_time_s")[done] >= o.col("request_time_s")[done]).all()
    assert (o.col("pickup_time_s")[done] > o.col("matched_time_s")[done]).all()
    assert (o.col("dropoff_time_s")[done] > o.col("pickup_time_s")[done]).all()
    np.testing.assert_allclose(o.col("pickup_time_s")[done],
                               o.col("matched_time_s")[done] + o.col("pickup_eta_min")[done].astype(np.float64) * 60)
    assert np.isnan(o.col("matched_time_s")[status == ABANDONED]).all()
    assert np.isnan(o.col("pickup_time_s")[status == CANCELLED]).all()
    assert (o.col("driver_id")[status == CANCELLED] >= 0).all()


# --- test_cooldown --------------------------------------------------------------


@pytest.mark.parametrize("policy", POLICIES)
def test_cooldown(day, policy):
    cfg, results = day
    res = results[policy]
    clock = Clock.from_config(cfg)
    s, o = res.sessions, res.orders
    request, status = o.col("request_time_s"), o.col("status")
    # Nothing is created after the window: sessions stop at window end.
    assert s.col("open_time_s").max() < clock.window_end_s and request.max() < clock.window_end_s
    # Warm-up orders exist but are not counted.
    warmup = request < clock.window_start_s
    assert warmup.any() and not o.col("in_window")[warmup].any() and o.col("in_window")[~warmup].all()
    # N(pi) counts orders requested inside the window, including those completed after it ended.
    late = (status == COMPLETED) & o.col("in_window") & (o.col("dropoff_time_s") > clock.window_end_s)
    assert late.any()
    assert res.N_completed == ((status == COMPLETED) & (request >= clock.window_start_s)).sum()
    # The loop ran on past the window until those orders finished, within cooldown_max_min.
    assert clock.window_end_s < res.sim_end_s <= clock.window_end_s + clock.cooldown_max_s
    assert o.col("dropoff_time_s")[late].max() <= res.sim_end_s
    assert res.n_truncated_orders == 0
    assert res.window_start_s == clock.window_start_s and res.window_end_s == clock.window_end_s


def test_cooldown_limit_truncates_open_orders():
    cfg = load_config(TINY, ["time.cooldown_max_min=2", "supply.shift_mode=always_on", "demand.demand_scale=3"])
    res = simulate(cfg, "all_on")
    clock = Clock.from_config(cfg)
    status = res.orders.col("status")
    assert res.sim_end_s == clock.window_end_s + clock.cooldown_max_s
    assert res.n_truncated_orders == (status == TRUNCATED).sum() > 0
    assert np.isin(status, [COMPLETED, ABANDONED, CANCELLED, TRUNCATED]).all()
    assert res.n_requests == res.N_completed + res.n_abandoned + res.n_cancelled + res.n_truncated_orders


# --- test_driver_state_consistency ----------------------------------------------


# W1: schedule shifts with attendance, jitter, an event and a rain spell inside the window, half
# of the drivers stopping while it rains.
W1_TICK_CHECK = ["shocks.enabled=true", "supply.fleet_size=30", "shocks.rain.p_per_day=1",
                 "shocks.rain.start_hour_range=[1.2, 1.5]", "shocks.rain.duration_h_range=[0.5, 0.8]",
                 "shocks.rain.driver_off_share=0.5", "shocks.events.venues=[[1, 1.0, 1.2, 1.5, 60.0, 4.0, 2.0]]"]


@pytest.mark.parametrize("world", ["w0", "w1"])
@pytest.mark.parametrize("policy", POLICIES)
def test_driver_state_consistency(monkeypatch, policy, world):
    """After step 9 of every tick: driver states add up, and drivers, orders and cell counters agree."""
    ticks = []
    original = reposition.step

    def checked_step(ctx, t):
        original(ctx, t)
        d, o = ctx.drivers, ctx.orders
        status = d.status
        counts = np.bincount(status, minlength=len(DriverStatus))
        online = d.n - counts[OFFLINE]
        assert counts[[IDLE, EN_ROUTE, ON_TRIP, REPOSITIONING]].sum() == online
        # No driver is idle (or offline, or repositioning) while holding an order.
        assert (d.order[np.isin(status, [OFFLINE, IDLE, REPOSITIONING])] == -1).all()
        # An idle driver past the shift end only became idle in this tick (cancelled at step 8); step 2 of
        # the next tick takes them offline, and matching never uses them (shift_end_s > t is required).
        late = (status == IDLE) & (d.shift_end_s <= t)
        assert (d.idle_since[late] > t - ctx.clock.tick_s).all()
        assert np.isinf(d.busy_until[np.isin(status, [OFFLINE, IDLE])]).all()
        # Busy drivers hold exactly one live order each, and the order points back at them.
        for d_status, o_status in ((EN_ROUTE, MATCHED), (ON_TRIP, O_ON_TRIP)):
            who = np.flatnonzero(status == d_status)
            held = d.order[who]
            assert (held >= 0).all() and len(set(held.tolist())) == len(held)
            assert (o.status[held] == o_status).all()
            np.testing.assert_array_equal(o.driver_id[held], who)
            assert (o.col("status") == o_status).sum() == len(who)
        # Per-cell counters equal what the arrays say.
        n = ctx.n_cells
        o_status_all, pu = o.col("status"), o.col("pu_cell")
        np.testing.assert_array_equal(ctx.cells.idle, np.bincount(d.cell[status == IDLE], minlength=n))
        np.testing.assert_array_equal(ctx.cells.waiting, np.bincount(pu[o_status_all == WAITING], minlength=n))
        np.testing.assert_array_equal(ctx.cells.enroute, np.bincount(pu[o_status_all == MATCHED], minlength=n))
        np.testing.assert_array_equal(ctx.cells.ontrip, np.bincount(pu[o_status_all == O_ON_TRIP], minlength=n))
        ctx.ledger.check_invariant()
        ticks.append(t)

    monkeypatch.setattr(reposition, "step", checked_step)
    # 7 cells, 10 drivers always on, 2 h of heavy demand: a busy little market with every kind of event.
    if world == "w0":
        cfg = load_config(TINY, ["demand.demand_scale=6", "supply.shift_mode=always_on"])
    else:
        cfg = load_config(TINY, ["demand.demand_scale=6", *W1_TICK_CHECK])
    res = simulate(cfg, policy)
    assert len(ticks) == res.sim_end_s / cfg.time.tick_s >= 180
    assert res.N_completed > 0 and res.n_cancelled > 0 and res.n_abandoned > 0


# --- determinism and common random numbers --------------------------------------


def test_same_seed_gives_the_same_run():
    cfg = load_config(TINY)
    a, b = simulate(cfg, "all_on"), simulate(cfg, "all_on")
    assert (a.N_completed, a.V_profit_vnd, a.n_requests, a.n_cancelled) == (b.N_completed, b.V_profit_vnd,
                                                                             b.n_requests, b.n_cancelled)
    for name in ("status", "driver_id", "pickup_time_s", "dropoff_time_s"):
        np.testing.assert_array_equal(a.orders.col(name), b.orders.col(name), err_msg=name)


def test_policies_face_the_same_sessions(day):
    _, results = day
    off, on = results["all_off"].sessions, results["all_on"].sessions
    for name in ("session_id", "rider_id", "pu_cell", "do_cell", "u_book", "e_cancel", "trip_noise"):
        np.testing.assert_array_equal(off.col(name), on.col(name), err_msg=name)


# --- ring tie-break --------------------------------------------------------------------------


def _source_cell_corr(tiebreak: str) -> float:
    """Correlation between cell id and how often a cell lends a driver to another cell, symmetric world."""
    cfg = load_config(DEFAULT, [f"matching.ring_tiebreak={tiebreak}", "demand.cell_weight_sigma=0",
                                "time.window_min=360"])
    world = stub_world(cfg)
    res = run(cfg, world, FixedPolicy(False, world.n_cells), Rng.from_config(cfg), log_level="full",
              enforce_budget=False)
    src, pu = res.orders.col("driver_origin_cell"), res.orders.col("pu_cell")
    cross = (src >= 0) & (src != pu)
    assert cross.sum() > 100                                    # enough cross-cell pickups to measure
    counts = np.bincount(src[cross], minlength=world.n_cells)
    return float(np.corrcoef(np.arange(world.n_cells), counts)[0, 1])


def test_most_idle_tiebreak_removes_the_cell_id_bias_on_a_symmetric_world():
    # Every cell of a ring has the same T, so lowest_id always lends from the smallest id (corr
    # -0,875 on the old grid). most_idle must make the cell id irrelevant.
    assert _source_cell_corr("lowest_id") < -0.5
    assert abs(_source_cell_corr("most_idle")) < 0.3
