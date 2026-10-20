"""Validate generated data sets.

``python -m analysis.check_dataset runs/b7a/legacy_28d [more dirs ...] [--markdown]``

For every run directory: each table of docs/schema.md exists with exactly the
declared columns and on-disk types (``sim.logger.RUN_TABLES``); no hidden column
appears in observed/ or market/; sessions, orders and results agree with
each other; and ``config_hash`` in the metadata is the hash of the stored config.
Directories with results only (``gte``, ``sweep_theta``, ``evaluate`` without a
full log) get the results and metadata checks. ``--markdown`` prints a Markdown
table, one row per directory. Exit code 1 when any check fails.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import yaml

from sim.logger import META_COLUMNS, RESULTS_TABLES, RUN_KEYS, RUN_TABLES
from sim.state import HIDDEN_COLUMNS

ARROW_TO_SCHEMA = {"string": "string", "large_string": "string", "bool": "bool", "int8": "int8", "int16": "int16",
                   "int32": "int32", "int64": "int64", "float": "float32", "double": "float64"}
TERMINAL = {"Completed", "Abandoned", "Cancelled", "Truncated"}
# run_metadata columns added after data sets had been generated: older directories may lack them.
META_ADDED_LATER = frozenset({"kappa_pilots"})


def arrow_types(path: Path) -> dict[str, str]:
    schema = pq.read_schema(path)
    return {name: ARROW_TO_SCHEMA.get(str(schema.field(name).type), str(schema.field(name).type))
            for name in schema.names}


def stored_config_hash(config_yaml: str) -> str:
    """``config_hash`` of a stored ``config_yaml``, computed on the stored dict itself.

    ``config_yaml`` is the dump of the normalized config, so this equals
    ``sim.config.config_hash(build_config(...))`` (checked on all 1.146 runs of
    ``runs/b7a`` and ``runs/b7b``), and it still works for configs written before a key was added to
    ``config/default.yaml``, which ``build_config`` rejects.
    """
    raw = yaml.safe_load(config_yaml)
    return hashlib.sha1(json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _check_types(path: Path, expected: dict[str, str], label: str, problems: list[str],
                 optional: frozenset[str] = frozenset()) -> None:
    if not path.exists():
        problems.append(f"{label}: file missing")
        return
    types = arrow_types(path)
    expected = {c: t for c, t in expected.items() if c in types or c not in optional}
    if types != expected:
        missing, extra = sorted(set(expected) - set(types)), sorted(set(types) - set(expected))
        wrong = sorted(c for c in set(types) & set(expected) if types[c] != expected[c])
        problems.append(f"{label}: columns differ from docs/schema.md (missing {missing}, extra {extra}, "
                        f"wrong type {wrong})")


def dir_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 1e6


def check_run_dir(run_dir: Path) -> tuple[list[str], dict]:
    """``(problems, summary)`` of one directory; an empty list means every check passed."""
    run_dir = Path(run_dir)
    problems: list[str] = []
    _check_types(run_dir / "meta" / "run_metadata.parquet", META_COLUMNS, "meta/run_metadata", problems,
                 optional=META_ADDED_LATER)
    if problems:
        return problems, {"name": run_dir.name}
    meta = pd.read_parquet(run_dir / "meta" / "run_metadata.parquet")
    for _, row in meta.iterrows():
        # generate runs on the config's own run_seed, so the stored hash must be the hash of the stored
        # config; multi-seed modes store the per-seed config, whose hash differs by construction.
        if row["mode"] == "generate" and stored_config_hash(row["config_yaml"]) != row["config_hash"]:
            problems.append(f"meta: config_hash {row['config_hash']} is not the hash of config_yaml ({row['run_id']})")
    cfg0 = yaml.safe_load(meta["config_yaml"].iloc[0])
    summary = {
        "name": run_dir.name, "mode": meta["mode"].iloc[0], "config_hash": meta["config_hash"].iloc[0],
        "git_sha": meta["git_sha"].iloc[0][:7], "policy": "/".join(sorted(meta["policy"].unique())),
        "design": "", "n_runs": len(meta), "seeds": f"{meta['seed'].min()}–{meta['seed'].max()}" if len(meta) > 1
        else str(meta["seed"].iloc[0]),
        "days": (meta["window_end_s"].iloc[0] - meta["window_start_s"].iloc[0]) / 86400.0,
        "budget_B_vnd": float(meta["budget_B_vnd"].iloc[0]), "size_mb": dir_size_mb(run_dir),
    }
    if "experiment" in set(meta["policy"]):
        e = cfg0["experiment"]
        level = "" if e["design"] == "rider_ab" else f", cụm {e['cluster_level']}"
        summary["design"] = f"{e['design']}{level}, p_on {e['p_on']}, block {e['block_min']} phút"

    results_path = run_dir / "results" / "policy_results.parquet"
    if results_path.exists():
        _check_types(results_path, RESULTS_TABLES["policy_results"], "results/policy_results", problems)
        results = pd.read_parquet(results_path)
        if set(results["run_id"]) != set(meta["run_id"]):
            problems.append("results/policy_results and meta/run_metadata list different runs")
        summary["N_mean"] = float(results["N_completed"].mean())
        summary["spent_mean"] = float(results["voucher_spent_vnd"].mean())
    else:
        results = None

    if not (run_dir / "observed" / "sessions.parquet").exists():
        return problems, summary                        # results-only directory

    for name, (folder, columns) in RUN_TABLES.items():
        path = run_dir / folder / f"{name}.parquet"
        _check_types(path, {**RUN_KEYS, **columns}, f"{folder}/{name}", problems)
        if path.exists() and folder != "hidden":
            leak = sorted(set(arrow_types(path)) & HIDDEN_COLUMNS)
            if leak:
                problems.append(f"{folder}/{name}: hidden columns leaked: {leak}")
    if problems:
        return problems, summary

    s = pd.read_parquet(run_dir / "observed" / "sessions.parquet",
                        columns=["run_id", "session_id", "in_window", "requested", "arm", "budget_blocked"])
    o = pd.read_parquet(run_dir / "observed" / "orders.parquet", columns=["session_id", "status", "in_window"])
    n_hidden = pq.read_metadata(run_dir / "hidden" / "sessions_hidden.parquet").num_rows
    run_ids = set(s["run_id"].unique())
    if len(run_ids) != 1 or run_ids != set(meta["run_id"]):
        problems.append(f"observed/sessions: run_id {sorted(run_ids)} does not match the metadata")
    if not s["session_id"].is_unique or not o["session_id"].is_unique:
        problems.append("session_id is not unique in sessions or orders")
    if len(o) != int(s["requested"].sum()) or not o["session_id"].isin(s.loc[s["requested"], "session_id"]).all():
        problems.append("orders do not match the requested sessions one to one")
    if not set(o["status"].unique()) <= TERMINAL:
        problems.append(f"orders with a non-terminal status: {sorted(set(o['status'].unique()) - TERMINAL)}")
    if n_hidden != len(s):
        problems.append(f"hidden/sessions_hidden has {n_hidden} rows, observed/sessions {len(s)}")
    n_completed = int(((o["status"] == "Completed") & o["in_window"]).sum())
    if results is not None:
        if int(results["N_completed"].iloc[0]) != n_completed:
            problems.append(f"N_completed {results['N_completed'].iloc[0]} != completed in-window orders {n_completed}")
        if int(results["n_sessions"].iloc[0]) != int(s["in_window"].sum()):
            problems.append("n_sessions in results differs from the in-window sessions")
    win = s[s["in_window"]]
    summary.update({
        "sessions": int(len(s)), "orders": int(len(o)), "N_completed": n_completed,
        "share_arm": float(win["arm"].mean()), "share_blocked": float(win["budget_blocked"].mean()),
        "truncated": int((o["status"] == "Truncated").sum()),
    })
    return problems, summary


def markdown_row(summary: dict) -> str:
    def num(key, fmt):
        value = summary.get(key)
        return "–" if value is None or value != value else format(value, fmt)

    policy = summary.get("policy", "")
    if summary.get("design"):
        policy += f" ({summary['design']})"
    return (f"| `{summary['name']}` | {summary.get('mode', '')} | {policy} | {num('days', '.0f')} | "
            f"{summary.get('seeds', '')} | {num('budget_B_vnd', ',.2f')} | {num('sessions', ',d')} | "
            f"{num('orders', ',d')} | {num('N_completed', ',d') if 'N_completed' in summary else num('N_mean', ',.1f')} | "
            f"{num('share_arm', '.1%')} | {num('share_blocked', '.1%')} | {num('size_mb', '.1f')} | "
            f"`{summary.get('config_hash', '')}` |")


MARKDOWN_HEADER = ("| Thư mục | Mode | Chính sách | Ngày | Seed | B (đ/kỳ) | Session | Order | N hoàn thành | "
                   "Tỷ lệ phát | Tỷ lệ chặn | MB | config_hash |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    as_markdown = "--markdown" in argv
    if not args:
        print(__doc__)
        return 2
    ok = True
    rows = []
    for arg in args:
        problems, summary = check_run_dir(Path(arg))
        ok &= not problems
        rows.append(markdown_row(summary))
        print(f"{arg}: {'OK' if not problems else 'FAILED'}")
        for p in problems:
            print(f"  - {p}")
        if not problems and not as_markdown:
            print("  " + ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in summary.items()))
    if as_markdown:
        print("\n" + MARKDOWN_HEADER)
        print("\n".join(rows))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
