"""Validation numbers of the simulator for notebook 01: A1, A2 (b), A3, A5, CAL, data sets.

Every function reads run directories written by the commands of README.md, section "Tái lập kết quả";
nothing here simulates. The definitions are those of the acceptance checks (README.md, section
"Kiểm thử") and of the slow tests (``tests/test_acceptance_core.py``, ``tests/test_acceptance.py``),
so the notebook shows the numbers the slow tests judge. ``tests/test_validation.py`` checks that,
on the same seed, the CAL numbers computed here from the saved tables equal the ones the slow test
computes in memory.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from analysis.io import load_hidden, load_run
from analysis.plots import vn

# ---------------------------------------------------------------------------
# A1: throughput curve
# ---------------------------------------------------------------------------

A1_COLUMNS = ("completed_per_h", "mean_slack", "mean_pickup_eta_min")


def throughput_summary(table: pd.DataFrame) -> pd.DataFrame:
    """Mean over seeds of ``results/throughput_curve`` by ascending ``demand_scale`` (mode throughput_curve)."""
    return table.groupby("demand_scale")[list(A1_COLUMNS)].mean().sort_index().reset_index()


def a1_checks(summary: pd.DataFrame, *, wgc_slack: float = 0.45, fall_ratio: float = 0.95,
              max_eta_step_min: float = 3.0) -> pd.DataFrame:
    """The four pass conditions of A1, each with the number it was judged on.

    Thresholds are those of acceptance check A1 (0.95 x peak, slack 0.45 from Castillo,
    3-minute ETA step) and default to them.
    """
    done = summary["completed_per_h"].to_numpy(dtype=float)
    slack = summary["mean_slack"].to_numpy(dtype=float)
    eta = summary["mean_pickup_eta_min"].to_numpy(dtype=float)
    scale = summary["demand_scale"].to_numpy(dtype=float)
    peak = int(np.argmax(done))
    steps = np.diff(eta)
    after = slack[peak + 1:]
    rows = [
        ("1. tăng rồi đạt đỉnh", f"đỉnh {vn(done[peak], 1)} chuyến/giờ ở demand_scale {vn(scale[peak], 2)}",
         bool(0 < peak < len(done) - 1 and (np.diff(done[: peak + 1]) > 0).all())),
        (f"2. mức cầu lớn nhất ≤ {vn(fall_ratio, 2)} × đỉnh", f"{vn(done[-1] / done[peak], 3)} × đỉnh",
         bool(done[-1] <= fall_ratio * done[peak])),
        (f"3. slack < {vn(wgc_slack, 2)} ở đoạn giảm",
         f"slack lớn nhất sau đỉnh {vn(after.max(), 3)}" if len(after) else "—",
         bool(len(after) and (after < wgc_slack).all())),
        (f"4. ETA đón tăng đơn điệu, bước ≤ {vn(max_eta_step_min, 0)} phút", f"bước lớn nhất {vn(steps.max(), 3)} phút",
         bool((steps > 0).all() and steps.max() <= max_eta_step_min)),
    ]
    return pd.DataFrame(rows, columns=["criterion", "measure", "passed"])


# ---------------------------------------------------------------------------
# CAL: calibration targets
# ---------------------------------------------------------------------------


def full_run_dirs(mode_dir: Path, policy: str) -> list[Path]:
    """Full-log run directories of ``policy`` under a mode output (``<mode_dir>/runs/<run_id>``), by seed."""
    meta = pd.read_parquet(Path(mode_dir) / "meta" / "run_metadata.parquet")
    meta = meta[meta["policy"] == policy].sort_values("seed")
    dirs = [Path(mode_dir) / "runs" / rid for rid in meta["run_id"]]
    missing = [str(d) for d in dirs if not d.exists()]
    if missing:
        raise FileNotFoundError(f"no full log for {missing}; run the mode with --log-level full")
    return dirs


def _run_metadata_row(run_dir: Path) -> pd.Series:
    """``meta/run_metadata`` row of a run: in the run directory (single-run mode) or in the mode
    output two levels up (``<mode_dir>/runs/<run_id>``, see :func:`full_run_dirs`)."""
    run_dir = Path(run_dir)
    own = run_dir / "meta" / "run_metadata.parquet"
    if own.exists():
        return pd.read_parquet(own).iloc[0]
    meta = pd.read_parquet(run_dir.parent.parent / "meta" / "run_metadata.parquet")
    return meta[meta["run_id"] == run_dir.name].iloc[0]


def _calibration_counts(run_dir: Path) -> dict[str, float]:
    run, hidden = load_run(run_dir), load_hidden(run_dir)
    s = run["sessions"]
    s = s[s["in_window"]].merge(hidden["sessions_hidden"][["session_id", "p_request_treat", "p_request_control"]],
                                on="session_id", how="left")
    snaps = run["slot_snapshots"]
    lag = snaps[["slot", "cell", "slack_lag_slot"]].rename(columns={"cell": "pu_cell"})
    s = s.merge(lag, on=["slot", "pu_cell"], how="left")
    surplus = (s["slack_lag_slot"] > 1.0).to_numpy()            # NaN (no previous slot) is not surplus
    window = snaps[snaps["slot"].isin(np.unique(s["slot"]))]["slack"].to_numpy(dtype=float)
    o = run["orders"]
    done = o[o["in_window"] & (o["status"] == "Completed")]
    meta = _run_metadata_row(run_dir)
    fleet = yaml.safe_load(meta["config_yaml"])["supply"]["fleet_size"]
    days = (float(meta["window_end_s"]) - float(meta["window_start_s"])) / 86400.0
    hour = (done["request_time_s"].to_numpy(np.float64) % 86400.0 // 3600.0).astype(np.int64)
    busy = np.zeros(24)
    np.add.at(busy, hour, done["trip_time_min"].to_numpy(np.float64) / 60.0)
    return {"requested": float(s["requested"].to_numpy()[surplus].sum()), "sessions": float(surplus.sum()),
            "p_treat": float(s["p_request_treat"].to_numpy(np.float64)[surplus].sum()),
            "p_control": float(s["p_request_control"].to_numpy(np.float64)[surplus].sum()),
            "n_slack": float(window.size), "n_tight": float((window < 0.35).sum()),
            "n_surplus": float((window > 1.0).sum()),
            "km_sum": float(done["trip_km"].astype(np.float64).sum()), "n_done": float(len(done)),
            "driver_days": float(fleet * days),
            **{f"busy_{h}": float(busy[h]) for h in range(24)}}


def calibration_metrics(run_dirs) -> dict[str, float]:
    """The CAL quantities, pooled over full-log all_off runs (as ``calibration_metrics`` of the slow test).

    - surplus session: the slack its cell had in the previous slot (``slack_lag_slot``) is above 1;
      P(request) without voucher and the request uplift
      ``sum p_request_treat / sum p_request_control - 1`` are taken over those sessions;
    - shares of (cell, slot) of the window with slack < 0.35 and > 1 (their own slot);
    - ``busy_rel_h6/h7/h8``: trip hours of completed in-window orders by hour of the request over the
      busiest hour (definition of ``analysis/tlc_hourly.py``);
    - ``mean_trip_km`` of completed in-window orders; ``trips_per_driver_day`` = completed in-window
      orders / (``fleet_size`` x days of the window), as the slow test.
    """
    total: dict[str, float] = {}
    for d in run_dirs:
        for k, v in _calibration_counts(Path(d)).items():
            total[k] = total.get(k, 0.0) + v
    return {
        "p_request_no_voucher": total["requested"] / total["sessions"],
        "request_uplift_surplus": total["p_treat"] / total["p_control"] - 1.0,
        "share_cellslots_slack_below_0_35": total["n_tight"] / total["n_slack"],
        "share_cellslots_slack_above_1": total["n_surplus"] / total["n_slack"],
        **{f"busy_rel_h{h}": total[f"busy_{h}"] / max(total[f"busy_{k}"] for k in range(24)) for h in (6, 7, 8)},
        "mean_trip_km": total["km_sum"] / total["n_done"],
        "trips_per_driver_day": total["n_done"] / total["driver_days"],
    }


def calibration_table(metrics: dict[str, float], targets: dict[str, tuple[float, float]]) -> pd.DataFrame:
    """One row per CAL target: value, range of ``calibration_targets`` and whether it is inside."""
    rows = [{"target": k, "value": metrics[k], "lo": float(targets[k][0]), "hi": float(targets[k][1]),
             "in_range": bool(targets[k][0] <= metrics[k] <= targets[k][1])} for k in targets]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# A2 (b), A3, A5
# ---------------------------------------------------------------------------


def crn_variance_ratio(n_a: pd.Series, n_b: pd.Series) -> dict[str, float]:
    """A2 (b): variance of N(a) - N(b) with common random numbers against independent seeds.

    ``n_a``, ``n_b``: N per seed (index = seed). With CRN the difference is paired by seed; with
    independent seeds Var(N(a) - N(b')) = Var(N(a)) + Var(N(b)), estimated from the same runs (the
    slow test draws extra seeds for b instead; same expectation).
    """
    both = pd.concat([n_a.rename("a"), n_b.rename("b")], axis=1).dropna().astype(float)
    var_crn = float((both["a"] - both["b"]).var(ddof=1))
    var_indep = float(both["a"].var(ddof=1) + both["b"].var(ddof=1))
    return {"n_seeds": int(len(both)), "mean_diff": float((both["a"] - both["b"]).mean()), "var_crn": var_crn,
            "var_indep": var_indep, "ratio": var_crn / var_indep if var_indep > 0 else math.nan}


def per_seed_summary(results: pd.DataFrame, column: str) -> dict[str, float]:
    """Median, min and max over the runs of a ``policy_results`` column (A3 switches, A5 run time)."""
    v = results[column].astype(float)
    return {"median": float(v.median()), "min": float(v.min()), "max": float(v.max()), "n": int(len(v))}


# ---------------------------------------------------------------------------
# Data sets and the market by hour
# ---------------------------------------------------------------------------


def datasets_table(run_dirs) -> pd.DataFrame:
    """``analysis.check_dataset`` summary of each run directory, with its problems (empty = OK)."""
    from analysis.check_dataset import check_run_dir

    rows = []
    for d in run_dirs:
        problems, summary = check_run_dir(Path(d))
        rows.append({**summary, "problems": "; ".join(problems) if problems else ""})
    return pd.DataFrame(rows)


def market_by_hour(cfg) -> pd.DataFrame:
    """Expected sessions, drivers on shift and travel speed factor per hour of the default world."""
    from sim.population import build_world, online_by_hour
    from sim.rng import Rng

    world = build_world(cfg, Rng.from_config(cfg))
    d = cfg.demand
    weight = float(world.cell_weight.sum())
    return pd.DataFrame({
        "hour": np.arange(24),
        "sessions_expected": [d.base_sessions_per_cell_h * d.demand_scale * float(p) * weight for p in d.hour_profile],
        "drivers_on_shift": online_by_hour(world.drivers),
        "speed_factor": [float(v) for v in cfg.space.speed_factor_by_hour],
    })
