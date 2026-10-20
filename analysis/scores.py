"""Uplift scores for the rider tier of pi_theta, learned from simulator output.

Use one as ``policy.threshold.score_fn = "analysis.scores:tau_x_baseline"``. A score
function only reads columns of the ``SessionBatch`` it is given, is deterministic, and
loads its table from a small JSON file next to this module, so it also works inside a
spawned worker process.

``tau_x_baseline`` is the simple baseline: the difference in completion rate between
treated and untreated sessions of the **explore slice** of the legacy data (the randomized
5%, propensity 0.5), by rider segment x frequency tercile, shrunk towards the overall
effect. It uses observed columns only.

``tau_per_dollar_baseline`` divides that effect by the expected voucher cost of an offer
in the stratum (a voucher is only paid when the trip completes). The budget B is a sum
of money (đồng), so under B the offers worth making first are those with the most extra trips
per đồng of voucher, not the most extra trips per offer.

``predict_frame`` gives the same scores for every session of a saved run, so the ranking can be
judged offline (Qini / AUUC) by joining on ``session_id``; the CLI writes it to parquet.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

MODEL_DIR = Path(__file__).resolve().parent / "models"
BASELINE_PATH = MODEL_DIR / "tau_x_baseline.json"


def fit_tau_strata(sessions: pd.DataFrame, riders: pd.DataFrame, completed: np.ndarray) -> dict:
    """Stratified effect on ``completed`` from the explore slice of a legacy run.

    ``sessions`` are the in-window sessions of ``observed/sessions`` and ``completed`` their
    outcome (``analysis.io.completed_outcome``). Strata are ``x_segment`` x tercile of
    ``x_freq`` (terciles of the rider population). Each stratum effect is shrunk towards the
    overall effect with the usual precision weight ``b2 / (b2 + se^2)``, where ``b2`` is the
    between-strata variance left after removing sampling noise.
    """
    s = sessions.assign(y=np.asarray(completed, dtype=np.float64))
    s = s[(s["assign_mechanism"] == "explore") & ~s["budget_blocked"]]
    s = s.merge(riders[["rider_id", "x_freq", "x_segment"]], on="rider_id", how="left")
    edges = np.quantile(riders["x_freq"].to_numpy(np.float64), [1 / 3, 2 / 3])
    n_seg = int(riders["x_segment"].max()) + 1
    freq_bin = np.searchsorted(edges, s["x_freq"].to_numpy(np.float64), side="right")
    seg = s["x_segment"].to_numpy(np.int64)
    arm, y = s["arm"].to_numpy() == 1, s["y"].to_numpy()

    cost = (s["voucher_value_vnd"].to_numpy(np.float64) * y)       # paid only when the trip completes

    def effect(mask: np.ndarray) -> tuple[float, float, int, int]:
        y1, y0 = y[mask & arm], y[mask & ~arm]
        if len(y1) == 0 or len(y0) == 0:
            return np.nan, np.nan, len(y1), len(y0)
        var = y1.var() / len(y1) + y0.var() / len(y0)
        return float(y1.mean() - y0.mean()), float(var), len(y1), len(y0)

    overall, overall_var, n1, n0 = effect(np.ones(len(s), dtype=bool))
    raw = np.full((n_seg, 3), np.nan)
    var = np.full((n_seg, 3), np.nan)
    counts = np.zeros((n_seg, 3), dtype=np.int64)
    cost_per_offer = np.full((n_seg, 3), np.nan)
    for g in range(n_seg):
        for b in range(3):
            mask = (seg == g) & (freq_bin == b)
            raw[g, b], var[g, b], a1, a0 = effect(mask)
            counts[g, b] = a1 + a0
            if a1:
                cost_per_offer[g, b] = cost[mask & arm].mean()
    overall_cost = float(cost[arm].mean())
    ok = np.isfinite(raw)
    between = max(0.0, float(np.var(raw[ok]) - np.mean(var[ok]))) if ok.any() else 0.0
    weight = np.where(ok, between / (between + np.where(ok, var, 1.0)), 0.0) if between > 0 else np.zeros_like(raw)
    tau = np.where(ok, weight * np.where(ok, raw, 0.0) + (1.0 - weight) * overall, overall)
    return {
        "name": "tau_x_baseline",
        "outcome": "completed",
        "source": "explore slice of a legacy run (assign_mechanism = explore, propensity 0.5)",
        "n_treated": n1, "n_control": n0,
        "overall_effect": overall, "overall_se": float(np.sqrt(overall_var)),
        "x_freq_edges": [float(e) for e in edges],
        "raw_effect": raw.tolist(), "n_sessions": counts.tolist(), "between_variance": between,
        "tau": tau.tolist(),
        "overall_cost_per_offer_vnd": overall_cost,
        "cost_per_offer_vnd": np.where(np.isfinite(cost_per_offer), cost_per_offer, overall_cost).tolist(),
    }


def save_table(table: dict, path: Path = BASELINE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    return path


@lru_cache(maxsize=4)
def load_table(path: str = str(BASELINE_PATH)) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(x_freq_edges, tau[segment, freq_bin], cost_per_offer_vnd[segment, freq_bin])`` of a saved table."""
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    return (np.asarray(table["x_freq_edges"], dtype=np.float64), np.asarray(table["tau"], dtype=np.float64),
            np.asarray(table["cost_per_offer_vnd"], dtype=np.float64))


