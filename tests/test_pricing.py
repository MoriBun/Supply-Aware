"""Pricing: base fare, voucher value, BudgetLedger and the voucher layer."""

import numpy as np
import pytest

from sim import choice, demand
from sim.budget import DONG_PER_VND, BudgetLedger, LedgerState, dong_to_vnd, resolve_budget_vnd, vnd_to_dong
from sim.config import load_config
from sim.engine import run
from sim.policies.base import SnapshotStore, SnapshotView
from sim.policies.fixed import FixedPolicy
from sim.pricing import VoucherLayer, base_fare_vnd, quote_eta_by_cell, voucher_dong
from sim.rng import Rng
from sim.state import Clock, Mechanism
from tests.fakes import make_batch, make_context, make_ledger, stub_world


# --- prices --------------------------------------------------------------------------


def test_base_fare_formula(default_yaml):
    # GrabCar Hanoi: 28.473đ for the first 2 km, 9.818đ/km and 442đ/min after,
    # 10.000đ platform fee; minutes after 2 km pro rata of the distance.
    cfg = load_config(default_yaml)
    fare = base_fare_vnd(cfg, [20.0, 5.0, 4.0], [6.0, 1.5, 2.0])
    assert fare.dtype == np.float32
    np.testing.assert_allclose(fare, [28473 + 4 * 9818 + 442 * 20 * 4 / 6 + 10000,     # 83.638đ
                                      28473 + 10000, 28473 + 10000], rtol=1e-6)        # within the opening 2 km
    assert float(fare[0]) == pytest.approx(83638.33, abs=0.01)
    # The old tariff (USD values in đồng, 1 USD = 100 đ): open + per_min x T, km-independent.
    old = load_config(default_yaml, ["pricing.open_fare_vnd=300", "pricing.open_km=0", "pricing.per_km_vnd=0",
                                     "pricing.per_min_vnd=170", "pricing.platform_fee_vnd=0"])
    np.testing.assert_allclose(base_fare_vnd(old, [0.0, 10.0], [1.5, 4.0]), [300.0, 300.0 + 1700.0], rtol=1e-6)


def test_voucher_dong_pct_and_cap(default_yaml):
    cfg = load_config(default_yaml, ["voucher.pct_of_fare=0.2", "voucher.max_vnd=null"])   # 20%, no cap
    np.testing.assert_array_equal(voucher_dong(cfg, [1700.0, 0.0, 1912.5]), [340, 0, 382])
    assert voucher_dong(cfg, [1700.0]).dtype == np.int64
    capped = load_config(default_yaml, ["voucher.pct_of_fare=0.2", "voucher.max_vnd=200.0"])
    np.testing.assert_array_equal(voucher_dong(capped, [1700.0, 500.0]), [200, 100])
    hanoi = load_config(default_yaml)                                  # 25%, at most 20.000đ
    np.testing.assert_array_equal(voucher_dong(hanoi, [40000.0, 80000.0, 83638.33]), [10000, 20000, 20000])


def test_money_helpers():
    assert vnd_to_dong(1912.5) == 1912 or vnd_to_dong(1912.5) == 1913  # banker's vs half-up; both exact dong
    assert vnd_to_dong(340.0) == 340 and dong_to_vnd(340) == 340.0


# --- ledger periods --------------------------------------------------------------------


def test_period_anchored_at_window_start():
    led = make_ledger(window_start_s=3600.0, period_s=86400.0, n_periods=2, warmup_s=3600.0)
    assert led.period_of(0.0) == -1 and led.period_of(3599.0) == -1
    assert led.period_of(3600.0) == 0 and led.period_of(3600.0 + 86399.0) == 0
    assert led.period_of(3600.0 + 86400.0) == 1
    with pytest.raises(ValueError):
        led.period_of(3600.0 + 2 * 86400.0)


def test_warmup_budget_is_prorated():
    led = make_ledger(budget_vnd=10000.0, period_s=86400.0, warmup_s=3600.0)
    assert led.limit_dong(0) == 10000
    assert led.limit_dong(-1) == 10000 // 24     # floor(B * warmup / period)
    led = make_ledger(budget_vnd=None, enforce=False)
    assert led.limit_dong(0) is None and led.available_dong(0) is None


def test_enforce_requires_budget():
    with pytest.raises(ValueError):
        make_ledger(enforce=True, budget_vnd=None)
    make_ledger(enforce=False, budget_vnd=None)  # fine


