"""Plots: N(pi_theta) over theta and the throughput curve (A1).

``python -m analysis.plots theta_sweep runs/p6/sweep_theta [--gte runs/b7a/gte]``
``python -m analysis.plots throughput runs/<throughput run>``

Each command reads the results table of the run directory and writes a PNG next
to it (``results/<name>.png``). One measure per panel (never two y-scales on one
axis), one hue for the single series, reference lines in neutral ink with a
direct label, recessive grid. Needs matplotlib (extra ``[analysis]``).

The result notebooks use the same style through :func:`pyplot`
and write their figures to ``docs/figures/`` with :func:`save_figure`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SERIES = "#2a78d6"            # categorical slot 1; every chart here has a single series
Z95 = 1.959963984540054
FIGURES_DIR = Path(__file__).resolve().parents[1] / "docs" / "figures"
FIGURE_NAME = re.compile(r"\d{2}_[a-z0-9_]+")   # <notebook number>_<what>, e.g. 04_regret_theta_hat


def _pyplot():
    import matplotlib
    if "ipykernel" not in sys.modules:          # a notebook keeps its inline backend, so figures show under the cell
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 10,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
        "axes.axisbelow": True, "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
    })
    return plt


def pyplot():
    """``matplotlib.pyplot`` with the style of these plots (for the notebooks)."""
    return _pyplot()


def save_figure(fig, name: str, *, figures_dir: Path | None = None, dpi: int = 150) -> Path:
    """Write ``fig`` to ``docs/figures/<name>.png``.

    ``name`` starts with the number of the notebook that owns the figure (``04_regret``),
    so two notebooks never write the same file. The figure stays open, so a
    notebook still shows it under the cell. ``dpi`` 150 by default; the slide figures
    of ``analysis.data_stats`` use 220.
    """
    if not FIGURE_NAME.fullmatch(name):
        raise ValueError(f"figure name {name!r}: use '<notebook number>_<lowercase words>', e.g. '04_regret'")
    path = Path(figures_dir or FIGURES_DIR) / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    return path


def _line(ax, x, y, lo=None, hi=None) -> None:
    """One series: 2 px line, markers with a surface ring, CI as a light wash."""
    if lo is not None and hi is not None:
        ax.fill_between(x, lo, hi, color=SERIES, alpha=0.12, linewidth=0)
    ax.plot(x, y, color=SERIES, linewidth=2, solid_capstyle="round", solid_joinstyle="round",
            marker="o", markersize=7, markeredgecolor=SURFACE, markeredgewidth=2)


def _rule(ax, y: float, label: str, *, x_text: float = 0.995, va: str = "bottom") -> None:
    """Horizontal reference line with a direct label at the right edge."""
    ax.axhline(y, color=MUTED, linewidth=1)
    ax.annotate(label, xy=(x_text, y), xycoords=("axes fraction", "data"), ha="right", va=va,
                xytext=(0, 3 if va == "bottom" else -3), textcoords="offset points", color=INK_2, fontsize=9)


def vn(value: float, digits: int = 0) -> str:
    """Number in Vietnamese notation: 3.899 / 5.545,8."""
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def plot_theta_sweep(sweep: pd.DataFrame, path: Path, *, budget_vnd: float | None = None,
                     baselines: dict[str, float] | None = None, share_cells_off=None,
                     star_interval: tuple[float, float] | None = None, title: str | None = None) -> Path:
    """N(pi_theta) with a 95% CI over seeds, zoomed on the sweep; below, how much each theta cuts.

    The y-axis of the top panel covers the sweep only, so the shape is readable;
    the baselines (all_off, all_on without budget) and the spend against B are
    given as a note, because drawn to scale they would flatten the curve.
    ``star_interval`` (lower and upper theta of ``metrics.theta_star_interval``)
    is shaded as the range of thetas not significantly worse than the best. The
    lower panel shows the share of (cell, slot) switched off when
    ``share_cells_off`` (one value per theta) is given, else the mean spend.
    """
    plt = _pyplot()
    theta = sweep["theta"].to_numpy(dtype=float)
    n_mean, n_se = sweep["N_mean"].to_numpy(dtype=float), np.nan_to_num(sweep["N_se"].to_numpy(dtype=float))
    spent = sweep["spent_mean"].to_numpy(dtype=float)
    # A grid that spans two orders of magnitude (0 ... 30) is drawn with evenly spaced grid points:
    # on a linear axis the dozen thetas below 2 would collapse into the left edge.
    gaps = np.diff(theta)
    ordinal = len(theta) > 2 and theta.max() / max(float(np.median(gaps)), 1e-9) > 40
    x = np.arange(len(theta), dtype=float) if ordinal else theta
    fig, (ax_n, ax_s) = plt.subplots(2, 1, figsize=(9, 7.4), sharex=True, height_ratios=[3, 2])

    if star_interval is not None:
        lo, hi = star_interval
        x_lo, x_hi = float(np.interp(lo, theta, x)), float(np.interp(hi, theta, x))
        pad = 0.3 if ordinal else 0.03 * (theta.max() - theta.min())
        ax_n.axvspan(x_lo - pad, x_hi + pad, color=INK, alpha=0.05, linewidth=0)
        ax_n.annotate(f"khoảng θ*: {lo:g}–{hi:g} (không kém θ tốt nhất, 95%)", xy=((x_lo + x_hi) / 2, 0.03),
                      xycoords=("data", "axes fraction"), ha="center", va="bottom", color=INK_2, fontsize=9)
    _line(ax_n, x, n_mean, n_mean - Z95 * n_se, n_mean + Z95 * n_se)
    best = int(np.argmax(n_mean))
    ax_n.annotate(f"lớn nhất: θ = {theta[best]:g}\nN = {vn(n_mean[best])}", xy=(x[best], n_mean[best]),
                  xytext=(0, 26), textcoords="offset points", ha="center", va="bottom", color=INK, fontsize=9,
                  fontweight="bold")
    ax_n.set_ylabel("Số chuyến hoàn thành / ngày")
    n_seeds = int(sweep["n_seeds"].iloc[0]) if "n_seeds" in sweep else 0
    ax_n.set_title(title or f"N(π_θ) theo ngưỡng θ, cùng ngân sách B (trung bình {n_seeds} seed, dải 95%)", pad=30)
    notes = []
    if budget_vnd is not None and budget_vnd == budget_vnd:
        notes.append(f"B = {vn(budget_vnd)} đ/kỳ, chi trung bình {vn(spent.min())}–{vn(spent.max())}")
    notes += [f"{label}: {vn(value)}" for label, value in (baselines or {}).items()]
    if notes:
        ax_n.text(0.0, 1.035, "  ·  ".join(notes), transform=ax_n.transAxes, ha="left", va="bottom",
                  color=INK_2, fontsize=9)
    ax_n.yaxis.set_major_formatter(lambda v, _: vn(v))
    ax_n.margins(y=0.35)

    if share_cells_off is not None:
        _line(ax_s, x, np.asarray(share_cells_off, dtype=float) * 100.0)
        ax_s.set_ylabel("% (ô, slot) bị tắt")
        ax_s.set_title("Mức cắt của tầng ô: tỷ lệ (ô, slot) tắt khuyến mãi")
        ax_s.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    else:
        _line(ax_s, x, spent)
        ax_s.set_ylabel("Chi voucher (đ / ngày)")
        ax_s.set_title("Chi tiêu voucher trung bình")
        ax_s.yaxis.set_major_formatter(lambda v, _: vn(v))
    ax_s.set_ylim(bottom=0)
    ax_s.margins(y=0.2)
    ax_s.set_xlabel("θ: tắt khuyến mãi ở ô có slack dự báo < θ"
                    + (" (lưới không đều; các mốc đặt cách đều trên trục)" if ordinal else ""))
    ax_s.set_xticks(x)
    ax_s.set_xticklabels([f"{t:g}" for t in theta], fontsize=8)

    fig.tight_layout()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_throughput(table: pd.DataFrame, path: Path, *, title: str | None = None) -> Path:
    """A1: :func:`throughput_figure` written to ``path`` (command line)."""
    plt = _pyplot()
    fig = throughput_figure(table, title=title)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def throughput_figure(table: pd.DataFrame, *, title: str | None = None):
    """A1: completed trips per hour, mean slack and pickup ETA against the demand level (one panel each)."""
    plt = _pyplot()
    g = table.groupby("demand_scale")
    x = np.array(sorted(g.groups), dtype=float)
    panels = (
        ("completed_per_h", "Chuyến hoàn thành / giờ", title or "Đường throughput (all_off, đội xe cố định)"),
        ("mean_slack", "Slack trung bình (I/E)", "Slack: xe rảnh / xe đang đi đón"),
        ("mean_pickup_eta_min", "ETA đón (phút)", "ETA đón trung bình"),
    )
    fig, axes = plt.subplots(3, 1, figsize=(9, 9.5), sharex=True, height_ratios=[3, 2, 2])
    for ax, (col, ylabel, panel_title) in zip(axes, panels):
        mean = g[col].mean().to_numpy(dtype=float)
        ax.scatter(table["demand_scale"], table[col], s=10, color=SERIES, alpha=0.35, linewidths=0)   # seeds
        _line(ax, x, mean)
        ax.set_ylabel(ylabel)
        ax.set_title(panel_title)
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.4g}")
        ax.margins(y=0.2)
        if col == "completed_per_h":
            peak = int(np.argmax(mean))
            level = f"{x[peak]:g}".replace(".", ",")
            ax.annotate(f"đỉnh {vn(mean[peak])} ở mức {level}", xy=(x[peak], mean[peak]), xytext=(0, 12),
                        textcoords="offset points", ha="center", color=INK, fontsize=9, fontweight="bold")
            _rule(ax, 0.95 * mean[peak], "0,95 × đỉnh", va="top")
        if col == "mean_slack":
            ax.set_yscale("log")
            _rule(ax, 0.45, "ngưỡng WGC 0,45")
    axes[-1].set_xlabel("demand_scale (hệ số cầu)")
    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels([f"{v:g}".replace(".", ",") for v in x], fontsize=8)
    fig.tight_layout()
    return fig


def gte_baselines(gte_dir: Path) -> dict[str, float]:
    """Mean N of all_off and of all_on without budget, from a ``gte`` run directory."""
    results = pd.read_parquet(Path(gte_dir) / "results" / "policy_results.parquet")
    mean = results.groupby("policy")["N_completed"].mean()
    return {"all_on không ngân sách": float(mean["all_on"]), "all_off": float(mean["all_off"])}


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("theta_sweep", "throughput"):
        print(__doc__)
        return 2
    run_dir = Path(argv[2])
    if argv[1] == "throughput":
        table = pd.read_parquet(run_dir / "results" / "throughput_curve.parquet")
        print(plot_throughput(table, run_dir / "results" / "throughput_curve.png"))
        return 0
    sweep = pd.read_parquet(run_dir / "results" / "theta_sweep.parquet")
    runs = pd.read_parquet(run_dir / "results" / "policy_results.parquet")
    baselines = gte_baselines(Path(argv[argv.index("--gte") + 1])) if "--gte" in argv else None
    off = runs.groupby("theta", sort=True)["share_cells_off"].mean().to_numpy()
    from analysis.metrics import theta_star_gaps, theta_star_interval, theta_star_set
    star = theta_star_set(runs)
    best, lo, hi = theta_star_interval(star)
    out = plot_theta_sweep(sweep, run_dir / "results" / "theta_sweep.png",
                           budget_vnd=float(runs["budget_B_vnd"].iloc[0]), baselines=baselines, share_cells_off=off,
                           star_interval=(lo, hi))
    print(star.round(2).to_string(index=False))
    gaps = theta_star_gaps(star)
    print(f"theta tốt nhất = {best:g}; khoảng θ* (không kém θ tốt nhất, 95% đồng thời, ghép cặp theo seed): "
          f"[{lo:g}, {hi:g}]" + (f"; mốc bị loại bên trong: {gaps}" if gaps else ""))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
