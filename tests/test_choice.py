"""Choice (sim/choice.py): logit booking decision, hidden ground truth, orders, ledger."""

import math
import warnings

import numpy as np
import pytest

from sim.budget import DONG_PER_VND
from sim import choice, demand
from sim.budget import LedgerState
from sim.choice import request_probability
from sim.config import load_config
from sim.pricing import base_fare_vnd, voucher_dong
from sim.state import HIDDEN_COLUMNS, SESSION_COLUMNS, OrderStatus
from tests.conftest import ROOT
from tests.fakes import make_context, with_forbidden

DEFAULT = ROOT / "config" / "default.yaml"
HIDDEN_TRUTH = ("p_request_treat", "p_request_control", "direct_request_effect_fixed_market")


def quote_by_hand(ctx, *, voucher: bool, eta_min: float = 4.0) -> None:
    """Stand-in for step 5 (pricing.quote): fare, quoted ETA and, if asked, a reserved voucher."""
    start, stop = ctx.tick_sessions
    s, space = ctx.sessions, ctx.world.space
    rows = slice(start, stop)
    t_min = space.T[s.pu_cell[rows], s.do_cell[rows], s.hour[rows]]
    fare = base_fare_vnd(ctx.cfg, t_min, t_min.astype(np.float64) / 60.0 * space.speed_kmh[s.hour[rows]])
    s.quoted_fare_vnd[rows] = fare
    s.quoted_eta_min[rows] = eta_min
    if voucher:
        dong = voucher_dong(ctx.cfg, fare)
        for i in range(start, stop):
            if ctx.ledger.reserve(int(s.session_id[i]), float(s.open_time_s[i]), int(dong[i - start])):
                s.voucher_dong[i] = dong[i - start]
                s.voucher_value_vnd[i] = dong[i - start] / DONG_PER_VND
                s.arm[i] = 1


def run_ticks(cfg, ticks, *, voucher: bool, eta_min: float = 4.0, **ctx_kw):
    ctx = make_context(cfg, on=voucher, **ctx_kw)
    for tick in ticks:
        t = tick * ctx.clock.tick_s
        demand.spawn(ctx, t)
        quote_by_hand(ctx, voucher=voucher, eta_min=eta_min)
        choice.decide(ctx, t)
    return ctx


@pytest.fixture(scope="module")
def cfg():
    return load_config(DEFAULT)


@pytest.fixture(scope="module")
def off(cfg):
    return run_ticks(cfg, range(8 * 60, 10 * 60), voucher=False)


@pytest.fixture(scope="module")
def on(cfg):
    return run_ticks(cfg, range(8 * 60, 10 * 60), voucher=True)


# --- the logit formula ----------------------------------------------------------


def test_probability_matches_reference_on_1000_parameter_sets():
    gen = np.random.default_rng(20261001)
    n = 1000
    alpha, delta = gen.normal(0.5, 1.0, n), gen.normal(0.15, 0.3, n)
    beta_price, beta_eta = -gen.lognormal(-2.3, 0.5, n), -gen.lognormal(-2.3, 0.5, n)
    fare, eta = gen.uniform(3, 60, n), gen.uniform(1, 30, n)
    voucher = np.where(gen.random(n) < 0.5, 0.2 * fare, 0.0)
    got = request_probability(alpha, beta_price, beta_eta, delta, fare, voucher, eta)
    for i in range(n):
        logit = alpha[i] + beta_price[i] * (fare[i] - voucher[i]) + beta_eta[i] * eta[i] + delta[i] * (voucher[i] > 0)
        assert abs(got[i] - 1.0 / (1.0 + math.exp(-logit))) < 1e-9
    assert got.dtype == np.float64


def test_probability_is_stable_at_extreme_logits():
    with warnings.catch_warnings():
        warnings.simplefilter("error")                       # no overflow warnings
        p = request_probability([800.0, -800.0, 0.0], 0.0, 0.0, 0.0, 10.0, 0.0, 5.0)
    assert p[0] == 1.0 and p[1] == pytest.approx(0.0, abs=1e-300) and p[2] == 0.5


def test_delta_applies_only_with_a_voucher():
    base = request_probability(0.0, -0.1, -0.1, 5.0, 20.0, 0.0, 4.0)
    assert base == pytest.approx(1 / (1 + math.exp(-(-0.1 * 20 - 0.1 * 4))))
    with_v = request_probability(0.0, -0.1, -0.1, 5.0, 20.0, 4.0, 4.0)
    assert with_v == pytest.approx(1 / (1 + math.exp(-(-0.1 * 16 - 0.1 * 4 + 5.0))))


# --- common random numbers ------------------------------------------------------