# --- ledger state machine ----------------------------------------------------------------


def test_reserve_commit_settle_flow():
    led = make_ledger(budget_vnd=1000.0)
    assert led.reserve(1, 3600.0, 300)
    assert led.totals(0) == (0, 0, 300) and led.entry(1) == (0, 300, LedgerState.RESERVED)
    led.commit(1)
    assert led.totals(0) == (0, 300, 0)
    led.settle(1)
    assert led.totals(0) == (300, 0, 0) and led.entry(1)[2] == LedgerState.SPENT
    np.testing.assert_allclose(led.spent_by_period_vnd(), [300.0])
    assert led.warmup_spent_vnd() == 0.0
    led.check_invariant()


def test_release_paths():
    led = make_ledger(budget_vnd=1000.0)
    led.reserve(1, 3600.0, 300)
    led.release_reserved(1)                        # rider did not book
    assert led.totals(0) == (0, 0, 0) and led.entry(1)[2] == LedgerState.RELEASED
    led.reserve(2, 3600.0, 400)
    led.commit(2)
    led.release_committed(2)                       # abandoned / cancelled / truncated
    assert led.totals(0) == (0, 0, 0)
    assert led.entry(99) is None and led.n_entries == 2


def test_invalid_transitions_raise():
    led = make_ledger(budget_vnd=1000.0)
    led.reserve(1, 3600.0, 100)
    with pytest.raises(ValueError):
        led.reserve(1, 3600.0, 100)                # duplicate
    with pytest.raises(ValueError):
        led.settle(1)                              # not committed yet
    with pytest.raises(KeyError):
        led.commit(2)                              # unknown session
    with pytest.raises(ValueError):
        led.reserve(3, 3600.0, -1)


def test_blocking_when_enforced():
    led = make_ledger(budget_vnd=500.0)              # 500 dong
    assert led.reserve(1, 3600.0, 300)
    assert led.can_reserve(3600.0, 200) and not led.can_reserve(3600.0, 201)
    assert not led.reserve(2, 3600.0, 201)         # blocked, nothing recorded
    assert led.entry(2) is None and led.totals(0) == (0, 0, 300)
    assert led.reserve(3, 3600.0, 200)             # exactly fills the budget
    assert led.available_dong(0) == 0
    led.check_invariant()


def test_no_blocking_when_not_enforced_but_totals_tracked():
    led = make_ledger(enforce=False, budget_vnd=100.0)
    assert led.reserve(1, 3600.0, 10_000)          # far above B, still granted
    assert led.totals(0) == (0, 0, 10_000)
    led.check_invariant()                          # no-op when not enforced


def test_warmup_period_has_its_own_ledger():
    led = make_ledger(budget_vnd=10000.0, warmup_s=3600.0)      # warm-up limit 416 dong
    assert led.reserve(1, 0.0, 400)
    assert not led.reserve(2, 10.0, 100)                       # warm-up exhausted
    assert led.reserve(3, 3600.0, 9000)                        # period 0 untouched
    led.commit(1)
    led.settle(1)
    assert led.warmup_spent_vnd() == 400.0
    np.testing.assert_allclose(led.spent_by_period_vnd(), [0.0])


def test_invariant_detects_corruption():
    led = make_ledger(budget_vnd=100.0)
    led.reserve(1, 3600.0, 100)
    led._reserved[1] += 1   # simulate a bug
    with pytest.raises(AssertionError):
        led.check_invariant()
    led = make_ledger(enforce=False, budget_vnd=None)
    led._committed[1] -= 1  # negative totals are a bug even without enforcement
    with pytest.raises(AssertionError):
        led.check_invariant()


# --- periods d, d+1 and cool-down --------------------------------------------------------------


def test_entry_stays_in_opening_period_when_order_ends_later():
    # Session opened in the last minute of period 0; the order completes in period 1 (or in
    # cool-down). The money stays on period 0's books.
    led = make_ledger(budget_vnd=1000.0, period_s=86400.0, n_periods=2)
    assert led.reserve(1, 3600.0 + 86400.0 - 60.0, 300)
    led.commit(1)
    led.settle(1)
    assert led.totals(0) == (300, 0, 0) and led.totals(1) == (0, 0, 0)
    assert led.entry(1) == (0, 300, LedgerState.SPENT)
    np.testing.assert_allclose(led.spent_by_period_vnd(), [300.0, 0.0])
    # Same for a release: a cancel after the period boundary frees period 0, not period 1.
    assert led.reserve(2, 3600.0 + 86400.0 - 1.0, 200)
    led.commit(2)
    led.release_committed(2)
    assert led.totals(0) == (300, 0, 0) and led.totals(1) == (0, 0, 0)


