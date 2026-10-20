"""Score functions for the rider tier of pi_theta.

A score function is ``f(batch: SessionBatch, s_hat: np.ndarray) -> np.ndarray``:
``s_hat`` is the forecast supply indicator of each session's pickup cell (float
[n], NaN when the policy does not forecast) and the result is one float per
session, higher = offer first. Built-ins: ``random`` returns the pre-drawn
``u_score`` (a policy never draws numbers itself), ``heuristic_low_freq`` returns
``-x_freq``. Any ``"module:function"`` string is imported at load time, so the
same string works inside a spawned worker process.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

import numpy as np

from sim.config import Config
from sim.policies.base import OracleHiddenView, SessionBatch

ScoreFn = Callable[[SessionBatch, np.ndarray], np.ndarray]


def random_score(batch: SessionBatch, s_hat: np.ndarray) -> np.ndarray:
    return batch.u_score.astype(np.float32)


def heuristic_low_freq(batch: SessionBatch, s_hat: np.ndarray) -> np.ndarray:
    return (-batch.x_freq).astype(np.float32)


BUILTIN_SCORES: dict[str, ScoreFn] = {"random": random_score, "heuristic_low_freq": heuristic_low_freq}
ORACLE = "oracle"


def is_oracle(spec: str) -> bool:
    return spec.strip() == ORACLE


def make_oracle_score(cfg: Config, hidden: OracleHiddenView) -> ScoreFn:
    """Oracle score: extra bookings per 1.000đ of expected voucher,
    ``1000 * (p1 - p0) / (v * p1)``, with ``p1``/``p0`` the booking probabilities of the choice model at
    the quoted fare and ETA with and without the voucher ``v = pricing.voucher_dong(cfg, fare) / DONG_PER_VND`` (the
    voucher the session would really get). ``v = 0`` gives NaN, so the session is not offered.

    Fixed-market oracle: it knows every rider's coefficients but not the externality on other riders, and it
    uses the booking probability in place of the completion probability. Upper bound of policies that rank
    by individual tau only. Only ``make_policy`` builds it (needs the hidden view); ``load_score_fn`` refuses it.
    """
    from sim.budget import DONG_PER_VND        # local imports: sim.choice -> sim.pricing -> sim.policies.base
    from sim.choice import request_probability
    from sim.pricing import voucher_dong

    def oracle(batch: SessionBatch, s_hat: np.ndarray) -> np.ndarray:
        fare = batch.quoted_fare_vnd.astype(np.float64)
        eta = batch.quoted_eta_min.astype(np.float64)
        v = voucher_dong(cfg, fare) / DONG_PER_VND
        alpha, beta_price, beta_eta, delta = hidden.coefficients_of(batch.rider_id)
        p1 = request_probability(alpha, beta_price, beta_eta, delta, fare, v, eta)
        p0 = request_probability(alpha, beta_price, beta_eta, delta, fare, 0.0, eta)
        with np.errstate(divide="ignore", invalid="ignore"):
            score = np.where(v > 0, 1000.0 * (p1 - p0) / (v * p1), np.nan)   # per 1.000đ
        return score.astype(np.float32)

    oracle.__name__ = ORACLE
    return oracle


def import_callable(spec: str) -> Callable:
    """Import ``"module:function"``. Raises ValueError for a malformed spec, a missing module or attribute."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name.strip() or not attr.strip():
        raise ValueError(f"expected 'module:function', got {spec!r}")
    try:
        module = importlib.import_module(module_name.strip())
    except ImportError as exc:
        raise ValueError(f"cannot import module {module_name!r} of {spec!r}: {exc}") from exc
    try:
        fn = getattr(module, attr.strip())
    except AttributeError:
        raise ValueError(f"module {module_name!r} has no attribute {attr!r}") from None
    if not callable(fn):
        raise ValueError(f"{spec!r} is not callable")
    return fn


def load_score_fn(spec: str) -> ScoreFn:
    """``random``, ``heuristic_low_freq`` or ``"module:function"`` (``policy.threshold.score_fn``)."""
    name = spec.strip()
    if name in BUILTIN_SCORES:
        return BUILTIN_SCORES[name]
    if is_oracle(name):
        raise ValueError("score_fn 'oracle' reads hidden coefficients: it is built only by sim.policies.make_policy "
                         "for policy.name = threshold")
    if ":" in name:
        return import_callable(name)
    raise ValueError(f"unknown score_fn {spec!r}: use {sorted(BUILTIN_SCORES)} or 'module:function'")


def score_batch(fn: ScoreFn, batch: SessionBatch, s_hat: np.ndarray) -> np.ndarray:
    """Call ``fn`` and return its scores as float32 [n]; a wrong shape or a non-numeric result is an error."""
    n = len(batch)
    s_hat = np.asarray(s_hat, dtype=np.float64)
    if s_hat.shape != (n,):
        raise ValueError(f"s_hat must have shape ({n},), got {s_hat.shape}")
    out = np.asarray(fn(batch, s_hat))
    if out.shape != (n,):
        raise ValueError(f"score_fn must return shape ({n},), got {out.shape}")
    if out.dtype == bool or not np.issubdtype(out.dtype, np.number):
        raise ValueError(f"score_fn must return numbers, got dtype {out.dtype}")
    return out.astype(np.float32)