def test_u_book_is_the_same_under_two_policies(off, on):
    assert off.sessions.n == on.sessions.n > 3000
    for name in ("session_id", "rider_id", "pu_cell", "do_cell", "u_book", "quoted_fare_vnd"):
        np.testing.assert_array_equal(off.sessions.col(name), on.sessions.col(name), err_msg=name)


def test_decision_is_u_book_against_the_actual_probability(off, on):
    for ctx, column in ((off, "p_request_control"), (on, "p_request_treat")):
        s = ctx.sessions
        np.testing.assert_array_equal(s.col("requested"), s.col("u_book") < s.col(column).astype(np.float64))
    # Same rider, same u_book: whoever books without a voucher also books with one whenever it raises P.
    helped = on.sessions.col("p_request_treat") >= on.sessions.col("p_request_control")
    assert on.sessions.col("requested")[helped & off.sessions.col("requested")].all()
    assert on.sessions.col("requested").sum() > off.sessions.col("requested").sum()


def test_booking_rate_matches_mean_probability(off):
    s = off.sessions
    p = s.col("p_request_control").astype(np.float64)
    se = math.sqrt((p * (1 - p)).sum()) / s.n
    assert abs(s.col("requested").mean() - p.mean()) < 4 * se


# --- hidden ground truth --------------------------------------------------------


def test_direct_effect_is_treat_minus_control(cfg, off, on):
    for ctx in (off, on):
        s, r = ctx.sessions, ctx.world.riders
        np.testing.assert_allclose(s.col("direct_request_effect_fixed_market"),
                                   s.col("p_request_treat") - s.col("p_request_control"), atol=1e-6)
        rider = s.col("rider_id")
        fare = s.col("quoted_fare_vnd").astype(np.float64)
        treat = request_probability(r.alpha[rider], r.beta_price[rider], r.beta_eta[rider], r.delta_promo[rider],
                                    fare, voucher_dong(cfg, fare) / DONG_PER_VND, s.col("quoted_eta_min"))
        np.testing.assert_allclose(s.col("p_request_treat"), treat, atol=1e-6)
        assert (s.col("p_request_treat") > 0).all() and (s.col("p_request_treat") < 1).all()
    # Ground truth is a property of the context, not of the arm: identical under both policies.
    for name in HIDDEN_TRUTH:
        np.testing.assert_array_equal(off.sessions.col(name), on.sessions.col(name), err_msg=name)


def test_ground_truth_columns_are_hidden_only(off):
    assert set(HIDDEN_TRUTH) <= HIDDEN_COLUMNS
    by_name = {c.name: c for c in SESSION_COLUMNS}
    assert all(by_name[name].hidden and by_name[name].step == "decide" for name in HIDDEN_TRUTH)
    observed = off.sessions.to_dict(hidden=False)
    assert not set(HIDDEN_TRUTH) & set(observed) and "u_book" not in observed
    assert "requested" in observed


def test_longer_quoted_eta_lowers_the_booking_probability(cfg):
    # no_supply sessions are quoted max_pickup_eta_min; beta_eta < 0 for every rider.
    near = run_ticks(cfg, range(8 * 60, 8 * 60 + 20), voucher=False, eta_min=3.0)
    far = run_ticks(cfg, range(8 * 60, 8 * 60 + 20), voucher=False, eta_min=cfg.matching.max_pickup_eta_min)
    assert (far.sessions.col("p_request_control") < near.sessions.col("p_request_control")).all()
    assert far.sessions.col("requested").sum() < near.sessions.col("requested").sum()


# --- orders ---------------------------------------------------------------------


@pytest.mark.parametrize("which", ["off", "on"])
def test_one_waiting_order_per_booking(which, request):
    ctx = request.getfixturevalue(which)
    s, o = ctx.sessions, ctx.orders
    booked = np.flatnonzero(s.col("requested"))
    assert o.n == len(booked) > 0
    np.testing.assert_array_equal(s.col("order_idx")[booked], np.arange(o.n))
    assert (s.col("order_idx")[~s.col("requested")] == -1).all()
    np.testing.assert_array_equal(o.col("session_idx"), booked)
    for name in ("session_id", "rider_id", "pu_cell", "do_cell", "in_window", "voucher_dong"):
        np.testing.assert_array_equal(o.col(name), s.col(name)[booked], err_msg=name)
    np.testing.assert_array_equal(o.col("order_id"), o.col("session_id"))
    np.testing.assert_array_equal(o.col("request_time_s"), s.col("open_time_s")[booked])
    np.testing.assert_array_equal(o.col("gross_fare_vnd"), s.col("quoted_fare_vnd")[booked])
    np.testing.assert_allclose(o.col("net_fare_vnd"), o.col("gross_fare_vnd") - o.col("voucher_value_vnd"), atol=1e-5)
    np.testing.assert_allclose(o.col("voucher_value_vnd"), o.col("voucher_dong") / DONG_PER_VND, atol=1e-6)
    assert (o.col("status") == int(OrderStatus.WAITING)).all() and o.count_open() == o.n
    # Later steps have written nothing yet.
    assert (o.col("driver_id") == -1).all() and np.isnan(o.col("matched_time_s")).all()
    if which == "off":
        assert (o.col("voucher_dong") == 0).all()
    else:
        assert (o.col("voucher_dong") > 0).all()


