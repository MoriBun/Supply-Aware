"""analysis/calibrate.py: the CAL numbers it prints are the ones the slow acceptance test computes."""

import dataclasses
import json
import math

import pytest

from analysis import calibrate
from sim.config import load_config
from tests.conftest import ROOT
from tests.test_acceptance_core import calibration_metrics as gate_calibration_metrics

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]


@pytest.fixture(scope="module")
def tiny_round():
    cfg = load_config(TINY)
    results = calibrate.run_all_off(cfg, 2, n_procs=1)
    return cfg, results


def test_cal_metrics_match_the_slow_acceptance_test_number_for_number(tiny_round):
    cfg, results = tiny_round
    ours = calibrate.cal_metrics(cfg, results)
    gate = gate_calibration_metrics(cfg, n_seeds=2)
    assert set(ours) == set(gate) == set(dataclasses.asdict(cfg.calibration_targets))
    for k in gate:
        assert ours[k] == pytest.approx(gate[k], rel=0, abs=0), k


def test_checks_are_finite_and_within_definitions(tiny_round):
    cfg, results = tiny_round
    checks = calibrate.check_metrics(cfg, results)
    for k in ("p_request_all", "mean_pickup_wait_min", "mean_trip_time_min", "cancel_rate", "abandon_rate",
              "reposition_share", "share_tight_outside_5_8h", "completed_per_seed"):
        assert math.isfinite(checks[k]), k
    assert 0.0 <= checks["cancel_rate"] <= 1.0 and 0.0 <= checks["abandon_rate"] <= 1.0
    assert 0.0 <= checks["reposition_share"] <= 1.0
    assert 0.0 <= checks["share_tight_outside_5_8h"] <= 1.0
    assert checks["mean_pickup_wait_min"] > 0 and checks["mean_trip_time_min"] > 0


def test_report_and_row_name_every_target_and_knob(tiny_round):
    cfg, results = tiny_round
    cal, checks = calibrate.cal_metrics(cfg, results), calibrate.check_metrics(cfg, results)
    text = calibrate.format_report(cfg, cal, checks, 2)
    for k in cal:
        assert k in text
    for key in calibrate.KNOBS:
        assert key.split(".")[-1] in text
    row = calibrate.decisions_row(cfg, cal, checks)
    assert row.startswith("| ") and row.count("|") == 1 + (1 + 6 + 9 + 5)   # cells: hash, knobs, targets, checks


def test_cli_writes_json_with_overrides(tmp_path):
    out = tmp_path / "round.json"
    rc = calibrate.main(["--config", str(TINY[0]), "--config", str(TINY[1]), "--seeds", "1", "--n-procs", "1",
                         "--set", "supply.fleet_size=12", "--json", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["knobs"]["supply.fleet_size"] == 12
    assert payload["overrides"] == ["supply.fleet_size=12"]
    assert set(payload["cal"]) == set(payload["targets"])
