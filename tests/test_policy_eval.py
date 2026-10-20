"""Policy evaluation: policy table under B, Qini against N(pi), interference."""

import math

import numpy as np
import pandas as pd
import pytest

from analysis.policy_table import DEFAULT_SPECS, PolicySpec, configure, markdown, parse_spec, run_policy_table
from sim.config import load_config
from tests.conftest import ROOT

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]
FAKE = "tests.fakes:fake_run"


# --- policy table under one budget -------------------------------------------------------------


def test_parse_spec_and_configure():
    spec = parse_spec("label=tau_x,policy=threshold,theta=0,score_fn=analysis.scores:tau_x_baseline")
    assert spec == PolicySpec("tau_x", "threshold", 0.0, "analysis.scores:tau_x_baseline", True)
    off = parse_spec("label=off,policy=all_off,budget=false")
    assert off.budget is False and math.isnan(off.theta) and off.score_fn is None
    for bad in ("label=x", "policy=all_on", "label=x,policy=surge", "label=x,policy=all_on,colour=red",
                "label=x,policy=all_on,budget=maybe", "label=x,policy=all_on,theta"):
        with pytest.raises(ValueError):
            parse_spec(bad)
    cfg = load_config(TINY)
    th = configure(cfg, spec)
    assert th.policy.name == "threshold" and th.policy.threshold.score_fn == "analysis.scores:tau_x_baseline"
    assert configure(cfg, off).policy.name == "all_off" and cfg.policy.threshold.score_fn == "heuristic_low_freq"
    ring = parse_spec("label=r,policy=threshold,theta=0.25,scope=ring1")
    assert ring.scope == "ring1" and configure(cfg, ring).policy.threshold.scope == "ring1"
    assert configure(cfg, spec).policy.threshold.scope == cfg.policy.threshold.scope == "cell"
    city = parse_spec("label=c,policy=threshold,theta=0.25,scope=city,score_fn=oracle")   # city tier, oracle
    assert configure(cfg, city).policy.threshold.scope == "city"
    assert configure(cfg, city).policy.threshold.score_fn == "oracle"
    with pytest.raises(ValueError):
        configure(cfg, PolicySpec("bad", "threshold", 0.0, scope="ring2"))


def test_policy_table_runs_every_policy_on_the_same_seeds_and_budget(tmp_path):
    cfg = load_config(TINY, ["runner.n_procs=1"])
    specs = [PolicySpec("all_off", "all_off", budget=False), PolicySpec("all_on_B", "all_on"),
             PolicySpec("random", "threshold", 0.0, "random"), PolicySpec("pi", "threshold", 0.5, "heuristic_low_freq")]
    summary = run_policy_table(cfg, specs, tmp_path, n_seeds=3, engine=FAKE)
    assert summary["label"].tolist() == ["all_off", "all_on_B", "random", "pi"]
    assert (summary["n_seeds"] == 3).all()
    budgets = summary.set_index("label")["budget_B_vnd"]
    assert math.isnan(budgets["all_off"]) and budgets["all_on_B"] > 0
    assert budgets["random"] == budgets["pi"] == budgets["all_on_B"]                 # one B for every policy
    th = summary[summary["policy"] == "threshold"]
    assert np.isfinite(th["kappa"]).all() and (th["kappa_pilots"] >= 1).all()
    ref = summary.set_index("label").loc["all_on_B"]
    assert ref["dN_vs_ref"] == 0.0 and ref["dV_vs_ref"] == 0.0
    runs = pd.read_parquet(tmp_path / "results" / "policy_results.parquet")
    labels = pd.read_parquet(tmp_path / "results" / "policy_labels.parquet")
    assert len(runs) == len(labels) == 12 and labels["run_id"].is_unique
    merged = runs.merge(labels, on="run_id")
    by_label = merged.groupby("label")["seed"].apply(sorted)
    assert all(s == [0, 1, 2] for s in by_label)                                      # common seeds (CRN)
    pi = merged[merged["label"] == "random"].set_index("seed")["N_completed"]
    on = merged[merged["label"] == "all_on_B"].set_index("seed")["N_completed"]
    assert summary.set_index("label").loc["random", "dN_vs_ref"] == pytest.approx((pi - on).mean())
    assert (tmp_path / "meta" / "run_metadata.parquet").exists()
    assert (tmp_path / "results" / "policy_table.parquet").exists()
    text = markdown(summary)
    assert text.count("\n") == 1 + len(specs) and "`pi`" in text and "0 (mốc)" in text


