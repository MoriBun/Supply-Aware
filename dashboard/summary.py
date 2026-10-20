"""Aggregates of a finished traced run for the dashboard pages (KPIs, per-slot series, switch events,
voucher distribution). Everything is computed from the engine buffers, the published snapshots and
the trace; nothing here changes the simulator's numbers (N and V come from ``RunResult``)."""

from __future__ import annotations

import math

import numpy as np

from sim.budget import DONG_PER_VND
from sim.config import Config
from sim.engine import RunResult
from sim.state import Clock, MECHANISM_NAMES, OrderStatus

from dashboard.trace import Trace, budget_period_table

NAN = float("nan")


def _f(x) -> float | None:
    """JSON-safe float (None for NaN / inf)."""
    x = float(x)
    return x if math.isfinite(x) else None


def _list(a) -> list:
    a = np.asarray(a)
    if a.dtype.kind == "f":
        return [_f(v) for v in a]
    return a.tolist()


def kpis(result: RunResult, cfg: Config, *, theta: float | None, kappa: float | None, kappa_pilots: int,
         budget_vnd: float | None, score_fn: str | None, scope: str | None) -> dict:
    spent_share = None
    if budget_vnd and budget_vnd > 0:
        n_periods = Clock.from_config(cfg).n_periods
        spent_share = result.voucher_spent_vnd / (budget_vnd * n_periods)
    return {
        "policy": result.policy, "theta": _f(theta) if theta is not None else None,
        "kappa": _f(kappa) if kappa is not None else None, "kappa_pilots": int(kappa_pilots),
        "score_fn": score_fn, "scope": scope,
        "budget_B_vnd": _f(budget_vnd) if budget_vnd is not None else None,
        "N_completed": int(result.N_completed), "V_profit_vnd": _f(result.V_profit_vnd),
        "voucher_spent_vnd": _f(result.voucher_spent_vnd), "spent_share_of_budget": _f(spent_share) if spent_share is not None else None,
        "n_sessions": int(result.n_sessions), "n_requests": int(result.n_requests),
        "n_abandoned": int(result.n_abandoned), "n_cancelled": int(result.n_cancelled),
        "mean_pickup_eta_min": _f(result.mean_pickup_eta_min), "share_cells_off": _f(result.share_cells_off),
        "n_switches_per_cell_day": _f(result.n_switches_per_cell_day), "mean_slack": _f(result.mean_slack),
        "abandon_rate": _f(result.abandon_rate), "cancel_rate": _f(result.cancel_rate),
        "completed_per_h": _f(result.completed_per_h), "requests_per_h": _f(result.requests_per_h),
        "window_start_s": _f(result.window_start_s), "window_end_s": _f(result.window_end_s),
        "sim_end_s": _f(result.sim_end_s), "n_truncated_orders": int(result.n_truncated_orders),
        "runtime_s": _f(result.runtime_s),
    }


MONEY = ("gross_fare_vnd", "voucher_value_vnd", "net_fare_vnd", "driver_pay_vnd", "platform_profit_vnd")


def _money_block(o, mask: np.ndarray) -> dict:
    """Sums of the money columns over the orders in ``mask`` (same dtype path as ``engine._aggregate``)."""
    n = int(mask.sum())
    out = {"trips": n}
    for c in MONEY:
        out[c] = float(o.col(c)[mask].sum()) if n else 0.0
    out["margin_before_voucher_vnd"] = out["platform_profit_vnd"] + out["voucher_value_vnd"]
    out["gross_per_trip_vnd"] = _f(out["gross_fare_vnd"] / n) if n else None
    out["profit_per_trip_vnd"] = _f(out["platform_profit_vnd"] / n) if n else None
    out["voucher_per_trip_vnd"] = _f(out["voucher_value_vnd"] / n) if n else None
    return out