def test_new_period_starts_with_full_budget():
    # "Daily reset": no reset at 00:00; the next period has its own counters and B.
    led = make_ledger(budget_vnd=500.0, period_s=86400.0, n_periods=2)
    assert led.reserve(1, 3600.0, 500)                     # period 0 exhausted
    assert not led.reserve(2, 3600.0 + 86399.0, 1)
    assert led.reserve(3, 3600.0 + 86400.0, 500)           # period 1 untouched by period 0
    assert not led.reserve(4, 3600.0 + 86400.0, 1)
    assert led.available_dong(0) == 0 and led.available_dong(1) == 0
    assert led.available_dong(-1) == led.limit_dong(-1)  # warm-up not consumed either
    led.check_invariant()


def test_ledger_built_from_clock(default_yaml, tiny_cfg):
    # The engine builds the ledger from Clock (window, period, n_periods, warm-up): two days of
    # default.yaml give two 1440-min periods; tiny.yaml (window 120) gives one 120-min period.
    two_days = load_config(default_yaml, ["time.days_per_run=2"])
    for cfg, n_periods, period_s, warmup_share in ((two_days, 2, 86400, 1 / 24), (tiny_cfg, 1, 7200, 1 / 2)):
        clock = Clock.from_config(cfg)
        assert clock.n_periods == n_periods and clock.period_s == period_s
        led = BudgetLedger(enforce=True, budget_dong=vnd_to_dong(24000.0), window_start_s=clock.window_start_s,
                           period_s=clock.period_s, n_periods=clock.n_periods, warmup_s=clock.warmup_s)
        assert led.limit_dong(-1) == int(24000 * warmup_share)
        assert all(led.limit_dong(p) == 24000 for p in range(n_periods))
        assert led.period_of(0.0) == -1 == clock.period_of(0.0)
        assert led.period_of(clock.window_start_s) == 0
        assert led.period_of(clock.window_end_s - 1) == n_periods - 1 == clock.period_of(clock.window_end_s - 1)
        with pytest.raises(ValueError):
            led.period_of(clock.window_end_s)              # sessions are never spawned after the window


def test_invariant_under_random_session_lifecycles():
    # Independent tally: every transition path over warm-up + 2 periods, thousands of sessions.
    # After each operation the invariant holds, blocking is exact, and the ledger's totals equal
    # what the shadow bookkeeping says (budget invariant at the ledger level; the 1-day engine run
    # is in tests/test_acceptance.py).
    gen = np.random.default_rng(20261001)
    led = make_ledger(budget_vnd=200000.0, period_s=86400.0, n_periods=2, warmup_s=3600.0)
    limits = {p: led.limit_dong(p) for p in (-1, 0, 1)}
    assert limits[-1] == 200_000 * 3600 // 86400
    shadow = {p: [0, 0, 0] for p in limits}                # spent, committed, reserved
    live: list[list] = []                                   # [sid, period, dong, state]
    sid, n_blocked, n_ops = 0, 0, 0
    for _ in range(6000):
        if gen.random() < 0.5 or not live:
            sid += 1
            open_time = float(gen.uniform(0.0, 3600.0 + 2 * 86400.0))
            period = led.period_of(open_time)
            dong = int(gen.integers(50, 600))
            fits = sum(shadow[period]) + dong <= limits[period]
            assert led.reserve(sid, open_time, dong) == fits
            if fits:
                shadow[period][2] += dong
                live.append([sid, period, dong, LedgerState.RESERVED])
            else:
                n_blocked += 1
                assert led.entry(sid) is None
        else:
            i = int(gen.integers(len(live)))
            s, p, c, st = live[i]
            if st == LedgerState.RESERVED:
                if gen.random() < 0.3:
                    led.release_reserved(s)
                    shadow[p][2] -= c
                    live.pop(i)
                else:
                    led.commit(s)
                    shadow[p][2] -= c
                    shadow[p][1] += c
                    live[i][3] = LedgerState.COMMITTED
            else:
                if gen.random() < 0.3:
                    led.release_committed(s)
                    shadow[p][1] -= c
                else:
                    led.settle(s)
                    shadow[p][1] -= c
                    shadow[p][0] += c
                live.pop(i)
            n_ops += 1
        led.check_invariant()
        for p in limits:
            assert led.totals(p) == tuple(shadow[p]) and sum(shadow[p]) <= limits[p]
    assert n_blocked > 0 and n_ops > 1000 and led.n_entries == sid - n_blocked
    np.testing.assert_allclose(led.spent_by_period_vnd(), [shadow[0][0] / DONG_PER_VND, shadow[1][0] / DONG_PER_VND])
    assert led.warmup_spent_vnd() == shadow[-1][0] / DONG_PER_VND


