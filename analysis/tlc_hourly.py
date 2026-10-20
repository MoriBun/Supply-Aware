"""Hourly demand, wait time and busy drivers from NYC TLC high-volume FHV trips, next to the simulator.

A read-only probe: it changes no parameter. Data (not committed; download it to ``runs/tlc/``):

    runs/tlc/fhvhv_tripdata_2024-03.parquet   https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2024-03.parquet
    runs/tlc/taxi_zone_lookup.csv             https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv

Usage: ``python -m analysis.tlc_hourly [config.yaml]``. Only completed trips exist in the TLC
file, and the driver-arrival time (``on_scene_datetime``) is filled for Uber (HV0003) only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

TLC_DIR = Path("runs/tlc")
TRIPS = TLC_DIR / "fhvhv_tripdata_2024-03.parquet"
ZONES = TLC_DIR / "taxi_zone_lookup.csv"
MONTH = ("2024-03-01", "2024-04-01")


def tlc_hourly() -> tuple[pd.DataFrame, dict]:
    """Per hour of day: demand profile (mean 1), wait request -> driver arrival, busy drivers relative to the peak."""
    cols = ["hvfhs_license_num", "request_datetime", "on_scene_datetime", "PULocationID", "trip_miles", "trip_time",
            "base_passenger_fare", "driver_pay"]
    d = pd.read_parquet(TRIPS, columns=cols)
    d = d[(d["request_datetime"] >= MONTH[0]) & (d["request_datetime"] < MONTH[1])]
    d["hour"] = d["request_datetime"].dt.hour
    d["weekday"] = d["request_datetime"].dt.dayofweek < 5
    d["date"] = d["request_datetime"].dt.date
    n_days = d["date"].nunique()

    def profile(x: pd.DataFrame) -> pd.Series:
        per_hour = x.groupby("hour").size() / x["date"].nunique()
        return per_hour / per_hour.mean()

    wait = (d["on_scene_datetime"] - d["request_datetime"]).dt.total_seconds() / 60
    uber = d.assign(wait=wait)[(d["hvfhs_license_num"] == "HV0003") & wait.between(0, 60)]
    busy = d.groupby("hour")["trip_time"].sum() / 3600 / n_days          # trip-hours started in the hour, per day
    table = pd.DataFrame({
        "demand_all": profile(d), "demand_weekday": profile(d[d["weekday"]]), "demand_weekend": profile(d[~d["weekday"]]),
        "wait_min": uber.groupby("hour")["wait"].mean(), "busy_rel": busy / busy.max(),
    })
    borough = d["PULocationID"].map(pd.read_csv(ZONES).set_index("LocationID")["Borough"])
    summary = {
        "trips": len(d), "days": n_days, "share_manhattan": float((borough == "Manhattan").mean()),
        "trip_min": float(d["trip_time"].mean() / 60), "trip_miles": float(d["trip_miles"].mean()),
        "fare_vnd": float(d["base_passenger_fare"].mean()),
        "driver_share": float(d["driver_pay"].sum() / d["base_passenger_fare"].sum()),
        "wait_mean": float(uber["wait"].mean()), "wait_median": float(uber["wait"].median()),
        "wait_p90": float(uber["wait"].quantile(0.9)), "share_wait_over_10": float((uber["wait"] > 10).mean()),
    }
    return table, summary


def sim_hourly(config_path: str, n_seeds: int = 5) -> tuple[pd.DataFrame, dict]:
    """The same hourly curves from ``n_seeds`` all_off days of the simulator."""
    from sim.config import load_config
    from sim.population import build_world, online_by_hour
    from sim.rng import Rng
    from sim.runner import Job, run_jobs, with_policy
    from sim.state import OrderStatus

    cfg = load_config(config_path)
    jobs = [Job(cfg=with_policy(cfg, "all_off"), seed=cfg.meta.run_seed + i, enforce_budget=False, log_level="full")
            for i in range(n_seeds)]
    wait_sum, n_done, n_req, busy, trip_sum = (np.zeros(24) for _ in range(5))
    for res in run_jobs(jobs, n_procs=cfg.runner.n_procs):
        o = res.orders
        in_w = o.col("in_window")
        hour = (o.col("request_time_s") // 3600).astype(int) % 24
        done = in_w & (o.col("status") == int(OrderStatus.COMPLETED))
        np.add.at(wait_sum, hour[done], (o.col("pickup_time_s") - o.col("request_time_s"))[done] / 60)
        np.add.at(n_done, hour[done], 1)
        np.add.at(n_req, hour[in_w], 1)
        np.add.at(busy, hour[done], o.col("trip_time_min")[done] / 60)
    profile = np.asarray(cfg.demand.hour_profile)
    online = online_by_hour(build_world(cfg, Rng.from_config(cfg)).drivers)
    table = pd.DataFrame({"demand": profile / profile.mean(), "wait_min": wait_sum / n_done,
                          "busy_rel": busy / busy.max(), "online_rel": online / online.max(),
                          "completed_pct": 100 * n_done / n_req}, index=pd.RangeIndex(24, name="hour"))
    summary = {"wait_mean": float(wait_sum.sum() / n_done.sum()), "trip_min": float(busy.sum() * 60 / n_done.sum())}
    return table, summary


def main(argv: list[str]) -> int:
    config_path = argv[1] if len(argv) > 1 else "config/default.yaml"
    real, real_sum = tlc_hourly()
    sim, sim_sum = sim_hourly(config_path)
    out = pd.DataFrame({
        "demand_real": real["demand_all"], "demand_weekday": real["demand_weekday"], "demand_sim": sim["demand"],
        "wait_real": real["wait_min"], "wait_sim": sim["wait_min"],
        "busy_real": real["busy_rel"], "online_sim": sim["online_rel"], "completed_sim_pct": sim["completed_pct"],
    })
    TLC_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(TLC_DIR / "hourly_comparison.csv")
    with pd.option_context("display.width", 200):
        print(out.round(2).to_string())
    print("TLC:", {k: round(v, 3) if isinstance(v, float) else v for k, v in real_sum.items()})
    print("sim:", {k: round(v, 3) for k, v in sim_sum.items()})
    for name in ("demand_real", "demand_weekday"):
        print(f"demand_sim vs {name}: corr {np.corrcoef(out['demand_sim'], out[name])[0, 1]:.2f}, "
              f"mean abs diff {np.abs(out['demand_sim'] - out[name]).mean():.2f}")
    for label, col in (("real", "wait_real"), ("sim", "wait_sim")):
        print(f"wait by hour, {label}: {out[col].min():.2f} to {out[col].max():.2f} min ({out[col].max() / out[col].min():.1f}x)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
