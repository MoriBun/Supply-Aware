"""State contract: codes, buffers, counters, Clock."""

import numpy as np
import pytest

from sim.config import load_config
from sim.state import (
    CANCEL_REASON_NAMES, HIDDEN_COLUMNS, MECHANISM_NAMES, ORDER_COLUMNS, ORDER_STATUS_NAMES, POLICY_UNIFORMS,
    SESSION_COLUMNS, BudgetLedger, CancelReason, CellCounters, Clock, Column, DriverState, DriverStatus,
    Mechanism, OrderBuffer, OrderStatus, SessionBuffer, SlotCounters, SoABuffer, decode_codes,
)


# --- codes ------------------------------------------------------------------


def test_codes_are_pinned():
    # int8 codes are written to buffers; renumbering would silently corrupt logs.
    assert [int(s) for s in DriverStatus] == [0, 1, 2, 3, 4]
    assert [int(s) for s in OrderStatus] == [0, 1, 2, 3, 4, 5, 6]
    assert [int(m) for m in Mechanism] == [0, 1, 2, 3, 4, 5]
    assert [int(c) for c in CancelReason] == [0, 1]


def test_code_names_match_schema_strings():
    assert set(ORDER_STATUS_NAMES) == set(OrderStatus)
    assert {ORDER_STATUS_NAMES[s] for s in (OrderStatus.COMPLETED, OrderStatus.ABANDONED, OrderStatus.CANCELLED,
                                             OrderStatus.TRUNCATED)} == {"Completed", "Abandoned", "Cancelled", "Truncated"}
    assert set(MECHANISM_NAMES) == set(Mechanism)
    assert set(MECHANISM_NAMES.values()) == {"legacy_rule", "legacy_eps", "explore", "experiment", "threshold", "fixed"}
    assert CANCEL_REASON_NAMES == {CancelReason.NONE: "", CancelReason.RIDER_EN_ROUTE: "rider_en_route"}


def test_policy_uniforms_are_hidden_columns():
    assert POLICY_UNIFORMS <= HIDDEN_COLUMNS
    assert "u_book" in HIDDEN_COLUMNS and "u_book" not in POLICY_UNIFORMS


def test_budget_ledger_is_reexported():
    from sim.budget import BudgetLedger as Original
    assert BudgetLedger is Original


# --- column specs -----------------------------------------------------------


@pytest.mark.parametrize("columns", [SESSION_COLUMNS, ORDER_COLUMNS], ids=["sessions", "orders"])
def test_column_specs_well_formed(columns):
    names = [c.name for c in columns]
    assert len(names) == len(set(names))
    for c in columns:
        assert isinstance(c, Column)
        np.full(2, c.fill, dtype=c.dtype)  # fill must be representable in dtype
        assert c.step in {"spawn", "quote", "decide", "match", "advance", "expire", "en_route"}
        assert not (c.hidden and c.internal)


def test_session_hidden_columns_are_exactly_the_hidden_session_fields():
    hidden = {c.name for c in SESSION_COLUMNS if c.hidden}
    assert hidden <= HIDDEN_COLUMNS
    observed = {c.name for c in SESSION_COLUMNS if not c.hidden and not c.internal}
    assert not observed & HIDDEN_COLUMNS


def test_string_columns_carry_code_maps():
    by_name = {c.name: c for c in SESSION_COLUMNS + ORDER_COLUMNS}
    assert by_name["assign_mechanism"].codes is MECHANISM_NAMES
    assert by_name["status"].codes is ORDER_STATUS_NAMES
    assert by_name["cancel_reason"].codes is CANCEL_REASON_NAMES
    for c in SESSION_COLUMNS + ORDER_COLUMNS:
        assert (c.codes is not None) == (c.dtype == "int8" and c.name in ("assign_mechanism", "status", "cancel_reason"))


def test_decode_codes_for_logger():
    by_name = {c.name: c for c in SESSION_COLUMNS + ORDER_COLUMNS}
    codes = np.array([int(OrderStatus.COMPLETED), int(OrderStatus.WAITING), -1, int(OrderStatus.TRUNCATED)], dtype=np.int8)
    out = decode_codes(by_name["status"], codes)
    assert out.tolist() == ["Completed", "Waiting", "", "Truncated"] and out.dtype.kind == "U"
    assert decode_codes(by_name["cancel_reason"], np.array([0, 1], dtype=np.int8)).tolist() == ["", "rider_en_route"]
    assert decode_codes(by_name["assign_mechanism"], np.array([-1, 5], dtype=np.int8)).tolist() == ["", "fixed"]
    with pytest.raises(ValueError):
        decode_codes(by_name["session_id"], np.array([1], dtype=np.int64))