# --- B from config -------------------------------------------------------------------------------


def test_resolve_budget_fixed_and_fraction(default_yaml):
    fixed = load_config(default_yaml, ["budget.mode=fixed", "budget.fixed_vnd=123.5"])
    assert resolve_budget_vnd(fixed) == 123.5
    frac = load_config(default_yaml)                        # fraction_of_all_on, fraction 0.30
    assert resolve_budget_vnd(frac, [100.0, 200.0]) == pytest.approx(0.3 * 150.0)
    assert resolve_budget_vnd(frac, np.array([80.0])) == pytest.approx(24.0)
    with pytest.raises(ValueError):
        resolve_budget_vnd(frac)                            # pilot required
    with pytest.raises(ValueError):
        resolve_budget_vnd(frac, [])


# --- voucher layer --------------------------------------------------------------------------


def _layer(cfg, on, ledger, n_cells=7):
    layer = VoucherLayer(cfg, FixedPolicy(on, n_cells), ledger, n_cells)
    layer.start_slot(4, SnapshotView(SnapshotStore(n_cells, 96), 4))
    return layer


def test_layer_requires_start_slot(default_yaml):
    layer = VoucherLayer(load_config(default_yaml), FixedPolicy(True, 7), make_ledger(), 7)
    with pytest.raises(RuntimeError):
        layer.decide(make_batch(2, 7))


def test_layer_all_off_offers_nothing(default_yaml):
    led = make_ledger(budget_vnd=10000.0)
    out = _layer(load_config(default_yaml), False, led).decide(make_batch(5, 7))
    assert not out.arm.any() and not out.budget_blocked.any() and (out.voucher_dong == 0).all()
    assert not out.promo_on_cell.any() and (out.cell_propensity == 0.0).all()
    assert led.n_entries == 0 and (out.budget_period == 0).all()


def test_layer_all_on_within_budget(default_yaml):
    led = make_ledger(budget_vnd=10000.0)
    out = _layer(load_config(default_yaml), True, led).decide(make_batch(5, 7, fare_vnd=1600.0))
    assert out.arm.tolist() == [1] * 5 and (out.voucher_dong == 400).all()                # 25% of 1.600đ
    assert out.decision.offer.all() and out.promo_on_cell.all() and np.isnan(out.slack_hat).all()
    assert led.totals(0) == (0, 0, 2000)


def test_layer_applies_budget_in_session_id_order(default_yaml):
    # Budget for exactly 2 vouchers of 400 đ; ids given in reverse so the smallest ids must win.
    led = make_ledger(budget_vnd=800.0)
    batch = make_batch(4, 7, fare_vnd=1600.0, session_ids=[40, 30, 20, 10])
    out = _layer(load_config(default_yaml), True, led).decide(batch)
    assert out.arm.tolist() == [0, 0, 1, 1]
    assert out.budget_blocked.tolist() == [True, True, False, False]
    assert out.voucher_dong.tolist() == [0, 0, 400, 400]
    assert led.entry(10)[2] == LedgerState.RESERVED and led.entry(40) is None
    led.check_invariant()


def test_layer_rejects_wrong_slot_or_length(default_yaml):
    cfg = load_config(default_yaml)
    layer = VoucherLayer(cfg, FixedPolicy(True, 7), make_ledger(), n_cells=5)
    with pytest.raises(ValueError):
        layer.start_slot(4, SnapshotView(SnapshotStore(7, 96), 4))   # policy built for 7 cells, layer for 5


# --- step 5 on the real session buffer: quote ------------------------------------------------


def quote_ticks(cfg, ticks, *, on: bool, **ctx_kw):
    """spawn -> start_slot (new slot) -> quote -> decide, as the engine does; returns (ctx, tick row ranges)."""
    ctx = make_context(cfg, on=on, **ctx_kw)
    ranges = []
    for tick in ticks:
        t = tick * ctx.clock.tick_s
        slot = ctx.clock.slot_of(t)
        if slot != ctx.current_slot:
            ctx.current_slot = slot
            ctx.layer.start_slot(slot, SnapshotView(ctx.monitor.store, slot))   # FixedPolicy ignores snapshots
        demand.spawn(ctx, t)
        ctx.layer.quote(ctx, t)
        choice.decide(ctx, t)
        ranges.append(ctx.tick_sessions)
    return ctx, ranges


