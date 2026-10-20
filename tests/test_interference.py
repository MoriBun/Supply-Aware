"""analysis/interference.py: the waterfall of three error sources and the burn-in split."""

import dataclasses

import numpy as np
import pytest

from analysis import interference
from sim.config import load_config
from sim.runner import generate, gte, with_policy
from tests.conftest import ROOT

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]
FAST = ["demand.demand_scale=4", "supply.shift_mode=always_on", "runner.n_procs=1", "gte.n_seeds=3"]


def _one_day(cfg):
    return dataclasses.replace(cfg, generate=dataclasses.replace(cfg.generate, days=1))


@pytest.fixture(scope="module")
def s9_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("s9")
    base = load_config(TINY, FAST)
    generate(_one_day(with_policy(base, "legacy")), root / "legacy")
    ab = load_config(TINY, FAST + ["experiment.design=rider_ab"])
    generate(_one_day(with_policy(ab, "experiment")), root / "rider_ab")
    sb = load_config(TINY, FAST + ["experiment.cluster_level=all"])
    generate(_one_day(with_policy(sb, "experiment")), root / "switchback_all")
    gte(base, root / "gte")
    gte(load_config(TINY, FAST + ["supply.fleet_size=60"]), root / "gte_big")
    return root


def test_waterfall_levels_steps_and_sum_check(s9_runs):
    t = interference.waterfall(s9_runs / "legacy", s9_runs / "rider_ab", s9_runs / "gte", s9_runs / "gte_big", n_boot=30)
    assert list(t["kind"]) == ["level"] * 4 + ["step"] * 3
    assert np.isfinite(t["value"]).all() and (t["ci_lo"] <= t["ci_hi"]).all()
    sc = t.attrs["sum_check"]
    assert sc["steps_sum"] == pytest.approx(sc["raw_minus_big"], abs=1e-9)
    assert sc["conf_plus_comp"] == pytest.approx(sc["raw_minus_gte"], abs=1e-9)      # naive error = confounding + competition
    levels = t.loc[t["kind"] == "level", "value"].to_numpy()
    steps = t.loc[t["kind"] == "step", "value"].to_numpy()
    np.testing.assert_allclose(steps, levels[:-1] - levels[1:], atol=1e-9)
    text = interference.waterfall_markdown(t)
    assert "khớp" in text and "KHÔNG" not in text


def test_naive_legacy_effect_uses_only_non_explore_sessions(s9_runs):
    r = interference.naive_legacy_effect(s9_runs / "legacy", n_boot=20)
    assert r["n_sessions"] > 0 and np.isfinite(r["per_day"]) and r["ci_lo"] <= r["ci_hi"]
    assert len(r["draws"]) == 20 and r["sessions_per_day"] > r["n_sessions"]        # explore slice excluded
    with pytest.raises(ValueError):
        interference.naive_legacy_effect(s9_runs / "rider_ab", n_boot=5)


def test_burnin_decomposition_adds_up(s9_runs):
    t = interference.burnin_decomposition(s9_runs / "switchback_all", s9_runs / "gte", n_boot=30)
    v = dict(zip(t["name"], t["value"]))
    full, trimmed, g = (v[k] for k in ("switchback toàn thành phố, mọi session", "switchback, bỏ burn-in", "GTE"))
    assert v["burn-in bias (mọi session − bỏ burn-in)"] == pytest.approx(full - trimmed)
    assert v["carry-over (bỏ burn-in − GTE)"] == pytest.approx(trimmed - g)
    assert t.attrs["n_sessions_trimmed"] < t.attrs["n_sessions_full"]
    assert "burn-in" in interference.burnin_markdown(t)


def test_cli_waterfall_and_burnin_write_results(s9_runs, tmp_path):
    out = tmp_path / "out"
    rc = interference.main(["waterfall", "--legacy", str(s9_runs / "legacy"), "--rider-ab", str(s9_runs / "rider_ab"),
                            "--gte", str(s9_runs / "gte"), "--gte-big", str(s9_runs / "gte_big"), "--n-boot", "10",
                            "--out", str(out)])
    assert rc == 0 and (out / "results" / "waterfall_completed.parquet").exists()
    assert (out / "results" / "waterfall_completed_attrs.json").exists()
    rc = interference.main(["burnin", "--switchback-all", str(s9_runs / "switchback_all"), "--gte", str(s9_runs / "gte"),
                            "--n-boot", "10", "--out", str(out)])
    assert rc == 0 and (out / "results" / "burnin_completed.parquet").exists()
