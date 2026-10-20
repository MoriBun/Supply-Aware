"""Demand (sim/demand.py); plus the world generation of sim/population.py."""

import dataclasses

import numpy as np
import pytest

from sim import demand
from sim.config import load_config
from sim.population import WorldPart, build_world
from sim.rng import Rng, Stream, draw_session_scalars, make_session_id, split_session_id
from sim.state import HIDDEN_COLUMNS, SESSION_COLUMNS
from tests.conftest import ROOT
from tests.fakes import make_context, with_forbidden

DEFAULT = ROOT / "config" / "default.yaml"
SPAWN_COLUMNS = [c.name for c in SESSION_COLUMNS if c.step == "spawn"]


def world_of(cfg, **rng_kw):
    rng = Rng(run_seed=rng_kw.get("run_seed", cfg.meta.run_seed), world_seed=rng_kw.get("world_seed", cfg.meta.world_seed))
    return build_world(cfg, rng)


def spawn_ticks(ctx, ticks):
    for tick in ticks:
        demand.spawn(ctx, tick * ctx.clock.tick_s)
    return ctx


@pytest.fixture(scope="module")
def cfg():
    return load_config(DEFAULT)


@pytest.fixture(scope="module")
def world(cfg):
    return world_of(cfg)


@pytest.fixture(scope="module")
def morning(cfg):
    """Default world, 300 ticks from 07:00 (about 10 000 sessions)."""
    return spawn_ticks(make_context(cfg), range(7 * 60, 12 * 60))


# --- world: seeds and shapes ----------------------------------------------------


def test_world_depends_on_world_seed_only(cfg, world):
    same = world_of(cfg, run_seed=123)
    other = world_of(cfg, world_seed=cfg.meta.world_seed + 1)
    for name in ("x_freq", "home_cell", "u_latent", "alpha", "max_wait_min"):
        np.testing.assert_array_equal(getattr(world.riders, name), getattr(same.riders, name))
    np.testing.assert_array_equal(world.cell_weight, same.cell_weight)
    np.testing.assert_array_equal(world.drivers.shift_start_h, same.drivers.shift_start_h)
    assert not np.array_equal(world.cell_weight, other.cell_weight)
    assert not np.array_equal(world.riders.u_latent, other.riders.u_latent)
    assert not world.is_stub and not world.space.is_stub


def test_world_parts_are_never_renumbered():
    # VENUES was added last; existing codes keep their numbers.
    assert [(p.name, int(p)) for p in WorldPart] == [("CELL_WEIGHT", 1), ("RIDERS", 2), ("DRIVERS", 3), ("VENUES", 4)]


def test_cell_weights(cfg, world):
    w = world.cell_weight
    assert w.shape == (world.n_cells,) and (w > 0).all()
    assert w.mean() == pytest.approx(1.0)
    assert np.log(w).std() == pytest.approx(cfg.demand.cell_weight_sigma, rel=0.35)   # 61 draws only
    assert world.eval_cell_mask.all()


def test_rider_dtypes_and_ranges(cfg, world):
    r = world.riders
    expected = {"rider_id": np.int32, "home_cell": np.int16, "x_freq": np.float32, "x_tenure": np.float32,
                "x_segment": np.int8, "zf": np.float32, "u_latent": np.float32, "alpha": np.float32,
                "beta_price": np.float32, "beta_eta": np.float32, "delta_promo": np.float32,
                "max_wait_min": np.float32}
    for name, dtype in expected.items():
        col = getattr(r, name)
        assert col.dtype == dtype and col.shape == (cfg.demand.n_riders,), name
    assert set(r.OBSERVED) | set(r.HIDDEN) | {"zf"} == set(expected)
    assert set(r.HIDDEN) <= HIDDEN_COLUMNS
    np.testing.assert_array_equal(r.rider_id, np.arange(r.n))
    assert r.home_cell.min() >= 0 and r.home_cell.max() < world.n_cells
    assert (r.x_freq > 0).all() and (r.max_wait_min > 0).all()
    assert r.x_tenure.min() >= 0 and r.x_tenure.max() <= cfg.riders.x_tenure_max_months
    assert set(np.unique(r.x_segment)) == set(range(len(cfg.riders.x_segment_probs)))
    assert (r.beta_price < 0).all() and (r.beta_eta < 0).all()


