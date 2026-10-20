"""Runner (sim/runner.py) on the fake engine."""

import math

import numpy as np
import pandas as pd
import pytest
import yaml

import sim.runner as runner
from sim.config import config_hash, load_config
from sim.logger import META_COLUMNS, RESULTS_TABLES, cast_table, write_results
from sim.runner import (
    Job, calibrate_budget, evaluate, generate, gte, gte_summary, kappa_auto, kappa_from_pilot, metadata_table,
    pilot_seed, pilot_spend_vnd, policy_results_table, resolve_kappa, resolve_kappas, run_id, run_jobs, run_mode,
    run_throughput_curve, seeds_for, summarize_throughput, sweep_theta, theta_sweep_table, throughput_config,
    throughput_jobs, with_policy, with_seed,
)
from sim.state import Clock
from tests.fakes import FAKE_VOUCHER_VND, make_run_result

FAKE = "tests.fakes:fake_run"
CONGESTED = "tests.fakes:fake_run_congested"
SMALL = ["sweep.n_seeds=2", "gte.n_seeds=2", "sweep.theta_grid=[0.0, 0.5, 1.0]", "runner.n_procs=1"]


@pytest.fixture
def off_cfg(tiny_layers):
    return load_config(tiny_layers, ["policy.name=all_off"])


@pytest.fixture
def th_cfg(tiny_layers):
    return load_config(tiny_layers, SMALL)                 # tiny default policy: threshold, kappa auto


# --- ids, seeds -------------------------------------------------------------------------


def test_run_id_format(tiny_cfg):
    assert run_id("evaluate", tiny_cfg, "threshold", 0.35, 3) == f"evaluate-{config_hash(tiny_cfg)}-threshold-0.35-3"
    assert run_id("gte", tiny_cfg, "all_on", float("nan"), 0).endswith("-all_on-nan-0")


def test_seeds_and_with_seed(tiny_cfg):
    assert seeds_for(tiny_cfg, 3) == [0, 1, 2]
    with_7 = with_seed(tiny_cfg, 7)
    assert with_7.meta.run_seed == 7 and with_7.meta.world_seed == tiny_cfg.meta.world_seed
    assert tiny_cfg.meta.run_seed == 0                                        # original untouched
    assert config_hash(with_7) != config_hash(tiny_cfg)
    assert pilot_seed(tiny_cfg) == 9000 and pilot_seed(with_7) == 9007          # one pilot seed for all thetas
    assert with_policy(tiny_cfg, "legacy").policy.name == "legacy" and tiny_cfg.policy.name == "threshold"
    with pytest.raises(ValueError):
        seeds_for(tiny_cfg, 0)


# --- running jobs ------------------------------------------------------------------------


def test_run_jobs_inline_keeps_order_and_is_deterministic(off_cfg):
    jobs = [Job(cfg=off_cfg, seed=s) for s in (2, 0, 1)]
    res = run_jobs(jobs, engine=FAKE, n_procs=1)
    assert [r.profile["run_seed"] for r in res] == [2.0, 0.0, 1.0]
    assert all(r.policy == "all_off" for r in res)
    again = run_jobs(jobs, engine=FAKE, n_procs=1)
    assert [r.N_completed for r in res] == [r.N_completed for r in again]
    assert len({r.N_completed for r in res}) > 1                              # different seeds, different jitter


def test_run_jobs_spawn_pool_matches_inline(off_cfg):
    # Windows starts workers with spawn; the engine spec is a string so the worker can import it.
    jobs = [Job(cfg=off_cfg, seed=s) for s in range(3)]
    inline = run_jobs(jobs, engine=FAKE, n_procs=1)
    pooled = run_jobs(jobs, engine=FAKE, n_procs=2)
    for a, b in zip(inline, pooled, strict=True):
        assert (a.policy, a.N_completed, a.n_requests) == (b.policy, b.N_completed, b.n_requests)
        np.testing.assert_equal(a.profile, b.profile)                        # NaN-aware dict comparison


