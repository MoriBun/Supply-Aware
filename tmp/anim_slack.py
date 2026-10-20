"""GIF: city-wide tension against the switch-off threshold, with the hex maps of the policy change.

Same three maps as ``analysis.animation`` (Δ requests, Δ completed by request slot, slack of the
new policy with the switched-off cells hatched). The panel below shows, for the display seed, the
forecast the city-scope policy actually reads in each slot (persistence: city slack of the previous
published slot, idle / en-route summed over every cell, rule H-14, capped at ``monitor.slack_cap``)
against the threshold θ. When the line drops below θ the whole city is switched off on the maps.

Usage (from the repository root)::

    .venv/Scripts/python.exe tmp/anim_slack.py \
        --old runs/anim_slack/w0_pair_full/runs/*-all_on-nan-* \
        --new runs/anim_slack/w0_pair_full/runs/*-threshold-1.6-* \
        --theta 1.6 --hours 5 10 --out img/07_anim_slack_w0.gif
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.animation import (ORANGE, SLACK_DISPLAY_CAP, _colormaps, check_totals, frame_slots,  # noqa: E402
                                hex_centres, hex_layer, load_pairs, slot_label, window_delta)
from analysis.io import load_run  # noqa: E402
from analysis.plots import INK, INK_2, MUTED, SERIES, _pyplot, vn  # noqa: E402

SLACK_CAP = 10.0      # monitor.slack_cap of config/default.yaml


def city_forecast(run_dir: Path, n_slots: int, cap: float = SLACK_CAP) -> np.ndarray:
    """ŝ read by the city-scope policy in each slot: city slack of the previous slot, capped (NaN before slot 1)."""
    snaps = load_run(run_dir)["slot_snapshots"]
    g = snaps.groupby("slot").agg(idle=("idle_avg", "sum"), enroute=("enroute_avg", "sum"))
    idle = np.full(n_slots, np.nan)
    enroute = np.full(n_slots, np.nan)
    keep = g.index.to_numpy() < n_slots
    idle[g.index.to_numpy()[keep]] = g["idle"].to_numpy()[keep]
    enroute[g.index.to_numpy()[keep]] = g["enroute"].to_numpy()[keep]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(enroute > 0, idle / enroute, np.where(idle > 0, np.inf, 0.0))
    ratio = np.where(np.isnan(idle) | np.isnan(enroute), np.nan, np.minimum(ratio, cap))
    forecast = np.full(n_slots, np.nan)
    forecast[1:] = ratio[:-1]                       # persistence: the slot k decision reads slot k - 1
    return forecast


def animate(pair, slots, s_hat, theta, out_path: Path, *, fps=1.0, hold_s=2.0, title=None, dpi=100) -> Path:
    from matplotlib.animation import FuncAnimation, PillowWriter
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize, TwoSlopeNorm

    plt = _pyplot()
    x, y = hex_centres(pair.new[0].radius)
    diverging, sequential = _colormaps()
    vmax = float(np.nanmax(np.abs(np.concatenate([pair.d_req[slots], pair.d_done[slots]])))) or 1.0
    div_norm = TwoSlopeNorm(vcenter=0.0, vmin=-vmax, vmax=vmax)
    seq_norm = Normalize(0.0, SLACK_DISPLAY_CAP)
    n_seeds = len(pair.seeds)
    seed = pair.seeds[pair.display]
    line = s_hat[slots]

    fig = plt.figure(figsize=(12.5, 7.6), dpi=dpi)
    gs = fig.add_gridspec(2, 3, height_ratios=[3.1, 1.5], hspace=0.28, wspace=0.08,
                          left=0.05, right=0.97, top=0.86, bottom=0.17)
    axes = [fig.add_subplot(gs[0, i]) for i in range(3)]
    ax_ts = fig.add_subplot(gs[1, :])
    layers = [
        (pair.d_req, diverging, div_norm, f"Δ lượt đặt (mới − cũ), trung bình {n_seeds} seed"),
        (pair.d_done, diverging, div_norm, f"Δ chuyến hoàn thành, trung bình {n_seeds} seed"),
        (np.minimum(pair.slack, SLACK_DISPLAY_CAP), sequential, seq_norm,
         f"độ căng từng ô (seed {seed}), gạch chéo = tắt voucher"),
    ]
    patches = []
    for ax, (_, _, _, name) in zip(axes, layers):
        patches.append(hex_layer(ax, x, y))
        ax.set_title(name, fontsize=9.5, color=INK)
    fig.colorbar(ScalarMappable(norm=div_norm, cmap=diverging), ax=axes[:2], orientation="horizontal",
                 fraction=0.04, pad=0.02, aspect=50, label="Δ số lượt mỗi ô mỗi slot")
    fig.colorbar(ScalarMappable(norm=seq_norm, cmap=sequential), ax=axes[2], orientation="horizontal",
                 fraction=0.04, pad=0.02, aspect=25, label=f"xe rảnh / xe đi đón, cắt ở {vn(SLACK_DISPLAY_CAP)}")

    # bottom panel: the forecast the policy reads, against θ
    fx = np.arange(len(slots))
    top = float(np.nanmax(line)) * 1.15 if np.isfinite(np.nanmax(line)) else theta * 2
    top = min(max(top, theta * 1.6), 6.0)
    ax_ts.axhspan(0, theta, color=ORANGE, alpha=0.10, linewidth=0)
    ax_ts.axhline(theta, color=ORANGE, linewidth=1.4, linestyle="--")
    ax_ts.text(len(fx) - 0.6, theta * 0.5, f"vùng tắt voucher: độ căng < ngưỡng {vn(theta, 1)}",
               ha="right", va="center", fontsize=9, color=ORANGE)
    ax_ts.plot(fx, line, color=MUTED, linewidth=1.2, alpha=0.6)
    below = line < theta
    ax_ts.scatter(fx[~below], line[~below], s=22, color=SERIES, zorder=3, label="bật voucher")
    ax_ts.scatter(fx[below], line[below], s=22, color=ORANGE, zorder=3, label="tắt voucher")
    (trace,) = ax_ts.plot([], [], color=INK_2, linewidth=2.0)
    (dot,) = ax_ts.plot([], [], "o", markersize=9, markerfacecolor="none", markeredgecolor=INK, markeredgewidth=1.6)
    cursor = ax_ts.axvline(0, color=INK_2, linewidth=0.8)
    step = max(1, len(fx) // 10)
    ax_ts.set_xticks(fx[::step])
    ax_ts.set_xticklabels([slot_label(pair, int(s)).split("–")[0] for s in slots[::step]])
    ax_ts.set_xlim(-0.5, len(fx) - 0.5)
    ax_ts.set_ylim(0, top)
    ax_ts.set_ylabel("độ căng toàn thành phố")
    ax_ts.set_title(f"Độ căng toàn thành phố mà chính sách đọc (slot trước, seed {seed})", fontsize=9.5,
                    color=INK, loc="left")
    ax_ts.legend(loc="upper right", fontsize=8.5, frameon=False, ncol=2)
    for side in ("top", "right"):
        ax_ts.spines[side].set_visible(False)

    head = fig.suptitle("", fontsize=12, color=INK, y=0.975)
    w = window_delta(pair)
    fig.text(0.5, 0.925, title or (f"Đề xuất (độ căng toàn thành phố, ngưỡng {vn(theta, 1)}) so với phát cho tất cả · "
                                   f"cùng ngân sách · {n_seeds} seed · cả ngày ΔN = {vn(w['mean'], 0)} chuyến"),
             ha="center", va="center", fontsize=9.5, color=INK_2)
    caption = fig.text(0.5, 0.045, "", ha="center", va="center", fontsize=10.5, color=INK)

    def draw(k: int):
        s = int(slots[k])
        for ps, (data, cmap, norm, _) in zip(patches, layers):
            colours = cmap(norm(np.nan_to_num(data[s], nan=0.0)))
            for c, p in enumerate(ps):
                p.set_facecolor(colours[c])
        off = pair.promo_off[s]
        for c, p in enumerate(patches[2]):
            p.set_hatch("///" if off[c] else None)
            p.set_edgecolor(INK_2 if off[c] else "white")
        trace.set_data(fx[: k + 1], line[: k + 1])
        dot.set_data([k], [line[k]])
        cursor.set_xdata([k, k])
        head.set_text(f"{slot_label(pair, s)} · khung {k + 1}/{len(slots)}")
        v = line[k]
        if np.isnan(v):
            state = "chưa có độ căng của slot trước: voucher bật"
        elif v < theta:
            state = f"độ căng {vn(v, 2)} < ngưỡng {vn(theta, 1)} → TẮT voucher ở {int(off.sum())}/{pair.n_cells} ô"
        else:
            state = f"độ căng {vn(v, 2)} ≥ ngưỡng {vn(theta, 1)} → BẬT voucher"
        dr, dd = float(pair.d_req[s].sum()), float(pair.d_done[s].sum())
        caption.set_text(f"{state}  ·  Δ lượt đặt {vn(dr, 1)}, Δ chuyến {vn(dd, 1)} trong slot")
        return []

    order = list(range(len(slots))) + [len(slots) - 1] * max(0, int(round(hold_s * fps)))
    anim = FuncAnimation(fig, draw, frames=order, blit=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    anim.save(str(out_path), writer=PillowWriter(fps=fps))
    plt.close(fig)
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="City tension vs θ with the hex maps of the policy change.")
    ap.add_argument("--old", nargs="+", required=True, type=Path)
    ap.add_argument("--new", nargs="+", required=True, type=Path)
    ap.add_argument("--theta", type=float, required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--hours", nargs=2, type=int, default=(5, 11))
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--display-seed", type=int, default=0)
    ap.add_argument("--frames-dir", type=Path, default=None, help="also save one PNG per frame here (for beamer)")
    ap.add_argument("--title", default=None)
    a = ap.parse_args(argv)

    pair = load_pairs(a.old, a.new, display_seed_index=a.display_seed)
    totals = check_totals(pair)
    if not (totals["delta_N"] == totals["delta_from_orders"]).all():
        raise SystemExit("Σ Δ completed over the window must equal N(new) − N(old)")
    slots = frame_slots(pair, a.hours[0], a.hours[1])
    s_hat = city_forecast(pair.new[pair.display].run_dir, pair.n_slots)

    # the hatching on the maps must follow the line exactly
    off_all = pair.promo_off[slots].all(axis=1)
    on_all = (~pair.promo_off[slots]).all(axis=1)
    below = s_hat[slots] < a.theta
    bad = [(slot_label(pair, int(s)), float(v)) for s, v, b, f, n in
           zip(slots, s_hat[slots], below, off_all, on_all) if (b and not f) or (not b and not n)]
    if bad:
        raise SystemExit(f"forecast and switched-off cells disagree in {len(bad)} slots: {bad[:5]}")

    path = animate(pair, slots, s_hat, a.theta, a.out, fps=a.fps, hold_s=a.hold, title=a.title)
    print(f"wrote {path}: {len(slots)} frames; slots below θ: "
          f"{', '.join(slot_label(pair, int(s)) for s, b in zip(slots, below) if b) or 'none'}")
    if a.frames_dir:
        from PIL import Image, ImageSequence
        a.frames_dir.mkdir(parents=True, exist_ok=True)
        for i, fr in enumerate(ImageSequence.Iterator(Image.open(path))):
            if i >= len(slots):
                break
            fr.convert("RGB").save(a.frames_dir / f"f_{i:02d}.png")
        print(f"frames in {a.frames_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