def test_rider_distributions(cfg, world):
    r, rc = world.riders, cfg.riders
    assert r.x_freq.mean() == pytest.approx(rc.x_freq_gamma_shape, rel=0.03)          # Gamma(k, 1) has mean k
    assert r.zf.mean() == pytest.approx(0.0, abs=1e-4) and r.zf.std() == pytest.approx(1.0, abs=1e-3)
    assert r.u_latent.mean() == pytest.approx(0.0, abs=0.03) and r.u_latent.std() == pytest.approx(1.0, abs=0.03)
    seg_share = np.bincount(r.x_segment) / r.n
    np.testing.assert_allclose(seg_share, rc.x_segment_probs, atol=0.015)
    home_share = np.bincount(r.home_cell, minlength=world.n_cells) / r.n
    np.testing.assert_allclose(home_share, world.cell_weight / world.cell_weight.sum(), atol=0.008)


def test_behaviour_coefficients_follow_spec(cfg, world):
    r, rc = world.riders, cfg.riders
    # alpha = alpha0 + alpha_freq*zf + alpha_u*u_latent + noise: recover the coefficients by least squares.
    X = np.column_stack([np.ones(r.n), r.zf, r.u_latent]).astype(np.float64)
    coef, *_ = np.linalg.lstsq(X, r.alpha.astype(np.float64), rcond=None)
    np.testing.assert_allclose(coef, [rc.alpha0, rc.alpha_freq, rc.alpha_u], atol=0.02)
    assert (r.alpha - X @ coef).std() == pytest.approx(rc.alpha_noise_sd, rel=0.05)
    # delta = delta0 + delta_u*u_latent + delta_seg[seg] + noise.
    for seg, shift in enumerate(rc.delta_seg):
        m = r.x_segment == seg
        resid = r.delta_promo[m] - rc.delta_u * r.u_latent[m]
        assert resid.mean() == pytest.approx(rc.delta0 + shift, abs=0.01)
        # Median of |beta_price| (per đồng) in a segment is beta_price_per_kvnd / 1000 * multiplier (log-normal noise).
        assert np.median(-r.beta_price[m]) == pytest.approx(rc.beta_price_per_kvnd / 1000 * rc.beta_price_seg_mult[seg],
                                                            rel=0.03)
    assert np.median(-r.beta_eta) == pytest.approx(rc.beta_eta_per_min, rel=0.03)


def test_max_wait_mode_is_five_minutes(cfg, world):
    # Mode ≈ 5 min (±0.5). For a log-normal, mode = exp(mean(log x) - var(log x)).
    log_wait = np.log(world.riders.max_wait_min.astype(np.float64))
    mode = np.exp(log_wait.mean() - log_wait.var())
    assert mode == pytest.approx(cfg.riders.max_wait_mode_min, abs=0.5)
    assert log_wait.std() == pytest.approx(cfg.riders.max_wait_sigma, rel=0.03)


def test_driver_schedule(cfg):
    big = load_config(DEFAULT, ["supply.fleet_size=6000"])
    d = world_of(big).drivers
    sc = big.supply
    assert d.driver_id.dtype == np.int32 and d.origin_cell.dtype == np.int16
    assert d.shift_start_h.dtype == np.float64 and d.shift_len_h.dtype == np.float64
    np.testing.assert_array_equal(d.driver_id, np.arange(6000))
    assert d.shift_len_h.min() >= sc.shift_len_clip_h[0] and d.shift_len_h.max() <= sc.shift_len_clip_h[1]
    assert d.shift_start_h.min() >= 0 and d.shift_start_h.max() < 24
    weights = np.array([w for _, _, w in sc.shift_start_mixture])
    in_any = np.zeros(d.n, dtype=bool)
    for (lo, hi, _), share in zip(sc.shift_start_mixture, weights / weights.sum()):
        inside = (d.shift_start_h >= lo) & (d.shift_start_h < hi)
        in_any |= inside
    assert in_any.all()
    # Intervals 15-19 and 19-23 touch, so check the shares on the merged edges instead.
    edges = sorted({lo for lo, _, _ in sc.shift_start_mixture} | {hi for _, hi, _ in sc.shift_start_mixture})
    hist, _ = np.histogram(d.shift_start_h, bins=edges)
    expected = [sum(w for lo, hi, w in sc.shift_start_mixture if lo <= a and b <= hi) for a, b in zip(edges, edges[1:])]
    np.testing.assert_allclose(hist / d.n, np.array(expected) / weights.sum(), atol=0.02)
    w = world_of(big).cell_weight
    np.testing.assert_allclose(np.bincount(d.origin_cell, minlength=len(w)) / d.n, w / w.sum(), atol=0.015)


