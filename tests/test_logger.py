"""Logger (sim/logger.py): every table matches docs/schema.md, and hidden columns never leak."""

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from sim.config import load_config
from sim.logger import RUN_KEYS, RUN_TABLES, run_tables
from sim.runner import evaluate, generate
from sim.state import CANCEL_REASON_NAMES, HIDDEN_COLUMNS, MECHANISM_NAMES, ORDER_STATUS_NAMES
from tests.conftest import ROOT
from tests.fakes import make_run_result
from tests.test_schema_contract import LOGGER_ADDS, _names, _sections

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]
SCHEMA_TITLES = {
    "riders": "observed/riders",
    "sessions": "observed/sessions (một dòng mỗi session)",
    "orders": "observed/orders (một dòng mỗi order, `order_id = session_id`)",
    "slot_snapshots": "market/slot_snapshots (một dòng mỗi (ô, slot))",
    "riders_hidden": "hidden/riders_hidden",
    "sessions_hidden": "hidden/sessions_hidden",
    "shocks": "meta/shocks (một dòng mỗi biến động; rỗng khi `shocks.enabled = false`)",
}
ARROW_TO_SCHEMA = {"string": "string", "large_string": "string", "bool": "bool", "int8": "int8", "int16": "int16",
                   "int32": "int32", "int64": "int64", "float": "float32", "double": "float64"}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """``generate`` 2 days on the tiny world with the legacy policy (real engine)."""
    out = tmp_path_factory.mktemp("gen")
    cfg = load_config(TINY, ["policy.name=legacy", "generate.days=2", "runner.n_procs=1"])
    table, result = generate(cfg, out)
    return cfg, out, table, result


def arrow_types(path) -> dict[str, str]:
    schema = pq.read_schema(path)
    return {name: ARROW_TO_SCHEMA[str(schema.field(name).type)] for name in schema.names}


# --- layout and schema ---------------------------------------------------------------------


def test_generate_writes_every_table(generated):
    _, out, table, result = generated
    for name, (folder, _) in RUN_TABLES.items():
        assert (out / folder / f"{name}.parquet").exists(), name
    assert (out / "results" / "policy_results.parquet").exists() and (out / "meta" / "run_metadata.parquet").exists()
    assert len(table) == 1 and table["policy"][0] == "legacy" and result.sessions is not None


@pytest.mark.parametrize("name", list(RUN_TABLES))
def test_tables_match_schema_columns_and_dtypes(generated, name):
    _, out, _, _ = generated
    folder, columns = RUN_TABLES[name]
    types = arrow_types(out / folder / f"{name}.parquet")
    schema = _names(_sections()[SCHEMA_TITLES[name]])
    assert set(types) == set(schema) | LOGGER_ADDS, name
    assert list(types)[:2] == ["run_id", "seed"] and types["run_id"] == "string" and types["seed"] == "int32"
    for col, want in schema.items():
        if want:
            assert types[col] == want, f"{name}.{col}: on disk {types[col]}, schema {want}"
    assert {**RUN_KEYS, **columns} == types                           # the declared dtypes are what gets written


def test_no_hidden_leak(generated):
    # No hidden column in observed/ or market/: hidden columns live only in hidden/.
    _, out, _, _ = generated
    for name, (folder, _) in RUN_TABLES.items():
        cols = set(arrow_types(out / folder / f"{name}.parquet"))
        if folder == "hidden":
            assert cols & HIDDEN_COLUMNS, name
        else:
            assert not cols & HIDDEN_COLUMNS, f"{folder}/{name} leaks {sorted(cols & HIDDEN_COLUMNS)}"
    truth = {"p_request_treat", "p_request_control", "direct_request_effect_fixed_market", "propensity_true"}
    assert truth <= set(arrow_types(out / "hidden" / "sessions_hidden.parquet"))
    assert not truth & set(arrow_types(out / "observed" / "sessions.parquet"))


