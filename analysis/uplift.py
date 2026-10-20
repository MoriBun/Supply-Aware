"""DR-learner for the effect of a voucher on ``completed`` (Kennedy 2023).

Two targets, both learned from a legacy run (observed columns only):

- ``tau_x``:  tau(x)    = E[Y(1) - Y(0) | rider features]
- ``tau_xs``: tau(x, s) = E[Y(1) - Y(0) | rider features, slack_lag_slot of the pickup cell]

Steps, with ``k`` cross-fitting folds split by rider (a rider's sessions stay in one fold):

1. Nuisances on the other folds: outcome models ``mu1``, ``mu0`` (completion with and without a
   voucher) and the propensity ``e`` (chance of an offer). Sessions of the explore slice have a
   known propensity (``propensity`` column, 0.5) and use it; ``e`` is only learned for the rest.
2. Pseudo-outcome ``phi = mu1 - mu0 + T (Y - mu1) / e - (1 - T) (Y - mu0) / (1 - e)``, with ``e``
   clipped to ``[e_clip, 1 - e_clip]``.
3. Regress ``phi`` on the target features: the fitted regression is tau-hat.

``sample = "explore"`` uses the explore slice only (randomized, unbiased, small); ``sample =
"all"`` uses every legacy session. The legacy rule targets riders with the hidden ``u_latent``,
which no observed column carries, so ``"all"`` keeps part of that confounding.

The score functions (``"analysis.uplift:tau_x_dr"`` and friends) load the saved boosters from
``analysis/models`` once per process and only read ``SessionBatch`` columns, so they work inside
spawned runner workers. The ``*_per_dollar`` variants divide by the expected voucher
cost of an offer, ``E[voucher paid | offer]`` (paid only when the trip completes), because the
budget B is a sum of money (đồng; the suffix keeps its old name). The cost is learned on the same features as the effect it divides:
``cost_x`` on x for tau(x), ``cost_xs`` on (x, s) for tau(x, s). A cost that also used the fare
would favour short trips whose (smaller) voucher the effect model cannot see.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

MODEL_DIR = Path(__file__).resolve().parent / "models"

X_COLS = ["x_freq", "x_tenure", "x_segment"]
S_COLS = ["slack_lag_slot"]
# Pre-treatment context for the nuisance models: everything the policy saw before the offer.
CONTEXT_COLS = ["slack_lag_slot", "slack_lag_day", "hour", "quoted_fare_vnd", "quoted_eta_min", "no_supply",
                "promo_on_cell"]

# Analysis settings (not simulator parameters); saved next to every fitted model.
DEFAULTS = {
    "k_folds": 5,
    "e_clip": 0.02,
    "nuisance": {"objective": "binary", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 200,
                 "num_boost_round": 200},
    "final": {"objective": "regression", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 2000,
              "lambda_l2": 10.0, "num_boost_round": 150},
    "cost": {"objective": "regression", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 500,
             "num_boost_round": 150},
    "cost_floor_frac": 0.05,      # predicted cost floored at this share of the mean paid per offer
    "seed": 0,
}


# --- data -------------------------------------------------------------------------


def uplift_frame(run: dict[str, pd.DataFrame], slack_cap: float) -> pd.DataFrame:
    """In-window, non-blocked sessions of a legacy run with features, ``T`` (arm), ``Y`` (completed), cost.

    ``slack_lag_*`` of the pickup cell come from ``market/slot_snapshots``; ``inf`` is capped at
    ``slack_cap`` the way the threshold policy caps its forecast. ``budget_blocked`` sessions are
    dropped: their propensity is no longer valid.
    """
    from analysis.io import completed_outcome

    s = run["sessions"]
    s = s[s["in_window"] & ~s["budget_blocked"]].reset_index(drop=True)
    y = completed_outcome(s, run["orders"])
    snaps = run["slot_snapshots"][["slot", "cell", "slack_lag_slot", "slack_lag_day"]].rename(columns={"cell": "pu_cell"})
    f = s.merge(snaps, on=["slot", "pu_cell"], how="left", validate="many_to_one")
    f = f.merge(run["riders"][["rider_id", *X_COLS]], on="rider_id", how="left", validate="many_to_one")
    for c in ["slack_lag_slot", "slack_lag_day"]:
        f[c] = np.minimum(f[c].to_numpy(np.float64), slack_cap)
    f["T"] = f["arm"].astype(np.int8)
    f["Y"] = y
    f["known_e"] = np.where(f["assign_mechanism"] == "explore", f["propensity"].astype(np.float64), np.nan)
    return f


def rider_folds(rider_id, k: int) -> np.ndarray:
    """Fold of each row; all sessions of a rider share a fold (riders come back many times)."""
    return (np.asarray(rider_id, dtype=np.int64) * 2654435761 % 2**32) % k


# --- fitting ----------------------------------------------------------------------


def _matrix(frame: pd.DataFrame, cols: list[str]) -> np.ndarray:
    return frame[cols].to_numpy(np.float64)


def _train(params: dict, X: np.ndarray, y: np.ndarray, seed: int) -> lgb.Booster:
    p = dict(params)
    rounds = p.pop("num_boost_round")
    p.update(seed=seed, deterministic=True, force_col_wise=True, num_threads=1, verbose=-1)
    return lgb.train(p, lgb.Dataset(X, label=y, free_raw_data=True), num_boost_round=rounds)


def cross_fit_nuisances(f: pd.DataFrame, params: dict = DEFAULTS) -> pd.DataFrame:
    """Out-of-fold ``mu0``, ``mu1``, ``e`` for every row (``e`` = known propensity where there is one)."""
    k, seed = params["k_folds"], params["seed"]
    feats = X_COLS + CONTEXT_COLS
    X = _matrix(f, feats)
    t, y = f["T"].to_numpy(), f["Y"].to_numpy(np.float64)
    known = f["known_e"].to_numpy()
    fold = rider_folds(f["rider_id"], k)
    mu0, mu1, e = np.empty(len(f)), np.empty(len(f)), np.empty(len(f))
    for j in range(k):
        tr, te = fold != j, fold == j
        mu1[te] = _train(params["nuisance"], X[tr & (t == 1)], y[tr & (t == 1)], seed + j).predict(X[te])
        mu0[te] = _train(params["nuisance"], X[tr & (t == 0)], y[tr & (t == 0)], seed + j).predict(X[te])
        learn = tr & np.isnan(known)
        if learn.any():
            e[te] = _train(params["nuisance"], X[learn], t[learn].astype(np.float64), seed + j).predict(X[te])
        else:
            e[te] = np.nan
    e = np.where(np.isnan(known), e, known)
    return pd.DataFrame({"mu0": mu0, "mu1": mu1, "e": e}, index=f.index)


def pseudo_outcome(y, t, mu0, mu1, e, e_clip: float) -> np.ndarray:
    """Doubly robust pseudo-outcome (Kennedy 2023, eq. for the DR-learner)."""
    e = np.clip(np.asarray(e, dtype=np.float64), e_clip, 1.0 - e_clip)
    y, t = np.asarray(y, dtype=np.float64), np.asarray(t, dtype=np.float64)
    return mu1 - mu0 + t * (y - mu1) / e - (1.0 - t) * (y - mu0) / (1.0 - e)


def fit_dr(f: pd.DataFrame, *, sample: str = "explore", params: dict = DEFAULTS) -> dict:
    """Fit tau(x), tau(x, s) and the cost of an offer; returns boosters and a summary."""
    if sample == "explore":
        f = f[f["assign_mechanism"] == "explore"].reset_index(drop=True)
    elif sample != "all":
        raise ValueError(f"sample must be 'explore' or 'all', got {sample!r}")
    nu = cross_fit_nuisances(f, params)
    phi = pseudo_outcome(f["Y"], f["T"], nu["mu0"], nu["mu1"], nu["e"], params["e_clip"])
    seed = params["seed"]
    treated = f["T"].to_numpy() == 1
    paid = f["voucher_value_vnd"].to_numpy(np.float64) * f["Y"].to_numpy(np.float64)   # paid only on completion
    boosters = {
        "tau_x": _train(params["final"], _matrix(f, X_COLS), phi, seed),
        "tau_xs": _train(params["final"], _matrix(f, X_COLS + S_COLS), phi, seed),
        "cost_x": _train(params["cost"], _matrix(f[treated], X_COLS), paid[treated], seed),
        "cost_xs": _train(params["cost"], _matrix(f[treated], X_COLS + S_COLS), paid[treated], seed),
    }
    summary = {
        "sample": sample, "n": int(len(f)), "n_treated": int(treated.sum()),
        "ate_dr": float(phi.mean()), "ate_dr_se": float(phi.std(ddof=1) / np.sqrt(len(phi))),
        "ate_naive": float(f.loc[treated, "Y"].mean() - f.loc[~treated, "Y"].mean()),
        "mean_paid_per_offer_vnd": float(paid[treated].mean()),
        "share_e_clipped": float(np.mean((nu["e"] < params["e_clip"]) | (nu["e"] > 1 - params["e_clip"]))),
    }
    return {"boosters": boosters, "summary": summary, "phi": phi, "nuisances": nu}


# --- saving and score functions ---------------------------------------------------


def save_model(fit: dict, name: str, *, slack_cap: float, run_id: str, params: dict = DEFAULTS,
               model_dir: Path = MODEL_DIR) -> Path:
    """Boosters as LightGBM text files plus ``<name>.json`` with the settings the score functions need."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for key, booster in fit["boosters"].items():
        booster.save_model(str(model_dir / f"{name}_{key}.txt"))
    meta = {"name": name, "outcome": "completed", "learner": "DR-learner (Kennedy 2023), cross-fitted by rider",
            "run_id": run_id, "slack_cap": slack_cap, "x_cols": X_COLS, "s_cols": S_COLS,
            "params": params, **fit["summary"]}
    path = model_dir / f"{name}.json"
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return path