def test_counters_follow_the_new_orders(on):
    by_cell = np.bincount(on.orders.col("pu_cell"), minlength=on.n_cells)
    np.testing.assert_array_equal(on.cells.waiting, by_cell)
    np.testing.assert_array_equal(on.slot_counters.n_requests, by_cell)
    np.testing.assert_array_equal(on.slot_counters.n_sessions,
                                  np.bincount(on.sessions.col("pu_cell"), minlength=on.n_cells))


# --- budget ledger --------------------------------------------------------------


def test_voucher_is_committed_by_a_booking_and_released_otherwise(on):
    s, ledger = on.sessions, on.ledger
    assert (s.col("voucher_dong") > 0).all()
    committed = 0
    for i in range(0, s.n, 37):                               # a spread sample of sessions
        _, dong, state = ledger.entry(int(s.col("session_id")[i]))
        assert dong == s.col("voucher_dong")[i]
        assert state == (LedgerState.COMMITTED if s.col("requested")[i] else LedgerState.RELEASED)
    spent, committed, reserved = ledger.totals(ledger.period_of(float(s.col("open_time_s")[0])))
    assert reserved == 0 and spent == 0
    assert committed == int(s.col("voucher_dong")[s.col("requested")].sum())
    ledger.check_invariant()


def test_sessions_without_voucher_never_touch_the_ledger(off):
    assert off.ledger.n_entries == 0


def test_budget_block_leaves_the_session_without_voucher(tiny_cfg):
    # With a small enforced budget some sessions are refused at quote time; they decide at v = 0.
    ctx = run_ticks(tiny_cfg, range(60, 180), voucher=True, budget_vnd=60000.0, enforce_budget=True)
    s = ctx.sessions
    granted = s.col("voucher_dong") > 0
    assert granted.any() and (~granted).any()
    np.testing.assert_array_equal(s.col("requested")[~granted],
                                  (s.col("u_book") < s.col("p_request_control").astype(np.float64))[~granted])
    assert all(ctx.ledger.entry(int(sid)) is None for sid in s.col("session_id")[~granted][:20])
    ctx.ledger.check_invariant()
    period = ctx.ledger.period_of(float(s.col("open_time_s")[-1]))
    assert ctx.ledger.used_dong(period) <= ctx.ledger.limit_dong(period)


# --- step contract --------------------------------------------------------------


def test_empty_tick_is_a_no_op(tiny_cfg):
    ctx = make_context(tiny_cfg)
    choice.decide(ctx, 0.0)
    assert ctx.orders.n == 0 and ctx.sessions.n == 0


def test_decide_reads_only_sessions_riders_and_ledger(tiny_cfg):
    ctx = make_context(tiny_cfg)
    for tick in range(60, 90):                                 # 30 ticks: enough for sessions at any demand scale
        demand.spawn(ctx, tick * 60.0)
        quote_by_hand(ctx, voucher=False)
        choice.decide(with_forbidden(ctx, "policy", "drivers", "layer", "monitor", "rng"), tick * 60.0)
    assert ctx.sessions.n > 0


def test_decide_handles_only_the_current_tick(tiny_cfg):
    ctx = make_context(tiny_cfg)
    demand.spawn(ctx, 3600.0)
    quote_by_hand(ctx, voucher=False)
    choice.decide(ctx, 3600.0)
    n_sessions, n_orders = ctx.sessions.n, ctx.orders.n
    requested = ctx.sessions.col("requested").copy()
    order_ids = ctx.orders.col("order_id").copy()
    demand.spawn(ctx, 3660.0)
    quote_by_hand(ctx, voucher=False)
    choice.decide(ctx, 3660.0)
    np.testing.assert_array_equal(ctx.sessions.col("requested")[:n_sessions], requested)
    np.testing.assert_array_equal(ctx.orders.col("order_id")[:n_orders], order_ids)
    assert (ctx.orders.col("request_time_s")[n_orders:] == 3660.0).all()