def score_from_table(x_freq, x_segment, edges: np.ndarray, tau: np.ndarray) -> np.ndarray:
    freq_bin = np.searchsorted(edges, np.asarray(x_freq, dtype=np.float64), side="right")
    seg = np.clip(np.asarray(x_segment, dtype=np.int64), 0, tau.shape[0] - 1)
    return tau[seg, freq_bin].astype(np.float32)


def tau_x_baseline(batch, s_hat: np.ndarray) -> np.ndarray:
    """Score function: estimated uplift of a voucher on completion, from rider features only."""
    edges, tau, _ = load_table()
    return score_from_table(batch.x_freq, batch.x_segment, edges, tau)


def tau_per_dollar_baseline(batch, s_hat: np.ndarray) -> np.ndarray:
    """Score function: estimated extra completed trips per đồng of voucher, from rider features only."""
    edges, tau, cost = load_table()
    return score_from_table(batch.x_freq, batch.x_segment, edges, tau / cost)


SCORE_FUNCTIONS = {"tau_x_baseline": tau_x_baseline, "tau_per_dollar_baseline": tau_per_dollar_baseline}


def predict_frame(sessions: pd.DataFrame, riders: pd.DataFrame) -> pd.DataFrame:
    """One row per session: its id, rider, window flag and the score of every function in ``SCORE_FUNCTIONS``.

    Rider features come from ``observed/riders``, as the simulator puts them in the ``SessionBatch``;
    the result is the score the policy would give the session (the scores ignore ``s_hat``).
    """
    feats = sessions[["session_id", "rider_id"]].merge(riders[["rider_id", "x_freq", "x_segment"]],
                                                        on="rider_id", how="left", validate="many_to_one")
    if feats["x_freq"].isna().any():
        raise ValueError("sessions with a rider_id missing from riders")
    edges, tau, cost = load_table()
    table = {"tau_x_baseline": tau, "tau_per_dollar_baseline": tau / cost}
    out = sessions[["session_id", "rider_id", "in_window"]].reset_index(drop=True).copy()
    for name in SCORE_FUNCTIONS:
        out[name] = score_from_table(feats["x_freq"], feats["x_segment"], edges, table[name])
    return out


USAGE = ("usage: python -m analysis.scores fit <legacy run dir>\n"
         "       python -m analysis.scores predict <run dir> <out.parquet>")


def main(argv: list[str]) -> int:
    """``fit <legacy run dir>``: refit and save the baseline table; ``predict <run dir> <out>``: score its sessions."""
    from analysis.io import completed_outcome, load_run

    if len(argv) == 4 and argv[1] == "predict":
        run = load_run(Path(argv[2]))
        pred = predict_frame(run["sessions"], run["riders"])
        trained_on = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["run_id"]
        out = Path(argv[3])
        out.parent.mkdir(parents=True, exist_ok=True)
        pred.to_parquet(out, index=False)
        print(f"saved {out}: {len(pred)} sessions of {run['run_metadata']['run_id'].iloc[0]}, "
              f"table fitted on {trained_on}")
        return 0
    if len(argv) != 3 or argv[1] != "fit":
        print(USAGE)
        return 2
    run = load_run(Path(argv[2]))
    sessions = run["sessions"][run["sessions"]["in_window"]]
    table = fit_tau_strata(sessions, run["riders"], completed_outcome(sessions, run["orders"]))
    table["run_id"] = str(run["run_metadata"]["run_id"].iloc[0])
    path = save_table(table)
    print(f"saved {path}: overall effect {table['overall_effect']:.4f} (se {table['overall_se']:.4f}), "
          f"n = {table['n_treated']} + {table['n_control']}")
    for g, (row, cost) in enumerate(zip(table["tau"], table["cost_per_offer_vnd"])):
        print(f"  segment {g}: tau " + "  ".join(f"{v:.4f}" for v in row)
              + "   | trips per 100.000đ " + "  ".join(f"{100_000 * v / c:.2f}" for v, c in zip(row, cost)))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