def test_policy_table_rejects_duplicate_labels_or_runs_and_a_missing_reference(tmp_path):
    cfg = load_config(TINY, ["runner.n_procs=1"])
    with pytest.raises(ValueError):
        run_policy_table(cfg, [PolicySpec("a", "all_on"), PolicySpec("a", "all_off")], tmp_path, engine=FAKE,
                         reference="a")
    with pytest.raises(ValueError):
        run_policy_table(cfg, [PolicySpec("a", "all_on")], tmp_path, engine=FAKE, reference="b")
    with pytest.raises(ValueError):
        run_policy_table(cfg, [PolicySpec("a", "all_on"), PolicySpec("b", "all_on")], tmp_path, n_seeds=1,
                         engine=FAKE, reference="a")


def test_break_ties_keeps_the_order_between_strata_and_randomizes_inside():
    from analysis import score_ties, scores
    from tests.fakes import make_batch
    batch = make_batch(400, 7, x_freq=np.linspace(0.1, 8.0, 400))
    batch.x_segment[:] = np.arange(400) % 3
    s_hat = np.full(400, np.nan)
    for name in ("tau_x_baseline", "tau_per_dollar_baseline"):
        raw = getattr(scores, name)(batch, s_hat).astype(np.float64)
        tied = getattr(score_ties, name)(batch, s_hat)
        assert len(np.unique(raw)) < 10 and len(np.unique(tied)) == 400            # ties gone
        hi, lo = raw[:, None] > raw[None, :], tied[:, None] > tied[None, :]
        assert (lo[hi]).all()                                                         # strata order kept
        same = raw[:, None] == raw[None, :]
        u_order = batch.u_score[:, None] > batch.u_score[None, :]
        assert (lo[same] == u_order[same]).all()                                      # inside a stratum: by u_score


def test_paired_steps_are_paired_by_seed():
    from analysis.policy_table import paired_steps
    runs = pd.DataFrame({"seed": [0, 1, 2] * 3, "label": ["a"] * 3 + ["b"] * 3 + ["c"] * 3,
                         "N_completed": [10.0, 20.0, 30.0, 12.0, 21.0, 33.0, 12.0, 25.0, 30.0]})
    steps = paired_steps(runs, [("a", "b"), ("b", "c")]).set_index("to")
    assert steps.loc["b", "diff"] == pytest.approx(2.0) and steps.loc["b", "diff_se"] == pytest.approx(1 / math.sqrt(3))
    assert steps.loc["b", "from_mean"] == pytest.approx(20.0) and steps.loc["b", "n_seeds"] == 3
    assert steps.loc["c", "diff"] == pytest.approx(1 / 3)


def test_default_specs_are_valid():
    from analysis.policy_table import S9_THRESHOLDS, specs_for_world

    cfg = load_config(TINY)
    for world in sorted(S9_THRESHOLDS):
        specs = specs_for_world(world)
        labels = [s.label for s in specs]
        assert len(set(labels)) == len(labels) and "all_on_B" in labels and "oracle" in labels
        for spec in specs:
            configure(cfg, spec)
        tiers = {s.label: s for s in specs}
        assert tiers["pi_hourB_city_dr_kvnd"].scope == "city" and tiers["pi_tension_ring1_dr_kvnd"].scope == "ring1"
        assert tiers["hour_off_random"].score_fn.endswith(f"off_hours_random_{world}")
        assert all("_w1" in s.score_fn for s in specs if s.score_fn and s.score_fn.startswith("analysis.uplift")) == (world == "w1")
    assert tuple(DEFAULT_SPECS) == specs_for_world("w0")
    with pytest.raises(ValueError):
        specs_for_world("w9")


# --- shared tiny data sets (one simulated day each, real engine) ----------------------------------


@pytest.fixture(scope="module")
def tiny_runs(tmp_path_factory):
    from sim.runner import generate, gte
    root = tmp_path_factory.mktemp("s5")
    base = ["generate.days=1", "runner.n_procs=1", "gte.n_seeds=2"]
    dirs = {}
    for name, extra in (("legacy", ["policy.name=legacy"]),
                        ("rider_ab", ["policy.name=experiment", "experiment.design=rider_ab"]),
                        ("switchback", ["policy.name=experiment", "experiment.cluster_level=1"])):
        generate(load_config(TINY, base + extra), root / name)
        dirs[name] = root / name
    gte(load_config(TINY, base), root / "gte")
    dirs["gte"] = root / "gte"
    return dirs


# --- Qini against N(pi) ------------------------------------------------------------------------