@pytest.fixture
def quoted_off(default_yaml):
    return quote_ticks(load_config(default_yaml), range(8 * 60, 8 * 60 + 20), on=False)[0]


@pytest.fixture
def quoted_on(default_yaml):
    return quote_ticks(load_config(default_yaml), range(8 * 60, 8 * 60 + 20), on=True, enforce_budget=False)[0]


def test_quote_writes_fare_eta_and_policy_columns_all_off(default_yaml, quoted_off):
    cfg, ctx = load_config(default_yaml), quoted_off
    s, space = ctx.sessions, ctx.world.space
    assert s.n > 300
    pu, do, hour = s.col("pu_cell"), s.col("do_cell"), s.col("hour")
    t_min = space.T[pu, do, hour]
    np.testing.assert_array_equal(s.col("quoted_fare_vnd"),
                                  base_fare_vnd(cfg, t_min, t_min.astype(np.float64) / 60.0 * space.speed_kmh[hour]))
    # Every driver is a stub (offline): no supply anywhere, quoted ETA = max_pickup_eta_min.
    assert s.col("no_supply").all() and (s.col("quoted_eta_min") == cfg.matching.max_pickup_eta_min).all()
    assert (s.col("arm") == 0).all() and (s.col("voucher_dong") == 0).all() and (s.col("voucher_value_vnd") == 0).all()
    assert not s.col("budget_blocked").any() and ctx.ledger.n_entries == 0
    assert (s.col("assign_mechanism") == int(Mechanism.FIXED)).all() and not s.col("promo_on_cell").any()
    assert (s.col("cell_propensity") == 0.0).all() and (s.col("propensity") == 0.0).all()
    assert (s.col("cluster_id") == -1).all() and (s.col("block") == -1).all() and not s.col("in_burnin").any()
    assert np.isnan(s.col("score")).all() and np.isnan(s.col("slack_hat")).all() and np.isnan(s.col("propensity_true")).all()
    np.testing.assert_array_equal(s.col("budget_period"), [ctx.ledger.period_of(t) for t in s.col("open_time_s")])
    assert (s.col("budget_period") == 0).all()                      # 08:00 is inside the first budget period
    assert s.col("requested").sum() > 0 and ctx.orders.n == s.col("requested").sum()


def test_quote_eta_by_cell_uses_live_idle_counts(tiny_cfg):
    space = stub_world(tiny_cfg).space
    idle = np.zeros(space.n_cells, dtype=np.int32)
    idle[3] = 2
    eta, no_supply = quote_eta_by_cell(space, np.array([3, 0, 3], dtype=np.int16), 9, idle)
    assert eta.dtype == np.float32 and no_supply.dtype == bool
    assert eta[0] == eta[2] == pytest.approx(space.eta_in(2, 9)) and not no_supply.any()   # R = 1: cell 3 reaches cell 0
    assert eta[1] == pytest.approx(float(space.T[3, 0, 9]))
    eta0, ns0 = quote_eta_by_cell(space, np.array([0, 1]), 9, np.zeros(space.n_cells, dtype=np.int32))
    assert ns0.all() and (eta0 == space.max_pickup_eta_min).all()


def test_quote_all_on_grants_vouchers_and_counts_offers(default_yaml, quoted_on):
    cfg, ctx = load_config(default_yaml), quoted_on
    s = ctx.sessions
    dong = voucher_dong(cfg, s.col("quoted_fare_vnd"))
    assert (s.col("arm") == 1).all() and (dong > 0).all()
    np.testing.assert_array_equal(s.col("voucher_dong"), dong)
    np.testing.assert_array_equal(s.col("voucher_value_vnd"), (dong / DONG_PER_VND).astype(np.float32))
    assert s.col("promo_on_cell").all() and (s.col("cell_propensity") == 1.0).all() and (s.col("propensity") == 1.0).all()
    assert ctx.ledger.n_entries == s.n                               # every session reserved, then committed / released
    assert not s.col("budget_blocked").any()
    np.testing.assert_array_equal(ctx.slot_counters.n_offers, np.bincount(s.col("pu_cell"), minlength=ctx.n_cells))
    states = {ctx.ledger.entry(int(sid))[2] for sid in s.col("session_id")}
    assert states <= {LedgerState.COMMITTED, LedgerState.RELEASED}
    ctx.ledger.check_invariant()