def test_riders_and_drivers_are_prefix_stable(cfg, world):
    # Rider i and driver j keep their attributes when the population or fleet size changes.
    small = world_of(load_config(DEFAULT, ["demand.n_riders=1000", "supply.fleet_size=50"]))
    for name in ("home_cell", "x_freq", "x_tenure", "x_segment", "u_latent", "beta_price", "beta_eta", "max_wait_min"):
        np.testing.assert_array_equal(getattr(small.riders, name), getattr(world.riders, name)[:1000], err_msg=name)
    for name in ("origin_cell", "shift_start_h", "shift_len_h"):
        np.testing.assert_array_equal(getattr(small.drivers, name), getattr(world.drivers, name)[:50], err_msg=name)


def test_world_arrays_are_read_only(world):
    for arr in (world.cell_weight, world.riders.x_freq, world.riders.u_latent, world.drivers.shift_start_h,
                world.tables.home_cdf, world.tables.dest_cdf):
        with pytest.raises(ValueError):
            arr[0] = arr[0]


def test_sampling_tables(cfg, world):
    tb, r, n = world.tables, world.riders, world.n_cells
    assert sorted(tb.home_order.tolist()) == list(range(r.n))
    for z in range(n):
        block = tb.home_order[tb.home_start[z]:tb.home_start[z + 1]]
        assert (r.home_cell[block] == z).all() and (np.diff(block) > 0).all()
    assert (np.diff(tb.home_cdf) > 0).all() and (np.diff(tb.all_cdf) > 0).all() and (np.diff(tb.dest_cdf) > 0).all()
    assert tb.all_cdf[-1] == pytest.approx(1.0)
    p = np.diff(np.concatenate(([0.0], tb.dest_cdf.reshape(n, n)[5] - 5)))
    expected = world.cell_weight * np.exp(-world.space.D[5] / cfg.demand.dest_decay_rings)
    np.testing.assert_allclose(p, expected / expected.sum(), atol=1e-12)


# --- demand: rate ---------------------------------------------------------------


def test_session_rate_formula(cfg, world):
    d = cfg.demand
    lam = demand.session_rate(cfg, world.cell_weight, 18)
    np.testing.assert_allclose(lam, d.base_sessions_per_cell_h * d.demand_scale * world.cell_weight
                               * d.hour_profile[18] * cfg.time.tick_s / 3600)
    assert lam.mean() * 3600 / cfg.time.tick_s == pytest.approx(d.base_sessions_per_cell_h * d.demand_scale * d.hour_profile[18])


def test_mean_sessions_per_cell_hour_match_lambda(tiny_layers):
    # Mean sessions per (cell, hour) over 2 000 ticks within ±5% of lambda.
    # Hour 18 of 34 days = 2 040 ticks; demand_scale = 4 gives 2 000 to 18 000 sessions per cell,
    # so ±5% is 2 to 7 standard deviations of the Poisson noise.
    cfg = load_config(tiny_layers, ["demand.demand_scale=4"])
    ticks = [day * 1440 + 18 * 60 + m for day in range(34) for m in range(60)]
    ctx = spawn_ticks(make_context(cfg), ticks)
    assert (ctx.sessions.col("hour") == 18).all() and ctx.sessions.col("day").max() == 33
    counts = np.bincount(ctx.sessions.col("pu_cell"), minlength=ctx.n_cells)
    lam = demand.session_rate(cfg, ctx.world.cell_weight, 18)
    np.testing.assert_allclose(counts / len(ticks), lam, rtol=0.05)


def test_hour_profile_shapes_the_day(tiny_layers):
    # One full day: each hour's total is within 4 Poisson standard deviations of its expectation.
    cfg = load_config(tiny_layers, ["demand.demand_scale=4"])
    ctx = spawn_ticks(make_context(cfg), range(1440))
    per_hour = np.bincount(ctx.sessions.col("hour"), minlength=24)
    expected = np.array([demand.session_rate(cfg, ctx.world.cell_weight, h).sum() * 60 for h in range(24)])
    assert (abs(per_hour - expected) < 4 * np.sqrt(expected)).all()
    assert per_hour.argmax() == int(np.argmax(cfg.demand.hour_profile))


def test_zero_demand_spawns_nothing(tiny_cfg):
    cfg = dataclasses.replace(tiny_cfg, demand=dataclasses.replace(tiny_cfg.demand, demand_scale=0.0))
    ctx = spawn_ticks(make_context(cfg), range(120))
    assert ctx.sessions.n == 0 and ctx.tick_sessions == (0, 0)
    assert ctx.slot_counters.n_sessions.sum() == 0