def test_weighted_qini_matches_metrics_and_duplication():
    from analysis.metrics import qini_auuc
    from analysis.qini_vs_value import RankedSessions
    gen = np.random.default_rng(3)
    n = 400
    y, t = gen.random(n) < 0.3, gen.random(n) < 0.5
    score = np.round(gen.random(n), 1)                                  # ties grouped
    ranked = RankedSessions(y, t, score)
    assert ranked.qini_coef() == pytest.approx(qini_auuc(y, t, score)["qini_coef"])
    w = gen.integers(0, 3, n)
    dup = np.repeat(np.arange(n), w)
    assert ranked.qini_coef(w) == pytest.approx(qini_auuc(y[dup], t[dup], score[dup])["qini_coef"])
    assert math.isnan(RankedSessions(y, np.ones(n, bool), score).qini_coef())


def test_unit_bootstrap_weights_follow_units():
    from analysis.qini_vs_value import UnitBootstrap
    units = np.array([5, 5, 9, 2, 9, 9])
    boot = UnitBootstrap(units, 50, seed=1)
    assert len(boot) == 50 and boot.n_units == 3
    for b in range(50):
        w = boot[b]
        assert w[0] == w[1] and w[2] == w[4] == w[5]                       # sessions of one unit move together
        assert boot.counts[b].sum() == 3
    again = UnitBootstrap(units, 50, seed=1)
    assert np.array_equal(again.counts, boot.counts)