def test_quote_applies_budget_in_session_id_order_and_marks_blocked(tiny_cfg):
    # No driver is online (stubs), so the quoted ETA is 30 min and few riders book: only committed
    # vouchers stay on the books, hence a small B is needed to see blocking within two hours.
    ctx, ranges = quote_ticks(tiny_cfg, range(60, 180), on=True, budget_vnd=30000.0, enforce_budget=True)
    s = ctx.sessions
    blocked, granted = s.col("budget_blocked"), s.col("arm") == 1
    assert blocked.any() and granted.any() and not (blocked & granted).any()
    assert (s.col("voucher_dong")[blocked] == 0).all() and (s.col("voucher_dong")[granted] > 0).all()
    assert (s.col("promo_on_cell")).all()                           # blocked sessions were still in an on cell
    ids, want = s.col("session_id"), voucher_dong(tiny_cfg, s.col("quoted_fare_vnd"))
    for start, stop in ranges:                                       # within a tick, in session_id order:
        g, b = granted[start:stop], blocked[start:stop]              # a session granted after a blocked one must have
        for j in np.flatnonzero(b):                                  # asked for less than the blocked one did
            later = g & (ids[start:stop] > ids[start:stop][j])
            assert (want[start:stop][later] < want[start:stop][j]).all()
    ctx.ledger.check_invariant()
    period = ctx.ledger.period_of(float(s.col("open_time_s")[-1]))
    assert ctx.ledger.used_dong(period) <= ctx.ledger.limit_dong(period) == 30000


def test_quote_keeps_session_randomness_identical_across_policies(quoted_off, quoted_on):
    # A2(c): same session_id, rider, destination and pre-drawn numbers under two policies.
    a, b = quoted_off.sessions, quoted_on.sessions
    assert a.n == b.n
    for name in ("session_id", "rider_id", "pu_cell", "do_cell", "u_book", "u_score", "quoted_fare_vnd",
                 "quoted_eta_min", "no_supply", "budget_period"):
        np.testing.assert_array_equal(a.col(name), b.col(name), err_msg=name)
    assert b.col("requested").sum() > a.col("requested").sum()      # vouchers raise bookings


def test_engine_runs_with_real_demand_through_the_voucher_layer(tiny_cfg):
    # all_off through the layer grants no voucher anywhere; all_on reserves, commits and settles; both
    # runs are deterministic and share their sessions (CRN). The core steps are real, so orders are
    # matched and completed.
    world = stub_world(tiny_cfg)
    off = run(tiny_cfg, world, FixedPolicy(False, 7), Rng.from_config(tiny_cfg), enforce_budget=False, log_level="full")
    on = run(tiny_cfg, world, FixedPolicy(True, 7), Rng.from_config(tiny_cfg), enforce_budget=False, log_level="full")
    again = run(tiny_cfg, world, FixedPolicy(False, 7), Rng.from_config(tiny_cfg), enforce_budget=False)
    assert off.n_sessions == on.n_sessions == again.n_sessions > 100
    assert off.N_completed > 0 and on.N_completed > 0
    assert off.voucher_spent_vnd == 0.0 and on.voucher_spent_vnd > 0.0 and on.V_profit_vnd < off.V_profit_vnd
    assert (off.N_completed, off.V_profit_vnd, off.n_requests, off.n_abandoned, off.n_cancelled) == (
        again.N_completed, again.V_profit_vnd, again.n_requests, again.n_abandoned, again.n_cancelled)
    assert off.n_truncated_orders == on.n_truncated_orders == 0          # every order ends before the cool-down cap
    assert not off.sessions.col("arm").any() and on.sessions.col("arm").all()
    assert (on.sessions.col("in_window") == off.sessions.col("in_window")).all()
    clock = Clock.from_config(tiny_cfg)
    assert clock.window_end_s <= off.sim_end_s <= clock.window_end_s + clock.cooldown_max_s
    np.testing.assert_array_equal(off.spent_by_period_vnd, [0.0])
    assert on.spent_by_period_vnd.sum() > 0.0
    # offered = in-window sessions; without a budget every completed in-window order carried a voucher
    assert len(on.offer_score) == on.n_sessions and on.offer_completed.sum() == on.N_completed
