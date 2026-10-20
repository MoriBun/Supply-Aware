"""Monitor (sim/monitor.py) with fake counters, and the no-look-ahead queue."""

import dataclasses

import numpy as np
import pytest

from sim.engine import run
from sim.monitor import MarketMonitor
from sim.policies.base import SNAPSHOT_FIELDS, CellDecision, LookAheadError, SnapshotStore, SnapshotView
from sim.policies.fixed import FixedPolicy
from sim.rng import Rng
from sim.state import CellCounters, Clock, Mechanism, SlotCounters
from tests.fakes import stub_world


@pytest.fixture
def monitor(tiny_cfg):
    clock = Clock.from_config(tiny_cfg)
    return MarketMonitor(tiny_cfg, clock, n_cells=7), clock


def publish_slots(mon, clock, n_slots, idle_of_slot=lambda s: 1, enroute=1):
    """Publish slots 0..n_slots-1 with one accumulated tick each; idle count varies by slot."""
    cells, acc = CellCounters(7), SlotCounters(7)
    cells.enroute[:] = enroute
    for s in range(n_slots):
        cells.idle[:] = idle_of_slot(s)
        mon.accumulate(cells, acc)
        mon.publish(s, (s + 1) * clock.slot_s, acc, None)


# --- means, slack, utilization ----------------------------------------------------------


def test_accumulate_and_publish_means(monitor):
    mon, clock = monitor
    cells, acc = CellCounters(7), SlotCounters(7)
    cells.idle[:] = 2
    cells.enroute[0] = 1          # cell 0: I=2, E=1 -> slack 2
    cells.ontrip[1] = 3           # cell 1: I=2, E=0 -> slack inf; utilization 3/5
    cells.waiting[2] = 4
    for _ in range(3):
        mon.accumulate(cells, acc)
    acc.n_matched[0] = 2
    acc.sum_pickup_eta_min[0] = 9.0
    acc.n_pickup_eta[0] = 2
    acc.voucher_spent_dong[3] = 250
    cd = CellDecision.blank(0, 7, promo_on=True, mechanism=Mechanism.FIXED)
    rec = mon.publish(0, clock.slot_s, acc, cd)
    assert set(rec) == set(SNAPSHOT_FIELDS)
    assert (rec["idle_avg"] == 2).all() and rec["enroute_avg"][0] == 1 and rec["waiting_avg"][2] == 4
    assert rec["slack"][0] == 2.0 and np.isinf(rec["slack"][1])
    assert rec["utilization"][0] == pytest.approx(1 / 3) and rec["utilization"][1] == pytest.approx(3 / 5)
    assert rec["mean_pickup_eta_min"][0] == 4.5 and np.isnan(rec["mean_pickup_eta_min"][1])
    assert rec["voucher_spent_vnd"][3] == np.float32(250.0) and rec["n_matched"][0] == 2
    assert rec["promo_on"].all() and (rec["assign_mechanism_cell"] == int(Mechanism.FIXED)).all()
    assert rec["block"][0] == -1 and not rec["in_burnin"].any()
    assert np.isnan(rec["slack_lag_slot"]).all() and np.isnan(rec["slack_lag_day"]).all()
    assert (rec["published_at_s"] == clock.slot_s).all() and (rec["slot"] == 0).all() and (rec["cell"] == np.arange(7)).all()
    assert acc.n_ticks == 0 and (acc.sum_idle == 0).all()      # reset after publish


def test_means_are_per_tick_averages_and_empty_cells_give_zero_slack_nan_utilization(monitor):
    mon, clock = monitor
    cells, acc = CellCounters(7), SlotCounters(7)
    for idle in (1, 2, 3):                       # cell 0 varies; cell 6 stays empty
        cells.idle[0] = idle
        cells.enroute[0] = 1
        cells.ontrip[0] = 2
        mon.accumulate(cells, acc)
    rec = mon.publish(0, clock.slot_s, acc, None)
    assert rec["idle_avg"][0] == 2.0 and rec["slack"][0] == 2.0
    assert rec["utilization"][0] == pytest.approx(3 / 5)
    assert rec["slack"][6] == 0.0 and np.isnan(rec["utilization"][6])       # I = E = 0: slack 0, utilization NaN
    assert rec["slack"].dtype == np.float64 and rec["utilization"].dtype == np.float32


def test_publish_without_ticks_gives_nan(monitor):
    mon, clock = monitor
    rec = mon.publish(0, clock.slot_s, SlotCounters(7), None)
    assert np.isnan(rec["idle_avg"]).all() and np.isnan(rec["slack"]).all() and np.isnan(rec["utilization"]).all()


# --- event counters by pickup cell -------------------------------------------------------