# --- content ---------------------------------------------------------------------------------


def test_row_counts_and_run_keys(generated):
    cfg, out, table, result = generated
    run_id = table["run_id"][0]
    frames = {name: pd.read_parquet(out / folder / f"{name}.parquet") for name, (folder, _) in RUN_TABLES.items()}
    assert len(frames["sessions"]) == len(frames["sessions_hidden"]) == result.sessions.n > 1000
    assert len(frames["orders"]) == result.orders.n > 0
    assert len(frames["slot_snapshots"]) == len(result.snapshots) * 7
    assert len(frames["riders"]) == len(frames["riders_hidden"]) == cfg.demand.n_riders
    for name, df in frames.items():
        assert (df["run_id"] == run_id).all() and (df["seed"] == cfg.meta.run_seed).all(), name
    np.testing.assert_array_equal(frames["sessions"]["session_id"], frames["sessions_hidden"]["session_id"])
    np.testing.assert_array_equal(frames["riders"]["rider_id"], frames["riders_hidden"]["rider_id"])
    # Two days of window: budget periods 0 and 1 are both used, day runs past 1.
    assert set(frames["sessions"]["budget_period"].unique()) >= {0, 1} and frames["sessions"]["day"].max() >= 1


def test_code_columns_are_strings(generated):
    _, out, _, _ = generated
    sessions = pd.read_parquet(out / "observed" / "sessions.parquet")
    orders = pd.read_parquet(out / "observed" / "orders.parquet")
    snaps = pd.read_parquet(out / "market" / "slot_snapshots.parquet")
    assert set(sessions["assign_mechanism"].unique()) <= set(MECHANISM_NAMES.values())
    assert {"legacy_rule", "explore"} <= set(sessions["assign_mechanism"].unique())
    assert set(orders["status"].unique()) <= set(ORDER_STATUS_NAMES.values())
    assert "Completed" in set(orders["status"].unique())
    assert set(orders["cancel_reason"].unique()) <= set(CANCEL_REASON_NAMES.values())
    assert set(snaps["assign_mechanism_cell"].unique()) <= set(MECHANISM_NAMES.values()) | {""}
    assert (sessions.loc[sessions["arm"] == 1, "voucher_value_vnd"] > 0).all()


def test_values_round_trip_from_the_buffers(generated):
    _, out, _, result = generated
    sessions = pd.read_parquet(out / "observed" / "sessions.parquet")
    hidden = pd.read_parquet(out / "hidden" / "sessions_hidden.parquet")
    np.testing.assert_array_equal(sessions["requested"].to_numpy(), result.sessions.col("requested"))
    np.testing.assert_allclose(hidden["u_book"].to_numpy(), result.sessions.col("u_book").astype(np.float32))
    snaps = pd.read_parquet(out / "market" / "slot_snapshots.parquet")
    first = snaps[snaps["slot"] == 0].sort_values("cell")
    np.testing.assert_array_equal(first["slack"].to_numpy(), result.snapshots[0]["slack"])
    assert snaps["published_at_s"].is_monotonic_increasing


def test_run_tables_need_a_full_log():
    with pytest.raises(ValueError):
        run_tables(make_run_result(), riders=None, run_id="x", seed=0)


def test_multi_seed_full_log_writes_one_folder_per_run(tmp_path):
    cfg = load_config(TINY, ["policy.name=all_off", "sweep.n_seeds=2", "runner.n_procs=1", "budget.enforce=false"])
    table = evaluate(cfg, tmp_path, log_level="full")
    for rid in table["run_id"]:
        assert (tmp_path / "runs" / rid / "observed" / "sessions.parquet").exists()
        assert (tmp_path / "runs" / rid / "hidden" / "sessions_hidden.parquet").exists()
    assert not (tmp_path / "observed").exists()
    minimal = evaluate(cfg, tmp_path / "min", log_level="minimal")
    assert len(minimal) == 2 and not (tmp_path / "min" / "runs").exists()