def revenue(result: RunResult) -> dict:
    """Money of the completed trips whose request lies in the evaluation window (``observed/orders``, H-42):
    gross fare → minus voucher = net fare the rider pays → minus driver pay = platform profit, whose window sum
    is exactly ``V_profit_vnd`` of ``RunResult``. ``by_arm`` (trips with / without a voucher) and ``by_hour``
    are descriptive: the arms are chosen by the policy, so their difference is not an uplift (see :func:`compare`)."""
    o, s = result.orders, result.sessions
    done = o.col("in_window") & (o.col("status") == int(OrderStatus.COMPLETED))
    order_idx = s.col("order_idx")
    has = order_idx >= 0
    arm_of_order = np.zeros(o.n, dtype=np.int8)
    arm_of_order[order_idx[has]] = s.col("arm")[has]
    hour = (np.floor(o.col("request_time_s").astype(np.float64) / 3600.0).astype(np.int64)) % 24
    by_hour = []
    for h in range(24):
        b = _money_block(o, done & (hour == h))
        by_hour.append({"hour": h, "trips": b["trips"], "gross_fare_vnd": b["gross_fare_vnd"],
                        "voucher_value_vnd": b["voucher_value_vnd"], "platform_profit_vnd": b["platform_profit_vnd"]})
    return {"total": _money_block(o, done),
            "by_arm": [{"arm": "voucher", **_money_block(o, done & (arm_of_order == 1))},
                       {"arm": "no_voucher", **_money_block(o, done & (arm_of_order == 0))}],
            "by_hour": by_hour}


def _same(a, b, keys) -> bool:
    for k in keys:
        x, y = a, b
        for part in k.split("."):
            x = (x or {}).get(part)
            y = (y or {}).get(part)
        if x != y:
            return False
    return True


def compare(run: dict, ref: dict) -> dict:
    """Paired difference ``run − ref`` of two finished single-theta runs (``summary`` + ``config`` of each):
    extra trips, extra voucher spend, extra profit, and the two ratios that say whether vouchers paid for
    themselves. Honest only under CRN: same seed and same world; the flags report both, and the budget."""
    for name, r in (("run", run), ("ref", ref)):
        if not (r.get("summary") or {}).get("revenue"):
            raise ValueError(f"{name}: lượt này chưa có mục doanh thu (chạy trước bản này); chạy lại để so sánh")
    ka, kb = run["summary"]["kpis"], ref["summary"]["kpis"]
    ra, rb = run["summary"]["revenue"]["total"], ref["summary"]["revenue"]["total"]
    delta = {"N_completed": ka["N_completed"] - kb["N_completed"]}
    for c in MONEY + ("margin_before_voucher_vnd",):
        delta[c] = ra[c] - rb[c]
    dv = delta["voucher_value_vnd"]
    ratios = {
        "extra_trips_per_100k_vnd": _f(delta["N_completed"] / (dv / 100_000.0)) if dv > 0 else None,
        "extra_margin_per_1k_vnd_voucher": _f(delta["margin_before_voucher_vnd"] / (dv / 1_000.0)) if dv > 0 else None,
        "extra_profit_per_trip_vnd": _f(delta["platform_profit_vnd"] / delta["N_completed"]) if delta["N_completed"] else None,
    }
    ca, cb = run.get("config") or {}, ref.get("config") or {}
    return {
        "run": {"id": run.get("id"), "name": run.get("name"), "policy": ka.get("policy"), "theta": ka.get("theta"),
                "N_completed": ka["N_completed"], "V_profit_vnd": ka["V_profit_vnd"], "voucher_spent_vnd": ka["voucher_spent_vnd"]},
        "ref": {"id": ref.get("id"), "name": ref.get("name"), "policy": kb.get("policy"), "theta": kb.get("theta"),
                "N_completed": kb["N_completed"], "V_profit_vnd": kb["V_profit_vnd"], "voucher_spent_vnd": kb["voucher_spent_vnd"]},
        "delta": delta, "ratios": ratios,
        "same_seed": _same(ca, cb, ("meta.run_seed", "meta.world_seed")),
        "same_world": _same(ca, cb, ("space.grid_radius", "space.edge_km", "supply.fleet_size", "demand.demand_scale",
                                     "time.window_min", "time.warmup_min", "shocks.enabled", "voucher.pct_of_fare")),
        "same_budget": (ka.get("budget_B_vnd") == kb.get("budget_B_vnd")),
    }


