"""Animation of a policy change on the hex grid.

Two runs of the same seed and the same budget B, both with ``--log-level full``: the old
policy (``all_on`` under B) and the new one (recommended policy). One frame per 15-minute
slot of a chosen window (5h–9h by default) with four layers:

1. Δ requests per cell (new − old), from ``market/slot_snapshots.n_requests``;
2. Δ completed trips per cell **by request slot** (new − old), recomputed from
   ``observed/orders`` because ``slot_snapshots.n_completed`` counts by drop-off time
   and would lag the request map by one or two slots;
3. slack of the new policy in the slot (concurrent slack, clipped at 3 for display) with
   the cells switched off hatched (``promo_on`` of the new policy);
4. below: cumulative ΔN over the window, mean over seeds with a 95% band, and a cursor.

Δ maps are the mean over the seed pairs; slack and the off cells come from one display
seed (one concrete morning). Both Δ maps share one diverging scale, symmetric around 0
and fixed for the whole clip (``vmax = max |Δ|`` over the frames), orange–neutral–blue
(colour-vision safe, the palette of ``analysis.plots``). Nothing here reads ``hidden/``
or touches ``sim/``.

Check: the sum of Δ completed-by-request-slot over the **whole evaluation
window** equals ``N(new) − N(old)`` of ``results/policy_results`` for every seed;
:func:`check_totals` raises otherwise.

CLI::

    python -m analysis.animation --old runs/anim/all_on/s0 runs/anim/all_on/s1 ...
        --new runs/anim/pi/s0 runs/anim/pi/s1 ... --out docs/figures/06_animation.gif
        [--hours 5 9] [--fps 1] [--hold 2] [--display-seed 0]
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from analysis.io import load_run
from analysis.plots import INK, INK_2, MUTED, SERIES, SURFACE, Z95, _pyplot, vn
from dashboard.geometry import axial_to_xy
from sim.space import hex_cells, n_cells_for_radius

ORANGE = "#eb6834"      # categorical slot 2 of the palette (analysis.figures.SLOT)
AQUA = "#1baf7a"        # categorical slot 3: sequential hue of the slack map, distinct from the Δ blue
NEUTRAL = "#eeede8"     # value 0 of the diverging scale: close to the background
SLACK_DISPLAY_CAP = 3.0
COMPLETED = "Completed"


# ---------------------------------------------------------------------------
# Matrices [n_slots, n_cells] from the logged tables
# ---------------------------------------------------------------------------


def hex_centres(radius: int) -> tuple[np.ndarray, np.ndarray]:
    """Centres of the cells of ``sim.space.hex_cells(radius)`` (same order as ``cell_id``), circumradius 1."""
    cells = hex_cells(radius)
    return axial_to_xy(cells[:, 0], cells[:, 1])


def pivot_counts(slot: np.ndarray, cell: np.ndarray, n_slots: int, n_cells: int,
                 values: np.ndarray | None = None) -> np.ndarray:
    """Sum ``values`` (default 1 per row) into a dense ``[n_slots, n_cells]`` float64 matrix; rows outside are dropped."""
    slot = np.asarray(slot, dtype=np.int64)
    cell = np.asarray(cell, dtype=np.int64)
    keep = (slot >= 0) & (slot < n_slots) & (cell >= 0) & (cell < n_cells)
    out = np.zeros((n_slots, n_cells), dtype=np.float64)
    vals = np.ones(int(keep.sum())) if values is None else np.asarray(values, dtype=np.float64)[keep]
    np.add.at(out, (slot[keep], cell[keep]), vals)
    return out


def requests_by_slot(snapshots: pd.DataFrame, n_slots: int, n_cells: int) -> np.ndarray:
    """``n_requests`` of ``market/slot_snapshots`` as ``[n_slots, n_cells]`` (counted at open time)."""
    return pivot_counts(snapshots["slot"].to_numpy(), snapshots["cell"].to_numpy(), n_slots, n_cells,
                        snapshots["n_requests"].to_numpy())


def completed_by_request_slot(orders: pd.DataFrame, slot_s: float, n_slots: int, n_cells: int, *,
                              in_window_only: bool = False) -> np.ndarray:
    """Completed orders counted at their **request** slot and pickup cell, ``[n_slots, n_cells]``.

    ``in_window_only`` keeps the orders whose request time is in the evaluation window: the
    sum of that matrix is exactly ``N(π)`` of ``results/policy_results``.
    """
    o = orders[orders["status"] == COMPLETED]
    if in_window_only:
        o = o[o["in_window"].to_numpy().astype(bool)]
    slot = np.floor(o["request_time_s"].to_numpy(dtype=np.float64) / float(slot_s)).astype(np.int64)
    return pivot_counts(slot, o["pu_cell"].to_numpy(), n_slots, n_cells)


def field_by_slot(snapshots: pd.DataFrame, field: str, n_slots: int, n_cells: int, fill: float = np.nan) -> np.ndarray:
    """One ``slot_snapshots`` field as ``[n_slots, n_cells]``; slots never published keep ``fill``."""
    out = np.full((n_slots, n_cells), fill, dtype=np.float64)
    slot = snapshots["slot"].to_numpy(dtype=np.int64)
    cell = snapshots["cell"].to_numpy(dtype=np.int64)
    keep = (slot >= 0) & (slot < n_slots) & (cell >= 0) & (cell < n_cells)
    out[slot[keep], cell[keep]] = snapshots[field].to_numpy(dtype=np.float64)[keep]
    return out


# ---------------------------------------------------------------------------
# One run, one pair of runs, the mean over seeds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMatrices:
    """Everything the animation needs from one full-log run directory (one seed)."""
    run_dir: Path
    policy: str
    seed: int
    budget: float               # NaN when the budget is not enforced
    n_completed: int            # N(π) of results/policy_results
    n_cells: int
    radius: int
    slot_s: float
    slots_per_day: int
    req: np.ndarray             # [n_slots, n_cells] requests by open slot
    done: np.ndarray            # [n_slots, n_cells] completed by request slot, every order
    done_window: np.ndarray     # same, orders requested inside the evaluation window
    slack: np.ndarray           # [n_slots, n_cells] concurrent slack (inf kept), NaN if not published
    promo_on: np.ndarray        # [n_slots, n_cells] 1/0, NaN if not published

    @property
    def n_slots(self) -> int:
        return self.req.shape[0]


def load_matrices(run_dir: Path | str) -> RunMatrices:
    """Read one run written with ``--log-level full`` (a single job, tables straight under ``run_dir``)."""
    run_dir = Path(run_dir)
    run = load_run(run_dir)
    for name in ("orders", "slot_snapshots", "policy_results", "run_metadata"):
        if name not in run:
            raise FileNotFoundError(f"{run_dir}: table {name} missing; run with --log-level full")
    meta, results = run["run_metadata"], run["policy_results"]
    if len(meta) != 1 or len(results) != 1:
        raise ValueError(f"{run_dir}: expected exactly one run per directory, got {len(meta)} metadata rows")
    m = meta.iloc[0]
    cfg = yaml.safe_load(m["config_yaml"])
    radius = int(cfg["space"]["grid_radius"])
    n_cells = n_cells_for_radius(radius)
    slot_s = float(cfg["time"]["slot_min"]) * 60.0
    n_slots = int(math.ceil(float(m["sim_end_s"]) / slot_s))
    snaps, orders = run["slot_snapshots"], run["orders"]
    return RunMatrices(
        run_dir=run_dir, policy=str(m["policy"]), seed=int(m["seed"]), budget=float(m["budget_B_vnd"]),
        n_completed=int(results["N_completed"].iloc[0]), n_cells=n_cells, radius=radius, slot_s=slot_s,
        slots_per_day=int(round(86400.0 / slot_s)),
        req=requests_by_slot(snaps, n_slots, n_cells),
        done=completed_by_request_slot(orders, slot_s, n_slots, n_cells),
        done_window=completed_by_request_slot(orders, slot_s, n_slots, n_cells, in_window_only=True),
        slack=field_by_slot(snaps, "slack", n_slots, n_cells),
        promo_on=field_by_slot(snaps, "promo_on", n_slots, n_cells),
    )


def _same_budget(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


@dataclass(frozen=True)
class PairDelta:
    """Δ = new − old over the seed pairs, plus the display seed's slack and off cells."""
    old: tuple[RunMatrices, ...]
    new: tuple[RunMatrices, ...]
    display: int                        # index of the display seed in ``new``
    d_req: np.ndarray                   # [n_slots, n_cells] mean over seeds
    d_done: np.ndarray                  # [n_slots, n_cells] mean over seeds
    d_done_by_seed: np.ndarray          # [n_seeds, n_slots, n_cells]

    @property
    def seeds(self) -> list[int]:
        return [r.seed for r in self.new]

    @property
    def n_cells(self) -> int:
        return self.new[0].n_cells

    @property
    def n_slots(self) -> int:
        return self.new[0].n_slots

    @property
    def slot_s(self) -> float:
        return self.new[0].slot_s

    @property
    def budget(self) -> float:
        return self.new[0].budget

    @property
    def slack(self) -> np.ndarray:
        return self.new[self.display].slack

    @property
    def promo_off(self) -> np.ndarray:
        """True where the new policy switched the cell off in the slot (NaN = not published → False)."""
        return self.new[self.display].promo_on == 0.0

    def hour_of_slot(self, slot: int) -> int:
        return int((slot * self.slot_s) // 3600) % 24

    def day_of_slot(self, slot: int) -> int:
        return int((slot * self.slot_s) // 86400)


def load_pairs(old_dirs, new_dirs, *, display_seed_index: int = 0) -> PairDelta:
    """Load the old/new runs, pair them by seed, check seed, budget and grid, and average the deltas.

    Both lists may be given in any order; pairs are formed on ``seed``.
    """
    old = sorted((load_matrices(d) for d in old_dirs), key=lambda r: r.seed)
    new = sorted((load_matrices(d) for d in new_dirs), key=lambda r: r.seed)
    if not old or len(old) != len(new):
        raise ValueError(f"need the same number of old and new runs, got {len(old)} and {len(new)}")
    for a, b in zip(old, new, strict=True):
        if a.seed != b.seed:
            raise ValueError(f"seeds differ: old {a.seed} ({a.run_dir}) vs new {b.seed} ({b.run_dir})")
        if not _same_budget(a.budget, b.budget):
            raise ValueError(f"seed {a.seed}: budgets differ, old {a.budget} vs new {b.budget}")
        if (a.n_cells, a.slot_s) != (b.n_cells, b.slot_s):
            raise ValueError(f"seed {a.seed}: grid or slot length differ between the two runs")
    n_slots = min(min(r.n_slots for r in old), min(r.n_slots for r in new))
    d_req = np.stack([b.req[:n_slots] - a.req[:n_slots] for a, b in zip(old, new)])
    d_done = np.stack([b.done[:n_slots] - a.done[:n_slots] for a, b in zip(old, new)])
    if not 0 <= display_seed_index < len(new):
        raise IndexError(f"display_seed_index {display_seed_index} out of range for {len(new)} seeds")
    return PairDelta(old=tuple(old), new=tuple(new), display=display_seed_index,
                     d_req=d_req.mean(axis=0), d_done=d_done.mean(axis=0), d_done_by_seed=d_done)


def check_totals(pair: PairDelta) -> pd.DataFrame:
    """Σ Δ completed-by-request-slot over the whole evaluation window = N(new) − N(old), per seed.

    Returns one row per seed; raises ``AssertionError`` on the first mismatch.
    """
    rows = []
    for a, b in zip(pair.old, pair.new, strict=True):
        from_orders_old, from_orders_new = int(a.done_window.sum()), int(b.done_window.sum())
        rows.append({"seed": a.seed, "N_old": a.n_completed, "N_new": b.n_completed,
                     "N_old_from_orders": from_orders_old, "N_new_from_orders": from_orders_new,
                     "delta_N": b.n_completed - a.n_completed, "delta_from_orders": from_orders_new - from_orders_old})
    table = pd.DataFrame(rows)
    bad = table[(table["N_old"] != table["N_old_from_orders"]) | (table["N_new"] != table["N_new_from_orders"])]
    if len(bad):
        raise AssertionError("Δ completed by request slot does not add up to policy_results:\n" + bad.to_string(index=False))
    return table


# ---------------------------------------------------------------------------
# Frames of a window, cumulative ΔN, captions
# ---------------------------------------------------------------------------


def frame_slots(pair: PairDelta, hour_from: int = 5, hour_to: int = 9, *, day: int = 0) -> np.ndarray:
    """Slot indices of ``day`` with ``hour_from <= hour < hour_to`` (one window, one story)."""
    slots = np.arange(pair.n_slots)
    hours = np.array([pair.hour_of_slot(s) for s in slots])
    days = np.array([pair.day_of_slot(s) for s in slots])
    keep = (days == day) & (hours >= hour_from) & (hours < hour_to)
    if not keep.any():
        raise ValueError(f"no slot of day {day} between {hour_from}h and {hour_to}h in {pair.n_slots} slots")
    return slots[keep]


def cumulative_delta(pair: PairDelta, slots: np.ndarray) -> dict[str, np.ndarray]:
    """Cumulative Σ_cells Δ completed over ``slots``: mean over seeds with a 95% band (normal, Z95)."""
    per_seed = pair.d_done_by_seed[:, slots, :].sum(axis=2).cumsum(axis=1)     # [n_seeds, n_frames]
    n = per_seed.shape[0]
    mean = per_seed.mean(axis=0)
    se = per_seed.std(axis=0, ddof=1) / math.sqrt(n) if n > 1 else np.zeros_like(mean)
    return {"mean": mean, "lo": mean - Z95 * se, "hi": mean + Z95 * se, "per_seed": per_seed}


def window_delta(pair: PairDelta) -> dict[str, float]:
    """ΔN over the whole evaluation window: mean over seeds, SE, 95% half-width."""
    d = np.array([b.n_completed - a.n_completed for a, b in zip(pair.old, pair.new)], dtype=np.float64)
    se = d.std(ddof=1) / math.sqrt(len(d)) if len(d) > 1 else float("nan")
    return {"mean": float(d.mean()), "se": float(se), "half95": float(Z95 * se), "n_seeds": len(d)}


def slot_label(pair: PairDelta, slot: int) -> str:
    t0 = int(slot * pair.slot_s) % 86400
    t1 = (t0 + int(pair.slot_s)) % 86400
    return f"{t0 // 3600:02d}:{(t0 % 3600) // 60:02d}–{t1 // 3600:02d}:{(t1 % 3600) // 60:02d}"


def default_caption(pair: PairDelta, slot: int) -> str:
    """One sentence per frame from the signs of Σ Δ requests and Σ Δ completed."""
    d_req, d_done = float(pair.d_req[slot].sum()), float(pair.d_done[slot].sum())
    n_off = int(pair.promo_off[slot].sum())
    off = f"{n_off}/{pair.n_cells} ô tắt voucher" if n_off else "không ô nào bị tắt"
    if abs(d_req) < 0.05 and abs(d_done) < 0.05:
        story = "slot này hai chính sách không khác nhau"
    elif d_req > 0 and d_done > 0:
        story = "voucher tăng cả request lẫn chuyến: còn xe để phục vụ"
    elif d_req > 0 and d_done <= 0:
        story = "request tăng nhưng chuyến không tăng: cung căng, xe bị kéo khỏi ô lân cận"
    elif d_req <= 0 and d_done >= 0:
        story = "bớt request ở ô bị tắt, số chuyến vẫn giữ: voucher ở đó không tạo thêm chuyến"
    else:
        story = "chính sách mới cắt voucher: request và chuyến đều giảm trong slot này"
    return f"{story}; {off}. Σ Δ request {vn(d_req, 1)}, Σ Δ chuyến {vn(d_done, 1)} (trung bình seed)."


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _colormaps():
    from matplotlib.colors import LinearSegmentedColormap
    diverging = LinearSegmentedColormap.from_list("cvd_diverging", [ORANGE, NEUTRAL, SERIES])
    sequential = LinearSegmentedColormap.from_list("cvd_sequential", [NEUTRAL, AQUA])
    return diverging, sequential


def hex_layer(ax, x: np.ndarray, y: np.ndarray) -> list:
    """One RegularPolygon per cell (pointy top, circumradius 0.97); returns the patches so frames can recolour them."""
    from matplotlib.patches import RegularPolygon
    patches = [RegularPolygon((float(xi), float(yi)), numVertices=6, radius=0.97, orientation=0.0,
                              edgecolor="white", linewidth=0.8) for xi, yi in zip(x, y)]
    for p in patches:
        ax.add_patch(p)
    ax.set_xlim(x.min() - 1.2, x.max() + 1.2)
    ax.set_ylim(y.min() - 1.2, y.max() + 1.2)
    ax.set_aspect("equal")
    ax.axis("off")
    return patches


def animate(pair: PairDelta, slots: np.ndarray, out_path: Path | str, *, captions: dict[int, str] | None = None,
            fps: float = 1.0, hold_s: float = 2.0, title: str | None = None,
            slack_cap: float = SLACK_DISPLAY_CAP, dpi: int = 100) -> Path:
    """Write the GIF: three hex maps and the cumulative ΔN below, one frame per slot of ``slots``.

    ``captions`` maps a slot index to the sentence shown under the maps; missing slots get
    :func:`default_caption`. The last frame is repeated for ``hold_s`` seconds.
    """
    from matplotlib.animation import FuncAnimation, PillowWriter
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize, TwoSlopeNorm

    plt = _pyplot()
    out_path = Path(out_path)
    slots = np.asarray(slots, dtype=np.int64)
    x, y = hex_centres(pair.new[0].radius)
    diverging, sequential = _colormaps()
    vmax = float(np.nanmax(np.abs(np.concatenate([pair.d_req[slots], pair.d_done[slots]]))))
    vmax = vmax if vmax > 0 else 1.0
    div_norm = TwoSlopeNorm(vcenter=0.0, vmin=-vmax, vmax=vmax)
    seq_norm = Normalize(0.0, slack_cap)
    cum = cumulative_delta(pair, slots)
    total = window_delta(pair)
    n_seeds = len(pair.seeds)
    new_name, old_name = pair.new[0].policy, pair.old[0].policy

    fig = plt.figure(figsize=(12.5, 7.6), dpi=dpi)
    gs = fig.add_gridspec(2, 3, height_ratios=[3.1, 1.5], hspace=0.28, wspace=0.08,
                          left=0.03, right=0.97, top=0.86, bottom=0.17)
    axes = [fig.add_subplot(gs[0, i]) for i in range(3)]
    ax_ts = fig.add_subplot(gs[1, :])
    layers = [
        (pair.d_req, diverging, div_norm, f"Δ request (mới − cũ), trung bình {n_seeds} seed"),
        (pair.d_done, diverging, div_norm, f"Δ chuyến hoàn thành theo slot đặt xe, trung bình {n_seeds} seed"),
        (np.minimum(pair.slack, slack_cap), sequential, seq_norm,
         f"slack dưới π mới (seed {pair.seeds[pair.display]}), gạch chéo = ô tắt voucher"),
    ]
    patches = []
    for ax, (_, _, _, name) in zip(axes, layers):
        patches.append(hex_layer(ax, x, y))
        ax.set_title(name, fontsize=9.5, color=INK)
    fig.colorbar(ScalarMappable(norm=div_norm, cmap=diverging), ax=axes[:2], orientation="horizontal",
                 fraction=0.04, pad=0.02, aspect=50, label="Δ số lượt mỗi ô mỗi slot (cùng thang cho hai bản đồ)")
    fig.colorbar(ScalarMappable(norm=seq_norm, cmap=sequential), ax=axes[2], orientation="horizontal",
                 fraction=0.04, pad=0.02, aspect=25, label=f"slack = xe rảnh / xe đi đón, cắt ở {vn(slack_cap)}")

    # cumulative ΔN with its band and a cursor
    frames_x = np.arange(len(slots))
    ax_ts.fill_between(frames_x, cum["lo"], cum["hi"], color=SERIES, alpha=0.18, linewidth=0)
    ax_ts.plot(frames_x, cum["mean"], color=SERIES, linewidth=1.8)
    ax_ts.axhline(0.0, color=MUTED, linewidth=0.8)
    cursor = ax_ts.axvline(0, color=INK_2, linewidth=1.0)
    ax_ts.set_xticks(frames_x[:: max(1, len(frames_x) // 8)])
    ax_ts.set_xticklabels([slot_label(pair, int(s)).split("–")[0] for s in slots[:: max(1, len(frames_x) // 8)]])
    ax_ts.set_xlim(-0.5, len(frames_x) - 0.5)
    ax_ts.set_ylabel("Δ chuyến cộng dồn trong khung")
    ax_ts.set_title(f"ΔN cộng dồn (mới − cũ), trung bình và dải 95% qua {n_seeds} seed", fontsize=9.5, color=INK, loc="left")
    half = "" if math.isnan(total["half95"]) else f" ± {vn(total['half95'], 1)}"
    ax_ts.text(0.995, 0.95, f"ΔN cả cửa sổ đánh giá = {vn(total['mean'], 1)}{half} chuyến",
               transform=ax_ts.transAxes, ha="right", va="top", fontsize=9, color=INK_2)
    for side in ("top", "right"):
        ax_ts.spines[side].set_visible(False)

    head = fig.suptitle("", fontsize=12, color=INK, y=0.975)
    sub = fig.text(0.5, 0.925, (title or f"{new_name} so với {old_name} · cùng seed, cùng B"
                                f"{'' if math.isnan(pair.budget) else ' = ' + vn(pair.budget, 2)} · trong mô phỏng này"),
                   ha="center", va="center", fontsize=9.5, color=INK_2)
    caption = fig.text(0.5, 0.045, "", ha="center", va="center", fontsize=10, color=INK, wrap=True)

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
        cursor.set_xdata([k, k])
        head.set_text(f"{slot_label(pair, s)} · khung {k + 1}/{len(slots)}")
        text = captions.get(s) if captions else None
        caption.set_text(text if text else default_caption(pair, s))
        return [*(p for ps in patches for p in ps), cursor, head, caption, sub]

    order = list(range(len(slots))) + [len(slots) - 1] * max(0, int(round(hold_s * fps)))
    anim = FuncAnimation(fig, draw, frames=order, blit=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    anim.save(str(out_path), writer=PillowWriter(fps=fps))
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hex-grid animation of a policy change.")
    ap.add_argument("--old", nargs="+", required=True, type=Path, help="full-log run dirs of the old policy, one per seed")
    ap.add_argument("--new", nargs="+", required=True, type=Path, help="full-log run dirs of the new policy, one per seed")
    ap.add_argument("--out", required=True, type=Path, help="output .gif")
    ap.add_argument("--hours", nargs=2, type=int, default=(5, 9), metavar=("FROM", "TO"))
    ap.add_argument("--day", type=int, default=0)
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--hold", type=float, default=2.0, help="seconds to hold the last frame")
    ap.add_argument("--display-seed", type=int, default=0, help="index (in seed order) of the seed drawn in the slack map")
    ap.add_argument("--title", default=None, help="subtitle under the frame label (policy pair, seeds, B, 'trong mô phỏng này')")
    a = ap.parse_args(argv)
    pair = load_pairs(a.old, a.new, display_seed_index=a.display_seed)
    totals = check_totals(pair)
    print(totals.to_string(index=False))
    if not (totals["delta_N"] == totals["delta_from_orders"]).all():
        raise SystemExit("Σ Δ completed over the window must equal N(new) − N(old) of policy_results")
    slots = frame_slots(pair, a.hours[0], a.hours[1], day=a.day)
    path = animate(pair, slots, a.out, fps=a.fps, hold_s=a.hold, title=a.title)
    w = window_delta(pair)
    print(f"wrote {path}: {len(slots)} frames, ΔN over the window = {w['mean']:.1f} ± {w['half95']:.1f} ({w['n_seeds']} seeds); "
          f"ΔN inside the frames = {float(pair.d_done[slots].sum()):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