def test_run_jobs_edge_cases_and_policy_arguments(off_cfg, tiny_cfg):
    assert run_jobs([], engine=FAKE) == []
    with pytest.raises(ValueError):
        run_jobs([Job(cfg=off_cfg, seed=0)], engine="sim.engine:no_such_function")
    res = run_jobs([Job(cfg=tiny_cfg, seed=0, theta=0.8, kappa=0.25)], engine=FAKE, n_procs=1)[0]
    assert res.policy == "threshold" and res.profile["theta"] == 0.8 and res.profile["kappa"] == 0.25
    default = run_jobs([Job(cfg=tiny_cfg, seed=0)], engine=FAKE, n_procs=1)[0]
    assert default.profile["theta"] == tiny_cfg.policy.threshold.theta and default.profile["kappa"] == -math.inf


def test_run_jobs_passes_budget_arguments(tiny_layers):
    cfg_on = load_config(tiny_layers, ["policy.name=all_on"])
    res = run_jobs([Job(cfg=cfg_on, seed=0, budget_vnd=12.5, enforce_budget=True)], engine=FAKE, n_procs=1)[0]
    assert res.policy == "all_on" and res.budget_B_vnd == 12.5 and res.profile["enforce"] == 1.0
    assert res.voucher_spent_vnd <= 12.5 * 1 + 1e-9                           # tiny window: one budget period
    free = run_jobs([Job(cfg=cfg_on, seed=0, enforce_budget=False)], engine=FAKE, n_procs=1)[0]
    assert np.isnan(free.budget_B_vnd) and free.voucher_spent_vnd >= res.voucher_spent_vnd


# --- throughput_curve ---------------------------------------------------------------------


def test_throughput_config_transforms(default_yaml):
    cfg = load_config(default_yaml)
    h = cfg.throughput.reference_hour
    t = throughput_config(cfg, 2.5)
    assert t.demand.demand_scale == 2.5 and t.policy.name == "all_off" and t.supply.shift_mode == "always_on"
    assert t.demand.hour_profile == (cfg.demand.hour_profile[h],) * 24
    assert t.space.speed_factor_by_hour == (cfg.space.speed_factor_by_hour[h],) * 24
    assert t.demand.hour_profile[0] == 1.85 and t.space.speed_factor_by_hour[0] == 0.76     # hour 18 (TomTom)
    assert cfg.demand.demand_scale == load_config(default_yaml).demand.demand_scale            # original untouched
    assert cfg.policy.name == "threshold" and cfg.supply.shift_mode == "schedule"


def test_throughput_jobs_grid_times_seeds(default_yaml):
    cfg = load_config(default_yaml, ["throughput.n_seeds=2", "throughput.demand_scale_grid=[0.5, 1.0, 2.0]"])
    jobs = throughput_jobs(cfg)
    assert [j.cfg.demand.demand_scale for j in jobs] == [0.5, 0.5, 1.0, 1.0, 2.0, 2.0]
    assert [j.seed for j in jobs] == [0, 1, 0, 1, 0, 1]
    assert all(j.enforce_budget is False and j.budget_vnd is None and math.isnan(j.theta) for j in jobs)


def test_fake_engine_receives_the_throughput_config(default_yaml):
    cfg = load_config(default_yaml, ["throughput.n_seeds=1", "throughput.demand_scale_grid=[1.0]"])
    res = run_jobs(throughput_jobs(cfg), engine=FAKE, n_procs=1)[0]
    expected = {"demand_scale": 1.0, "always_on": 1.0, "hour_profile_const": 1.0, "speed_const": 1.0,
                "enforce": 0.0, "run_seed": 0.0, "offered": 0.0}
    assert {k: res.profile[k] for k in expected} == expected
    assert res.policy == "all_off" and np.isnan(res.budget_B_vnd)