def slot_series(trace: Trace, clock: Clock) -> dict:
    """One value per slot: cells off, mean forecast, completed / requests / offers / spend, window flag."""
    rows = {"slot": [], "t_start": [], "hour": [], "in_window": [], "cells_off": [], "s_hat_mean": [],
            "n_completed": [], "n_requests": [], "n_offers": [], "n_sessions": [], "voucher_spent_vnd": [],
            "slack_mean": [], "mean_pickup_eta_min": [], "n_abandoned": [], "n_cancelled": []}
    for rec in trace.slots:
        t0 = rec["t_start"]
        rows["slot"].append(rec["slot"])
        rows["t_start"].append(t0)
        rows["hour"].append(clock.hour_of(t0))
        rows["in_window"].append(bool(clock.window_start_s <= t0 < clock.window_end_s))
        rows["cells_off"].append(int((~rec["promo_on"]).sum()))
        s = rec["s_hat"].astype(np.float64)
        rows["s_hat_mean"].append(_f(np.nanmean(np.minimum(s, 10.0))) if np.isfinite(s).any() else None)
        if rec["published"]:
            rows["n_completed"].append(int(rec["n_completed"].sum()))
            rows["n_requests"].append(int(rec["n_requests"].sum()))
            rows["n_offers"].append(int(rec["n_offers"].sum()))
            rows["n_sessions"].append(int(rec["n_sessions"].sum()))
            rows["n_abandoned"].append(int(rec["n_abandoned"].sum()))
            rows["n_cancelled"].append(int(rec["n_cancelled"].sum()))
            rows["voucher_spent_vnd"].append(_f(rec["voucher_spent_vnd"].sum()))
            sl = rec["slack"].astype(np.float64)
            fin = np.isfinite(sl)
            rows["slack_mean"].append(_f(np.minimum(sl[fin], 10.0).mean()) if fin.any() else None)
            eta = rec["mean_pickup_eta_min"].astype(np.float64)
            rows["mean_pickup_eta_min"].append(_f(np.nanmean(eta)) if np.isfinite(eta).any() else None)
        else:
            for k in ("n_completed", "n_requests", "n_offers", "n_sessions", "n_abandoned", "n_cancelled",
                      "voucher_spent_vnd", "slack_mean", "mean_pickup_eta_min"):
                rows[k].append(None)
    return rows


def switch_events(trace: Trace, clock: Clock, theta: float | None) -> list[dict]:
    """Every (slot, cell) where the promotion state changed from the previous slot."""
    out = []
    prev = None
    for rec in trace.slots:
        on = rec["promo_on"]
        if prev is not None:
            for c in np.flatnonzero(on != prev):
                s_hat = float(rec["s_hat"][c])
                out.append({"slot": rec["slot"], "t_s": rec["t_start"], "hour": clock.hour_of(rec["t_start"]),
                            "cell": int(c), "to_on": bool(on[c]), "s_hat": _f(s_hat),
                            "theta": _f(theta) if theta is not None else None})
        prev = on
    return out


def cell_matrix(trace: Trace) -> dict:
    """Per-slot arrays for the heatmap: ``promo_on``, ``s_hat``, ``slack``, ``idle_avg``, ``enroute_avg``."""
    slots = trace.slots
    n = len(slots)
    nan_row = [None] * trace.n_cells
    return {
        "slots": [rec["slot"] for rec in slots],
        "t_start": [rec["t_start"] for rec in slots],
        "promo_on": [rec["promo_on"].astype(int).tolist() for rec in slots],
        "s_hat": [_list(rec["s_hat"]) for rec in slots],
        "slack": [_list(rec["slack"]) if rec["published"] else nan_row for rec in slots],
        "idle_avg": [_list(rec["idle_avg"]) if rec["published"] else nan_row for rec in slots],
        "enroute_avg": [_list(rec["enroute_avg"]) if rec["published"] else nan_row for rec in slots],
        "waiting_avg": [_list(rec["waiting_avg"]) if rec["published"] else nan_row for rec in slots],
        "n_offers": [rec["n_offers"].tolist() if rec["published"] else nan_row for rec in slots],
        "n_completed": [rec["n_completed"].tolist() if rec["published"] else nan_row for rec in slots],
        "n_requests": [rec["n_requests"].tolist() if rec["published"] else nan_row for rec in slots],
        "mechanism": [[MECHANISM_NAMES.get(int(m), "") for m in rec["mechanism"]] for rec in slots],
        "cluster_id": [rec["cluster_id"].tolist() for rec in slots],
        "n_slots": n,
    }


