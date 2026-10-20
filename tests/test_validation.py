"""Validation numbers read from saved runs (analysis/validation.py) for notebook 01."""

import math

import numpy as np
import pandas as pd
import pytest

from analysis import validation
from sim.config import load_config
from tests.conftest import ROOT

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]


def _summary(done, slack, eta):
    return pd.DataFrame({"demand_scale": np.arange(1, len(done) + 1) * 0.5, "completed_per_h": done,
                         "mean_slack": slack, "mean_pickup_eta_min": eta})


def test_a1_checks_pass_and_fail_each_condition():
    good = _summary([10, 20, 30, 25, 20], [3.0, 1.0, 0.5, 0.3, 0.1], [2.0, 2.5, 3.5, 5.0, 7.5])
    assert validation.a1_checks(good)["passed"].all()
    flat_end = _summary([10, 20, 30, 29, 29], [3.0, 1.0, 0.5, 0.3, 0.1], [2.0, 2.5, 3.5, 5.0, 7.5])
    high_slack = _summary([10, 20, 30, 25, 20], [3.0, 1.0, 0.5, 0.5, 0.1], [2.0, 2.5, 3.5, 5.0, 7.5])
    jump = _summary([10, 20, 30, 25, 20], [3.0, 1.0, 0.5, 0.3, 0.1], [2.0, 2.5, 3.5, 7.0, 7.5])
    peak_last = _summary([10, 20, 30, 35, 40], [3.0, 1.0, 0.5, 0.3, 0.1], [2.0, 2.5, 3.5, 5.0, 7.5])
    for table, failing in ((flat_end, 1), (high_slack, 2), (jump, 3), (peak_last, 0)):
        passed = validation.a1_checks(table)["passed"].tolist()
        assert passed[failing] is False, (failing, passed)
    seeds = pd.concat([good.assign(seed=0), good.assign(seed=1, completed_per_h=good["completed_per_h"] + 2)])
    mean = validation.throughput_summary(seeds)
    assert mean["completed_per_h"].tolist() == [11, 21, 31, 26, 21]


def test_crn_variance_ratio():
    a = pd.Series([10.0, 12.0, 14.0, 16.0], index=[0, 1, 2, 3])
    b = pd.Series([9.0, 11.5, 13.0, 15.5, 99.0], index=[0, 1, 2, 3, 4])           # seed 4 has no pair
    m = validation.crn_variance_ratio(a, b)
    d = a - b.loc[a.index]
    assert m["n_seeds"] == 4 and m["mean_diff"] == pytest.approx(d.mean())
    assert m["var_crn"] == pytest.approx(d.var(ddof=1))
    assert m["var_indep"] == pytest.approx(a.var(ddof=1) + b.loc[a.index].var(ddof=1))
    assert m["ratio"] == pytest.approx(m["var_crn"] / m["var_indep"])


def test_per_seed_summary_and_calibration_table():
    s = validation.per_seed_summary(pd.DataFrame({"runtime_s": [7.0, 5.0, 6.0]}), "runtime_s")
    assert s == {"median": 6.0, "min": 5.0, "max": 7.0, "n": 3}
    t = validation.calibration_table({"a": 0.2, "b": 5.0}, {"a": (0.1, 0.3), "b": (1.0, 2.0)})
    assert t["in_range"].tolist() == [True, False]


def test_market_by_hour_matches_the_world_description():
    cfg = load_config(ROOT / "config" / "default.yaml")
    m = validation.market_by_hour(cfg)
    assert len(m) == 24 and (m["drivers_on_shift"] >= 0).all()
    d = cfg.demand
    from sim.population import build_world
    from sim.rng import Rng
    w = build_world(cfg, Rng.from_config(cfg)).cell_weight.sum()
    assert m["sessions_expected"].sum() == pytest.approx(d.base_sessions_per_cell_h * d.demand_scale
                                                         * sum(d.hour_profile) * w)


@pytest.fixture(scope="module")
def tiny_gte_full(tmp_path_factory):
    from sim.runner import gte
    cfg = load_config(TINY, ["gte.n_seeds=2", "runner.n_procs=1"])
    out = tmp_path_factory.mktemp("s6") / "gte_full"
    gte(cfg, out, log_level="full")
    return cfg, out


def test_calibration_from_saved_runs_equals_the_slow_test_in_memory(tiny_gte_full):
    from tests.test_acceptance_core import calibration_metrics as in_memory
    cfg, out = tiny_gte_full
    dirs = validation.full_run_dirs(out, "all_off")
    assert len(dirs) == 2
    saved = validation.calibration_metrics(dirs)
    expect = in_memory(cfg, n_seeds=2)
    assert set(saved) == set(expect)
    for k in expect:
        # rel 1e-6: the in-memory version sums the float32 fares in float32, this one in float64
        assert saved[k] == pytest.approx(expect[k], rel=1e-6), k
    assert validation.full_run_dirs(out, "threshold") == []


def test_datasets_table_reports_each_directory(tiny_gte_full):
    _, out = tiny_gte_full
    t = validation.datasets_table([out])
    assert len(t) == 1 and t["problems"].iloc[0] == "" and t["mode"].iloc[0] == "gte"
    assert not math.isnan(t["N_mean"].iloc[0])