def test_throughput_curve_end_to_end(default_yaml, tmp_path):
    # The fake engine peaks at requests = capacity (demand_scale ~ 3.8 on the Hanoi fleet), so this plumbing test
    # keeps the old 16-level grid regardless of the city grid in default.yaml (0.25..2.5).
    grid = "[0.25,0.5,0.75,1.0,1.25,1.5,1.75,2.0,2.25,2.5,2.75,3.0,3.25,3.5,3.75,4.0]"
    cfg = load_config(default_yaml, ["throughput.n_seeds=2", f"throughput.demand_scale_grid={grid}"])
    table = run_throughput_curve(cfg, tmp_path, engine=FAKE, n_procs=1)
    path = tmp_path / "results" / "throughput_curve.parquet"
    assert path.exists()
    disk = pd.read_parquet(path)
    assert list(disk.columns) == list(RESULTS_TABLES["throughput_curve"])
    assert len(disk) == len(cfg.throughput.demand_scale_grid) * 2 and set(disk["seed"]) == {0, 1}
    assert str(disk["seed"].dtype) == "int32" and str(disk["fleet_size"].dtype) == "int32"
    assert str(disk["run_id"].dtype) == "string" and disk["run_id"].is_unique
    assert disk["run_id"].str.startswith("throughput_curve-").all()
    assert disk["run_id"].str.endswith("-all_off-nan-0").sum() == len(cfg.throughput.demand_scale_grid)   # seed 0 once per level
    assert (disk["fleet_size"] == cfg.supply.fleet_size).all()

    s = summarize_throughput(table)
    assert s["demand_scale"].tolist() == list(cfg.throughput.demand_scale_grid)
    c = s["completed_per_h"].to_numpy()
    peak = int(c.argmax())
    assert 0 < peak < len(c) - 1 and c[-1] <= 0.95 * c[peak]                  # the fake has the A1 hump
    assert (np.diff(s["mean_slack"]) < 0).all() and (np.diff(s["mean_pickup_eta_min"]) > 0).all()
    assert (np.diff(s["requests_per_h"]) > 0).all()


# --- results tables (docs/schema.md) ------------------------------------------------------


def test_policy_results_table(off_cfg):
    jobs = [Job(cfg=off_cfg, seed=s, theta=0.3) for s in range(2)]
    res = run_jobs(jobs, engine=FAKE, n_procs=1)
    df = policy_results_table("evaluate", jobs, res)
    assert list(df.columns) == list(RESULTS_TABLES["policy_results"])
    cast = cast_table(df, RESULTS_TABLES["policy_results"])
    assert cast["run_id"].tolist() == [run_id("evaluate", off_cfg, "all_off", 0.3, s) for s in range(2)]
    assert cast["N_completed"].tolist() == [r.N_completed for r in res] and cast["seed"].tolist() == [0, 1]
    assert str(cast["theta"].dtype) == "float64" and (cast["theta"] == 0.3).all()
    assert str(cast["N_completed"].dtype) == "int64" and str(cast["policy"].dtype) == "string"


def test_theta_sweep_table_aggregates_seeds():
    rows = pd.DataFrame({
        "theta": [0.0, 0.0, 0.5, 0.5, 1.0, 1.0],
        "N_completed": [100, 110, 130, 120, 90, 90],
        "V_profit_vnd": [1.0, 3.0, 2.0, 2.0, 0.0, 0.0],
        "voucher_spent_vnd": [10.0, 10.0, 8.0, 6.0, 4.0, 4.0],
    })
    t = theta_sweep_table(rows)
    assert list(t.columns) == list(RESULTS_TABLES["theta_sweep"]) and t["theta"].tolist() == [0.0, 0.5, 1.0]
    assert t["N_mean"].tolist() == [105.0, 125.0, 90.0] and t["is_argmax"].tolist() == [False, True, False]
    assert t["N_se"][0] == pytest.approx(np.std([100, 110], ddof=1) / math.sqrt(2)) and t["N_se"][2] == 0.0
    assert t["V_mean"].tolist() == [2.0, 2.0, 0.0] and t["spent_mean"].tolist() == [10.0, 7.0, 4.0]
    assert t["n_seeds"].tolist() == [2, 2, 2]
    cast_table(t, RESULTS_TABLES["theta_sweep"])                              # dtypes castable


def test_cast_table_rejects_wrong_columns():
    with pytest.raises(ValueError):
        cast_table(pd.DataFrame({"run_id": ["a"]}), RESULTS_TABLES["policy_results"])
    full = {c: [0] for c in RESULTS_TABLES["throughput_curve"]}
    full["extra"] = [1]
    with pytest.raises(ValueError):
        cast_table(pd.DataFrame(full), RESULTS_TABLES["throughput_curve"])


def test_write_results_roundtrip(off_cfg, tmp_path):
    jobs = [Job(cfg=off_cfg, seed=4)]
    df = policy_results_table("gte", jobs, [make_run_result(policy="all_on", N_completed=5, V_profit_vnd=1.5)])
    path = write_results(tmp_path, "policy_results", df)
    assert path == tmp_path / "results" / "policy_results.parquet"
    back = pd.read_parquet(path)
    assert back["N_completed"].tolist() == [5] and back["policy"].tolist() == ["all_on"] and back["seed"].tolist() == [4]
    assert np.isnan(back["budget_B_vnd"][0]) and back["V_profit_vnd"][0] == 1.5