def distribution(result: RunResult, trace: Trace, clock: Clock, *, kappa: float | None,
                 x_segment: np.ndarray | None = None, n_bins: int = 30) -> dict:
    """Who got the vouchers: by hour, segment, mechanism, score bins; budget by period; outcome by arm.

    ``x_segment`` is the rider attribute (``world.riders.x_segment``), indexed by ``rider_id``.
    """
    s = result.sessions
    o = result.orders
    in_win = s.col("in_window")
    arm = s.col("arm") == 1
    blocked = s.col("budget_blocked")
    hour = s.col("hour")
    by_segment = []
    if x_segment is not None:
        seg = np.asarray(x_segment)[s.col("rider_id")]
        for k, label in enumerate(("thuong", "nhay_gia", "it_nhay_gia")):
            m = in_win & (seg == k)
            n = int(m.sum())
            by_segment.append({"segment": k, "label": label, "sessions": n, "offers": int((m & arm).sum()),
                               "offer_share": _f((m & arm).sum() / n) if n else None})
    by_hour = []
    for h in range(24):
        m = in_win & (hour == h)
        by_hour.append({"hour": h, "sessions": int(m.sum()), "offers": int((m & arm).sum()),
                        "blocked": int((m & blocked).sum()), "requests": int((m & s.col("requested")).sum())})
    mech = s.col("assign_mechanism")
    by_mech = []
    for code, name in MECHANISM_NAMES.items():
        m = in_win & (mech == int(code))
        if m.any():
            by_mech.append({"mechanism": name, "sessions": int(m.sum()), "offers": int((m & arm).sum())})
    score = s.col("score").astype(np.float64)
    on_cell = s.col("promo_on_cell")
    hist = None
    fin = in_win & np.isfinite(score)
    if fin.any():
        lo, hi = float(np.nanmin(score[fin])), float(np.nanmax(score[fin]))
        if hi > lo:
            edges = np.linspace(lo, hi, n_bins + 1)
            all_counts, _ = np.histogram(score[fin], bins=edges)
            off_counts, _ = np.histogram(score[fin & arm], bins=edges)
            hist = {"edges": _list(edges), "all": all_counts.tolist(), "offered": off_counts.tolist(),
                    "kappa": _f(kappa) if kappa is not None else None,
                    "share_on_cell": _f(on_cell[in_win].mean())}
    # Outcome by arm (descriptive: the arms are not randomized under most policies).
    order_idx = s.col("order_idx")
    status = o.col("status")
    completed = np.zeros(s.n, dtype=bool)
    has = order_idx >= 0
    completed[has] = status[order_idx[has]] == int(OrderStatus.COMPLETED)
    by_arm = []
    for a, label in ((1, "voucher"), (0, "no_voucher")):
        m = in_win & ((s.col("arm") == 1) == (a == 1))
        n = int(m.sum())
        by_arm.append({"arm": label, "sessions": n, "requested": int((m & s.col("requested")).sum()),
                       "completed": int((m & completed).sum()),
                       "request_rate": _f((m & s.col("requested")).sum() / n) if n else None,
                       "completion_rate": _f((m & completed).sum() / n) if n else None})
    frames = trace.frame_arrays() if trace.n_frames else None
    periods = budget_period_table(frames["ledger"], clock.n_periods) if frames is not None else []
    ledger_curve = None
    if frames is not None:
        led = frames["ledger"]
        ledger_curve = {"t": _list(frames["t"]), "period": led[:, 0].tolist(),
                        "used_vnd": _list((led[:, 1] + led[:, 2] + led[:, 3]) / DONG_PER_VND),
                        "spent_vnd": _list(led[:, 1] / DONG_PER_VND), "committed_vnd": _list(led[:, 2] / DONG_PER_VND),
                        "reserved_vnd": _list(led[:, 3] / DONG_PER_VND),
                        "limit_vnd": [None if v < 0 else v / DONG_PER_VND for v in led[:, 4]]}
    voucher = s.col("voucher_value_vnd").astype(np.float64)
    return {
        "by_hour": by_hour, "by_mechanism": by_mech, "by_segment": by_segment, "score_hist": hist, "by_arm": by_arm,
        "budget_periods": periods, "ledger_curve": ledger_curve,
        "n_offers": int((in_win & arm).sum()), "n_blocked": int((in_win & blocked).sum()),
        "n_sessions": int(in_win.sum()), "mean_voucher_vnd": _f(voucher[in_win & arm].mean()) if (in_win & arm).any() else None,
        "voucher_pct_of_fare": None,
    }
