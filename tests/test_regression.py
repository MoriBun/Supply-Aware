"""Regression check: with the newer model options switched off, the code reproduces the recorded baseline.

One simulated day of every policy, run with every newer model option switched off by
``tests/fixtures/regression_pinned.yaml``, must reproduce the recorded outputs number for number.

* Baseline: ``tests/fixtures/regression_baseline.json``, first written on commit 1c2eb85 (before
  the newer model options existed) by ``python -m tests.test_regression --write``, then written
  **once more** after money moved from USD to VND: the unit-equivalence check showed identical
  counts and money columns equal to 100 x the old USD values within 1e-6, but float rounding of
  the booking logit changes the hashes. It is not regenerated otherwise; a mismatch is a real
  behaviour change.
* Pinned config: ``tests/fixtures/regression_pinned.yaml`` = ``config/default.yaml``
  of that commit, overlaid on the current ``default.yaml``, plus the old values of the keys
  added since (``matching.ring_tiebreak: lowest_id``, ``cancel.exact_time: false``,
  ``shocks.enabled: false`` ...).
* Fingerprint: for every parquet table, the kept columns (volatile ones dropped), row
  count, dtypes and a blake2b over ``pd.util.hash_pandas_object`` of each column. Columns
  added after the baseline are ignored; columns of the baseline must still exist.
* Deliberate normalisations of log-only columns, which never feed back into the run:
  ``sessions.slack_hat`` is capped at ``monitor.slack_cap`` (the threshold policy now caps the
  published inf) and set back to NaN outside threshold sessions, and ``lag_slot_same_block`` is
  set back to its baseline placeholder ``True``: the code now fills both for the experiment and
  legacy policies only as information for the analysis.

Run: ``pytest -q -m slow tests/test_regression.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sim import runner
from sim.config import load_config
from tests.conftest import ROOT

DEFAULT = ROOT / "config" / "default.yaml"
PINNED = ROOT / "tests" / "fixtures" / "regression_pinned.yaml"
BASELINE = ROOT / "tests" / "fixtures" / "regression_baseline.json"

# run name -> (runner mode, --set overrides). One day, one seed, full log, written straight into out_dir.
RUNS: dict[str, tuple[str, list[str]]] = {
    "generate_legacy": ("generate", ["policy.name=legacy", "generate.days=1"]),
    "generate_experiment": ("generate", ["policy.name=experiment", "generate.days=1"]),
    "evaluate_threshold": ("evaluate", ["policy.name=threshold", "sweep.n_seeds=1"]),   # kappa auto: pilots too
    "evaluate_all_on": ("evaluate", ["policy.name=all_on", "sweep.n_seeds=1"]),
}
# Differ between two identical runs (ids carry config_hash; metadata carries time and git state).
VOLATILE = frozenset({"run_id", "config_hash", "config_yaml", "git_sha", "created_at", "runtime_s"})


def _column_bytes(s: pd.Series) -> bytes:
    """Hash of one column's values, independent of the pandas extension dtype used to store them."""
    if pd.api.types.is_bool_dtype(s):
        arr = s.to_numpy().astype(np.uint8)
    elif pd.api.types.is_integer_dtype(s):
        arr = s.to_numpy().astype(np.int64)
    elif pd.api.types.is_float_dtype(s):
        arr = s.to_numpy().astype(np.float64)
    else:
        arr = s.astype(str).to_numpy()
    return pd.util.hash_pandas_object(pd.Series(arr), index=False).to_numpy().tobytes()


