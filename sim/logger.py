"""M12 Logger: Parquet tables of docs/schema.md split into observed/, hidden/, market/, results/, meta/.

One run writes under ``out_dir``::

    observed/riders, observed/sessions, observed/orders    (what models and policies may read)
    market/slot_snapshots                                   (idem)
    hidden/riders_hidden, hidden/sessions_hidden            (ground truth: evaluation and debugging only)
    results/*, meta/run_metadata                            (written by the runner)

Every table carries ``run_id`` (string) and ``seed`` (int32). Code columns of the
buffers (``assign_mechanism``, ``status``, ``cancel_reason``,
``assign_mechanism_cell``) are written as strings; the pre-drawn session numbers
are float64 in memory and float32 on disk. No column of
``state.HIDDEN_COLUMNS`` is ever written to observed/ or market/, which
``tests/test_logger.py::test_no_hidden_leak`` checks on a generated data set.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from sim.policies.base import SNAPSHOT_FIELDS
from sim.population import Riders
from sim.state import (
    HIDDEN_COLUMNS, MECHANISM_NAMES, ORDER_COLUMNS, SESSION_COLUMNS, Column, OrderBuffer, SessionBuffer,
    decode_codes,
)

RESULTS_TABLES: dict[str, dict[str, str]] = {
    "policy_results": {
        "run_id": "string", "policy": "string", "theta": "float64", "seed": "int32", "world_seed": "int64",
        "N_completed": "int64", "V_profit_vnd": "float64", "voucher_spent_vnd": "float64",
        "budget_B_vnd": "float64",
        "n_sessions": "int64", "n_requests": "int64", "n_abandoned": "int64", "n_cancelled": "int64",
        "mean_pickup_eta_min": "float64", "share_cells_off": "float64", "n_switches_per_cell_day": "float64",
        "runtime_s": "float64",
    },
    "theta_sweep": {
        "theta": "float64", "N_mean": "float64", "N_se": "float64", "V_mean": "float64", "V_se": "float64",
        "spent_mean": "float64", "n_seeds": "int32", "is_argmax": "bool",
    },
    "throughput_curve": {
        "run_id": "string", "demand_scale": "float64", "fleet_size": "int32", "seed": "int32",
        "completed_per_h": "float64", "requests_per_h": "float64", "mean_pickup_eta_min": "float64",
        "mean_slack": "float64", "abandon_rate": "float64", "cancel_rate": "float64",
    },
}

META_COLUMNS: dict[str, str] = {
    "run_id": "string", "mode": "string", "config_hash": "string", "config_yaml": "string", "git_sha": "string",
    "policy": "string", "hidden_view": "string", "theta": "float64", "seed": "int32", "world_seed": "int64",
    "budget_B_vnd": "float64", "budget_pilot_seed": "int64", "budget_pilot_spent_vnd": "float64",
    "kappa": "float64", "kappa_pilots": "int32", "window_start_s": "float64", "window_end_s": "float64", "sim_end_s": "float64",
    "n_truncated_orders": "int32", "runtime_s": "float64", "created_at": "string",
}

RUN_KEYS: dict[str, str] = {"run_id": "string", "seed": "int32"}


def _buffer_dtypes(columns: tuple[Column, ...], *, hidden: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in columns:
        if c.internal or c.hidden != hidden:
            continue
        if c.codes is not None:
            out[c.name] = "string"
        elif hidden and c.dtype == "float64":
            out[c.name] = "float32"                       # pre-drawn numbers: float64 in memory, float32 on disk
        else:
            out[c.name] = c.dtype
    return out


# On-disk columns and dtypes of the per-run tables (docs/schema.md), without the run keys.
SESSIONS_COLUMNS = _buffer_dtypes(SESSION_COLUMNS, hidden=False)
SESSIONS_HIDDEN_COLUMNS = {"session_id": "int64", **_buffer_dtypes(SESSION_COLUMNS, hidden=True)}
ORDERS_COLUMNS = _buffer_dtypes(ORDER_COLUMNS, hidden=False)
RIDERS_COLUMNS = {"rider_id": "int32", "home_cell": "int16", "x_freq": "float32", "x_tenure": "float32",
                  "x_segment": "int8"}
RIDERS_HIDDEN_COLUMNS = {"rider_id": "int32", **{name: "float32" for name in Riders.HIDDEN}}
SNAPSHOT_COLUMNS = {
    "cell": "int16", "slot": "int32", "day": "int16", "slot_of_day": "int16", "hour": "int16",
    "idle_avg": "float32", "enroute_avg": "float32", "ontrip_avg": "float32", "waiting_avg": "float32",
    "slack": "float64", "utilization": "float32", "mean_pickup_eta_min": "float32",
    "n_sessions": "int32", "n_offers": "int32", "n_requests": "int32", "n_matched": "int32", "n_completed": "int32",
    "n_abandoned": "int32", "n_cancelled": "int32", "voucher_spent_vnd": "float32",
    "promo_on": "bool", "cell_propensity": "float32", "assign_mechanism_cell": "string", "cluster_id": "int16",
    "block": "int32", "in_burnin": "bool", "slack_lag_slot": "float64", "slack_lag_day": "float64",
    "published_at_s": "float64",
}
assert set(SNAPSHOT_COLUMNS) == set(SNAPSHOT_FIELDS)

# meta/shocks (docs/schema.md): one row per shock of the run, empty without shocks.
SHOCKS_COLUMNS: dict[str, str] = {
    "day": "int16", "kind": "string", "cell": "int16", "start_s": "float64", "end_s": "float64",
    "demand_mult": "float32", "ring1_mult": "float32", "speed_mult": "float32", "n_drivers_off": "int32",
}

RUN_TABLES: dict[str, tuple[str, dict[str, str]]] = {
    "riders": ("observed", RIDERS_COLUMNS),
    "sessions": ("observed", SESSIONS_COLUMNS),
    "orders": ("observed", ORDERS_COLUMNS),
    "slot_snapshots": ("market", SNAPSHOT_COLUMNS),
    "riders_hidden": ("hidden", RIDERS_HIDDEN_COLUMNS),
    "sessions_hidden": ("hidden", SESSIONS_HIDDEN_COLUMNS),
    "shocks": ("meta", SHOCKS_COLUMNS),
}
# Hard rule 3, checked once at import: no hidden column in an observed or market table.
for _name, (_folder, _cols) in RUN_TABLES.items():
    if _folder != "hidden":
        assert not set(_cols) & HIDDEN_COLUMNS, f"hidden column in {_folder}/{_name}"


# ---------------------------------------------------------------------------
# Generic writing
# ---------------------------------------------------------------------------


def cast_table(df: pd.DataFrame, columns: dict[str, str]) -> pd.DataFrame:
    """Columns in schema order with schema dtypes; missing or extra columns are an error."""
    missing, extra = set(columns) - set(df.columns), set(df.columns) - set(columns)
    if missing or extra:
        raise ValueError(f"table columns: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return pd.DataFrame({name: df[name].astype(dtype) for name, dtype in columns.items()})


def write_table(out_dir: Path, folder: str, name: str, df: pd.DataFrame, columns: dict[str, str]) -> Path:
    path = Path(out_dir) / folder / f"{name}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    cast_table(df, columns).to_parquet(path, index=False)
    return path


def write_results(out_dir: Path, name: str, df: pd.DataFrame) -> Path:
    """Write ``results/<name>.parquet`` under ``out_dir`` and return its path."""
    return write_table(out_dir, "results", name, df, RESULTS_TABLES[name])


def write_metadata(out_dir: Path, df: pd.DataFrame) -> Path:
    """Write ``meta/run_metadata.parquet`` (one row per run)."""
    return write_table(out_dir, "meta", "run_metadata", df, META_COLUMNS)


@functools.lru_cache(maxsize=1)
def git_sha() -> str:
    """Commit of the code, or ``"unknown"`` outside a git checkout."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
                             cwd=Path(__file__).resolve().parents[1], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


