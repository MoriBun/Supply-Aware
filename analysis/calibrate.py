"""Calibration helper for the CAL targets.

Runs ``all_off`` on the given configuration for a few seeds (full log, in memory, no files) and
prints, for one calibration round:

- the six calibration knobs (``fleet_size``, ``demand_scale``, ``shift_start_mixture``,
  ``dest_decay_rings``, ``alpha0``, ``delta0``);
- the nine CAL targets of ``calibration_targets`` with their ranges, computed with the formulas of
  ``tests/test_acceptance_core.py::calibration_metrics``, so the numbers here are the
  numbers the slow test will judge;
- the report-only market checks: mean pickup wait, mean trip time, cancel and abandon
  rates, share of driver-hours spent repositioning (warning above ``REPOSITION_WARN``);
- one Markdown row for a calibration table.

Usage::

    python -m analysis.calibrate --config config/default.yaml [--config overlay.yaml]
        [--set supply.fleet_size=1200 ...] [--seeds 3] [--n-procs 3] [--json round.json]

Nothing here changes ``sim/``; the knobs are passed as ``--set`` overrides exactly as ``sim.cli``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import numpy as np

from sim.config import Config, config_hash, load_config
from sim.engine import RunResult
from sim.population import build_world, on_shift
from sim.rng import Rng
from sim.runner import Job, run_jobs, with_policy
from sim.state import OrderStatus

KNOBS = ("supply.fleet_size", "demand.demand_scale", "supply.shift_start_mixture", "demand.dest_decay_rings",
         "riders.alpha0", "riders.delta0")
CHECK_RANGES = {"mean_pickup_wait_min": (3.0, 8.0), "mean_trip_time_min": (15.0, 25.0)}   # report only
REPOSITION_WARN = 0.15
_DAY_S = 86400.0


def knob_values(cfg: Config) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in KNOBS:
        node = cfg
        for part in key.split("."):
            node = getattr(node, part)
        out[key] = [list(row) for row in node] if isinstance(node, tuple) else node
    return out


def run_all_off(cfg: Config, n_seeds: int, *, n_procs: int | None = None) -> list[RunResult]:
    """Full-log ``all_off`` runs on seeds ``run_seed .. run_seed + n_seeds - 1`` (as the CAL slow test)."""
    seeds = [cfg.meta.run_seed + i for i in range(n_seeds)]
    jobs = [Job(cfg=with_policy(cfg, "all_off"), seed=s, enforce_budget=False, log_level="full") for s in seeds]
    return run_jobs(jobs, n_procs=n_procs)


def cal_metrics(cfg: Config, results: Sequence[RunResult]) -> dict[str, float]:
    """The nine CAL targets, pooled over seeds; same formulas as the slow test."""
    requested = sessions = 0
    p_treat = p_control = 0.0
    n_slack = n_tight = n_surplus = 0
    km_sum, n_done, driver_days = 0.0, 0, 0.0
    busy = np.zeros(24)
    for res in results:
        first_slot = int(res.snapshots[0]["slot"][0])
        slack = np.stack([rec["slack"] for rec in res.snapshots])
        lag = np.stack([rec["slack_lag_slot"] for rec in res.snapshots])
        s = res.sessions
        in_w = s.col("in_window")
        surplus = lag[s.col("slot")[in_w] - first_slot, s.col("pu_cell")[in_w]] > 1.0
        requested += int(s.col("requested")[in_w][surplus].sum())
        sessions += int(surplus.sum())
        p_treat += float(s.col("p_request_treat")[in_w][surplus].astype(np.float64).sum())
        p_control += float(s.col("p_request_control")[in_w][surplus].astype(np.float64).sum())
        window_slots = np.unique(s.col("slot")[in_w])
        table = slack[window_slots - first_slot]
        n_slack += table.size
        n_tight += int((table < 0.35).sum())
        n_surplus += int((table > 1.0).sum())
        o = res.orders
        done = o.col("in_window") & (o.col("status") == int(OrderStatus.COMPLETED))
        km_sum += float(o.col("trip_km")[done].astype(np.float64).sum())
        n_done += int(done.sum())
        driver_days += cfg.supply.fleet_size * (res.window_end_s - res.window_start_s) / _DAY_S
        hour = (o.col("request_time_s")[done] % _DAY_S // 3600.0).astype(np.int64)
        np.add.at(busy, hour, o.col("trip_time_min")[done].astype(np.float64) / 60.0)
    busy_rel = busy / busy.max() if busy.max() > 0 else busy
    return {
        "p_request_no_voucher": requested / sessions if sessions else float("nan"),
        "request_uplift_surplus": p_treat / p_control - 1.0 if p_control > 0 else float("nan"),
        "share_cellslots_slack_below_0_35": n_tight / n_slack,
        "share_cellslots_slack_above_1": n_surplus / n_slack,
        "busy_rel_h6": float(busy_rel[6]), "busy_rel_h7": float(busy_rel[7]), "busy_rel_h8": float(busy_rel[8]),
        "mean_trip_km": km_sum / n_done if n_done else float("nan"),
        "trips_per_driver_day": n_done / driver_days,
    }


def check_metrics(cfg: Config, results: Sequence[RunResult]) -> dict[str, float]:
    """Report-only market checks, pooled over seeds and in-window orders.

    ``reposition_share``: driver-hours of the window not idle, en route or on trip, over the driver-hours
    on shift (W0 schedule: ``on_shift`` at every slot midpoint); the remainder of an online driver's time
    is repositioning. On W1 the nominal schedule still counts absent drivers (attendance) and
    drivers paused by rain as on shift, so the on-shift hours are overstated and the share is an upper
    bound there (W0 is exact). ``p_request_all``: requested / sessions over every in-window session.
    ``share_tight_outside_5_8h``: (cell, slot) of the window with slack < 0.35 whose slot starts outside
    5:00-8:59, over all such (cell, slot): a check that W1's shocks create tightness beyond the
    morning supply gap of W0.
    """
    world = build_world(cfg, Rng.from_config(cfg))
    wait_s = trip_min = 0.0
    n_done = n_requests = n_cancelled = n_abandoned = n_sessions = n_requested = 0
    busy_h = online_h = 0.0
    n_outside = n_tight_outside = 0
    slot_h = cfg.time.slot_min / 60.0
    for res in results:
        o, s = res.orders, res.sessions
        in_w = o.col("in_window")
        status = o.col("status")
        done = in_w & (status == int(OrderStatus.COMPLETED))
        wait_s += float((o.col("pickup_time_s")[done] - o.col("request_time_s")[done]).sum())
        trip_min += float(o.col("trip_time_min")[done].astype(np.float64).sum())
        n_done += int(done.sum())
        n_requests += int(in_w.sum())
        n_cancelled += int((in_w & (status == int(OrderStatus.CANCELLED))).sum())
        n_abandoned += int((in_w & (status == int(OrderStatus.ABANDONED))).sum())
        s_in = s.col("in_window")
        n_sessions += int(s_in.sum())
        n_requested += int(s.col("requested")[s_in].sum())
        for rec in res.snapshots:
            t_mid = float(rec["published_at_s"][0]) - cfg.time.slot_min * 30.0
            if not res.window_start_s <= t_mid < res.window_end_s:
                continue
            busy_h += float((rec["idle_avg"] + rec["enroute_avg"] + rec["ontrip_avg"]).sum()) * slot_h
            online_h += float(on_shift(world.drivers, t_mid).sum()) * slot_h
            if not 5 <= int(rec["hour"][0]) <= 8:
                n_outside += int(np.size(rec["slack"]))
                n_tight_outside += int((rec["slack"] < 0.35).sum())
    return {
        "p_request_all": n_requested / n_sessions if n_sessions else float("nan"),
        "mean_pickup_wait_min": wait_s / 60.0 / n_done if n_done else float("nan"),
        "mean_trip_time_min": trip_min / n_done if n_done else float("nan"),
        "cancel_rate": n_cancelled / n_requests if n_requests else float("nan"),
        "abandon_rate": n_abandoned / n_requests if n_requests else float("nan"),
        "reposition_share": max(0.0, 1.0 - busy_h / online_h) if online_h > 0 else float("nan"),
        "share_tight_outside_5_8h": n_tight_outside / n_outside if n_outside else float("nan"),
        "completed_per_seed": n_done / len(results),
    }


def per_seed_spread(cfg: Config, results: Sequence[RunResult]) -> dict[str, tuple[float, float]]:
    """Min and max of every CAL target over the seeds taken one at a time (the slow test pools them, but a
    target that only passes pooled is fragile)."""
    singles = [cal_metrics(cfg, [res]) for res in results]
    return {k: (min(m[k] for m in singles), max(m[k] for m in singles)) for k in singles[0]}


def targets_of(cfg: Config) -> dict[str, tuple[float, float]]:
    import dataclasses
    return {k: (float(v[0]), float(v[1])) for k, v in dataclasses.asdict(cfg.calibration_targets).items()}


def in_range(value: float, lo: float, hi: float) -> bool:
    return bool(lo <= value <= hi)


def format_report(cfg: Config, cal: dict[str, float], checks: dict[str, float], n_seeds: int,
                  spread: dict[str, tuple[float, float]] | None = None) -> str:
    targets = targets_of(cfg)
    world = "W1" if cfg.shocks.enabled else "W0"
    lines = [f"config_hash={config_hash(cfg)}  seeds={n_seeds}  policy=all_off  world={world}"]
    lines.append("knobs: " + "  ".join(f"{k.split('.')[-1]}={v}" for k, v in knob_values(cfg).items()))
    lines.append("")
    head = f"{'CAL target':34s} {'value':>8s}  {'range':>14s}  ok"
    lines.append(head + ("   per-seed min..max" if spread else ""))
    n_ok = 0
    for k, (lo, hi) in targets.items():
        ok = in_range(cal[k], lo, hi)
        n_ok += ok
        line = f"{k:34s} {cal[k]:8.4f}  [{lo:5.2f}, {hi:5.2f}]  {'yes' if ok else 'NO '}"
        if spread:
            s_lo, s_hi = spread[k]
            flag = "" if in_range(s_lo, lo, hi) and in_range(s_hi, lo, hi) else "  (a seed is outside)"
            line += f"   {s_lo:.4f}..{s_hi:.4f}{flag}"
        lines.append(line)
    lines.append(f"{n_ok}/{len(targets)} targets in range")
    lines.append("")
    lines.append(f"{'check (report only)':34s} {'value':>8s}  note")
    for k, v in checks.items():
        note = ""
        if k in CHECK_RANGES:
            lo, hi = CHECK_RANGES[k]
            note = f"[{lo:.0f}, {hi:.0f}] {'ok' if in_range(v, lo, hi) else 'OUTSIDE'}"
        elif k == "reposition_share":
            note = f"warn > {REPOSITION_WARN:.0%}" + ("  HIGH" if v > REPOSITION_WARN else "")
        lines.append(f"{k:34s} {v:8.4f}  {note}")
    return "\n".join(lines)


def decisions_row(cfg: Config, cal: dict[str, float], checks: dict[str, float]) -> str:
    """One row for a calibration table (knobs | CAL targets | checks)."""
    kv = knob_values(cfg)
    mix = ";".join("-".join(f"{x:g}" for x in row) for row in kv["supply.shift_start_mixture"])
    knobs = (f"{kv['supply.fleet_size']} | {kv['demand.demand_scale']:g} | {mix} | "
             f"{kv['demand.dest_decay_rings']:g} | {kv['riders.alpha0']:g} | {kv['riders.delta0']:g}")
    t = targets_of(cfg)
    cells = " | ".join(f"{cal[k]:.3f}{'' if in_range(cal[k], *t[k]) else ' ✗'}" for k in t)
    chk = (f"{checks['mean_pickup_wait_min']:.1f} | {checks['mean_trip_time_min']:.1f} | "
           f"{checks['cancel_rate']:.3f} | {checks['abandon_rate']:.3f} | {checks['reposition_share']:.3f}")
    return f"| {config_hash(cfg)} | {knobs} | {cells} | {chk} |"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", action="append", required=True, help="YAML layer(s), in order")
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="a.b=c")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--n-procs", type=int, default=None)
    ap.add_argument("--json", type=Path, default=None, help="write knobs, CAL and checks to this file")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, args.overrides)
    results = run_all_off(cfg, args.seeds, n_procs=args.n_procs)
    cal, checks = cal_metrics(cfg, results), check_metrics(cfg, results)
    spread = per_seed_spread(cfg, results) if args.seeds > 1 else None
    print(format_report(cfg, cal, checks, args.seeds, spread))
    print()
    print("calibration table row:")
    print(decisions_row(cfg, cal, checks))
    runtime = [r.runtime_s for r in results]
    print(f"runtime per run (s): {', '.join(f'{x:.1f}' for x in runtime)}")
    if args.json is not None:
        payload = {"config_hash": config_hash(cfg), "overrides": list(args.overrides), "seeds": args.seeds,
                   "knobs": knob_values(cfg), "cal": cal, "targets": targets_of(cfg), "checks": checks,
                   "per_seed_min_max": spread, "runtime_s": runtime}
        args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