def table_hash(df: pd.DataFrame, columns: list[str]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for c in columns:
        h.update(c.encode())
        h.update(_column_bytes(df[c]))
    return h.hexdigest()


def fingerprint(out_dir: Path, *, slack_cap: float, columns_of: dict[str, list[str]] | None = None) -> dict:
    """``{table: {n_rows, columns, dtypes, hash}}`` for every parquet table under ``out_dir``.

    ``columns_of`` restricts each table to the baseline's columns (new columns are ignored).
    """
    out: dict[str, dict] = {}
    for path in sorted(out_dir.rglob("*.parquet")):
        key = path.relative_to(out_dir).with_suffix("").as_posix()
        df = pd.read_parquet(path)
        if "slack_hat" in df.columns:
            df["slack_hat"] = np.minimum(df["slack_hat"].astype(np.float64), slack_cap)   # now capped at slack_cap
            if "assign_mechanism" in df.columns:                                           # baseline: NaN off threshold
                df.loc[df["assign_mechanism"].astype(str) != "threshold", "slack_hat"] = np.nan
        if "lag_slot_same_block" in df.columns:
            df["lag_slot_same_block"] = True                                               # baseline: always True
        cols = [c for c in df.columns if c not in VOLATILE]
        if columns_of is not None and key in columns_of:
            missing = [c for c in columns_of[key] if c not in df.columns]
            if missing:
                raise AssertionError(f"{key}: baseline columns missing: {missing}")
            cols = list(columns_of[key])
        out[key] = {"n_rows": int(len(df)), "columns": cols, "dtypes": {c: str(df[c].dtype) for c in cols},
                    "hash": table_hash(df, cols), "col_hash": {c: table_hash(df, [c]) for c in cols}}
    return out


def run_one(name: str, out_dir: Path, *, layers: list[Path] | None = None) -> dict:
    mode, overrides = RUNS[name]
    cfg = load_config(layers or [DEFAULT, PINNED], list(overrides))
    out_dir.mkdir(parents=True, exist_ok=True)
    if mode == "generate":
        runner.generate(cfg, out_dir, log_level="full")
    elif mode == "evaluate":
        runner.evaluate(cfg, out_dir, log_level="full")
    else:
        raise ValueError(mode)
    return fingerprint(out_dir, slack_cap=float(cfg.monitor.slack_cap))


def write_baseline(root: Path) -> dict:
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    baseline = {
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_sha": sha,
        "layers": [p.relative_to(ROOT).as_posix() for p in (DEFAULT, PINNED)],
        "runs": {name: {"mode": RUNS[name][0], "overrides": RUNS[name][1], "tables": run_one(name, root / name)}
                 for name in RUNS},
    }
    BASELINE.write_text(json.dumps(baseline, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return baseline


def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_baseline_fixture_is_complete():
    base = _load_baseline()
    assert set(base["runs"]) == set(RUNS)
    for name, run in base["runs"].items():
        assert (run["mode"], run["overrides"]) == RUNS[name], name
        assert {"results/policy_results", "meta/run_metadata", "observed/sessions", "observed/orders",
                "market/slot_snapshots", "hidden/sessions_hidden"} <= set(run["tables"]), name


@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(RUNS))
def test_one_day_matches_baseline(name: str, tmp_path: Path):
    base = _load_baseline()["runs"][name]["tables"]
    columns_of = {table: info["columns"] for table, info in base.items()}
    mode, overrides = RUNS[name]
    cfg = load_config([DEFAULT, PINNED], list(overrides))
    out_dir = tmp_path / name
    out_dir.mkdir()
    (runner.generate if mode == "generate" else runner.evaluate)(cfg, out_dir, log_level="full")
    now = fingerprint(out_dir, slack_cap=float(cfg.monitor.slack_cap), columns_of=columns_of)
    diffs = []
    for table, info in base.items():
        cur = now.get(table)
        if cur is None:
            diffs.append(f"{table}: table missing")
            continue
        if cur["n_rows"] != info["n_rows"]:
            diffs.append(f"{table}: rows {info['n_rows']} -> {cur['n_rows']}")
        elif cur["hash"] != info["hash"]:
            changed = [c for c in info["columns"] if cur["col_hash"][c] != info["col_hash"][c]]
            diffs.append(f"{table}: values differ in columns {changed}")
        if cur["dtypes"] != info["dtypes"]:
            changed = {c: (info["dtypes"][c], cur["dtypes"][c]) for c in info["columns"] if cur["dtypes"][c] != info["dtypes"][c]}
            diffs.append(f"{table}: dtypes changed {changed}")
    assert not diffs, f"{name}: output differs from the baseline:\n  " + "\n  ".join(diffs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="(re)write tests/fixtures/regression_baseline.json")
    ap.add_argument("--out", type=Path, default=ROOT / "runs" / "regression_baseline")
    a = ap.parse_args()
    if not a.write:
        ap.error("nothing to do without --write")
    b = write_baseline(a.out)
    for n, r in b["runs"].items():
        pr = pd.read_parquet(a.out / n / "results" / "policy_results.parquet")
        print(f"{n}: N_completed={int(pr['N_completed'].iloc[0])} spent={float(pr['voucher_spent_vnd'].iloc[0]):.2f} "
              f"tables={len(r['tables'])}")
    print("wrote", BASELINE.relative_to(ROOT), "from", b["git_sha"])
    sys.exit(0)
