"""``python -m sim run``: config validation, mode dispatch, modes end to end on the real engine."""

import subprocess
import sys
from pathlib import Path

import pandas as pd

from sim.cli import main
from sim.config import config_hash, load_config

ROOT = Path(__file__).resolve().parents[1]

# Runs are kept small: zero demand where the policy does not matter, one seed, one process.
SMALL = ["--set", "throughput.n_seeds=1", "--set", "throughput.demand_scale_grid=[0.5, 1.0]",
         "--set", "sweep.n_seeds=1", "--set", "gte.n_seeds=1", "--set", "sweep.theta_grid=[0.0, 0.5]",
         "--set", "runner.n_procs=1"]
ZERO_DEMAND = ["--set", "demand.base_sessions_per_cell_h=0"]


def _configs(paths) -> list[str]:
    args = []
    for p in paths:
        args += ["--config", str(p)]
    return args


def test_run_prints_config_hash(capsys, tiny_layers, tmp_path):
    overrides = ["--set", "policy.name=all_off"]
    assert main(["run", "--mode", "evaluate"] + _configs(tiny_layers) + SMALL + ZERO_DEMAND + overrides
                + ["--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    expected = config_hash(load_config(tiny_layers, [a for a in (SMALL + ZERO_DEMAND + overrides) if a != "--set"]))
    assert f"config_hash={expected}" in out and "policy=all_off" in out and "N_mean=" in out


def test_run_rejects_bad_config(capsys, default_yaml):
    assert main(["run", "--mode", "evaluate", "--config", str(default_yaml),
                 "--set", "space.grid_radus=2"]) == 2
    assert "config error" in capsys.readouterr().err


def test_unimplemented_indicator_exits_1(capsys, tiny_layers, tmp_path):
    code = main(["run", "--mode", "evaluate"] + _configs(tiny_layers) + SMALL + ZERO_DEMAND
                + ["--set", "policy.threshold.indicator=utilization", "--out", str(tmp_path)])
    assert code == 1 and "only 'slack' is defined" in capsys.readouterr().err


def test_generate_needs_legacy_or_experiment(capsys, tiny_layers, tmp_path):
    code = main(["run", "--mode", "generate"] + _configs(tiny_layers) + SMALL + ZERO_DEMAND
                + ["--set", "policy.name=all_on", "--out", str(tmp_path)])
    assert code == 1 and "error:" in capsys.readouterr().err


def test_throughput_curve_writes_results(capsys, tiny_layers, tmp_path):
    code = main(["run", "--mode", "throughput_curve"] + _configs(tiny_layers) + SMALL + ZERO_DEMAND
                + ["--out", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert f"out={tmp_path}" in out and "demand_scale" in out and "completed_per_h" in out
    table = pd.read_parquet(tmp_path / "results" / "throughput_curve.parquet")
    assert table["demand_scale"].tolist() == [0.5, 1.0] and table["seed"].tolist() == [0, 0]
    assert (table["completed_per_h"] == 0.0).all()          # stubs: no trips yet


def test_gte_and_sweep_with_real_demand(capsys, tiny_layers, tmp_path):
    # Real sessions flow through every step of the loop.
    assert main(["run", "--mode", "gte"] + _configs(tiny_layers) + SMALL + ["--out", str(tmp_path / "gte")]) == 0
    gte = pd.read_parquet(tmp_path / "gte" / "results" / "policy_results.parquet")
    assert gte["policy"].tolist() == ["all_on", "all_off"] and (gte["n_sessions"] > 0).all()
    assert (gte["N_completed"] > 0).all()
    assert gte["voucher_spent_vnd"][0] > 0 and gte["voucher_spent_vnd"][1] == 0   # only all_on spends
    assert "GTE=" in capsys.readouterr().out
    assert main(["run", "--mode", "sweep_theta"] + _configs(tiny_layers) + SMALL + ["--out", str(tmp_path / "sw")]) == 0
    sweep = pd.read_parquet(tmp_path / "sw" / "results" / "theta_sweep.parquet")
    assert sweep["theta"].tolist() == [0.0, 0.5] and sweep["is_argmax"].sum() == 1


def test_python_dash_m_entry_point(tiny_layers, tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "sim", "run", "--mode", "calibrate_budget"] + _configs(tiny_layers) + SMALL
        + ["--out", str(tmp_path)],
        capture_output=True, text=True, check=False, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "config_hash=" in result.stdout and "budget_B_vnd=" in result.stdout
    assert (tmp_path / "meta" / "run_metadata.parquet").exists()