# ---------------------------------------------------------------------------
# Per-run tables
# ---------------------------------------------------------------------------


def _with_keys(frame: dict[str, np.ndarray], run_id: str, seed: int, n: int) -> pd.DataFrame:
    data = {"run_id": pd.array([run_id] * n, dtype="string"), "seed": np.full(n, int(seed), dtype=np.int32)}
    data.update(frame)
    return pd.DataFrame(data)


def _decoded(buf: SessionBuffer | OrderBuffer, names: list[str]) -> dict[str, np.ndarray]:
    by_name = {c.name: c for c in buf.columns}
    out = {}
    for name in names:
        col = by_name[name]
        values = buf.col(name)
        out[name] = decode_codes(col, values) if col.codes is not None else values
    return out


def sessions_frames(buf: SessionBuffer, run_id: str, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(observed/sessions, hidden/sessions_hidden)`` of a session buffer."""
    observed = _with_keys(_decoded(buf, list(SESSIONS_COLUMNS)), run_id, seed, buf.n)
    hidden = _with_keys(_decoded(buf, list(SESSIONS_HIDDEN_COLUMNS)), run_id, seed, buf.n)
    return observed, hidden


def orders_frame(buf: OrderBuffer, run_id: str, seed: int) -> pd.DataFrame:
    return _with_keys(_decoded(buf, list(ORDERS_COLUMNS)), run_id, seed, buf.n)


def riders_frames(riders: Riders, run_id: str, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(observed/riders, hidden/riders_hidden)`` of the world's riders."""
    n = riders.n
    observed = _with_keys({name: getattr(riders, name) for name in RIDERS_COLUMNS}, run_id, seed, n)
    hidden = _with_keys({name: getattr(riders, name) for name in RIDERS_HIDDEN_COLUMNS}, run_id, seed, n)
    return observed, hidden


_MECHANISM_CELL = Column("assign_mechanism_cell", "int8", -1, "monitor", codes=MECHANISM_NAMES)


def snapshots_frame(records: list[dict[str, np.ndarray]], run_id: str, seed: int) -> pd.DataFrame:
    """``market/slot_snapshots``: one row per (cell, slot) from the published records."""
    if not records:
        return _with_keys({name: np.zeros(0, dtype=np.float64 if "float" in dt else dt if dt != "string" else object)
                           for name, dt in SNAPSHOT_COLUMNS.items()}, run_id, seed, 0)
    frame: dict[str, np.ndarray] = {}
    for name in SNAPSHOT_COLUMNS:
        values = np.concatenate([np.asarray(rec[name]) for rec in records])
        frame[name] = decode_codes(_MECHANISM_CELL, values) if name == "assign_mechanism_cell" else values
    n = len(frame["cell"])
    return _with_keys(frame, run_id, seed, n)


def run_tables(result, riders: Riders, run_id: str, seed: int) -> dict[str, pd.DataFrame]:
    """All per-run tables of a full-log ``RunResult`` (``log_level = "full"``), keyed as in :data:`RUN_TABLES`."""
    if result.sessions is None or result.orders is None or result.snapshots is None:
        raise ValueError("per-run tables need a RunResult from log_level='full'")
    sessions, sessions_hidden = sessions_frames(result.sessions, run_id, seed)
    riders_obs, riders_hidden = riders_frames(riders, run_id, seed)
    return {
        "riders": riders_obs, "sessions": sessions, "orders": orders_frame(result.orders, run_id, seed),
        "slot_snapshots": snapshots_frame(result.snapshots, run_id, seed),
        "riders_hidden": riders_hidden, "sessions_hidden": sessions_hidden,
        "shocks": shocks_frame(getattr(result, "shocks", None), run_id, seed),
    }


def shocks_frame(rows: dict | None, run_id: str, seed: int) -> pd.DataFrame:
    """``meta/shocks`` of one run; an empty table when the run had no shocks (W0)."""
    if rows is None:
        rows = {name: np.zeros(0, dtype=object if dtype == "string" else dtype) for name, dtype in SHOCKS_COLUMNS.items()}
    n = len(rows["day"])
    return _with_keys({name: np.asarray(rows[name]) for name in SHOCKS_COLUMNS}, run_id, seed, n)


def write_run(out_dir: Path, result, riders: Riders, run_id: str, seed: int) -> dict[str, Path]:
    """Write the observed/, market/ and hidden/ tables of one run; returns ``{table: path}``."""
    paths = {}
    for name, df in run_tables(result, riders, run_id, seed).items():
        folder, columns = RUN_TABLES[name]
        paths[name] = write_table(out_dir, folder, name, df, {**RUN_KEYS, **columns})
    return paths
