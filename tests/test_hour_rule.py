"""analysis/hour_rule.py: NaN in the harmful hours, u_score elsewhere; loads by spec in a worker."""

import json
import multiprocessing

import numpy as np
import pytest

from analysis import hour_rule
from sim.policies.scores import load_score_fn, score_batch
from tests.fakes import make_batch


@pytest.fixture
def rule_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(hour_rule, "MODEL_DIR", tmp_path)       # rule_path reads MODEL_DIR at call time
    hour_rule._load.cache_clear()
    hour_rule.save_hours("w0", [8, 17, 18, 19], source="test")
    assert hour_rule.rule_path("w0") == tmp_path / "hour_rule_w0.json"
    yield tmp_path
    hour_rule._load.cache_clear()


def test_save_and_load_hours_round_trip(rule_dir):
    assert hour_rule.load_hours("w0", rule_dir) == (8, 17, 18, 19)
    data = json.loads(hour_rule.rule_path("w0", rule_dir).read_text(encoding="utf-8"))
    assert data["harmful_hours"] == [8, 17, 18, 19] and data["source"] == "test"
    with pytest.raises(ValueError):
        hour_rule.save_hours("w0", [24], source="bad", model_dir=rule_dir)


def test_random_variant_is_nan_in_harmful_hours_and_u_score_elsewhere(rule_dir):
    batch = make_batch(24, 7)
    batch.hour[:] = np.arange(24)
    out = score_batch(hour_rule.off_hours_random_w0, batch, np.zeros(24))
    off = np.isin(np.arange(24), [8, 17, 18, 19])
    assert np.isnan(out[off]).all()
    np.testing.assert_array_equal(out[~off], batch.u_score.astype(np.float32)[~off])
    np.testing.assert_array_equal(out, score_batch(hour_rule.off_hours_random_w0, batch, np.full(24, np.inf)))  # ignores s


def test_apply_rule_is_vectorised_and_float32():
    out = hour_rule.apply_rule(np.array([7, 8, 9]), np.array([0.1, 0.2, 0.3]), (8,))
    assert out.dtype == np.float32 and np.isnan(out[1]) and out[0] == np.float32(0.1) and out[2] == np.float32(0.3)


def _in_worker(spec):
    b = make_batch(6, 7)
    b.hour[:] = [7, 8, 9, 17, 20, 23]
    return score_batch(load_score_fn(spec), b, np.zeros(6))


@pytest.mark.skipif(not hour_rule.rule_path("w0").exists(), reason="committed hour_rule_w0.json not present yet")
def test_committed_rule_loads_by_spec_in_a_spawned_worker():
    spec = "analysis.hour_rule:off_hours_random_w0"
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        remote = pool.apply(_in_worker, (spec,))
    np.testing.assert_array_equal(remote, _in_worker(spec))
    hours = hour_rule.load_hours("w0")
    assert all(np.isnan(v) == (h in hours) for h, v in zip([7, 8, 9, 17, 20, 23], remote))