def test_metadata_table_fields(off_cfg):
    jobs = [Job(cfg=off_cfg, seed=3, theta=0.4, kappa=0.1, budget_vnd=7.0)]
    res = run_jobs(jobs, engine=FAKE, n_procs=1)
    meta = metadata_table("evaluate", jobs, res)
    assert list(meta.columns) == list(META_COLUMNS) and len(meta) == 1
    row = meta.iloc[0]
    assert row["run_id"] == run_id("evaluate", off_cfg, "all_off", 0.4, 3) and row["mode"] == "evaluate"
    assert row["config_hash"] == config_hash(off_cfg) and row["seed"] == 3 and row["world_seed"] == off_cfg.meta.world_seed
    assert yaml.safe_load(row["config_yaml"])["meta"]["run_seed"] == 3           # the config the run actually used
    assert row["kappa"] == 0.1 and row["theta"] == 0.4 and row["budget_B_vnd"] == 7.0
    assert row["kappa_pilots"] == 0                                               # kappa given, not kappa-auto
    assert len(row["git_sha"]) == 40 or row["git_sha"] == "unknown"
    assert row["created_at"].endswith("+00:00") and row["window_end_s"] == res[0].window_end_s
    assert metadata_table("x", jobs, res, budget_B_vnd=1.5)["budget_B_vnd"][0] == 1.5


# --- budget B and kappa ----------------------------------------------------------------------


def test_kappa_from_pilot():
    score = [0.9, 0.8, 0.7, 0.6]
    voucher = [2.0, 2.0, 2.0, 2.0]
    completed = [True, False, True, True]                                     # spend by score: 2, 0, 2, 2
    assert kappa_from_pilot(score, voucher, completed, 3.0) == 0.8            # cum 2, 2, 4: the top two fit
    assert kappa_from_pilot(score, voucher, completed, 4.0) == 0.7
    assert kappa_from_pilot(score, voucher, completed, 1.0) == math.inf       # not even the best fits
    assert kappa_from_pilot(score, voucher, completed, 6.0) == -math.inf      # everyone fits
    assert kappa_from_pilot([math.nan, 0.5], [1.0, 1.0], [True, True], 1.0) == -math.inf   # NaN scores ignored
    assert kappa_from_pilot([], [], [], 5.0) == -math.inf
    unordered = kappa_from_pilot([0.2, 0.9, 0.5], [1.0, 1.0, 1.0], [True, True, True], 2.0)
    assert unordered == 0.5


def test_kappa_from_pilot_keeps_tied_scores_together():
    # The policy offers on score >= kappa, so equal scores (one rider's sessions) are kept or dropped together.
    tied = ([0.9, 0.8, 0.8, 0.7], [1.0] * 4, [True] * 4)
    assert kappa_from_pilot(*tied, 2.0) == 0.9            # top 0.8 alone would fit, but both 0.8 sessions are offered
    assert kappa_from_pilot(*tied, 3.0) == 0.8
    assert kappa_from_pilot(*tied, 3.5) == 0.8
    assert kappa_from_pilot(*tied, 4.0) == -math.inf
    assert kappa_from_pilot([0.5, 0.5], [1.0, 1.0], [True, True], 1.0) == math.inf
    assert kappa_from_pilot([0.5, 0.5, 0.4], [1.0, 1.0, 1.0], [True, False, True], 1.0) == 0.5   # spend 1 + 0


def test_calibrate_budget_fixed_and_pilot(tiny_layers):
    fixed = load_config(tiny_layers, ["budget.mode=fixed", "budget.fixed_vnd=42.0"])
    assert calibrate_budget(fixed, engine=FAKE) == (42.0, None)
    cfg = load_config(tiny_layers, ["policy.name=all_off"])
    budget, pilot = calibrate_budget(cfg, engine=FAKE)
    assert pilot.policy == "all_on" and pilot.profile["run_seed"] == 9000.0 and pilot.profile["enforce"] == 0.0
    n_periods = Clock.from_config(cfg).n_periods
    assert budget == pytest.approx(cfg.budget.fraction * pilot.voucher_spent_vnd / n_periods) and budget > 0