def test_scoring_logged_sessions_uses_observed_columns_only(tiny_runs):
    from analysis.io import load_run
    from analysis.qini_vs_value import randomized_sessions, score_sessions, session_batch, slack_hat
    data = randomized_sessions(tiny_runs["rider_ab"])
    s, riders = data["sessions"], data["riders"]
    assert np.array_equal(data["unit"], s["rider_id"].to_numpy())             # rider-level randomization
    heuristic = score_sessions(s, riders, data["snapshots"], "heuristic_low_freq")
    x_freq = riders.set_index("rider_id").loc[s["rider_id"], "x_freq"].to_numpy(np.float32)
    np.testing.assert_array_equal(heuristic, -x_freq)
    rnd = score_sessions(s, riders, data["snapshots"], "random", seed=4)
    assert ((rnd >= 0) & (rnd < 1)).all()
    np.testing.assert_array_equal(rnd, score_sessions(s, riders, data["snapshots"], "random", seed=4))
    assert len(score_sessions(s, riders, data["snapshots"], "analysis.scores:tau_x_baseline")) == len(s)
    batch = session_batch(s, riders)
    assert np.isnan(batch.u_target).all() and np.isnan(batch.u_explore).all()
    snaps = load_run(tiny_runs["rider_ab"])["slot_snapshots"]
    row = s.iloc[len(s) // 2]
    expect = snaps[(snaps["slot"] == row["slot"]) & (snaps["cell"] == row["pu_cell"])]["slack_lag_slot"].iloc[0]
    got = slack_hat(s, snaps)[len(s) // 2]
    assert (got == expect) or (np.isnan(got) and np.isnan(expect))
    sb = randomized_sessions(tiny_runs["switchback"])
    assert len(np.unique(sb["unit"])) < len(sb["unit"])                       # (cluster, block) units
    explore = randomized_sessions(tiny_runs["legacy"], explore_only=True)
    assert (explore["sessions"]["assign_mechanism"] == "explore").all()
    with pytest.raises(ValueError):
        randomized_sessions(tiny_runs["legacy"])                               # observational data


def test_offer_efficiency_by_hand():
    from analysis.qini_vs_value import offer_efficiency
    # 8 sessions over 2 days; score >= kappa offers the first four; slack known for all but one.
    sessions = pd.DataFrame({"day": [0, 0, 0, 0, 1, 1, 1, 1], "slot": [1, 1, 2, 2, 97, 97, 98, 98],
                             "pu_cell": [0, 1, 0, 1, 0, 1, 0, 1],
                             "voucher_value_vnd": [4.0, 6.0, 5.0, 5.0, 9.0, 9.0, 9.0, 9.0]})
    snaps = pd.DataFrame({"slot": [1, 1, 2, 97, 97, 98, 98], "cell": [0, 1, 0, 0, 1, 0, 1],
                          "slack_lag_slot": [0.0, 2.0, 0.0, 1.0, 1.0, 1.0, 1.0]})
    data = {"sessions": sessions, "snapshots": snaps,
            "y": np.array([1, 1, 0, 1, 1, 0, 0, 0], float), "t": np.array([1, 1, 0, 0, 1, 1, 0, 0], bool)}
    score = np.array([4, 3, 2, 1, 0, 0, 0, 0], float)
    eff = offer_efficiency(data, {"a": score, "none": score}, {"a": 1.0, "none": math.inf}).set_index("label")
    a = eff.loc["a"]
    assert a["share_offered"] == 0.5 and a["q1"] == 1.0 and a["q0"] == 0.5          # treated 0, 1; control 2, 3
    assert a["voucher_per_offer_vnd"] == 5.0
    assert a["voucher_cost_per_day"] == pytest.approx(5.0 * 2)                       # 4 offers / 2 days, 5 VND each
    assert a["extra_trips_per_day"] == pytest.approx(0.5 * 2)
    assert a["extra_trips_per_100k_vnd"] == pytest.approx(100_000 * 1.0 / 10.0)        # 1 extra trip per 10 đ
    assert a["share_offered_slack0"] == pytest.approx(2 / 3)                         # cell 1 of slot 2 unknown
    assert eff.loc["none", "share_offered"] == 0 and math.isnan(eff.loc["none", "extra_trips_per_100k_vnd"])


def test_compare_pairs_flags_higher_qini_with_lower_value():
    from analysis.qini_vs_value import compare_pairs
    qini = pd.DataFrame({"label": ["a", "b"], "qini_coef": [10.0, 5.0]})
    draws = {"a": np.linspace(9.0, 11.0, 100), "b": np.linspace(4.0, 6.0, 100)}
    runs = pd.DataFrame({"label": ["a"] * 5 + ["b"] * 5, "seed": list(range(5)) * 2,
                         "N_completed": [100, 101, 99, 100, 102, 110, 112, 109, 111, 113]})
    pairs = compare_pairs(qini, draws, runs).set_index(["a", "b"])
    assert pairs.loc[("a", "b"), "qini_higher_n_lower"]
    assert pairs.loc[("a", "b"), "dN"] == pytest.approx(-10.6)
    assert not pairs.loc[("b", "a"), "qini_higher_n_lower"]


# --- designs against the GTE, hidden confounding -------------------------------------------------


def test_design_effect_scales_to_sessions_per_day_and_drops_burnin(tiny_runs):
    from analysis.interference import design_effect, design_table, gte_truth
    from analysis.io import load_run
    from sim.runner import gte_summary
    ab = design_effect(tiny_runs["rider_ab"], n_boot=50)
    sb = design_effect(tiny_runs["switchback"], n_boot=50)
    for r in (ab, sb):
        assert r["per_day"] == pytest.approx(r["effect_per_session"] * r["sessions_per_day"])
        assert r["ci_lo"] <= r["per_day"] <= r["ci_hi"]
    sessions = load_run(tiny_runs["switchback"])["sessions"]
    window = sessions[sessions["in_window"]]
    assert sb["n_sessions"] == int((~window["in_burnin"]).sum()) < len(window)
    assert sb["sessions_per_day"] == pytest.approx(len(window))                 # one-day window
    assert ab["design"] == "rider_ab" and sb["design"].endswith("cụm 1")
    truth = gte_truth(tiny_runs["gte"])
    table = pd.read_parquet(tiny_runs["gte"] / "results" / "policy_results.parquet")
    assert truth["GTE"] == pytest.approx(gte_summary(table)["GTE"])
    t = design_table([tiny_runs["rider_ab"]], tiny_runs["gte"], n_boot=20)
    assert t["bias"].iloc[0] == pytest.approx(t["per_day"].iloc[0] - truth["GTE"])


def test_stratified_difference():
    from analysis.interference import _stratified_difference
    y = np.array([1, 0, 1, 1, 0, 0, 1, 0], dtype=float)
    t = np.array([1, 0, 1, 0, 1, 0, 1, 1], dtype=bool)
    strata = np.array([0, 0, 0, 1, 1, 1, 2, 2])                              # stratum 2 has no control
    w = np.ones(8)
    # stratum 0: 1 - 0 = 1 (size 3); stratum 1: 0 - 0.5 = -0.5 (size 3)
    assert _stratified_difference(y, t, strata, w) == pytest.approx((1 * 3 - 0.5 * 3) / 6)


def test_confounding_estimates_on_a_legacy_run(tiny_runs):
    from analysis.interference import confounding_estimates
    r = confounding_estimates(tiny_runs["legacy"], n_boot=20)
    assert r["target_g_u"] == 1.0 and 0 < r["share_offered"] < 1 and r["n_explore"] > 0
    assert r["bias_naive"] == pytest.approx(r["naive"] - r["explore_all"])
    assert r["bias_adjusted"] == pytest.approx(r["adjusted"] - r["explore_on"])
    with pytest.raises(ValueError):
        confounding_estimates(tiny_runs["rider_ab"])