# --- SoABuffer ----------------------------------------------------------------


def test_buffer_append_fill_and_grow():
    buf = SoABuffer((Column("a", "int32", -1, "spawn"), Column("b", "float32", float("nan"), "quote")), capacity=2)
    assert buf.append(a=1) == 0
    assert buf.append(a=2, b=0.5) == 1
    assert buf.append(a=3) == 2          # triggers grow
    assert buf.capacity == 4 and buf.n == 3
    np.testing.assert_array_equal(buf.col("a"), [1, 2, 3])
    assert np.isnan(buf.col("b")[0]) and buf.col("b")[1] == np.float32(0.5) and np.isnan(buf.col("b")[2])
    assert buf.a.dtype == np.int32 and buf.b.dtype == np.float32
    assert len(buf.a) == buf.capacity


def test_buffer_reserve_and_errors():
    buf = SoABuffer((Column("a", "int64", -1, "spawn"),), capacity=1)
    assert buf.reserve(5) == (0, 5)
    assert buf.n == 5 and buf.capacity >= 5
    np.testing.assert_array_equal(buf.col("a"), [-1] * 5)
    with pytest.raises(KeyError):
        buf.append(zzz=1)
    with pytest.raises(AttributeError):
        buf.zzz
    with pytest.raises(ValueError):
        SoABuffer(())


def test_session_and_order_buffers():
    s = SessionBuffer()
    assert s.names(hidden=False, internal=False)[0] == "session_id"
    assert "order_idx" in s.names(internal=True)
    assert "u_book" in s.names(hidden=True)
    d = s.to_dict()  # default: no internal columns, hidden included
    assert "order_idx" not in d and "u_book" in d and "session_id" in d
    o = OrderBuffer()
    assert o.count_open() == 0
    o.append(order_id=1, status=int(OrderStatus.WAITING))
    o.append(order_id=2, status=int(OrderStatus.COMPLETED))
    o.append(order_id=3, status=int(OrderStatus.MATCHED))
    assert o.count_open() == 2


# --- drivers and counters --------------------------------------------------------


def test_driver_state_defaults():
    d = DriverState(5)
    assert (d.status == int(DriverStatus.OFFLINE)).all()
    assert d.status.dtype == np.int8 and d.cell.dtype == np.int16 and d.order.dtype == np.int64
    assert np.isinf(d.busy_until).all() and np.isnan(d.idle_since).all()
    assert d.count_by_status()[DriverStatus.OFFLINE] == 5


def test_slot_counters_reset():
    acc = SlotCounters(3)
    acc.sum_idle += 1
    acc.n_ticks = 4
    acc.voucher_spent_dong[1] = 250
    acc.reset()
    assert acc.n_ticks == 0 and (acc.sum_idle == 0).all() and (acc.voucher_spent_dong == 0).all()
    cells = CellCounters(3)
    assert cells.idle.dtype == np.int32 and (cells.waiting == 0).all()


# --- Clock ------------------------------------------------------------------------


def test_clock_default_config(default_yaml):
    clk = Clock.from_config(load_config(default_yaml))
    assert (clk.tick_s, clk.slot_s, clk.warmup_s, clk.window_s) == (60, 900, 3600, 86400)
    assert clk.window_start_s == 3600 and clk.window_end_s == 90000
    assert clk.period_s == 86400 and clk.n_periods == 1
    assert clk.ticks_per_day == 1440 and clk.slots_per_day == 96
    assert list(clk.window_slots) == list(range(4, 100))
    assert clk.period_of(0) == -1 and clk.period_of(3600) == 0 and clk.period_of(89999) == 0
    assert clk.in_window(3600) and not clk.in_window(90000) and not clk.in_window(0)
    assert clk.slot_of(900) == 1 and clk.hour_of(86400 + 3600 * 5) == 5 and clk.slot_of_day(86400 + 900) == 1
    assert clk.tick_of_day(86400 + 120) == 2


def test_clock_tiny_config(tiny_cfg):
    clk = Clock.from_config(tiny_cfg)
    assert clk.window_s == 7200 and clk.period_s == 7200 and clk.n_periods == 1
    assert list(clk.window_slots) == list(range(4, 12))