@lru_cache(maxsize=4)
def load_model(name: str = "dr", model_dir: str = str(MODEL_DIR)) -> tuple[dict, dict[str, lgb.Booster]]:
    d = Path(model_dir)
    meta = json.loads((d / f"{name}.json").read_text(encoding="utf-8"))
    boosters = {k: lgb.Booster(model_file=str(d / f"{name}_{k}.txt")) for k in BOOSTER_COLS}
    return meta, boosters


def batch_features(batch, s_hat: np.ndarray, slack_cap: float) -> dict[str, np.ndarray]:
    """The model columns of a ``SessionBatch``; ``slack_lag_slot`` is the policy's forecast ``s_hat``."""
    return {"x_freq": batch.x_freq, "x_tenure": batch.x_tenure, "x_segment": batch.x_segment,
            "slack_lag_slot": np.minimum(np.asarray(s_hat, dtype=np.float64), slack_cap)}


# Feature columns of each booster, as keys of the model metadata.
BOOSTER_COLS = {"tau_x": ("x_cols",), "tau_xs": ("x_cols", "s_cols"), "cost_x": ("x_cols",),
                "cost_xs": ("x_cols", "s_cols")}


def _predict(name: str, key: str, feats: dict[str, np.ndarray]) -> np.ndarray:
    meta, boosters = load_model(name)
    cols = [c for group in BOOSTER_COLS[key] for c in meta[group]]
    X = np.column_stack([np.asarray(feats[c], dtype=np.float64) for c in cols])
    return boosters[key].predict(X, num_threads=1)