def test_event_counters_add_up_per_cell_and_reset(monitor):
    mon, clock = monitor
    acc = SlotCounters(7)
    cells = np.array([3, 3, 5], dtype=np.int16)              # two events in cell 3 in the same tick
    acc.n_sessions += np.bincount(cells, minlength=7).astype(np.int32)   # as demand.spawn does
    mon.on_offers(acc, cells[:1])
    mon.on_requests(acc, cells[1:])
    mon.on_matched(acc, cells, np.array([4.0, 8.0, 5.0]))
    mon.on_abandoned(acc, np.array([1]))
    mon.on_cancelled(acc, 2)                                 # scalar cell works too
    mon.on_completed(acc, np.array([3, 3]), np.array([340, 0]))
    assert acc.n_sessions.tolist() == [0, 0, 0, 2, 0, 1, 0]
    assert acc.n_offers[3] == 1 and acc.n_requests.tolist() == [0, 0, 0, 1, 0, 1, 0]
    assert acc.n_matched[3] == 2 and acc.sum_pickup_eta_min[3] == 12.0 and acc.n_pickup_eta[3] == 2
    assert acc.n_abandoned[1] == 1 and acc.n_cancelled[2] == 1
    assert acc.n_completed[3] == 2 and acc.voucher_spent_dong[3] == 340
    mon.accumulate(CellCounters(7), acc)
    rec = mon.publish(0, clock.slot_s, acc, None)
    assert rec["mean_pickup_eta_min"][3] == 6.0 and rec["mean_pickup_eta_min"][5] == 5.0
    assert rec["voucher_spent_vnd"][3] == np.float32(340.0) and rec["n_completed"][3] == 2
    assert rec["n_sessions"][3] == 2 and rec["n_matched"][5] == 1
    for name in ("n_sessions", "n_offers", "n_requests", "n_matched", "n_completed", "n_abandoned", "n_cancelled"):
        assert rec[name].dtype == np.int32 and getattr(acc, name).sum() == 0     # copied, then reset
    assert acc.voucher_spent_dong.sum() == 0 and acc.n_pickup_eta.sum() == 0


# --- lags and time fields -----------------------------------------------------------------


def test_lags_and_time_fields(monitor):
    mon, clock = monitor
    cells, acc = CellCounters(7), SlotCounters(7)
    cells.idle[:] = 1
    cells.enroute[:] = 1
    mon.accumulate(cells, acc)
    mon.publish(0, clock.slot_s, acc, None)
    cells.idle[:] = 3
    mon.accumulate(cells, acc)
    rec = mon.publish(1, 2 * clock.slot_s, acc, None)
    assert (rec["slack"] == 3.0).all() and (rec["slack_lag_slot"] == 1.0).all()
    assert np.isnan(rec["slack_lag_day"]).all()
    assert not rec["promo_on"].any() and np.isnan(rec["cell_propensity"]).all() and (rec["cluster_id"] == -1).all()
    # slot 5 of day 0 starts at 75 min -> hour 1, slot_of_day 5
    for s in range(2, 6):
        mon.accumulate(cells, acc)
        mon.publish(s, (s + 1) * clock.slot_s, acc, None)
    rec5 = mon.store.get("hour", 5)
    assert rec5[0] == 1 and mon.store.get("slot_of_day", 5)[0] == 5 and mon.store.get("day", 5)[0] == 0


def test_slack_lag_day_after_one_day(monitor):
    mon, clock = monitor
    publish_slots(mon, clock, clock.slots_per_day + 1, idle_of_slot=lambda s: s)   # 97 slots
    last = clock.slots_per_day
    assert (mon.store.get("slack_lag_day", last) == 0.0).all()        # slot 0 had I = 0
    assert (mon.store.get("slack_lag_slot", last) == last - 1).all()
    assert mon.store.get("day", last)[0] == 1 and mon.store.get("slot_of_day", last)[0] == 0


def test_slack_lag_day_nan_on_day_one_then_exact_for_whole_day_two(monitor):
    mon, clock = monitor
    spd = clock.slots_per_day
    publish_slots(mon, clock, 2 * spd, idle_of_slot=lambda s: (s * 7) % 11)
    for s in range(spd):
        assert np.isnan(mon.store.get("slack_lag_day", s)).all()
    for s in range(spd, 2 * spd):
        np.testing.assert_array_equal(mon.store.get("slack_lag_day", s), mon.store.get("slack", s - spd))
        np.testing.assert_array_equal(mon.store.get("slack_lag_slot", s), mon.store.get("slack", s - 1))
    # The policy view gives the same numbers for the decision slot k: slack of k-1 and k-96.
    k = 2 * spd
    view = mon.view(k)
    np.testing.assert_array_equal(view.lag("slack", 1), mon.store.get("slack", k - 1))
    np.testing.assert_array_equal(view.lag_day("slack"), mon.store.get("slack", k - spd))
    # A view for a slot of day one (built directly: the monitor refuses views of past slots) has no lag day.
    assert np.isnan(SnapshotView(mon.store, spd - 1).lag_day("slack")).all()


