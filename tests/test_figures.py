"""The figures of notebooks 01–03 (analysis/figures.py) draw from small tables."""

import numpy as np
import pandas as pd
import pytest

matplotlib = pytest.importorskip("matplotlib")

from analysis import figures  # noqa: E402
from analysis.plots import pyplot, save_figure, throughput_figure  # noqa: E402


@pytest.fixture
def plt():
    p = pyplot()
    yield p
    p.close("all")


def test_market_and_throughput_figures(plt, tmp_path):
    market = pd.DataFrame({"hour": np.arange(24), "sessions_expected": np.linspace(200, 2000, 24),
                           "drivers_on_shift": np.arange(24) + 10, "speed_factor": np.full(24, 1.0)})
    fig = figures.market_figure(market)
    assert len(fig.axes) == 3
    path = save_figure(fig, "01_test_market", figures_dir=tmp_path)
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    curve = pd.DataFrame({"demand_scale": np.repeat([0.5, 1.0, 2.0], 2), "seed": [0, 1] * 3,
                          "completed_per_h": [100, 104, 190, 196, 150, 154], "mean_slack": [5, 5.2, 1, 1.1, 0.1, 0.1],
                          "mean_pickup_eta_min": [2, 2.1, 3, 3.1, 6, 6.2]})
    assert len(throughput_figure(curve).axes) == 3


def test_sweeps_figure_marks_theta_star_and_skips_large_thetas(plt):
    def sweep(shift):
        theta = np.array([0.0, 0.5, 1.0, 5.0, 30.0])
        return pd.DataFrame({"theta": theta, "mean": 100 + shift - (theta - 0.5) ** 2,
                             "in_set": [False, True, True, False, False]})
    fig = figures.sweeps_figure({("a", "cell"): sweep(0), ("a", "ring1"): sweep(2), ("b", "cell"): sweep(1)})
    assert len(fig.axes) == 2
    ax = fig.axes[0]
    assert len(ax.get_xticks()) == 4                                    # theta = 30 > theta_max is not drawn


def test_ladder_qini_design_and_u_latent_figures(plt):
    summary = pd.DataFrame({"label": ["all_on_B", "random", "tau_per_vnd", "all_off"], "N_mean": [100.0, 110, 130, 80],
                            "dN_vs_ref": [0.0, 10, 30, -20], "dN_vs_ref_se": [0.0, 1, 2, 1], "n_seeds": [30] * 4})
    assert len(figures.ladder_figure(summary).axes) == 1
    qini = pd.DataFrame({"label": ["random", "tau_x", "tau_per_vnd"], "qini_coef": [10.0, 800, -500],
                         "qini_lo": [-100.0, 400, -1000], "qini_hi": [120.0, 1200, -10]})
    eff = pd.DataFrame({"label": ["random", "tau_x", "tau_per_vnd"], "N_mean": [110.0, 108, 130],
                        "extra_trips_per_100k_vnd": [8.0, 7.5, 12.5]})
    fig = figures.qini_vs_n_figure(qini, eff)
    assert len(fig.axes) == 2 and "−1,00" in fig.axes[0].get_title(loc="left")           # Spearman of Qini with N
    designs = pd.DataFrame({"design": ["rider_ab", "cluster_switchback cụm all"], "per_day": [1600.0, 1250],
                            "ci_lo": [1500.0, 1050], "ci_hi": [1700.0, 1450], "GTE": [1200.0] * 2,
                            "GTE_se": [14.0] * 2, "bias_pct": [33.3, 4.2]})
    assert len(figures.design_bias_figure(designs).axes) == 1
    conf = pd.DataFrame({"target_g_u": [0.0, 1.0, 2.0], "bias_naive": [-0.01, 0.03, 0.06],
                         "bias_naive_lo": [-0.02, 0.02, 0.05], "bias_naive_hi": [0.0, 0.04, 0.07],
                         "bias_adjusted": [0.0, 0.04, 0.07], "bias_adjusted_lo": [-0.01, 0.03, 0.06],
                         "bias_adjusted_hi": [0.01, 0.05, 0.08]})
    assert len(figures.u_latent_figure(conf).axes) == 1