def test_resolve_kappa(tiny_layers, th_cfg):
    assert resolve_kappa(load_config(tiny_layers, ["policy.name=all_on"]), float("nan"), 10.0, engine=FAKE) is None
    fixed = load_config(tiny_layers, ["policy.threshold.kappa=0.3"])
    assert resolve_kappa(fixed, 0.35, 10.0, engine=FAKE) == 0.3
    assert resolve_kappas(fixed, [0.0, 1.0], 10.0, engine=FAKE)[1].n_pilots == 0   # no pilot for a fixed kappa
    assert resolve_kappa(th_cfg, 0.35, None, engine=FAKE) == -math.inf     # no budget: nothing to ration
    budget = 20.0
    kappa = resolve_kappa(th_cfg, 0.35, budget, engine=FAKE)
    assert 0.0 < kappa < 1.0
    # Fixed point: the pilot rerun at the kappa found (no budget, the shared pilot seed) fits the budget.
    pilot = run_jobs([Job(cfg=th_cfg, seed=pilot_seed(th_cfg), theta=0.35, kappa=kappa, enforce_budget=False)],
                     engine=FAKE, n_procs=1)[0]
    n_periods = Clock.from_config(th_cfg).n_periods
    assert pilot_spend_vnd(pilot) <= budget * n_periods < pilot_spend_vnd(pilot) + 2 * FAKE_VOUCHER_VND


def test_kappa_auto_iterates_to_a_fixed_point_under_congestion(tiny_layers, th_cfg):
    def with_max_iter(n):
        return load_config(tiny_layers, SMALL + [f"policy.threshold.kappa_max_iter={n}"])

    # A pilot at kappa = -inf under-predicts the spend at a higher kappa (fewer offers, less
    # congestion, more completed trips), so kappa is raised until the pilot rerun at kappa fits B x n_periods.
    budget, thetas = 20.0, [0.0, 0.35, 1.0]
    for ka in kappa_auto(th_cfg, thetas, budget, engine=CONGESTED):
        assert ka.n_pilots >= 3 and ka.n_pilots == len(ka.pilot_kappas) == len(ka.spend_ratios)
        assert ka.pilot_kappas[0] == -math.inf and np.all(np.diff(ka.pilot_kappas) > 0)     # kappa only rises
        assert ka.kappa == ka.pilot_kappas[-1] and ka.spend_ratios[-1] <= 1.0                # last pilot fits
        assert all(r > 1.0 for r in ka.spend_ratios[:-1])                                   # every earlier one overspent
    # kappa_max_iter = 0 is the original single pilot; a rerun at its kappa overspends.
    single = with_max_iter(0)
    ka0 = kappa_auto(single, [0.35], budget, engine=CONGESTED)[0]
    first = run_jobs([Job(cfg=single, seed=pilot_seed(single), theta=0.35, kappa=-math.inf, enforce_budget=False)],
                     engine=CONGESTED, n_procs=1)[0]
    total = budget * Clock.from_config(single).n_periods
    assert ka0.n_pilots == 1
    assert ka0.kappa == kappa_from_pilot(first.offer_score, first.offer_voucher_vnd, first.offer_completed, total)
    rerun = run_jobs([Job(cfg=single, seed=pilot_seed(single), theta=0.35, kappa=ka0.kappa, enforce_budget=False)],
                     engine=CONGESTED, n_procs=1)[0]
    assert pilot_spend_vnd(rerun) > total
    # Out of reruns: the last kappa found is kept, untested.
    ka1 = kappa_auto(with_max_iter(1), [0.35], budget, engine=CONGESTED)[0]
    assert ka1.n_pilots == 2 and ka1.kappa > ka1.pilot_kappas[-1]


def test_kappa_auto_pilots_share_one_seed_and_run_by_round(th_cfg, monkeypatch):
    calls = []

    def spy(jobs, **kwargs):
        calls.append(list(jobs))
        return run_jobs(jobs, **kwargs)

    monkeypatch.setattr(runner, "run_jobs", spy)
    found = kappa_auto(th_cfg, [0.0, 0.5, 1.0], 20.0, engine=CONGESTED)
    assert [j.theta for j in calls[0]] == [0.0, 0.5, 1.0]                      # round 0: every theta at once
    assert all(j.kappa == -math.inf for j in calls[0])
    pilots = [j for round_ in calls for j in round_]
    assert {j.seed for j in pilots} == {pilot_seed(th_cfg)}                     # one pilot seed for every theta
    assert all(j.enforce_budget is False and j.policy_name == "threshold" for j in pilots)
    assert len(pilots) == sum(ka.n_pilots for ka in found)
    assert len(calls) == max(ka.n_pilots for ka in found)                       # one call per round


