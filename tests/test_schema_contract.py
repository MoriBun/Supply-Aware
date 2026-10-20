"""Column names in code match docs/schema.md.

The tables in docs/schema.md are parsed, so editing the schema without the
buffers (or the other way round) fails here.
"""

import re
from pathlib import Path

import pytest

from sim.policies.base import SNAPSHOT_FIELDS
from sim.population import Riders
from sim.state import HIDDEN_COLUMNS, ORDER_COLUMNS, SESSION_COLUMNS

SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "schema.md"
LOGGER_ADDS = {"run_id", "seed"}   # added by the logger to every table (docs/schema.md)
_TYPE_TOKENS = {"int8", "int16", "int32", "int64", "float32", "float64", "bool", "string"}


def _sections() -> dict[str, list[tuple[str, str]]]:
    """``{section title: [(column names cell, type cell), ...]}`` for every markdown table."""
    out: dict[str, list[tuple[str, str]]] = {}
    title = None
    for line in SCHEMA.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            out[title] = []
        elif title and line.startswith("|") and not set(line) <= set("|-: "):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells[0] in ("Cột",):
                continue
            out[title].append((cells[0], cells[1] if len(cells) > 1 else ""))
    return out


def _names(rows) -> dict[str, str]:
    """Column name -> type token ('' if not a single plain type)."""
    names: dict[str, str] = {}
    for name_cell, type_cell in rows:
        name_cell = re.sub(r"\(.*?\)", "", name_cell.replace("`", ""))
        types = [t for t in re.split(r"[/,\s]+", type_cell.replace("`", "")) if t]
        for name in [n.strip() for n in name_cell.split(",") if n.strip()]:
            names[name] = types[0] if len(types) == 1 and types[0] in _TYPE_TOKENS else ""
    return names


@pytest.fixture(scope="module")
def schema():
    return {k: _names(v) for k, v in _sections().items()}


def test_hidden_list_matches_schema():
    text = SCHEMA.read_text(encoding="utf-8")
    m = re.search(r"Danh sách ẩn gồm:\s*`([^`]+)`", text)
    assert m, "hidden list not found in docs/schema.md"
    listed = {s.strip() for s in m.group(1).split(",")}
    assert listed == HIDDEN_COLUMNS


def test_observed_sessions_columns(schema):
    expected = set(schema["observed/sessions (một dòng mỗi session)"]) - LOGGER_ADDS
    actual = {c.name for c in SESSION_COLUMNS if not c.hidden and not c.internal}
    assert actual == expected


def test_hidden_sessions_columns(schema):
    expected = set(schema["hidden/sessions_hidden"]) - LOGGER_ADDS - {"session_id"}
    actual = {c.name for c in SESSION_COLUMNS if c.hidden}
    assert actual == expected


def test_observed_orders_columns(schema):
    expected = set(schema["observed/orders (một dòng mỗi order, `order_id = session_id`)"]) - LOGGER_ADDS
    actual = {c.name for c in ORDER_COLUMNS if not c.hidden and not c.internal}
    assert actual == expected


def test_slot_snapshot_fields(schema):
    expected = set(schema["market/slot_snapshots (một dòng mỗi (ô, slot))"]) - LOGGER_ADDS
    assert set(SNAPSHOT_FIELDS) == expected


def test_rider_columns(schema):
    assert set(Riders.OBSERVED) == set(schema["observed/riders"]) - LOGGER_ADDS
    assert set(Riders.HIDDEN) == set(schema["hidden/riders_hidden"]) - LOGGER_ADDS - {"rider_id"}


def test_results_and_meta_tables_match_schema(schema):
    from sim.logger import META_COLUMNS, RESULTS_TABLES
    tables = {
        "policy_results": (RESULTS_TABLES["policy_results"], "results/policy_results (một dòng mỗi lượt chạy)", True),
        "throughput_curve": (RESULTS_TABLES["throughput_curve"], "results/throughput_curve", True),
        "theta_sweep": (RESULTS_TABLES["theta_sweep"], "results/theta_sweep (tổng hợp từ policy_results)", False),
        "run_metadata": (META_COLUMNS, "meta/run_metadata", True),
    }
    for name, (columns, title, per_run) in tables.items():
        types = schema[title]
        assert set(columns) - LOGGER_ADDS == set(types) - LOGGER_ADDS, name
        if per_run:
            assert LOGGER_ADDS <= set(columns), name                     # every per-run table has run_id and seed
        for col, want in types.items():
            if want:
                assert columns[col] == want, f"{name}.{col}: {columns[col]} vs schema {want}"


def test_observed_dtypes_match_schema(schema):
    for columns, key in ((SESSION_COLUMNS, "observed/sessions (một dòng mỗi session)"),
                         (ORDER_COLUMNS, "observed/orders (một dòng mỗi order, `order_id = session_id`)")):
        types = schema[key]
        for c in columns:
            if c.hidden or c.internal:
                continue
            want = types.get(c.name, "")
            if want in ("", "string"):
                continue                    # string columns are int8 codes in the buffer
            assert c.dtype == want, f"{c.name}: buffer {c.dtype} vs schema {want}"