# --- no look-ahead: the queue (hard rule 2) -------------------------------------------------


def test_view_from_monitor_refuses_look_ahead(monitor):
    mon, clock = monitor
    cells, acc = CellCounters(7), SlotCounters(7)
    mon.accumulate(cells, acc)
    mon.publish(0, clock.slot_s, acc, None)
    view = mon.view(1)
    assert view.get("slack", 0).shape == (7,)
    with pytest.raises(LookAheadError):
        view.get("slack", 1)
    with pytest.raises(LookAheadError):
        mon.view(0).get("slack", 0)


def test_view_sees_only_snapshots_published_before_the_slot_starts(monitor):
    # The policy of slot k only receives snapshots with published_at_s <= start of k.
    mon, clock = monitor
    publish_slots(mon, clock, 4)
    k = 4
    view = mon.view(k)
    for s in range(k):
        assert view.available(s)
        assert view.get("published_at_s", s)[0] <= k * clock.slot_s
    assert not view.available(k) and not view.available(k + 1)
    for s in (k, k + 1):
        with pytest.raises(LookAheadError):
            view.get("slack", s)
    with pytest.raises(LookAheadError):
        view.lag("slack", 0)
    assert np.isnan(view.get("slack", -1)).all()          # before the first publish: NaN, no error


def test_view_requires_exactly_the_previous_slot_published(monitor):
    mon, clock = monitor
    assert isinstance(mon.view(0), SnapshotView)        # nothing published yet: fine at slot 0
    with pytest.raises(RuntimeError):
        mon.view(1)                                     # slot 0 not published: gap
    publish_slots(mon, clock, 2)                        # slots 0, 1 published
    mon.view(2)
    with pytest.raises(LookAheadError):
        mon.view(1)                                     # slot 1 already published
    with pytest.raises(RuntimeError):
        mon.view(3)                                     # slot 2 missing


def test_publish_must_happen_at_slot_end_and_in_order(monitor):
    mon, clock = monitor
    acc = SlotCounters(7)
    with pytest.raises(ValueError):
        mon.publish(0, clock.slot_s - 1, acc, None)      # too early
    with pytest.raises(ValueError):
        mon.publish(0, 2 * clock.slot_s, acc, None)      # too late
    with pytest.raises(ValueError):
        mon.publish(1, 2 * clock.slot_s, acc, None)      # out of order (store)
    assert len(mon.store) == 0
    mon.publish(0, clock.slot_s, acc, None)
    with pytest.raises(ValueError):
        mon.publish(0, clock.slot_s, acc, None)          # duplicate
    assert len(mon.store) == 1


def test_store_rejects_bad_records():
    store = SnapshotStore(7, 96)
    with pytest.raises(ValueError):
        store.publish(0, {"slack": np.zeros(7)})                       # missing fields
    rec = {f: np.zeros(7) for f in SNAPSHOT_FIELDS}
    rec["slack"] = np.zeros(6)
    with pytest.raises(ValueError):
        store.publish(0, rec)                                          # wrong shape
    with pytest.raises(KeyError):
        store.get("slack", 0)


# --- integration with the engine skeleton --------------------------------------------------


def test_engine_publishes_every_slot_in_order(tiny_cfg):
    # demand_scale = 0, as in test_engine.py: this checks the publishing loop, not the market.
    cfg = dataclasses.replace(tiny_cfg, demand=dataclasses.replace(tiny_cfg.demand, demand_scale=0.0))
    world = stub_world(cfg)
    res = run(cfg, world, FixedPolicy(True, world.n_cells), Rng.from_config(cfg),
              enforce_budget=False, log_level="full")
    clock = Clock.from_config(cfg)
    snaps = res.snapshots
    assert len(snaps) == clock.window_end_s // clock.slot_s          # warm-up + window, no partial slot
    for s, rec in enumerate(snaps):
        assert (rec["slot"] == s).all() and (rec["published_at_s"] == (s + 1) * clock.slot_s).all()
        assert (rec["slot_of_day"] == s % clock.slots_per_day).all() and (rec["hour"] == (s * clock.slot_s) // 3600).all()
        assert rec["promo_on"].all() and (rec["assign_mechanism_cell"] == int(Mechanism.FIXED)).all()
        if s == 0:
            assert np.isnan(rec["slack_lag_slot"]).all()
        else:
            np.testing.assert_array_equal(rec["slack_lag_slot"], snaps[s - 1]["slack"])
        assert np.isnan(rec["slack_lag_day"]).all()                  # window shorter than a day
