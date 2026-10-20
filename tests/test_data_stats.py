"""analysis/data_stats.py: descriptive table and figures of a history data set (guide
``huong_dan_thong_ke_du_lieu.md`` §5).

Two tiny ``generate`` runs (7 cells, 2 days, legacy policy): W0 and W1 (``config/world_w1.yaml``,
shocks on). ``hidden/`` is deleted right after generation, so every test also checks that the
statistics and figures never read it.
"""

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analysis import data_stats as ds
from sim.config import load_config
from tests.conftest import ROOT

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]
W1 = ROOT / "config" / "world_w1.yaml"
DAYS = 2


def _generate(out: Path, layers, overrides) -> Path:
    from sim.runner import generate
    cfg = load_config(layers, ["policy.name=legacy", f"generate.days={DAYS}", "runner.n_procs=1", *overrides])
    generate(cfg, out)
    shutil.rmtree(out / "hidden")          # the statistics must work without the ground truth
    return out


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("data_stats")
    w0 = _generate(base / "w0", TINY, [])
    w1 = _generate(base / "w1", TINY + [W1], ["supply.fleet_size=10"])   # world_w1 sets the Hanoi fleet
    return {"w0": w0, "w1": w1}


@pytest.fixture(scope="module")
def data(runs):
    return {k: ds.load_dataset(v) for k, v in runs.items()}


def test_completed_by_hour_equals_by_day_equals_n(data):
    for d in data.values():
        stats = ds.describe(d)
        n = stats["05a_chuyen_hoan_thanh"]
        assert d.n_window_days == DAYS and d.n_days == pytest.approx(DAYS)
        assert ds.hourly_profile(d)["completed"].sum() * d.n_days == pytest.approx(n)
        daily = ds.daily_completed(d)
        assert len(daily) == DAYS and daily["completed"].sum() == n
        assert n == d.n_completed_results > 0                # N of results/policy_results
        assert stats["05c_chuyen_ngay_min"] <= stats["05b_chuyen_hoan_thanh_moi_ngay"] <= stats["05d_chuyen_ngay_max"]
        ds.check_against_results(d, stats)


def test_rates_are_shares(data):
    for d in data.values():
        stats = ds.describe(d)
        rates = {k: v for k, v in stats.items() if "_ty_le_" in k}
        assert len(rates) >= 12
        for k, v in rates.items():
            assert np.isnan(v) or 0.0 <= v <= 1.0, k
        tight = ds.hourly_profile(d)["tight_share"].dropna()
        assert ((tight >= 0) & (tight <= 1)).all()
        assert stats["06a_ty_le_hoan_thanh"] + stats["06b_ty_le_huy_xe_dang_toi"] + stats["06c_ty_le_bo_cho"] <= 1 + 1e-12
        # (cell, slot) of the window: every slot that starts in the window, for every cell
        assert stats["14a_so_o_slot"] == d.n_cells * DAYS * 86400 / d.slot_s


def test_table_has_the_17_indicators(data):
    s0, s1 = ds.describe(data["w0"]), ds.describe(data["w1"])
    assert tuple(s0) == ds.METRIC_KEYS
    assert {ds.indicator_of(k) for k in s0} == set(ds.INDICATORS) == set(range(1, 18))
    assert {no for no, _, _ in ds.MARKDOWN_ROWS} == set(range(1, 18))
    frame = ds.stats_frame(s0, s1)
    assert list(frame.columns) == ["chi_tieu", "w0", "w1"] and len(frame) == len(ds.METRICS)
    table = ds.markdown_table(s0, s1)
    assert table.count("\n") == len(ds.MARKDOWN_ROWS) + 1
    assert "không bật" in table                       # W0 has no shocks
    assert np.isnan(s0["16a_ngay_mua"]) and s1["16f_cau_ngay_min"] > 0


def test_shocks_give_the_kind_of_each_day_and_the_venues(data):
    w1 = data["w1"]
    kinds = ds.day_types(w1)
    assert len(kinds) == DAYS and set(kinds) <= {"none", "event", "rain", "rain_event"}
    sh = w1.shocks
    assert (sh["day"] < DAYS).all()                     # the cool-down day after the window is dropped
    for day, kind in enumerate(kinds):
        has_rain = ((sh["kind"] == "rain") & (sh["day"] == day)).any()
        has_event = ((sh["kind"] == "event") & (sh["day"] == day)).any()
        assert kind == {(False, False): "none", (False, True): "event", (True, False): "rain",
                        (True, True): "rain_event"}[(has_rain, has_event)]
    venues = ds.event_venues(w1)
    assert all(0 <= c < w1.n_cells and 0 <= k < len(w1.venue_mults) for c, k in venues.items())
    assert ds.day_types(data["w0"]) == ["none"] * DAYS


def test_booking_rates_by_mechanism_use_observed_columns(data):
    t = ds.booking_by_mechanism(data["w0"])
    assert set(t["group"]) <= {"legacy", "explore"} and ((t["booking_rate"] >= 0) & (t["booking_rate"] <= 1)).all()
    s = data["w0"].sessions
    legacy = s["assign_mechanism"].isin(ds.LEGACY_MECHANISMS) | (s["assign_mechanism"] == ds.EXPLORE)
    assert t["sessions"].sum() == int(legacy.sum())


def test_cli_writes_csv_and_figures_without_hidden(runs, tmp_path, capsys):
    pytest.importorskip("matplotlib")
    assert not (runs["w0"] / "hidden").exists() and not (runs["w1"] / "hidden").exists()
    assert ds.main(["--w0", str(runs["w0"]), "--w1", str(runs["w1"]), "--out", str(tmp_path)]) == 0
    csv = pd.read_csv(tmp_path / "07_du_lieu_thong_ke.csv")
    assert list(csv["chi_tieu"]) == list(ds.METRIC_KEYS)
    names = {"tong_quan", "theo_gio", "ban_do", "gia_cuoc", "theo_ngay", "quang_duong_eta", "gay_nhieu"}
    for name in names:
        assert (tmp_path / f"07_du_lieu_{name}.png").stat().st_size > 0
    assert "| # | Chỉ tiêu | W0 | W1 |" in capsys.readouterr().out
