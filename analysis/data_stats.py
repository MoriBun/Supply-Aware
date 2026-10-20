"""Descriptive statistics and figures of a 28-day history data set, W0 next to W1 (guide
``huong_dan_thong_ke_du_lieu.md``: the "Dữ liệu sinh ra" slides and section 4.1.2 of the report).

Reads only ``observed/``, ``market/`` and ``meta/`` of a ``generate`` run directory (docs/schema.md),
never ``hidden/``. Every quantity is taken over the evaluation window (``in_window``: the 60-minute
warm-up is dropped); "per day" divides by the number of window days (28 for ``runs/s8/*/legacy_28d``).

Day of an order: ``floor((request_time_s - window_start_s) / 86400)``. The window starts at 1:00 of
calendar day 0, so window day k runs from 1:00 of day k to 1:00 of day k + 1 and every window day has
24 hours (calendar days would leave day 0 with 23 hours and a one-hour day 28). Shocks are logged per
calendar day (``meta/shocks.day``); rain and events of calendar day k fall in window day k.

(cell, slot) of the window: the slot **starts** inside the window, the filter of the G8 check
(``analysis.calibrate.check_metrics``); 61 cells x 28 x 96 = 163.968 per world.

CLI::

    python -m analysis.data_stats --w0 runs/s8/w0/legacy_28d --w1 runs/s8/w1/legacy_28d --out docs/figures

writes ``07_du_lieu_thong_ke.csv`` (raw numbers, columns ``chi_tieu, w0, w1``), the ``07_du_lieu_*.png``
figures, and prints the rounded markdown table for ``docs/datasets.md``.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml

from analysis.io import OBSERVED_TABLES
from analysis.plots import INK, INK_2, MUTED, SERIES, SURFACE, _pyplot, save_figure, vn
from sim.space import n_cells_for_radius

DAY_S = 86400.0
COMPLETED, CANCELLED, ABANDONED = "Completed", "Cancelled", "Abandoned"
TIGHT_SLACK = 0.35          # (cell, slot) "căng": slack < 0.35 (CAL target, G8, H-14)
SURPLUS_SLACK = 1.0         # (cell, slot) "dư": slack > 1 (inf counts as surplus)
LEGACY_MECHANISMS = ("legacy_rule", "legacy_eps")
EXPLORE = "explore"
VENUE_NAMES = ("Sân vận động", "Phố đi bộ")   # rows of shocks.events.venues in config order
ORANGE, AQUA = "#eb6834", "#1baf7a"            # categorical slots 2 and 3 (analysis.figures.SLOT)
BLUE_RAMP = ("#86b6ef", "#2a78d6", "#104281")  # sequential blue, steps 250 / 450 / 650
FIGURE_DPI = 220
STYLE = {"font.size": 11, "axes.labelsize": 11, "xtick.labelsize": 10, "ytick.labelsize": 10,
         "legend.fontsize": 10, "legend.frameon": False}

SESSION_COLUMNS = ["rider_id", "hour", "pu_cell", "requested", "arm", "budget_blocked", "assign_mechanism",
                   "in_window"]
ORDER_COLUMNS = ["rider_id", "status", "request_time_s", "matched_time_s", "pickup_eta_min", "gross_fare_vnd",
                 "trip_km", "trip_time_min", "voucher_value_vnd", "in_window"]
SNAPSHOT_COLUMNS = ["cell", "slot", "hour", "slack"]

# (key, label) of every number of the table; the first two characters are the indicator (1-17) of the guide.
METRICS: tuple[tuple[str, str], ...] = (
    ("01a_so_o", "Số ô"),
    ("01b_so_khach", "Số khách"),
    ("01c_so_xe", "Số xe (supply.fleet_size)"),
    ("02a_khach_co_phien", "Khách có ít nhất 1 phiên trong cửa sổ"),
    ("02b_ty_le_khach_co_phien", "Tỷ lệ khách có ít nhất 1 phiên"),
    ("03a_phien", "Phiên trong cửa sổ"),
    ("03b_phien_moi_ngay", "Phiên mỗi ngày"),
    ("04a_luot_dat", "Lượt đặt trong cửa sổ"),
    ("04b_luot_dat_moi_ngay", "Lượt đặt mỗi ngày"),
    ("04c_ty_le_dat", "Tỷ lệ đặt (lượt đặt / phiên)"),
    ("05a_chuyen_hoan_thanh", "Chuyến hoàn thành trong cửa sổ (N)"),
    ("05b_chuyen_hoan_thanh_moi_ngay", "Chuyến hoàn thành mỗi ngày"),
    ("05c_chuyen_ngay_min", "Chuyến hoàn thành của ngày thấp nhất"),
    ("05d_chuyen_ngay_max", "Chuyến hoàn thành của ngày cao nhất"),
    ("06a_ty_le_hoan_thanh", "Tỷ lệ lượt đặt hoàn thành"),
    ("06b_ty_le_huy_xe_dang_toi", "Tỷ lệ lượt đặt hủy khi xe đang tới"),
    ("06c_ty_le_bo_cho", "Tỷ lệ lượt đặt bỏ chờ (chưa ghép)"),
    ("07a_ty_le_phat_voucher", "Tỷ lệ phiên được phát voucher"),
    ("07b_ty_le_chan_ngan_sach", "Tỷ lệ phiên bị chặn vì hết ngân sách"),
    ("08_ty_le_lat_ngau_nhien", "Tỷ lệ phiên thuộc lát phát ngẫu nhiên"),
    ("09a_voucher_moi_ngay_vnd", "Voucher đã chi mỗi ngày (đ)"),
    ("09b_ngan_sach_B_vnd", "Ngân sách B mỗi ngày (đ)"),
    ("09c_voucher_tren_B", "Voucher đã chi / B"),
    ("10a_gia_tb_vnd", "Giá cước TB (đ)"),
    ("10b_gia_trung_vi_vnd", "Giá cước trung vị (đ)"),
    ("10c_gia_p90_vnd", "Giá cước p90 (đ)"),
    ("10d_km_tb", "Quãng đường TB (km)"),
    ("10e_km_trung_vi", "Quãng đường trung vị (km)"),
    ("10f_km_p90", "Quãng đường p90 (km)"),
    ("10g_phut_tb", "Thời gian chuyến TB (phút)"),
    ("10h_phut_trung_vi", "Thời gian chuyến trung vị (phút)"),
    ("10i_phut_p90", "Thời gian chuyến p90 (phút)"),
    ("11a_eta_don_tb", "ETA đón TB (phút)"),
    ("11b_eta_don_trung_vi", "ETA đón trung vị (phút)"),
    ("11c_eta_don_p90", "ETA đón p90 (phút)"),
    ("12a_ty_le_cham_tran", "Tỷ lệ chuyến có voucher chạm trần"),
    ("12b_ty_le_chuyen_co_voucher", "Tỷ lệ chuyến hoàn thành có voucher"),
    ("13_chuyen_moi_xe_ngay", "Chuyến hoàn thành / xe / ngày"),
    ("14a_so_o_slot", "Số (ô, slot) trong cửa sổ"),
    ("14b_ty_le_cang", "Tỷ lệ (ô, slot) căng (slack < 0,35)"),
    ("14c_ty_le_du", "Tỷ lệ (ô, slot) dư (slack > 1)"),
    ("15a_chuyen_moi_khach_tb", "Chuyến / khách / cửa sổ: TB"),
    ("15b_chuyen_moi_khach_trung_vi", "Chuyến / khách / cửa sổ: trung vị"),
    ("15c_chuyen_moi_khach_p90", "Chuyến / khách / cửa sổ: p90"),
    ("16a_ngay_mua", "Số ngày mưa"),
    ("16b_su_kien", "Số sự kiện"),
    ("16c_su_kien_san_van_dong", "Số sự kiện ở sân vận động"),
    ("16d_xe_nghi_min", "Xe nghỉ / ngày: thấp nhất"),
    ("16e_xe_nghi_max", "Xe nghỉ / ngày: cao nhất"),
    ("16f_cau_ngay_min", "Hệ số cầu ngày: thấp nhất"),
    ("16g_cau_ngay_max", "Hệ số cầu ngày: cao nhất"),
    ("17a_khach_dong", "observed/riders: số dòng"),
    ("17b_khach_cot", "observed/riders: số cột"),
    ("17c_phien_dong", "observed/sessions: số dòng"),
    ("17d_phien_cot", "observed/sessions: số cột"),
    ("17e_luot_dat_dong", "observed/orders: số dòng"),
    ("17f_luot_dat_cot", "observed/orders: số cột"),
    ("17g_o_slot_dong", "market/slot_snapshots: số dòng"),
    ("17h_o_slot_cot", "market/slot_snapshots: số cột"),
)
METRIC_KEYS = tuple(k for k, _ in METRICS)
INDICATORS = tuple(range(1, 18))


def indicator_of(key: str) -> int:
    return int(key[:2])


# ---------------------------------------------------------------------------
# Loading (observed/, market/, meta/ only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dataset:
    """The window part of one ``generate`` run directory, with the config values the statistics need."""
    run_dir: Path
    window_start_s: float
    window_end_s: float
    n_days: float
    budget_vnd: float
    fleet_size: int
    n_cells: int
    grid_radius: int
    slot_s: float
    voucher_pct: float
    voucher_cap_vnd: float
    venue_mults: tuple[float, ...]   # cell demand multiplier of each venue type (shocks.events.venues)
    rider_ids: np.ndarray            # every rider of observed/riders
    sessions: pd.DataFrame           # in-window sessions
    orders: pd.DataFrame             # in-window orders
    snapshots: pd.DataFrame          # (cell, slot) whose slot starts in the window
    shocks: pd.DataFrame             # shocks of the window days; empty when shocks are off
    shapes: dict[str, tuple[int, int]]   # (rows, columns) on disk of the four observed tables
    n_completed_results: int | None  # N_completed of results/policy_results, if present

    @property
    def n_window_days(self) -> int:
        return int(round(self.n_days))


def _read(path: Path, columns: list[str]) -> pd.DataFrame:
    return pq.read_table(path, columns=columns).to_pandas(strings_to_categorical=True)


def load_dataset(run_dir: Path | str) -> Dataset:
    """Read the window of one run directory written by ``python -m sim run --mode generate``."""
    run_dir = Path(run_dir)
    meta = pd.read_parquet(run_dir / "meta" / "run_metadata.parquet")
    if len(meta) != 1:
        raise ValueError(f"{run_dir}: expected one run (generate), got {len(meta)} metadata rows")
    m = meta.iloc[0]
    cfg = yaml.safe_load(m["config_yaml"])
    ws, we = float(m["window_start_s"]), float(m["window_end_s"])
    n_days = (we - ws) / DAY_S
    slot_s = float(cfg["time"]["slot_min"]) * 60.0
    radius = int(cfg["space"]["grid_radius"])

    s = _read(run_dir / "observed" / "sessions.parquet", SESSION_COLUMNS)
    o = _read(run_dir / "observed" / "orders.parquet", ORDER_COLUMNS)
    snaps = _read(run_dir / "market" / "slot_snapshots.parquet", SNAPSHOT_COLUMNS)
    start = snaps["slot"].to_numpy(np.float64) * slot_s
    snaps = snaps[(start >= ws) & (start < we)].reset_index(drop=True)
    shocks_path = run_dir / "meta" / "shocks.parquet"
    shocks = pd.read_parquet(shocks_path) if shocks_path.exists() else pd.DataFrame(
        columns=["day", "kind", "cell", "demand_mult", "n_drivers_off"])
    shocks = shocks[shocks["day"].astype(int) < int(round(n_days))].reset_index(drop=True)
    riders = pq.read_table(run_dir / "observed" / "riders.parquet", columns=["rider_id"]).column(0).to_numpy()
    shapes = {}
    for name in OBSERVED_TABLES:
        md = pq.read_metadata(run_dir / f"{name}.parquet")
        shapes[name] = (md.num_rows, md.num_columns)
    results_path = run_dir / "results" / "policy_results.parquet"
    n_results = int(pd.read_parquet(results_path, columns=["N_completed"])["N_completed"].iloc[0]) \
        if results_path.exists() else None
    voucher = cfg["voucher"]
    cap = voucher.get("max_vnd")
    venues = cfg.get("shocks", {}).get("events", {}).get("venues", []) or []
    return Dataset(
        run_dir=run_dir, window_start_s=ws, window_end_s=we, n_days=n_days,
        budget_vnd=float(m["budget_B_vnd"]), fleet_size=int(cfg["supply"]["fleet_size"]),
        n_cells=n_cells_for_radius(radius), grid_radius=radius, slot_s=slot_s,
        voucher_pct=float(voucher["pct_of_fare"]), voucher_cap_vnd=float("nan") if cap is None else float(cap),
        venue_mults=tuple(float(v[5]) for v in venues),
        rider_ids=riders, sessions=s[s["in_window"]].reset_index(drop=True),
        orders=o[o["in_window"]].reset_index(drop=True), snapshots=snaps, shocks=shocks, shapes=shapes,
        n_completed_results=n_results,
    )


# ---------------------------------------------------------------------------
# Tables behind the figures
# ---------------------------------------------------------------------------


def _completed(data: Dataset) -> pd.DataFrame:
    return data.orders[data.orders["status"] == COMPLETED]


def order_day(data: Dataset, orders: pd.DataFrame) -> np.ndarray:
    """Window day (0-based) of each order, from its request time."""
    return np.floor((orders["request_time_s"].to_numpy(np.float64) - data.window_start_s) / DAY_S).astype(np.int64)


def hourly_profile(data: Dataset) -> pd.DataFrame:
    """Mean day by clock hour: sessions, requests and completed trips (by request hour) per day, and the
    share of (cell, slot) with slack < 0.35. Each clock hour occurs exactly ``n_days`` times in the window."""
    o = data.orders
    hour_o = (o["request_time_s"].to_numpy(np.float64) % DAY_S // 3600.0).astype(np.int64)
    done = (o["status"] == COMPLETED).to_numpy()
    snaps = data.snapshots
    tight = (snaps["slack"].to_numpy(np.float64) < TIGHT_SLACK)
    tight_h = pd.Series(tight).groupby(snaps["hour"].to_numpy(np.int64)).mean().reindex(range(24))
    return pd.DataFrame({
        "hour": np.arange(24),
        "sessions": np.bincount(data.sessions["hour"].to_numpy(np.int64), minlength=24)[:24] / data.n_days,
        "requests": np.bincount(hour_o, minlength=24)[:24] / data.n_days,
        "completed": np.bincount(hour_o[done], minlength=24)[:24] / data.n_days,
        "tight_share": tight_h.to_numpy(),
    })


def day_types(data: Dataset) -> list[str]:
    """Kind of each window day from ``meta/shocks``: none / event / rain / rain_event."""
    sh = data.shocks
    rain = set(sh.loc[sh["kind"] == "rain", "day"].astype(int))
    event = set(sh.loc[sh["kind"] == "event", "day"].astype(int))
    out = []
    for d in range(data.n_window_days):
        out.append({(False, False): "none", (False, True): "event", (True, False): "rain",
                    (True, True): "rain_event"}[(d in rain, d in event)])
    return out


def daily_completed(data: Dataset) -> pd.DataFrame:
    """Completed trips per window day (by request time) with the kind of day."""
    c = _completed(data)
    counts = np.bincount(order_day(data, c), minlength=data.n_window_days)
    if len(counts) != data.n_window_days:
        raise ValueError(f"{data.run_dir}: completed orders outside the {data.n_window_days} window days")
    return pd.DataFrame({"day": np.arange(1, data.n_window_days + 1), "completed": counts,
                         "kind": day_types(data)})


def sessions_per_cell(data: Dataset) -> np.ndarray:
    """Sessions per day by pickup cell, ``[n_cells]``."""
    return np.bincount(data.sessions["pu_cell"].to_numpy(np.int64), minlength=data.n_cells)[:data.n_cells] / data.n_days


def venue_kind(data: Dataset, demand_mult: float) -> int:
    """Index of the venue type in ``shocks.events.venues`` whose cell multiplier is ``demand_mult``."""
    for i, mult in enumerate(data.venue_mults):
        if math.isclose(mult, demand_mult, rel_tol=1e-6):
            return i
    raise ValueError(f"event with demand_mult {demand_mult} matches no venue type of the config")


def event_venues(data: Dataset) -> dict[int, int]:
    """``{cell: venue type}`` of the cells that held an event in the window (venues are drawn per world_seed)."""
    ev = data.shocks[data.shocks["kind"] == "event"]
    return {int(c): venue_kind(data, float(m)) for c, m in zip(ev["cell"], ev["demand_mult"])}


def booking_by_mechanism(data: Dataset) -> pd.DataFrame:
    """Booking rate with and without voucher for sessions decided by the legacy rule and for the random slice."""
    s = data.sessions
    group = np.where(s["assign_mechanism"].isin(LEGACY_MECHANISMS), "legacy",
                     np.where(s["assign_mechanism"] == EXPLORE, "explore", "other"))
    t = pd.DataFrame({"group": group, "arm": s["arm"].to_numpy(np.int64), "requested": s["requested"].to_numpy(bool)})
    t = t[t["group"] != "other"]
    out = t.groupby(["group", "arm"])["requested"].agg(["mean", "size"]).reset_index()
    return out.rename(columns={"mean": "booking_rate", "size": "sessions"})


# ---------------------------------------------------------------------------
# The 17 indicators
# ---------------------------------------------------------------------------


def _stats(x: np.ndarray) -> tuple[float, float, float]:
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return (float("nan"),) * 3
    return float(x.mean()), float(np.median(x)), float(np.quantile(x, 0.9))


def describe(data: Dataset) -> dict[str, float]:
    """Every number of the descriptive table (``METRICS``), raw (not rounded)."""
    s, o = data.sessions, data.orders
    c = _completed(data)
    n_days = data.n_days
    n_orders = len(o)
    n_done = len(c)
    status = o["status"]
    daily = daily_completed(data)["completed"].to_numpy()
    vouchered = c[c["voucher_value_vnd"].to_numpy(np.float64) > 0]
    matched = o[o["matched_time_s"].notna() & o["pickup_eta_min"].notna()]
    slack = data.snapshots["slack"].to_numpy(np.float64)
    trips = pd.Series(np.zeros(len(data.rider_ids)), index=data.rider_ids)
    per_rider = c.groupby("rider_id", observed=True).size()
    trips.loc[per_rider.index] = per_rider.to_numpy()
    fare, km, minutes = (_stats(c[col].to_numpy(np.float64)) for col in ("gross_fare_vnd", "trip_km", "trip_time_min"))
    eta = _stats(matched["pickup_eta_min"].to_numpy(np.float64))
    trips_s = _stats(trips.to_numpy())
    spent_day = float(c["voucher_value_vnd"].to_numpy(np.float64).sum()) / n_days

    sh = data.shocks
    has_shocks = len(sh) > 0
    ev = sh[sh["kind"] == "event"]
    att = sh.loc[sh["kind"] == "attendance", "n_drivers_off"].to_numpy(np.float64)
    mult = sh.loc[sh["kind"] == "day_demand", "demand_mult"].to_numpy(np.float64)
    nan = float("nan")
    stadium = sum(1 for m in ev["demand_mult"] if venue_kind(data, float(m)) == 0) if has_shocks else nan

    def shape(name: str, i: int) -> float:
        return float(data.shapes[name][i])

    out = {
        "01a_so_o": data.n_cells, "01b_so_khach": len(data.rider_ids), "01c_so_xe": data.fleet_size,
        "02a_khach_co_phien": s["rider_id"].nunique(),
        "02b_ty_le_khach_co_phien": s["rider_id"].nunique() / len(data.rider_ids),
        "03a_phien": len(s), "03b_phien_moi_ngay": len(s) / n_days,
        "04a_luot_dat": int(s["requested"].sum()), "04b_luot_dat_moi_ngay": int(s["requested"].sum()) / n_days,
        "04c_ty_le_dat": float(s["requested"].mean()),
        "05a_chuyen_hoan_thanh": n_done, "05b_chuyen_hoan_thanh_moi_ngay": n_done / n_days,
        "05c_chuyen_ngay_min": float(daily.min()), "05d_chuyen_ngay_max": float(daily.max()),
        "06a_ty_le_hoan_thanh": float((status == COMPLETED).sum()) / n_orders,
        "06b_ty_le_huy_xe_dang_toi": float((status == CANCELLED).sum()) / n_orders,
        "06c_ty_le_bo_cho": float((status == ABANDONED).sum()) / n_orders,
        "07a_ty_le_phat_voucher": float(s["arm"].mean()), "07b_ty_le_chan_ngan_sach": float(s["budget_blocked"].mean()),
        "08_ty_le_lat_ngau_nhien": float((s["assign_mechanism"] == EXPLORE).mean()),
        "09a_voucher_moi_ngay_vnd": spent_day, "09b_ngan_sach_B_vnd": data.budget_vnd,
        "09c_voucher_tren_B": spent_day / data.budget_vnd if data.budget_vnd > 0 else nan,
        "10a_gia_tb_vnd": fare[0], "10b_gia_trung_vi_vnd": fare[1], "10c_gia_p90_vnd": fare[2],
        "10d_km_tb": km[0], "10e_km_trung_vi": km[1], "10f_km_p90": km[2],
        "10g_phut_tb": minutes[0], "10h_phut_trung_vi": minutes[1], "10i_phut_p90": minutes[2],
        "11a_eta_don_tb": eta[0], "11b_eta_don_trung_vi": eta[1], "11c_eta_don_p90": eta[2],
        "12a_ty_le_cham_tran": float((vouchered["voucher_value_vnd"].to_numpy(np.float64)
                                      >= data.voucher_cap_vnd).mean()) if len(vouchered) else nan,
        "12b_ty_le_chuyen_co_voucher": len(vouchered) / n_done if n_done else nan,
        "13_chuyen_moi_xe_ngay": n_done / n_days / data.fleet_size,
        "14a_so_o_slot": slack.size, "14b_ty_le_cang": float((slack < TIGHT_SLACK).mean()),
        "14c_ty_le_du": float((slack > SURPLUS_SLACK).mean()),
        "15a_chuyen_moi_khach_tb": trips_s[0], "15b_chuyen_moi_khach_trung_vi": trips_s[1],
        "15c_chuyen_moi_khach_p90": trips_s[2],
        "16a_ngay_mua": float(sh.loc[sh["kind"] == "rain", "day"].nunique()) if has_shocks else nan,
        "16b_su_kien": float(len(ev)) if has_shocks else nan, "16c_su_kien_san_van_dong": stadium,
        "16d_xe_nghi_min": float(att.min()) if att.size else nan, "16e_xe_nghi_max": float(att.max()) if att.size else nan,
        "16f_cau_ngay_min": float(mult.min()) if mult.size else nan,
        "16g_cau_ngay_max": float(mult.max()) if mult.size else nan,
        "17a_khach_dong": shape("observed/riders", 0), "17b_khach_cot": shape("observed/riders", 1),
        "17c_phien_dong": shape("observed/sessions", 0), "17d_phien_cot": shape("observed/sessions", 1),
        "17e_luot_dat_dong": shape("observed/orders", 0), "17f_luot_dat_cot": shape("observed/orders", 1),
        "17g_o_slot_dong": shape("market/slot_snapshots", 0), "17h_o_slot_cot": shape("market/slot_snapshots", 1),
    }
    assert tuple(out) == METRIC_KEYS, "describe() and METRICS list different numbers"
    return {k: float(v) for k, v in out.items()}


def describe_dataset(run_dir: Path | str) -> dict[str, float]:
    """``describe(load_dataset(run_dir))``: the descriptive table of one run directory."""
    return describe(load_dataset(run_dir))


def stats_frame(w0: dict[str, float], w1: dict[str, float]) -> pd.DataFrame:
    """CSV layout of the guide: ``chi_tieu, w0, w1``, raw numbers."""
    return pd.DataFrame({"chi_tieu": list(METRIC_KEYS), "w0": [w0[k] for k in METRIC_KEYS],
                         "w1": [w1[k] for k in METRIC_KEYS]})


# ---------------------------------------------------------------------------
# Markdown table (rounded, Vietnamese notation)
# ---------------------------------------------------------------------------


def _pct(x: float, digits: int = 1) -> str:
    return "–" if not np.isfinite(x) else f"{vn(100.0 * x, digits)}%"


def _num(x: float, digits: int = 0) -> str:
    return "–" if not np.isfinite(x) else vn(x, digits)


def _three(d: dict, keys: tuple[str, str, str], digits: int, unit: str = "") -> str:
    a, b, c = (_num(d[k], digits) for k in keys)
    return f"TB {a} · trung vị {b} · p90 {c}{unit}"


MARKDOWN_ROWS = (
    (1, "Số ô · số khách · số xe", lambda d: f"{_num(d['01a_so_o'])} · {_num(d['01b_so_khach'])} · {_num(d['01c_so_xe'])}"),
    (2, "Khách có ít nhất 1 phiên trong cửa sổ", lambda d: f"{_num(d['02a_khach_co_phien'])} ({_pct(d['02b_ty_le_khach_co_phien'])})"),
    (3, "Phiên: mỗi ngày (tổng)", lambda d: f"{_num(d['03b_phien_moi_ngay'])} ({_num(d['03a_phien'])})"),
    (4, "Lượt đặt: mỗi ngày (tổng); tỷ lệ đặt",
     lambda d: f"{_num(d['04b_luot_dat_moi_ngay'])} ({_num(d['04a_luot_dat'])}); {_pct(d['04c_ty_le_dat'])}"),
    (5, "Chuyến hoàn thành: mỗi ngày (tổng = N); thấp nhất–cao nhất theo ngày",
     lambda d: f"{_num(d['05b_chuyen_hoan_thanh_moi_ngay'])} ({_num(d['05a_chuyen_hoan_thanh'])}); "
               f"{_num(d['05c_chuyen_ngay_min'])}–{_num(d['05d_chuyen_ngay_max'])}"),
    (6, "Lượt đặt: hoàn thành / hủy khi xe đang tới / bỏ chờ",
     lambda d: f"{_pct(d['06a_ty_le_hoan_thanh'])} / {_pct(d['06b_ty_le_huy_xe_dang_toi'])} / {_pct(d['06c_ty_le_bo_cho'], 2)}"),
    (7, "Phiên được phát voucher; bị chặn vì hết ngân sách",
     lambda d: f"{_pct(d['07a_ty_le_phat_voucher'])}; {_pct(d['07b_ty_le_chan_ngan_sach'])}"),
    (8, "Phiên thuộc lát phát ngẫu nhiên", lambda d: _pct(d["08_ty_le_lat_ngau_nhien"])),
    (9, "Voucher đã chi mỗi ngày (triệu đ); so với B",
     lambda d: f"{_num(d['09a_voucher_moi_ngay_vnd'] / 1e6, 1)} = {_pct(d['09c_voucher_tren_B'], 2)} B "
               f"(B = {_num(d['09b_ngan_sach_B_vnd'] / 1e6, 1)})"),
    (10, "Giá cước chuyến hoàn thành (đ)", lambda d: _three(d, ("10a_gia_tb_vnd", "10b_gia_trung_vi_vnd", "10c_gia_p90_vnd"), 0)),
    (10, "Quãng đường (km)", lambda d: _three(d, ("10d_km_tb", "10e_km_trung_vi", "10f_km_p90"), 2)),
    (10, "Thời gian chuyến (phút)", lambda d: _three(d, ("10g_phut_tb", "10h_phut_trung_vi", "10i_phut_p90"), 1)),
    (11, "ETA đón, order đã ghép (phút)", lambda d: _three(d, ("11a_eta_don_tb", "11b_eta_don_trung_vi", "11c_eta_don_p90"), 1)),
    (12, "Chuyến có voucher chạm trần 20.000đ (trong chuyến có voucher); chuyến có voucher",
     lambda d: f"{_pct(d['12a_ty_le_cham_tran'])}; {_pct(d['12b_ty_le_chuyen_co_voucher'])} chuyến hoàn thành"),
    (13, "Chuyến hoàn thành / xe / ngày", lambda d: _num(d["13_chuyen_moi_xe_ngay"], 1)),
    (14, "(ô, slot) trong cửa sổ; căng (slack < 0,35) · dư (slack > 1)",
     lambda d: f"{_num(d['14a_so_o_slot'])}; {_pct(d['14b_ty_le_cang'])} · {_pct(d['14c_ty_le_du'])}"),
    (15, "Chuyến hoàn thành / khách trong cửa sổ (gồm khách 0 chuyến)",
     lambda d: _three(d, ("15a_chuyen_moi_khach_tb", "15b_chuyen_moi_khach_trung_vi", "15c_chuyen_moi_khach_p90"), 1)),
    (16, "Biến động: ngày mưa · sự kiện (sân vận động) · xe nghỉ/ngày · hệ số cầu ngày",
     lambda d: "không bật" if not np.isfinite(d["16a_ngay_mua"]) else
     f"{_num(d['16a_ngay_mua'])} · {_num(d['16b_su_kien'])} ({_num(d['16c_su_kien_san_van_dong'])}) · "
     f"{_num(d['16d_xe_nghi_min'])}–{_num(d['16e_xe_nghi_max'])} · ×{_num(d['16f_cau_ngay_min'], 2)}–{_num(d['16g_cau_ngay_max'], 2)}"),
    (17, "observed/riders (dòng × cột)", lambda d: f"{_num(d['17a_khach_dong'])} × {_num(d['17b_khach_cot'])}"),
    (17, "observed/sessions", lambda d: f"{_num(d['17c_phien_dong'])} × {_num(d['17d_phien_cot'])}"),
    (17, "observed/orders", lambda d: f"{_num(d['17e_luot_dat_dong'])} × {_num(d['17f_luot_dat_cot'])}"),
    (17, "market/slot_snapshots", lambda d: f"{_num(d['17g_o_slot_dong'])} × {_num(d['17h_o_slot_cot'])}"),
)


def markdown_table(w0: dict[str, float], w1: dict[str, float]) -> str:
    lines = ["| # | Chỉ tiêu | W0 | W1 |", "|---|---|---|---|"]
    lines += [f"| {no} | {label} | {fmt(w0)} | {fmt(w1)} |" for no, label, fmt in MARKDOWN_ROWS]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------


def _panel_label(ax, label: str | None) -> None:
    if label:
        ax.text(-0.02, 1.04, label, transform=ax.transAxes, ha="right", va="bottom", fontsize=12,
                fontweight="bold", color=INK)


def _vn_axis(axis, digits: int = 0) -> None:
    from matplotlib.ticker import FuncFormatter
    axis.set_major_formatter(FuncFormatter(lambda v, _: vn(v, digits)))


def _line(ax, x, y, color, *, lw: float = 2.0, ls: str = "-", marker: str | None = "o") -> None:
    ax.plot(x, y, color=color, linewidth=lw, linestyle=ls, marker=marker, markersize=5,
            markeredgecolor=SURFACE, markeredgewidth=1.2, solid_capstyle="round")


def panel_theo_gio(ax, data: Dataset, *, label: str | None = None) -> list:
    """(a) Mean day by clock hour, in three rows sharing the hour axis (one y-scale per row, never two on
    one axis): sessions; requests and completed trips; share of (cell, slot) with slack < 0.35."""
    fig = ax.figure
    spec = ax.get_subplotspec()
    ax.remove()
    gs = spec.subgridspec(3, 1, height_ratios=[1.0, 1.15, 0.8], hspace=0.14)
    a1 = fig.add_subplot(gs[0])
    a2 = fig.add_subplot(gs[1], sharex=a1)
    a3 = fig.add_subplot(gs[2], sharex=a1)
    p = hourly_profile(data)
    h = p["hour"].to_numpy()

    _line(a1, h, p["sessions"] / 1e3, INK_2)
    a1.set_ylabel("Phiên/giờ\n(nghìn)")
    a1.set_ylim(0, None)
    _vn_axis(a1.yaxis)

    _line(a2, h, p["requests"], MUTED, lw=1.6, ls="--")
    _line(a2, h, p["completed"], SERIES)
    a2.set_ylabel("Mỗi giờ")
    a2.set_ylim(0, None)
    _vn_axis(a2.yaxis)
    a2.legend(["Lượt đặt", "Chuyến hoàn thành"], loc="lower right", ncols=2, handlelength=1.8)

    a3.fill_between(h, 100.0 * p["tight_share"], color=ORANGE, alpha=0.25, linewidth=0, step=None)
    a3.plot(h, 100.0 * p["tight_share"], color=ORANGE, linewidth=1.8)
    a3.set_ylabel("(ô, slot)\ncăng (%)")
    a3.set_ylim(0, None)
    a3.set_xlabel("Giờ trong ngày")
    a3.set_xticks(range(0, 24, 3))
    a3.set_xlim(-0.5, 23.5)
    for a in (a1, a2):
        a.tick_params(labelbottom=False)
    for a in (a1, a2, a3):
        a.yaxis.set_label_coords(-0.1, 0.5)
    _panel_label(a1, label)
    return [a1, a2, a3]


def _blue_cmap():
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list("seq_blue", BLUE_RAMP)


def panel_ban_do(ax, data: Dataset, *, venues: dict[int, int] | None = None, label: str | None = None) -> None:
    """(b) Sessions per day by pickup cell on the hex grid; ``venues`` marks the event cells of W1."""
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize
    from matplotlib.lines import Line2D

    from analysis.animation import hex_centres, hex_layer

    x, y = hex_centres(data.grid_radius)
    values = sessions_per_cell(data)
    cmap, norm = _blue_cmap(), Normalize(vmin=0.0, vmax=float(values.max()))
    for patch, v in zip(hex_layer(ax, x, y), values):
        patch.set_facecolor(cmap(norm(v)))
    cb = ax.figure.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=ax, fraction=0.045, pad=0.02)
    cb.set_label("Phiên/ô/ngày")
    cb.outline.set_visible(False)
    _vn_axis(cb.ax.yaxis)
    handles = []
    markers = ("^", "s", "D")
    for cell, kind in sorted((venues or {}).items()):
        ax.plot(x[cell], y[cell], marker=markers[kind % 3], markersize=10, color=ORANGE, markeredgecolor=INK,
                markeredgewidth=1.0, linestyle="none", zorder=5)
    for kind in sorted(set((venues or {}).values())):
        name = VENUE_NAMES[kind] if kind < len(VENUE_NAMES) else f"Địa điểm loại {kind + 1}"
        handles.append(Line2D([], [], marker=markers[kind % 3], markersize=9, color=ORANGE, markeredgecolor=INK,
                              linestyle="none", label=f"{name} (sự kiện W1)"))
    if handles:
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncols=len(handles),
                  handletextpad=0.3, columnspacing=1.2)
    _panel_label(ax, label)


def panel_gia_cuoc(ax, data: Dataset, *, label: str | None = None) -> None:
    """(c) Fare of completed trips (thousand dong, cut at p99); mean, and the fare from which the voucher
    hits its cap (``max_vnd / pct_of_fare`` of the config)."""
    fare = _completed(data)["gross_fare_vnd"].to_numpy(np.float64) / 1e3
    p99 = float(np.quantile(fare, 0.99))
    lo = math.floor(fare.min() / 2.5) * 2.5
    bins = np.arange(lo, p99 + 2.5, 2.5)
    weights = np.full(fare.size, 100.0 / fare.size)            # % of trips per bin
    ax.hist(fare[fare <= p99], bins=bins, weights=weights[fare <= p99], color=SERIES, edgecolor=SURFACE,
            linewidth=1.0)
    ymax = ax.get_ylim()[1]
    ax.set_ylim(0, ymax * 1.22)
    mean = float(fare.mean())
    ax.axvline(mean, color=INK, linewidth=1.6)
    ax.annotate(f"TB {vn(mean * 1e3)}đ", xy=(mean, ymax * 1.16), xytext=(-4, 0), textcoords="offset points",
                ha="right", va="center", fontsize=10, color=INK)
    if np.isfinite(data.voucher_cap_vnd):
        cap_fare = data.voucher_cap_vnd / data.voucher_pct / 1e3
        above = float((fare >= cap_fare).mean())
        ax.axvline(cap_fare, color=MUTED, linewidth=1.4, linestyle="--")
        ax.annotate(f"≥ {vn(cap_fare * 1e3)}đ: voucher chạm trần {vn(data.voucher_cap_vnd)}đ\n"
                    f"{vn(100 * above, 1)}% chuyến", xy=(cap_fare, ymax * 1.03), xytext=(6, 0),
                    textcoords="offset points", ha="left", va="center", fontsize=10, color=INK_2)
    ax.set_xlabel(f"Giá cước chuyến hoàn thành (nghìn đ, cắt ở p99 = {vn(p99, 0)})")
    ax.set_ylabel("% chuyến")
    _vn_axis(ax.yaxis, 1)
    _panel_label(ax, label)


DAY_KIND_STYLE = {   # kind -> (label, colour, marker, filled)
    "none": ("W1: không biến động", INK_2, "o", False),
    "event": ("W1: chỉ sự kiện", SERIES, "s", True),
    "rain": ("W1: chỉ mưa", ORANGE, "^", True),
    "rain_event": ("W1: mưa + sự kiện", AQUA, "D", True),
}


def panel_theo_ngay(ax, w0: Dataset, w1: Dataset, *, label: str | None = None) -> None:
    """(d) Completed trips per window day: W0 as a grey line, W1 as points by kind of day (meta/shocks)."""
    d0, d1 = daily_completed(w0), daily_completed(w1)
    ax.plot(d0["day"], d0["completed"], color=MUTED, linewidth=1.8, marker="o", markersize=4,
            markeredgecolor=SURFACE, markeredgewidth=0.8, label="W0", zorder=2)
    for kind, (name, color, marker, filled) in DAY_KIND_STYLE.items():
        m = d1["kind"] == kind
        if not m.any():
            continue
        ax.plot(d1.loc[m, "day"], d1.loc[m, "completed"], linestyle="none", marker=marker, markersize=8,
                color=color, markerfacecolor=color if filled else SURFACE, markeredgecolor=color if not filled else SURFACE,
                markeredgewidth=1.6 if not filled else 1.0, label=f"{name} ({int(m.sum())} ngày)", zorder=3)
    ax.set_xlabel("Ngày (tính từ 1h, đầu cửa sổ)")
    ax.set_ylabel("Chuyến hoàn thành/ngày")
    ax.set_xlim(0.3, max(len(d0), len(d1)) + 0.7)
    ax.set_xticks([1] + list(range(7, max(len(d0), len(d1)) + 1, 7)))
    _vn_axis(ax.yaxis)
    lo = min(d0["completed"].min(), d1["completed"].min())
    hi = max(d0["completed"].max(), d1["completed"].max())
    pad = 0.08 * (hi - lo if hi > lo else max(hi, 1))
    ax.set_ylim(lo - pad, hi + 3.2 * pad)
    ax.legend(loc="upper left", ncols=2, handletextpad=0.3, columnspacing=1.0, borderaxespad=0.2)
    _panel_label(ax, label)


def figure_quang_duong_eta(data: Dataset):
    """Optional: trip distance of completed trips and pickup ETA of matched orders, with their means."""
    plt = _pyplot()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8))
    km = _completed(data)["trip_km"].to_numpy(np.float64)
    eta = data.orders["pickup_eta_min"].dropna().to_numpy(np.float64)
    for ax, x, step, xlabel, unit, digits in ((a1, km, 0.5, "Quãng đường chuyến hoàn thành (km)", " km", 2),
                                              (a2, eta, 0.5, "ETA đón lúc ghép (phút)", " phút", 1)):
        p99 = float(np.quantile(x, 0.99))
        keep = x <= p99
        bins = np.arange(math.floor(x.min() / step) * step, p99 + step, step)
        ax.hist(x[keep], bins=bins, weights=np.full(int(keep.sum()), 100.0 / x.size), color=SERIES,
                edgecolor=SURFACE, linewidth=1.0)
        top = ax.get_ylim()[1]
        ax.set_ylim(0, top * 1.15)
        ax.axvline(x.mean(), color=INK, linewidth=1.6)
        ax.annotate(f"TB {vn(x.mean(), digits)}{unit}", xy=(x.mean(), top * 1.08), xytext=(6, 0),
                    textcoords="offset points", ha="left", va="center", fontsize=10, color=INK)
        ax.set_xlabel(f"{xlabel}, cắt ở p99 = {vn(p99, 1)}")
        ax.set_ylabel("%")
        _vn_axis(ax.yaxis, 1)
    _panel_label(a1, "(a)")
    _panel_label(a2, "(b)")
    fig.tight_layout()
    return fig


def figure_gay_nhieu(data: Dataset):
    """Optional: booking rate with and without voucher, sessions decided by the legacy rule against the
    random slice (observed columns only: ``arm``, ``requested``, ``assign_mechanism``)."""
    plt = _pyplot()
    t = booking_by_mechanism(data)
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    groups = (("legacy", "Luật cũ quyết định\n(có gây nhiễu)"), ("explore", "Lát phát ngẫu nhiên\n(không gây nhiễu)"))
    width = 0.36
    for j, (arm, name, color) in enumerate(((1, "Có voucher", SERIES), (0, "Không voucher", ORANGE))):
        xs, ys = [], []
        for i, (g, _) in enumerate(groups):
            row = t[(t["group"] == g) & (t["arm"] == arm)]
            xs.append(i + (j - 0.5) * width)
            ys.append(100.0 * float(row["booking_rate"].iloc[0]) if len(row) else float("nan"))
        ax.bar(xs, ys, width=width - 0.03, color=color, label=name)
        for xv, yv in zip(xs, ys):
            ax.annotate(f"{vn(yv, 1)}%", xy=(xv, yv), xytext=(0, 3), textcoords="offset points", ha="center",
                        va="bottom", fontsize=10, color=INK_2)
    top = ax.get_ylim()[1]
    for i, (g, _) in enumerate(groups):
        r = t[t["group"] == g].set_index("arm")["booking_rate"]
        if {0, 1} <= set(r.index):
            ax.annotate(f"chênh {vn(100 * (r[1] - r[0]), 1)} điểm", xy=(i, top * 1.08), ha="center", va="bottom",
                        fontsize=10, color=INK)
    ax.set_ylim(0, top * 1.22)
    ax.set_xticks(range(len(groups)), [name for _, name in groups])
    ax.set_ylabel("Tỷ lệ đặt (%)")
    _vn_axis(ax.yaxis, 0)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncols=2)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Figures and CLI
# ---------------------------------------------------------------------------


def figure_tong_quan(w0: Dataset, w1: Dataset):
    """Main 2 x 2 figure: (a) by hour, (b) map, (c) fares (W0), (d) by day (W0 and W1)."""
    plt = _pyplot()
    fig = plt.figure(figsize=(12, 8.6))
    gs = fig.add_gridspec(2, 2, hspace=0.38, wspace=0.26, left=0.08, right=0.98, top=0.95, bottom=0.08)
    panel_theo_gio(fig.add_subplot(gs[0, 0]), w0, label="(a)")
    panel_ban_do(fig.add_subplot(gs[0, 1]), w0, venues=event_venues(w1), label="(b)")
    panel_gia_cuoc(fig.add_subplot(gs[1, 0]), w0, label="(c)")
    panel_theo_ngay(fig.add_subplot(gs[1, 1]), w0, w1, label="(d)")
    return fig


def _single(panel, *args, figsize=(6.5, 4.4), **kwargs):
    plt = _pyplot()
    fig = plt.figure(figsize=figsize)
    ax = fig.add_subplot(1, 1, 1)
    panel(ax, *args, **kwargs)
    fig.subplots_adjust(left=0.15, right=0.97, top=0.95, bottom=0.15)
    return fig


def write_figures(w0: Dataset, w1: Dataset, out_dir: Path) -> list[Path]:
    """Every ``07_du_lieu_*.png`` of the guide (main 2 x 2, the four panels, the two optional figures)."""
    plt = _pyplot()
    paths = []
    with plt.rc_context(STYLE):
        figures = {
            "07_du_lieu_tong_quan": figure_tong_quan(w0, w1),
            "07_du_lieu_theo_gio": _single(panel_theo_gio, w0, figsize=(6.5, 5.2)),
            "07_du_lieu_ban_do": _single(panel_ban_do, w0, venues=event_venues(w1), figsize=(6.5, 5.6)),
            "07_du_lieu_gia_cuoc": _single(panel_gia_cuoc, w0),
            "07_du_lieu_theo_ngay": _single(panel_theo_ngay, w0, w1),
            "07_du_lieu_quang_duong_eta": figure_quang_duong_eta(w0),
            "07_du_lieu_gay_nhieu": figure_gay_nhieu(w0),
        }
        for name, fig in figures.items():
            paths.append(save_figure(fig, name, figures_dir=out_dir, dpi=FIGURE_DPI))
            plt.close(fig)
    return paths


def check_against_results(data: Dataset, stats: dict[str, float]) -> None:
    """Completed trips by hour, by day and in total all equal N of ``results/policy_results``."""
    n = stats["05a_chuyen_hoan_thanh"]
    by_hour = float(hourly_profile(data)["completed"].sum() * data.n_days)
    by_day = float(daily_completed(data)["completed"].sum())
    if not (math.isclose(by_hour, n, abs_tol=1e-6) and by_day == n):
        raise ValueError(f"{data.run_dir}: completed by hour {by_hour}, by day {by_day}, total {n} differ")
    if data.n_completed_results is not None and data.n_completed_results != int(n):
        raise ValueError(f"{data.run_dir}: {int(n)} completed in-window orders, policy_results says "
                         f"{data.n_completed_results}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m analysis.data_stats", description=__doc__.split("\n")[0])
    ap.add_argument("--w0", type=Path, required=True, help="history run directory of W0 (generate, legacy)")
    ap.add_argument("--w1", type=Path, required=True, help="history run directory of W1")
    ap.add_argument("--out", type=Path, default=None, help="figure directory (default docs/figures)")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    w0, w1 = load_dataset(args.w0), load_dataset(args.w1)
    s0, s1 = describe(w0), describe(w1)
    check_against_results(w0, s0)
    check_against_results(w1, s1)
    from analysis.plots import FIGURES_DIR
    out = args.out or FIGURES_DIR
    out.mkdir(parents=True, exist_ok=True)
    csv = out / "07_du_lieu_thong_ke.csv"
    stats_frame(s0, s1).to_csv(csv, index=False)
    paths = write_figures(w0, w1, out)
    if hasattr(sys.stdout, "reconfigure"):         # Windows console: print the Vietnamese table as UTF-8
        sys.stdout.reconfigure(encoding="utf-8")
    print(markdown_table(s0, s1))
    print()
    print(f"wrote {csv}")
    for p in paths:
        print(f"wrote {p}")
    print(f"{time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
