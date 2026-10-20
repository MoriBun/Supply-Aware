"""M4 RiderChoice: logit booking decision and hidden ground-truth probabilities.

Contract: ``decide`` reads the rows ``ctx.tick_sessions`` of ``ctx.sessions``,
writes ``requested`` and the hidden ``p_request_*`` columns, creates one order
per booking (``ctx.orders``, columns of step "decide"), sets ``order_idx`` on
the session, and calls ``ctx.ledger.commit`` / ``release_reserved`` for sessions
with ``voucher_dong > 0``.

The decision uses only what step 5 wrote on the session (fare, quoted ETA,
granted voucher) and the rider's own coefficients; the pre-drawn ``u_book``
makes a rider facing the same quote decide the same way under every policy.
"""

from __future__ import annotations

import numpy as np

from sim.budget import DONG_PER_VND
from sim.pricing import voucher_dong
from sim.state import SimContext


def request_probability(alpha, beta_price, beta_eta, delta, fare_vnd, voucher_vnd, eta_min) -> np.ndarray:
    """Booking probability, vectorized, float64.

    ``logit = alpha + beta_price * (p - v) + beta_eta * eta_q + delta * 1[v > 0]``,
    ``P = 1 / (1 + exp(-logit))``.
    """
    v = np.asarray(voucher_vnd, dtype=np.float64)
    logit = (np.asarray(alpha, dtype=np.float64)
             + np.asarray(beta_price, dtype=np.float64) * (np.asarray(fare_vnd, dtype=np.float64) - v)
             + np.asarray(beta_eta, dtype=np.float64) * np.asarray(eta_min, dtype=np.float64)
             + np.asarray(delta, dtype=np.float64) * (v > 0))
    return np.exp(-np.logaddexp(0.0, -logit))   # stable form of 1 / (1 + exp(-logit))


def decide(ctx: SimContext, t: float) -> None:
    """Engine step 6: each session of this tick books or leaves; bookings become Waiting orders."""
    start, stop = ctx.tick_sessions
    if stop <= start:
        return
    sessions, riders = ctx.sessions, ctx.world.riders
    rows = slice(start, stop)

    rider = sessions.rider_id[rows]
    alpha, beta_price = riders.alpha[rider], riders.beta_price[rider]
    beta_eta, delta = riders.beta_eta[rider], riders.delta_promo[rider]
    fare = sessions.quoted_fare_vnd[rows].astype(np.float64)
    eta = sessions.quoted_eta_min[rows]            # already max_pickup_eta_min when no_supply
    granted_dong = sessions.voucher_dong[rows]
    granted_vnd = granted_dong / DONG_PER_VND

    p_actual = request_probability(alpha, beta_price, beta_eta, delta, fare, granted_vnd, eta)
    requested = sessions.u_book[rows] < p_actual
    sessions.requested[rows] = requested

    # Hidden ground truth in the same context: with the full voucher and with none.
    treat_vnd = voucher_dong(ctx.cfg, fare) / DONG_PER_VND
    p_treat = request_probability(alpha, beta_price, beta_eta, delta, fare, treat_vnd, eta)
    p_control = request_probability(alpha, beta_price, beta_eta, delta, fare, 0.0, eta)
    sessions.p_request_treat[rows] = p_treat
    sessions.p_request_control[rows] = p_control
    sessions.direct_request_effect_fixed_market[rows] = p_treat - p_control

    # Budget: a granted voucher is committed by a booking and released otherwise.
    session_id = sessions.session_id[rows]
    ledger = ctx.ledger
    for i in np.flatnonzero(granted_dong > 0):
        if requested[i]:
            ledger.commit(int(session_id[i]))
        else:
            ledger.release_reserved(int(session_id[i]))

    booked = np.flatnonzero(requested)
    if len(booked) == 0:
        return
    session_idx = start + booked
    pu_cell = sessions.pu_cell[session_idx]

    orders = ctx.orders
    o_start, o_stop = orders.reserve(len(booked))
    o_rows = slice(o_start, o_stop)
    orders.order_id[o_rows] = session_id[booked]       # order_id = session_id
    orders.session_id[o_rows] = session_id[booked]
    orders.rider_id[o_rows] = rider[booked]
    orders.pu_cell[o_rows] = pu_cell
    orders.do_cell[o_rows] = sessions.do_cell[session_idx]
    orders.request_time_s[o_rows] = t
    orders.gross_fare_vnd[o_rows] = fare[booked]
    orders.voucher_value_vnd[o_rows] = granted_vnd[booked]
    orders.net_fare_vnd[o_rows] = fare[booked] - granted_vnd[booked]
    orders.in_window[o_rows] = ctx.clock.in_window(t)
    orders.session_idx[o_rows] = session_idx
    orders.voucher_dong[o_rows] = granted_dong[booked]
    sessions.order_idx[session_idx] = np.arange(o_start, o_stop)

    new_per_cell = np.bincount(pu_cell, minlength=ctx.n_cells)
    ctx.cells.waiting += new_per_cell.astype(ctx.cells.waiting.dtype)
    ctx.slot_counters.n_requests += new_per_cell.astype(ctx.slot_counters.n_requests.dtype)  # by open time
