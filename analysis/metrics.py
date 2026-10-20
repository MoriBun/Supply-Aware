"""Evaluation metrics: policy value with CI, paired differences, Qini / AUUC.

Policy value comes from ``results/policy_results`` (one row per seed). Standard
errors are over seeds; the normal-approximation CI uses ``z``; a bootstrap CI
is available when the number of seeds is small. Policies run with common random
numbers, so two policies are compared by the paired difference by seed (A2(b)).

The Qini and uplift curves are the plain cumulative definitions (Radcliffe
2007; the "uplift curve" of scikit-uplift), with tied scores grouped into one
point so a constant score gives a straight line. Areas are over the fraction
targeted (x in [0, 1]) so runs of different size are comparable.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

Z95 = 1.959963984540054


def _se(values: pd.Series) -> float:
    n = int(values.count())
    return float(values.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan")


def value_table(policy_results: pd.DataFrame, *, z: float = Z95) -> pd.DataFrame:
    """N(pi), V(pi) and spend per policy (and theta when present): mean, SE and normal CI over seeds."""
    keys = ["policy"]
    if "theta" in policy_results.columns and policy_results["theta"].notna().any():
        keys.append("theta")
    rows = []
    for key, g in policy_results.groupby(keys, dropna=False, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(keys, key))
        row["n_seeds"] = int(len(g))
        for metric, prefix in (("N_completed", "N"), ("V_profit_vnd", "V")):
            mean, se = float(g[metric].mean()), _se(g[metric])
            row[f"{prefix}_mean"], row[f"{prefix}_se"] = mean, se
            row[f"{prefix}_lo"], row[f"{prefix}_hi"] = mean - z * se, mean + z * se
        row["spent_mean"] = float(g["voucher_spent_vnd"].mean())
        rows.append(row)
    return pd.DataFrame(rows)


def paired_difference(policy_results: pd.DataFrame, a: str, b: str, *, metric: str = "N_completed",
                      theta_a: float | None = None, theta_b: float | None = None, z: float = Z95) -> dict:
    """``metric(a) - metric(b)`` paired by seed (common random numbers): mean, SE, CI, share of seeds with a > b."""
    def pick(name, theta):
        sel = policy_results["policy"] == name
        if theta is not None:
            sel &= np.isclose(policy_results["theta"].astype(float), theta)
        return policy_results.loc[sel].set_index("seed")[metric].astype(float)

    diff = (pick(a, theta_a) - pick(b, theta_b)).dropna()
    n = int(len(diff))
    if n == 0:
        raise ValueError(f"no common seeds between {a!r} and {b!r}")
    mean, se = float(diff.mean()), _se(diff)
    return {"a": a, "b": b, "metric": metric, "n_seeds": n, "mean": mean, "se": se,
            "lo": mean - z * se, "hi": mean + z * se, "share_a_better": float((diff > 0).mean())}


def bootstrap_mean_ci(values, *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean over seeds (analysis code may use numpy's generator directly)."""
    x = np.asarray(values, dtype=np.float64)
    if len(x) == 0:
        return float("nan"), float("nan")
    gen = np.random.default_rng(seed)
    means = gen.choice(x, size=(n_boot, len(x)), replace=True).mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def _by_seed(policy_results: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Sweep results as a seed x theta table of ``metric`` (seeds missing for some theta are dropped)."""
    wide = policy_results.pivot_table(index="seed", columns="theta", values=metric, aggfunc="first")
    return wide.astype(float).dropna().sort_index(axis=1)


def t_quantile(p: float, df: int) -> float:
    """Student-t quantile from the Cornish-Fisher expansion around the normal quantile.

    Accurate to about 0.01 for df >= 3 (2.262 for p = 0.975, df = 9), which is enough for
    confidence bounds; it avoids a scipy dependency.
    """
    z = NormalDist().inv_cdf(p)
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    g4 = (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z) / 92160
    return z + g1 / df + g2 / df**2 + g3 / df**3 + g4 / df**4


def theta_star_set(policy_results: pd.DataFrame, *, metric: str = "N_completed", alpha: float = 0.05) -> pd.DataFrame:
    """Which thetas of a sweep are statistically as good as the best one.

    Multiple comparisons with the best: for every theta, the paired difference ``best - theta`` by
    seed (common random numbers), its SE, and a simultaneous one-sided lower bound ``gap_lo`` =
    gap - crit x SE with ``crit`` the Student-t quantile at ``1 - alpha / (k - 1)`` (Bonferroni
    over the k - 1 comparisons, df = seeds - 1). ``in_set`` = the bound is <= 0: theta cannot be
    told from the best at family-wise level ``alpha``. When N(pi_theta) has a plateau, theta* is
    this set, not a point; more seeds shrink it.
    """
    wide = _by_seed(policy_results, metric)
    means = wide.mean()
    best = float(means.idxmax())
    n, k = len(wide), len(wide.columns)
    crit = t_quantile(1.0 - alpha / max(k - 1, 1), n - 1) if n > 1 else float("nan")
    rows = []
    for theta in wide.columns:
        diff = wide[best] - wide[theta]
        gap = float(diff.mean())
        se = float(diff.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan")
        rows.append({"theta": float(theta), "mean": float(means[theta]), "gap_to_best": gap, "gap_se": se,
                     "gap_lo": gap - crit * se, "gap_hi": gap + crit * se,
                     "in_set": bool(theta == best or gap - crit * se <= 0.0), "n_seeds": n, "crit": crit})
    return pd.DataFrame(rows)


def theta_star_interval(star: pd.DataFrame) -> tuple[float, float, float]:
    """``(best theta, lower end, upper end)``: the hull of the ``in_set`` thetas of :func:`theta_star_set`.

    The hull, not the contiguous run around the best: kappa-auto takes each theta's kappa from its
    own pilot run, which adds a theta-specific error that the paired-by-seed SE does not see, so a
    single theta inside the plateau can drop out of the set (:func:`theta_star_gaps` lists them).
    """
    s = star.sort_values("theta")
    inside = s.loc[s["in_set"], "theta"]
    return float(s.loc[s["mean"].idxmax(), "theta"]), float(inside.min()), float(inside.max())


def theta_star_gaps(star: pd.DataFrame) -> list[float]:
    """Thetas inside the hull of the theta* set that are not themselves in the set."""
    _, lo, hi = theta_star_interval(star)
    s = star.sort_values("theta")
    return [float(t) for t in s.loc[(s["theta"] > lo) & (s["theta"] < hi) & ~s["in_set"], "theta"]]


def sweep_by_theta(policy_results: pd.DataFrame, *, metric: str = "N_completed", alpha: float = 0.05) -> pd.DataFrame:
    """One row per theta of a sweep: mean and SE over seeds, share of (cell, slot) off, spend, theta* membership."""
    g = policy_results.groupby("theta")
    out = pd.DataFrame({"mean": g[metric].mean(), "se": g[metric].std(ddof=1) / np.sqrt(g.size()),
                        "share_cells_off": g["share_cells_off"].mean(), "spent_mean": g["voucher_spent_vnd"].mean()})
    out["spent_over_B"] = out["spent_mean"] / float(policy_results["budget_B_vnd"].iloc[0])
    out["in_set"] = theta_star_set(policy_results, metric=metric, alpha=alpha).set_index("theta")["in_set"]
    return out.reset_index()


def sweep_overview(sweeps: dict[str, pd.DataFrame], *, theta_hats=(), metric: str = "N_completed") -> pd.DataFrame:
    """Headline numbers of several sweeps (``{name: policy_results}``): best theta, theta* hull, regret.

    ``regret_<theta>`` (with ``_se``) is :func:`sweep_regret` at theta = 0 and at each of ``theta_hats``.
    """
    rows = []
    for name, runs in sweeps.items():
        star = theta_star_set(runs, metric=metric)
        best, lo, hi = theta_star_interval(star)
        means = runs.groupby("theta")[metric].mean()
        row = {"sweep": name, "theta_best": best, "mean_best": float(means[best]), "star_lo": lo, "star_hi": hi,
               "star_gaps": theta_star_gaps(star), "mean_theta0": float(means.get(0.0, np.nan)),
               "n_seeds": int(star["n_seeds"].iloc[0])}
        for theta in (0.0, *theta_hats):
            reg = sweep_regret(runs, theta, metric=metric)
            row[f"regret_{theta:g}"], row[f"regret_{theta:g}_se"] = reg["regret"], reg["se"]
        rows.append(row)
    return pd.DataFrame(rows)


def sweep_regret(policy_results: pd.DataFrame, theta_hat: float, *, metric: str = "N_completed",
                 alpha: float = 0.05) -> dict:
    """Regret of running pi_theta at ``theta_hat`` instead of the sweep's best theta: ``N(best) - N(theta_hat)``.

    This, not ``|theta_hat - theta*|``, scores an estimated threshold (the criterion is N):
    inside a plateau the regret is about 0 whatever the distance. On a grid point it is the paired
    difference by seed; between grid points each seed's curve is interpolated linearly in theta;
    outside the grid it is an error. The CI is a two-sided Student-t interval for this one
    comparison (``theta_hat`` is fixed in advance, so no multiplicity correction).
    """
    wide = _by_seed(policy_results, metric)
    thetas = wide.columns.to_numpy(dtype=float)
    if not thetas.min() <= theta_hat <= thetas.max():
        raise ValueError(f"theta_hat {theta_hat} is outside the sweep grid [{thetas.min()}, {thetas.max()}]")
    best = float(wide.mean().idxmax())
    at_hat = np.array([np.interp(theta_hat, thetas, row) for row in wide.to_numpy()])
    diff = wide[best].to_numpy() - at_hat
    n = len(diff)
    mean = float(diff.mean())
    se = float(diff.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan")
    crit = t_quantile(1.0 - alpha / 2.0, n - 1) if n > 1 else float("nan")
    return {"theta_hat": float(theta_hat), "theta_best": best, "regret": mean, "se": se, "lo": mean - crit * se,
            "hi": mean + crit * se, "relative": mean / float(wide[best].mean()), "n_seeds": n}


def uplift_curve(y, t, score) -> pd.DataFrame:
    """Cumulative Qini and uplift curves on sessions ranked by score (descending), ties grouped.

    Columns: ``k`` (sessions targeted), ``frac`` (= k / n), ``n_t``, ``n_c``, ``y_t``, ``y_c``,
    ``qini`` = ``y_t - y_c * n_t / n_c`` and ``uplift`` = ``(y_t/n_t - y_c/n_c) * k``; a term with an
    empty group counts as 0. The first row is the origin.
    """
    y = np.asarray(y, dtype=np.float64)
    t = np.asarray(t).astype(bool)
    s = np.asarray(score, dtype=np.float64)
    if not (len(y) == len(t) == len(s)):
        raise ValueError("y, t and score must have the same length")
    n = len(y)
    order = np.argsort(-s, kind="stable")
    s_sorted = s[order]
    # One point per distinct score: the last index of each tie group.
    ends = np.flatnonzero(np.r_[s_sorted[1:] != s_sorted[:-1], True]) if n else np.zeros(0, dtype=int)
    cum_t = np.cumsum(t[order])
    cum_yt = np.cumsum(y[order] * t[order])
    cum_yc = np.cumsum(y[order] * ~t[order])
    k = ends + 1
    n_t = cum_t[ends].astype(np.float64)
    n_c = k - n_t
    y_t, y_c = cum_yt[ends], cum_yc[ends]
    with np.errstate(divide="ignore", invalid="ignore"):
        qini = y_t - np.where(n_c > 0, y_c * n_t / n_c, 0.0)
        rate_t = np.where(n_t > 0, y_t / n_t, 0.0)
        rate_c = np.where(n_c > 0, y_c / n_c, 0.0)
        uplift = (rate_t - rate_c) * k
    frame = pd.DataFrame({"k": k, "frac": k / n if n else k, "n_t": n_t, "n_c": n_c, "y_t": y_t, "y_c": y_c,
                          "qini": qini, "uplift": uplift})
    origin = pd.DataFrame({c: [0.0] for c in frame.columns})
    return pd.concat([origin, frame], ignore_index=True)


def qini_auuc(y, t, score) -> dict[str, float]:
    """Areas over the fraction targeted: Qini area, its random-ranking baseline, their difference, and AUUC.

    ``qini_coef`` = area under the Qini curve minus the area under the straight line from the origin
    to the curve's end point (random ranking). NaN when there is no treated or no control session.
    """
    curve = uplift_curve(y, t, score)
    t = np.asarray(t).astype(bool)
    if len(curve) < 2 or t.all() or not t.any():
        return {k: float("nan") for k in ("qini_area", "qini_random_area", "qini_coef", "auuc", "qini_end")}
    x = curve["frac"].to_numpy()
    qini_area = float(np.trapezoid(curve["qini"].to_numpy(), x))
    qini_end = float(curve["qini"].iloc[-1])
    random_area = 0.5 * qini_end
    auuc = float(np.trapezoid(curve["uplift"].to_numpy(), x))
    return {"qini_area": qini_area, "qini_random_area": random_area, "qini_coef": qini_area - random_area,
            "auuc": auuc, "qini_end": qini_end}