# --- modes ------------------------------------------------------------------------------


def test_evaluate_mode(th_cfg, tmp_path):
    table = evaluate(th_cfg, tmp_path, engine=FAKE)
    assert len(table) == th_cfg.sweep.n_seeds == 2 and table["policy"].tolist() == ["threshold"] * 2
    assert (table["theta"] == th_cfg.policy.threshold.theta).all() and table["seed"].tolist() == [0, 1]
    budget = table["budget_B_vnd"].iloc[0]
    assert budget > 0 and (table["voucher_spent_vnd"] <= budget * Clock.from_config(th_cfg).n_periods + 1e-9).all()
    disk = pd.read_parquet(tmp_path / "results" / "policy_results.parquet")
    assert list(disk.columns) == list(RESULTS_TABLES["policy_results"]) and len(disk) == 2
    meta = pd.read_parquet(tmp_path / "meta" / "run_metadata.parquet")
    assert list(meta.columns) == list(META_COLUMNS) and len(meta) == 2
    assert np.isfinite(meta["kappa"]).all() and (meta["budget_B_vnd"] == budget).all()
    assert (meta["kappa_pilots"] >= 2).all()                                    # pilot at -inf, then rerun(s)
    assert (meta["policy"] == "threshold").all() and meta["run_id"].is_unique
    from sim.runner import pilot_seed
    assert (meta["budget_pilot_seed"] == pilot_seed(th_cfg)).all() and (meta["budget_pilot_spent_vnd"] > 0).all()
    assert (meta["hidden_view"] == "none").all() and (disk["world_seed"] == th_cfg.meta.world_seed).all()


def test_evaluate_without_budget_enforcement(tiny_layers, tmp_path):
    cfg = load_config(tiny_layers, SMALL + ["budget.enforce=false", "policy.name=all_on"])
    table = evaluate(cfg, tmp_path, engine=FAKE)
    assert np.isnan(table["budget_B_vnd"]).all() and (table["policy"] == "all_on").all()
    meta = pd.read_parquet(tmp_path / "meta" / "run_metadata.parquet")
    assert (meta["budget_pilot_seed"] == -1).all() and np.isnan(meta["budget_pilot_spent_vnd"]).all()        # no pilot


def test_gte_mode_and_summary(tiny_layers, tmp_path):
    cfg = load_config(tiny_layers, SMALL)
    table = gte(cfg, tmp_path, engine=FAKE)
    assert len(table) == 4 and table["policy"].tolist() == ["all_on", "all_on", "all_off", "all_off"]
    assert table["seed"].tolist() == [0, 1, 0, 1] and np.isnan(table["budget_B_vnd"]).all()
    s = gte_summary(table)
    on = table[table.policy == "all_on"]["N_completed"].to_numpy(dtype=float)
    off = table[table.policy == "all_off"]["N_completed"].to_numpy(dtype=float)
    assert s["N_on"] == on.mean() and s["N_off"] == off.mean() and s["GTE"] == pytest.approx((on - off).mean())
    assert s["n_seeds"] == 2 and np.isfinite(s["GTE_se"])
    assert (tmp_path / "meta" / "run_metadata.parquet").exists()


def test_sweep_theta_mode(th_cfg, tmp_path):
    sweep = sweep_theta(th_cfg, tmp_path, engine=FAKE)
    assert list(sweep.columns) == list(RESULTS_TABLES["theta_sweep"])
    assert sweep["theta"].tolist() == [0.0, 0.5, 1.0] and sweep["n_seeds"].tolist() == [2, 2, 2]
    assert sweep["is_argmax"].sum() == 1 and sweep["is_argmax"][int(sweep["N_mean"].argmax())]
    assert np.isfinite(sweep["N_se"]).all()
    runs = pd.read_parquet(tmp_path / "results" / "policy_results.parquet")
    assert len(runs) == 6 and sorted(set(runs["theta"])) == [0.0, 0.5, 1.0] and (runs["policy"] == "threshold").all()
    budget = runs["budget_B_vnd"].iloc[0]
    assert (runs["budget_B_vnd"] == budget).all() and budget > 0                 # same B for every theta
    meta = pd.read_parquet(tmp_path / "meta" / "run_metadata.parquet")
    kappa_by_theta = meta.groupby("theta")["kappa"].nunique()
    assert (kappa_by_theta == 1).all()                                            # one kappa per theta, shared by seeds
    assert (meta["kappa_pilots"] >= 1).all() and (meta.groupby("theta")["kappa_pilots"].nunique() == 1).all()
    assert (tmp_path / "results" / "theta_sweep.parquet").exists()
    np.testing.assert_allclose(sweep["N_mean"], runs.groupby("theta")["N_completed"].mean().to_numpy())