# --- demand: common random numbers ----------------------------------------------


def test_sessions_do_not_depend_on_policy(tiny_cfg):
    # The same (day, tick, cell) gives the same sessions whatever the policy.
    off = spawn_ticks(make_context(tiny_cfg, on=False), range(240))
    on = spawn_ticks(make_context(tiny_cfg, on=True), range(240))
    assert off.sessions.n == on.sessions.n > 0
    for name in SPAWN_COLUMNS:
        np.testing.assert_array_equal(off.sessions.col(name), on.sessions.col(name), err_msg=name)


def test_spawn_touches_only_world_and_rng(tiny_cfg):
    # Hard rule 4: demand must not read the policy, the fleet, the market state or the budget.
    ctx = make_context(tiny_cfg)
    guarded = with_forbidden(ctx, "policy", "drivers", "orders", "cells", "ledger", "layer", "monitor")
    spawn_ticks(guarded, range(60))
    assert guarded.sessions.n > 0


def test_sessions_depend_on_run_seed(tiny_cfg):
    a = spawn_ticks(make_context(tiny_cfg), range(120))
    other = dataclasses.replace(tiny_cfg, meta=dataclasses.replace(tiny_cfg.meta, run_seed=tiny_cfg.meta.run_seed + 1))
    b = spawn_ticks(make_context(other), range(120))
    np.testing.assert_array_equal(a.world.riders.x_freq, b.world.riders.x_freq)     # same world
    assert a.sessions.n != b.sessions.n or not np.array_equal(a.sessions.col("u_book"), b.sessions.col("u_book"))


def test_spawning_twice_is_identical(tiny_cfg):
    a = spawn_ticks(make_context(tiny_cfg), range(120))
    b = spawn_ticks(make_context(tiny_cfg), range(120))
    for name in SPAWN_COLUMNS:
        np.testing.assert_array_equal(a.sessions.col(name), b.sessions.col(name), err_msg=name)


def test_a_tick_gives_the_same_sessions_whatever_ran_before(tiny_cfg):
    # Keyed streams: tick 500 alone equals tick 500 after ticks 0..499.
    alone = spawn_ticks(make_context(tiny_cfg), [500])
    after = spawn_ticks(make_context(tiny_cfg), range(501))
    start, stop = after.tick_sessions
    assert stop - start == alone.sessions.n > 0
    for name in SPAWN_COLUMNS:
        np.testing.assert_array_equal(after.sessions.col(name)[start:stop], alone.sessions.col(name), err_msg=name)


# --- demand: session rows -------------------------------------------------------


