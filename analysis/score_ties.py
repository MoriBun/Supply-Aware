"""Table scores with ties broken at random, for use under a budget.

``policy.threshold.score_fn = "analysis.score_ties:tau_x_baseline"``

The baseline scores of ``analysis.scores`` take one value per stratum, 9 values in all.
kappa-auto keeps or drops a whole group of equal scores, so with so few
groups it can only stop at a group edge: in the policy-table run ``tau_x_baseline`` got kappa = +inf
(the top stratum alone costs more than B, nobody gets a voucher) and
``tau_per_dollar_baseline`` spent 72% of B. Here each score gets ``u_score`` (the pre-drawn
uniform of the session) times half the smallest gap between table values:
the order between strata is unchanged and the order inside a stratum is random, so the budget
can stop inside a stratum. ``u_score`` is a column of the ``SessionBatch``, the same in every
policy, so common random numbers hold.
"""

from __future__ import annotations

import numpy as np

from analysis import scores


def _half_gap(values: np.ndarray) -> float:
    distinct = np.unique(np.asarray(values, dtype=np.float64).ravel())
    return 0.5 * float(np.diff(distinct).min()) if len(distinct) > 1 else 1.0


def break_ties(score: np.ndarray, u: np.ndarray, table: np.ndarray) -> np.ndarray:
    """``score + u * half_gap(table)`` in float64: within-stratum order by ``u``, strata order kept."""
    return np.asarray(score, dtype=np.float64) + np.asarray(u, dtype=np.float64) * _half_gap(table)


def tau_x_baseline(batch, s_hat: np.ndarray) -> np.ndarray:
    """``analysis.scores.tau_x_baseline`` with ties broken at random."""
    _, tau, _ = scores.load_table()
    return break_ties(scores.tau_x_baseline(batch, s_hat), batch.u_score, tau)


def tau_per_dollar_baseline(batch, s_hat: np.ndarray) -> np.ndarray:
    """``analysis.scores.tau_per_dollar_baseline`` with ties broken at random."""
    _, tau, cost = scores.load_table()
    return break_ties(scores.tau_per_dollar_baseline(batch, s_hat), batch.u_score, tau / cost)