KVND = 1000.0   # scores "per 1.000đ": tau / (expected đồng paid per offer / 1000)


def predict_scores(name: str, feats: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """``tau_x``, ``tau_xs``, their per-1.000đ versions (``*_per_kvnd``) and, for older
    specs, the plain ratio ``*_per_dollar`` = tau / expected đồng paid (same ranking, 1000x smaller)."""
    meta, _ = load_model(name)
    tau_x, tau_xs = _predict(name, "tau_x", feats), _predict(name, "tau_xs", feats)
    floor = meta["params"]["cost_floor_frac"] * meta["mean_paid_per_offer_vnd"]
    cost_x = np.maximum(_predict(name, "cost_x", feats), floor)      # expected đồng paid per offer
    cost_xs = np.maximum(_predict(name, "cost_xs", feats), floor)
    return {"tau_x": tau_x, "tau_xs": tau_xs,
            "tau_x_per_dollar": tau_x / cost_x, "tau_xs_per_dollar": tau_xs / cost_xs,
            "tau_x_per_kvnd": KVND * tau_x / cost_x, "tau_xs_per_kvnd": KVND * tau_xs / cost_xs}


def _score(batch, s_hat, which: str, name: str) -> np.ndarray:
    meta, _ = load_model(name)
    return predict_scores(name, batch_features(batch, s_hat, meta["slack_cap"]))[which].astype(np.float32)


# Score functions for ``policy.threshold.score_fn``, one per (sample, world, unit):
#   tau_{x|xs}_dr[_all][_w1][_per_dollar|_per_kvnd]
# ``dr_explore`` is learned on the explore slice only (unbiased); ``dr_all`` on every legacy session
# (more data, keeps the u_latent confounding). No suffix = world W0 (models ``dr_explore``, ``dr_all``);
# ``_w1`` = world W1 (models ``dr_explore_w1``, ``dr_all_w1``). The functions are built here
# at import time so that ``sim.policies.scores.load_score_fn("analysis.uplift:<name>")`` finds them by
# attribute in a spawned worker too; the eight older W0 names (raw and ``_per_dollar``) are unchanged.
MODEL_SUFFIXES = {"": "dr_explore", "_all": "dr_all", "_w1": "dr_explore_w1", "_all_w1": "dr_all_w1"}
UNIT_SUFFIXES = {"": "", "_per_dollar": "_per_dollar", "_per_kvnd": "_per_kvnd"}


def _make_score(fname: str, which: str, model: str) -> "callable":
    def score(batch, s_hat: np.ndarray) -> np.ndarray:
        return _score(batch, s_hat, which, model)

    feat = "rider features and forecast slack" if which.startswith("tau_xs") else "rider features"
    unit = {"": "", "_per_dollar": " per expected đồng paid (legacy name)", "_per_kvnd": " per 1.000đ expected paid"}
    usuf = max((u for u in UNIT_SUFFIXES if fname.endswith(u)), key=len)
    score.__name__ = score.__qualname__ = fname
    score.__doc__ = f"DR effect of a voucher on completion from {feat}, model ``{model}``{unit[usuf]}."
    return score


SCORE_FUNCTIONS: dict[str, "callable"] = {}
for _xx in ("x", "xs"):
    for _msuf, _model in MODEL_SUFFIXES.items():
        for _usuf in UNIT_SUFFIXES:
            _fname = f"tau_{_xx}_dr{_msuf}{_usuf}"
            _fn = _make_score(_fname, f"tau_{_xx}{_usuf}", _model)
            globals()[_fname] = _fn
            SCORE_FUNCTIONS[_fname] = _fn
del _xx, _msuf, _model, _usuf, _fname, _fn


def main(argv: list[str]) -> int:
    """``python -m analysis.uplift fit <legacy run dir> [config.yaml ...] [--suffix w1]``: fit and save
    ``dr_explore<suffix>`` and ``dr_all<suffix>`` (suffix ``_w1`` for world W1)."""
    from analysis.io import load_run
    from sim.config import load_config

    args = list(argv[1:])
    suffix = ""
    if "--suffix" in args:
        i = args.index("--suffix")
        suffix = "_" + args[i + 1].lstrip("_")
        del args[i:i + 2]
    if len(args) < 2 or args[0] != "fit":
        print("usage: python -m analysis.uplift fit <legacy run dir> [config.yaml ...] [--suffix w1]")
        return 2
    configs = args[2:] or [Path(__file__).resolve().parents[1] / "config" / "default.yaml"]
    cfg = load_config(configs)
    run = load_run(Path(args[1]))
    run_id = str(run["run_metadata"]["run_id"].iloc[0])
    cap = float(cfg.monitor.slack_cap)
    frame = uplift_frame(run, cap)
    for sample in ("explore", "all"):
        fit = fit_dr(frame, sample=sample)
        path = save_model(fit, f"dr_{sample}{suffix}", slack_cap=cap, run_id=run_id)
        sm = fit["summary"]
        print(f"saved {path}: n = {sm['n']} ({sm['n_treated']} offered), ATE DR {sm['ate_dr']:.4f} "
              f"(se {sm['ate_dr_se']:.4f}), naive {sm['ate_naive']:.4f}, paid/offer {sm['mean_paid_per_offer_vnd']:.0f}đ, "
              f"e clipped {sm['share_e_clipped']:.1%}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
