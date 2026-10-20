"""Figures of the result notebooks 01–03.

Each function takes tables already computed by ``analysis/`` and returns one figure; the
notebook shows it under the cell and writes it to ``docs/figures/`` with
``analysis.plots.save_figure``. Nothing here reads run directories or simulates.

Style follows ``analysis.plots``: one measure per panel (never two y-scales), categorical
hues in fixed order (blue, orange, aqua: the first three slots of the validated palette,
safe for every pair), reference lines in neutral ink with a direct label, recessive grid,
Vietnamese number format.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.plots import INK, INK_2, MUTED, SERIES, SURFACE, Z95, _line, _pyplot, vn

SLOT = (SERIES, "#eb6834", "#1baf7a")          # categorical slots 1-3

# Display names of the policy-table labels (analysis.policy_table.DEFAULT_SPECS).
NAMES = {
    "all_off": "all_off (không voucher)",
    "all_on_B": "all_on có B (mốc)",
    "random": "điểm random",
    "heuristic": "heuristic tần suất thấp",
    "tau_x": r"$\hat\tau(x)$ bảng nền",
    "tau_per_vnd": r"$\hat\tau(x)$/đ bảng nền",
    "tau_x_dr_all": r"$\hat\tau(x)$ DR",
    "tau_xs_dr_all": r"$\hat\tau(x,s)$ DR",
    "tau_x_dr_all_vnd": r"$\hat\tau(x)$/đ DR",
    "tau_xs_dr_all_vnd": r"$\hat\tau(x,s)$/đ DR",
    "tau_x_dr_vnd": r"$\hat\tau(x)$/đ DR (lát explore)",
    "tau_xs_dr_vnd": r"$\hat\tau(x,s)$/đ DR (lát explore)",
    "pi_theta_0.5_heuristic": r"$\pi_\theta$, θ = 0,5, heuristic",
    "pi_thetahatA_ring1_dr_vnd": r"$\pi_{\hat\theta}$ (A) ring1 + $\hat\tau(x)$/đ DR",
    "pi_thetahatB_ring1_dr_vnd": r"$\pi_{\hat\theta}$ (B) ring1 + $\hat\tau(x)$/đ DR",
    # analysis.policy_table.specs_for_world: scores per 1.000đ, tension / hour thresholds, hour rule, oracle.
    "tau_x_dr_all_kvnd": r"$\hat\tau(x)$/1.000đ DR",
    "tau_xs_dr_all_kvnd": r"$\hat\tau(x,s)$/1.000đ DR",
    "tau_x_dr_kvnd": r"$\hat\tau(x)$/1.000đ DR (lát explore)",
    "tau_xs_dr_kvnd": r"$\hat\tau(x,s)$/1.000đ DR (lát explore)",
    "pi_tension_ring1_dr_kvnd": r"$\pi_{\hat\theta}$ độ căng ring1 (θ̂ = 0) + $\hat\tau(x)$/1.000đ",
    "pi_hourB_ring1_dr_kvnd": r"$\pi_{\hat\theta_B}$ ring1 + $\hat\tau(x)$/1.000đ",
    "pi_hourB_city_dr_kvnd": r"$\pi_{\hat\theta_B}$ city + $\hat\tau(x)$/1.000đ",
    "hour_off_random": "luật tắt theo giờ + điểm random",
    "hour_off_dr_kvnd": r"luật tắt theo giờ + $\hat\tau(x)$/1.000đ",
    "oracle": "oracle (τ thật mỗi khách)",
}
DR_TITLE = r"$\hat\tau(x)$/đ học bằng DR"     # panel title of the sweeps with the learned score
# Rider layer ranked by effect per đồng (a score of this set, or a pi_theta_hat / hour rule built on one).
PER_VND = {"tau_per_vnd", "tau_x_dr_all_vnd", "tau_xs_dr_all_vnd", "tau_x_dr_vnd", "tau_xs_dr_vnd",
           "pi_thetahatA_ring1_dr_vnd", "pi_thetahatB_ring1_dr_vnd",
           "tau_x_dr_all_kvnd", "tau_xs_dr_all_kvnd", "tau_x_dr_kvnd", "tau_xs_dr_kvnd",
           "pi_tension_ring1_dr_kvnd", "pi_hourB_ring1_dr_kvnd", "pi_hourB_city_dr_kvnd", "hour_off_dr_kvnd"}
BASELINE = {"random", "heuristic"}
DESIGN_NAMES = {"rider_ab": "A/B theo rider", "cluster_switchback cụm 1": "switchback cụm 1 ô",
                "cluster_switchback cụm 7": "switchback cụm 7 ô", "cluster_switchback cụm all": "switchback toàn hệ"}


def _num(value: float) -> str:
    """Short number with a decimal comma: 0,25 / 1 / 1,5."""
    return f"{value:g}".replace(".", ",")


def _signed(value: float) -> str:
    return ("+" if value > 0 else "−" if value < 0 else "") + vn(abs(value))


# ---------------------------------------------------------------------------
# Notebook 01: the market
# ---------------------------------------------------------------------------


def market_figure(market: pd.DataFrame):
    """Demand, supply and speed by hour (``analysis.validation.market_by_hour``), one panel each."""
    plt = _pyplot()
    fig, axes = plt.subplots(3, 1, figsize=(9, 7.5), sharex=True, height_ratios=[3, 3, 2])
    panels = (("sessions_expected", "Session / giờ", "Cầu: số session kỳ vọng mỗi giờ (cả thành phố)", 0),
              ("drivers_on_shift", "Xe trong ca", "Cung: số xe trong ca theo giờ (lịch ca cố định)", 0),
              ("speed_factor", "Hệ số tốc độ", "Tốc độ di chuyển so với mức chuẩn", 2))
    x = market["hour"].to_numpy(dtype=float)
    for ax, (col, ylabel, title, digits) in zip(axes, panels):
        _line(ax, x, market[col].to_numpy(dtype=float))
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.yaxis.set_major_formatter(lambda v, _, digits=digits: vn(v, digits))
        ax.margins(y=0.15)
    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels([f"{int(h)}" for h in x], fontsize=8)
    axes[-1].set_xlabel("Giờ trong ngày")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Notebook 02: policy evaluation
# ---------------------------------------------------------------------------


def sweeps_figure(sweeps: dict[tuple[str, str], pd.DataFrame], *, theta_max: float = 5.0):
    """N(pi_theta) over theta: one panel per score, s_hat by cell and by ring1 in each.

    ``sweeps[(score title, "cell" | "ring1")]`` = ``analysis.metrics.sweep_by_theta`` of that sweep.
    Filled markers are in the theta* set, hollow ones are not; the ring marks the best theta. No CI
    wash: the CI of each theta overlaps its neighbours although the paired differences are
    significant, and the theta* test already accounts for the pairing.
    """
    plt = _pyplot()
    scores = list(dict.fromkeys(score for score, _ in sweeps))
    fig, axes = plt.subplots(1, len(scores), figsize=(12, 4.8), squeeze=False)
    for ax, score in zip(axes[0], scores):
        bests = []
        for i, scope in enumerate(("cell", "ring1")):
            if (score, scope) not in sweeps:
                continue
            s = sweeps[(score, scope)]
            s = s[s["theta"] <= theta_max].reset_index(drop=True)
            x = np.arange(len(s), dtype=float)
            y = s["mean"].to_numpy(dtype=float)
            color, name = SLOT[i], ("ŝ theo ô" if scope == "cell" else "ŝ vòng 1 (ring1)")
            ax.plot(x, y, color=color, linewidth=2, marker="o", markersize=7, markeredgecolor=SURFACE,
                    markeredgewidth=2, label=name, solid_capstyle="round", solid_joinstyle="round")
            out = ~s["in_set"].to_numpy(dtype=bool)
            ax.plot(x[out], y[out], linestyle="none", marker="o", markersize=7, markerfacecolor=SURFACE,
                    markeredgecolor=color, markeredgewidth=2)
            best = int(np.argmax(y))
            ax.plot(x[best], y[best], "o", markersize=14, markerfacecolor="none", markeredgecolor=color,
                    markeredgewidth=1.5)
            bests.append(f"{name} θ = {_num(s['theta'].iloc[best])} ({vn(y[best])})")
        ax.set_xticks(x)
        ax.set_xticklabels([_num(t) for t in s["theta"]], fontsize=8)
        ax.set_xlabel("θ (lưới không đều; các mốc cách đều trên trục)")
        ax.set_title(score, pad=22)
        ax.text(0.0, 1.015, "tốt nhất (vòng tròn): " + "; ".join(bests), transform=ax.transAxes, fontsize=8.5,
                color=INK_2, va="bottom")
        ax.yaxis.set_major_formatter(lambda v, _: vn(v))
        ax.margins(y=0.15)
    axes[0][0].set_ylabel("Chuyến hoàn thành / ngày, dưới cùng B")
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncols=2, frameon=False, fontsize=9)
    fig.suptitle(r"$N(\pi_\theta)$ theo θ: chấm đặc = thuộc tập θ* (không kém θ tốt nhất, 95%, ghép cặp theo seed), "
                 "chấm rỗng = kém hơn", x=0.01, ha="left", fontsize=11, fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


def ladder_figure(summary: pd.DataFrame, *, reference: str = "all_on_B"):
    """N(pi) - N(reference) with a 95% interval, paired by seed (``analysis.policy_table.summarize``)."""
    plt = _pyplot()
    t = summary[summary["label"] != reference].sort_values("dN_vs_ref").reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(10, 0.42 * len(t) + 1.8))
    y = np.arange(len(t))
    half = Z95 * t["dN_vs_ref_se"].to_numpy(dtype=float)
    d = t["dN_vs_ref"].to_numpy(dtype=float)
    colors = [SLOT[1] if lab in PER_VND else SERIES for lab in t["label"]]
    ax.barh(y, d, height=0.62, color=colors, xerr=half, ecolor=INK_2, error_kw={"elinewidth": 1})
    for yi in range(len(t)):
        ax.annotate(f"{_signed(d[yi])}  (N = {vn(t['N_mean'].iloc[yi])})", xy=(d[yi] + np.sign(d[yi]) * half[yi], yi),
                    xytext=(6 if d[yi] >= 0 else -6, 0), textcoords="offset points", va="center",
                    ha="left" if d[yi] >= 0 else "right", fontsize=8.5, color=INK_2)
    ax.set_yticks(y)
    ax.set_yticklabels([NAMES.get(lab, lab) for lab in t["label"]], fontsize=9)
    ax.axvline(0, color=MUTED, linewidth=1)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", visible=True)
    ax.xaxis.set_major_formatter(lambda v, _: _signed(v) if v else "0")
    lo, hi = float((d - half).min()), float((d + half).max())
    ax.set_xlim(lo - 0.4 * (hi - lo), hi + 0.45 * (hi - lo))
    ax.set_xlabel(f"Chuyến hoàn thành / ngày so với {NAMES.get(reference, reference)} (ghép cặp theo seed, thanh = 95%)")
    n = int(summary["n_seeds"].iloc[0]) if "n_seeds" in summary else 0
    ax.set_title(f"Cùng ngân sách B, mỗi chính sách hơn all_on có B bao nhiêu chuyến ({n} seed)", pad=22)
    ax.text(0.0, 1.012, "cam = tầng rider xếp theo hiệu ứng mỗi đồng;  xanh = xếp theo cách khác",
            transform=ax.transAxes, fontsize=9, color=INK_2, va="bottom")
    fig.tight_layout()
    return fig


def qini_vs_n_figure(qini: pd.DataFrame, efficiency: pd.DataFrame):
    """Left: Qini (95% CI) against N under B. Right: extra trips per 100.000đ against N.

    ``qini``: ``results/qini_completed`` of ``analysis.qini_vs_value``; ``efficiency``: its
    ``offer_efficiency`` (with ``N_mean``). Shared y-axis = the same N.
    """
    plt = _pyplot()
    df = qini.set_index("label").join(efficiency.set_index("label")[["N_mean", "extra_trips_per_100k_vnd"]],
                                      how="inner")
    group = np.where(df.index.isin(list(PER_VND)), 1, np.where(df.index.isin(list(BASELINE)), 2, 0))
    legend = {0: "xếp theo hiệu ứng mỗi lượt phát", 1: "xếp theo hiệu ứng mỗi đồng", 2: "điểm nền (random, heuristic)"}
    fig, (ax_q, ax_e) = plt.subplots(1, 2, figsize=(12, 5.4), sharey=True)
    for g in (0, 1, 2):
        m = group == g
        if not m.any():
            continue
        ax_q.errorbar(df["qini_coef"][m], df["N_mean"][m],
                      xerr=[df["qini_coef"][m] - df["qini_lo"][m], df["qini_hi"][m] - df["qini_coef"][m]],
                      fmt="o", color=SLOT[g], ecolor=SLOT[g], elinewidth=1, markersize=8, markeredgecolor=SURFACE,
                      markeredgewidth=1.5, label=legend[g])
        ax_e.plot(df["extra_trips_per_100k_vnd"][m], df["N_mean"][m], "o", color=SLOT[g], markersize=8,
                  markeredgecolor=SURFACE, markeredgewidth=1.5)
    # Points that sit almost on top of each other get their label on the left or lower side.
    left = {"right": {"tau_x", "tau_x_dr_all_vnd", "tau_x_dr_all_kvnd"}, "left": {"tau_per_vnd"}}
    below = {"right": {"tau_xs_dr_vnd", "tau_per_vnd", "tau_x_dr_all_vnd", "tau_x_dr_all_kvnd"},
             "left": {"tau_xs_dr_vnd", "tau_x_dr_all_vnd", "tau_x_dr_all_kvnd", "pi_tension_ring1_dr_kvnd"}}
    for label, r in df.iterrows():
        name = NAMES.get(label, label)
        for side, ax, x in (("left", ax_q, r["qini_coef"]), ("right", ax_e, r["extra_trips_per_100k_vnd"])):
            dx, ha = (-6, "right") if label in left[side] else (6, "left")
            dy, va = (-3, "top") if label in below[side] else (3, "bottom")
            ax.annotate(name, xy=(x, r["N_mean"]), xytext=(dx, dy), textcoords="offset points", ha=ha, va=va,
                        fontsize=7.5, color=INK_2)
    rho_e = df[["N_mean", "extra_trips_per_100k_vnd"]].corr(method="spearman").iloc[0, 1]
    rho_q = df[["N_mean", "qini_coef"]].corr(method="spearman").iloc[0, 1]
    ax_q.set_title(f"Qini và N dưới B (Spearman {vn(rho_q, 2).replace('-', '−')})")
    ax_e.set_title(f"Chuyến thêm / 100.000đ và N dưới B (Spearman {vn(rho_e, 2)})")
    ax_q.set_xlabel("Hệ số Qini trên A/B theo rider (hoàn thành chuyến; thanh = 95% bootstrap theo rider)")
    ax_e.set_xlabel("Chuyến thêm / 100.000đ của tập được phát (A/B theo rider, chợ giữ nguyên)")
    ax_q.set_ylabel("N dưới cùng B (chuyến / ngày)")
    for ax in (ax_q, ax_e):
        ax.yaxis.set_major_formatter(lambda v, _: vn(v))
        ax.grid(axis="x", visible=True)
        ax.margins(x=0.25, y=0.08)
    ax_q.xaxis.set_major_formatter(lambda v, _: _signed(v) if v else "0")
    ax_e.xaxis.set_major_formatter(lambda v, _: vn(v, 0))
    ax_q.axvline(0, color=MUTED, linewidth=1)
    fig.legend(loc="lower center", ncols=3, frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    return fig


# ---------------------------------------------------------------------------
# Notebook 03: interference and u_latent
# ---------------------------------------------------------------------------


def design_bias_figure(designs: pd.DataFrame):
    """Total effect per day estimated by each design (95% CI) against the GTE (``design_table``)."""
    plt = _pyplot()
    d = designs.iloc[::-1].reset_index(drop=True)
    gte, gte_se = float(d["GTE"].iloc[0]), float(d["GTE_se"].iloc[0])
    fig, ax = plt.subplots(figsize=(10, 3.8))
    y = np.arange(len(d))
    ax.barh(y, d["per_day"], height=0.6, color=SERIES,
            xerr=[d["per_day"] - d["ci_lo"], d["ci_hi"] - d["per_day"]], ecolor=INK_2, error_kw={"elinewidth": 1})
    for yi, r in d.iterrows():
        sign = "+" if r["bias_pct"] >= 0 else "−"
        ax.annotate(f"{vn(r['per_day'])}  (chệch {sign}{vn(abs(r['bias_pct']), 1)}%)", xy=(r["ci_hi"], yi),
                    xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK_2)
    ax.axvspan(gte - Z95 * gte_se, gte + Z95 * gte_se, color=INK, alpha=0.08, linewidth=0)
    ax.axvline(gte, color=INK, linewidth=1.2)
    ax.annotate(f"GTE thật = {vn(gte, 1)} (all_on so với all_off, không ngân sách)", xy=(gte, -0.62), xytext=(6, 0),
                textcoords="offset points", ha="left", va="center", fontsize=9, color=INK, fontweight="bold")
    ax.set_ylim(-0.95, len(d) - 0.4)
    ax.set_yticks(y)
    ax.set_yticklabels([DESIGN_NAMES.get(n, n) for n in d["design"]])
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", visible=True)
    ax.set_xlim(0, float(d["ci_hi"].max()) * 1.3)
    ax.xaxis.set_major_formatter(lambda v, _: vn(v))
    ax.set_xlabel("Hiệu ứng tổng của voucher ước lượng (chuyến / ngày; thanh = 95% bootstrap theo đơn vị ngẫu nhiên hóa)")
    ax.set_title("Mỗi thiết kế thí nghiệm ước lượng hiệu ứng toàn hệ lệch bao nhiêu (28 ngày, bỏ burn-in)")
    fig.tight_layout()
    return fig


def u_latent_figure(confounding: pd.DataFrame):
    """Bias against the explore slice as the legacy targeting leans on u_latent (``confounding_table``)."""
    plt = _pyplot()
    c = confounding.sort_values("target_g_u").reset_index(drop=True)
    x = c["target_g_u"].to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    for i, (col, label) in enumerate((("bias_naive", "so thô (được phát so với không)"),
                                      ("bias_adjusted", "sau khi điều chỉnh theo X"))):
        ax.fill_between(x, c[f"{col}_lo"], c[f"{col}_hi"], color=SLOT[i], alpha=0.12, linewidth=0)
        ax.plot(x, c[col], color=SLOT[i], linewidth=2, marker="o", markersize=7, markeredgecolor=SURFACE,
                markeredgewidth=2, label=label)
        ax.annotate(label, xy=(x[-1], c[col].iloc[-1]), xytext=(8, 0), textcoords="offset points", va="center",
                    fontsize=9, color=INK_2)
    ax.axhline(0, color=MUTED, linewidth=1)
    ax.annotate("không chệch", xy=(1.0, 0), xycoords=("axes fraction", "data"), xytext=(-4, 4),
                textcoords="offset points", ha="right", fontsize=9, color=INK_2)
    ax.set_xticks(x)
    ax.set_xticklabels([_num(v) + (" (mặc định)" if v == 1 else "") for v in x])
    ax.set_xlabel("target_g_u: mức chính sách cũ nhắm theo biến ẩn u_latent")
    ax.set_ylabel("Chệch so với lát explore (tỷ lệ hoàn thành)")
    ax.yaxis.set_major_formatter(lambda v, _: vn(v, 3).replace("-", "−"))
    ax.set_xlim(x.min() - 0.15, x.max() + 0.95)
    ax.set_title("Ước lượng quan sát chệch theo mức nhắm vào biến ẩn")
    fig.tight_layout()
    return fig