def test_generate_mode(tiny_layers, tmp_path):
    legacy = load_config(tiny_layers, ["policy.name=legacy", "generate.days=2", "runner.n_procs=1"])
    table, result = generate(legacy, tmp_path, engine=FAKE)
    assert len(table) == 1 and table["policy"][0] == "legacy" and result.policy == "legacy"
    assert result.window_end_s == 3600.0 + 2 * 86400.0                           # generate.days, not window_min
    assert table["budget_B_vnd"][0] > 0                                           # legacy runs under B
    assert len(pd.read_parquet(tmp_path / "meta" / "run_metadata.parquet")) == 1
    exp = load_config(tiny_layers, ["policy.name=experiment", "generate.days=1", "runner.n_procs=1"])
    table, result = generate(exp, tmp_path / "exp", engine=FAKE)
    assert np.isnan(table["budget_B_vnd"][0]) and result.profile["enforce"] == 0.0   # experiment.budget_enforce false
    with pytest.raises(ValueError):
        generate(load_config(tiny_layers, ["policy.name=all_on"]), tmp_path, engine=FAKE)


def test_run_mode_dispatch(th_cfg, tiny_layers, tmp_path):
    for mode in ("evaluate", "gte", "sweep_theta", "calibrate_budget"):
        table = run_mode(mode, th_cfg, tmp_path / mode, engine=FAKE)
        assert isinstance(table, pd.DataFrame) and len(table) >= 1, mode
    cal = run_mode("calibrate_budget", th_cfg, tmp_path / "cal", engine=FAKE)
    assert cal["budget_B_vnd"][0] > 0 and cal["pilot_seed"][0] == 9000
    meta = pd.read_parquet(tmp_path / "cal" / "meta" / "run_metadata.parquet")
    assert meta["budget_B_vnd"][0] == cal["budget_B_vnd"][0] and meta["policy"][0] == "all_on"
    gen = run_mode("generate", load_config(tiny_layers, ["policy.name=legacy", "generate.days=1", "runner.n_procs=1"]),
                   tmp_path / "gen", engine=FAKE)
    assert len(gen) == 1
    with pytest.raises(ValueError):
        run_mode("bogus", th_cfg, tmp_path, engine=FAKE)
    assert (tmp_path / "evaluate" / "results" / "policy_results.parquet").exists()
    assert (tmp_path / "sweep_theta" / "results" / "theta_sweep.parquet").exists()


def test_full_log_run_dirs_are_self_contained(tmp_path):
    """Every per-seed directory of a multi-seed full-log run carries its own one-row results and metadata,
    matching the table-level files, so animation and check_dataset can read one run at a time."""
    import pandas as pd

    from analysis.check_dataset import check_run_dir
    from sim.config import load_config
    from sim.runner import evaluate
    from tests.conftest import ROOT as REPO

    cfg = load_config([REPO / "config" / "default.yaml", REPO / "tests" / "fixtures" / "tiny.yaml"],
                      ["sweep.n_seeds=2", "policy.name=all_on", "budget.enforce=false"])
    table = evaluate(cfg, tmp_path, log_level="full")
    run_dirs = sorted((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 2 == len(table)
    for d in run_dirs:
        res = pd.read_parquet(d / "results" / "policy_results.parquet")
        meta = pd.read_parquet(d / "meta" / "run_metadata.parquet")
        assert len(res) == len(meta) == 1 and res["run_id"].iloc[0] == meta["run_id"].iloc[0] == d.name
        assert res["N_completed"].iloc[0] == table.set_index("run_id").loc[d.name, "N_completed"]
        problems, summary = check_run_dir(d)
        assert problems == [] and summary["n_runs"] == 1