def test_session_ids_are_stable_and_unique(morning):
    s, clock, n = morning.sessions, morning.clock, morning.n_cells
    ids = s.col("session_id")
    assert len(np.unique(ids)) == s.n and (np.diff(ids) > 0).all()
    for i in (0, s.n // 2, s.n - 1):
        day, tick_of_day, cell, k = split_session_id(int(ids[i]), n_cells=n, ticks_per_day=clock.ticks_per_day)
        assert (day, cell) == (int(s.col("day")[i]), int(s.col("pu_cell")[i]))
        assert tick_of_day * clock.tick_s == s.col("open_time_s")[i] % 86400
        assert ids[i] == make_session_id(day, tick_of_day, cell, k, n_cells=n, ticks_per_day=clock.ticks_per_day)


def test_pre_drawn_numbers_follow_the_session_draw_order(cfg, morning):
    # The stored numbers are exactly what rng.SESSION_DRAW_ORDER gives for that session id.
    s = morning.sessions
    for i in (0, 17, s.n - 1):
        gen = morning.rng.rng_for(Stream.SESSION, int(s.col("session_id")[i]))
        gen.random(3)                                   # home-cell coin, rider, destination
        scalars = draw_session_scalars(gen, cfg.demand.trip_time_noise_sigma)
        for name, value in scalars._asdict().items():
            assert s.col(name)[i] == value, name


def test_session_columns_are_filled(morning):
    s, clock, world = morning.sessions, morning.clock, morning.world
    t = s.col("open_time_s")
    np.testing.assert_array_equal(s.col("day"), t // 86400)
    np.testing.assert_array_equal(s.col("hour"), (t % 86400) // 3600)
    np.testing.assert_array_equal(s.col("slot"), t // clock.slot_s)
    np.testing.assert_array_equal(s.col("slot_of_day"), (t % 86400) // clock.slot_s)
    np.testing.assert_array_equal(s.col("in_window"), (t >= clock.window_start_s) & (t < clock.window_end_s))
    for name, n in (("pu_cell", world.n_cells), ("do_cell", world.n_cells), ("rider_id", world.riders.n)):
        assert s.col(name).min() >= 0 and s.col(name).max() < n
    for name in ("u_book", "u_target", "u_explore", "u_explore_arm", "u_score"):
        u = s.col(name)
        assert u.min() >= 0 and u.max() < 1 and u.mean() == pytest.approx(0.5, abs=0.02)
    assert (s.col("trip_noise") > 0).all() and (s.col("e_cancel") > 0).all()
    assert s.col("e_cancel").mean() == pytest.approx(1.0, abs=0.05)
    # Later steps have not written anything yet.
    assert np.isnan(s.col("quoted_fare_vnd")).all() and not s.col("requested").any()
    assert (s.col("order_idx") == -1).all()


def test_warmup_sessions_are_outside_the_window(tiny_cfg):
    n_ticks = 90
    ctx = spawn_ticks(make_context(tiny_cfg), range(n_ticks))
    t = ctx.sessions.col("open_time_s")
    assert n_ticks * 60 > ctx.clock.window_start_s
    assert not ctx.sessions.col("in_window")[t < ctx.clock.window_start_s].any()
    assert ctx.sessions.col("in_window")[t >= ctx.clock.window_start_s].all()


def test_tick_range_and_slot_counter(tiny_cfg):
    ctx = make_context(tiny_cfg)
    seen = 0
    for tick in range(30):
        demand.spawn(ctx, tick * 60)
        start, stop = ctx.tick_sessions
        assert start == seen and stop == ctx.sessions.n
        assert (ctx.sessions.col("open_time_s")[start:stop] == tick * 60).all()
        seen = stop
    np.testing.assert_array_equal(ctx.slot_counters.n_sessions,
                                  np.bincount(ctx.sessions.col("pu_cell"), minlength=ctx.n_cells))


def test_second_day_uses_new_keys(tiny_cfg):
    ctx = spawn_ticks(make_context(tiny_cfg), [600, 1440 + 600])
    s = ctx.sessions
    d0, d1 = s.col("day") == 0, s.col("day") == 1
    assert d0.any() and d1.any() and (s.col("hour") == 10).all()
    assert not np.intersect1d(s.col("session_id")[d0], s.col("session_id")[d1]).size
    assert not np.array_equal(s.col("u_book")[d0][:5], s.col("u_book")[d1][:5])


# --- demand: who rides and where -----------------------------------------------


def test_riders_come_mostly_from_the_pickup_cell(cfg, morning):
    s, r = morning.sessions, morning.world.riders
    resident = r.home_cell[s.col("rider_id")] == s.col("pu_cell")
    share = cfg.demand.home_cell_share
    # home_cell_share from the local draw, plus the few global draws that happen to live there.
    assert share <= resident.mean() <= share + (1 - share) * 0.15
    # Probability ∝ x_freq: the sampled mean is the size-biased mean E[x^2] / E[x].
    x = r.x_freq.astype(np.float64)
    assert x[s.col("rider_id")].mean() == pytest.approx((x**2).mean() / x.mean(), rel=0.04)


@pytest.mark.parametrize("share, all_resident", [(1.0, True), (0.0, False)])
def test_home_cell_share_extremes(share, all_resident):
    cfg = load_config(DEFAULT, [f"demand.home_cell_share={share}"])
    ctx = spawn_ticks(make_context(cfg), range(8 * 60, 9 * 60))
    s, r = ctx.sessions, ctx.world.riders
    resident = r.home_cell[s.col("rider_id")] == s.col("pu_cell")
    if all_resident:
        assert resident.all()
    else:
        assert resident.mean() < 0.15                    # global draw: about one cell's share of riders


def test_destination_frequency_decreases_with_distance(morning):
    # Per destination cell, a farther ring is chosen less often.
    s, space = morning.sessions, morning.world.space
    ring = space.D[s.col("pu_cell"), s.col("do_cell")]
    cells_in_ring = np.array([(space.D[0] == k).sum() for k in range(space.radius + 1)])
    per_cell = np.bincount(ring, minlength=space.radius + 1) / cells_in_ring
    assert (np.diff(per_cell) < 0).all()
    assert (ring == 0).any()                             # trips inside the pickup cell exist
