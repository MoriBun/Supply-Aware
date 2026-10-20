"""analysis/animation.py: animation of a policy change on the hex grid.

Small synthetic tables for the 61-cell Hanoi grid (shapes, pivots, totals, captions) and one
real pair of tiny runs through the whole pipeline: totals match ``policy_results`` and a GIF
is written.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("PIL")

from analysis import animation  # noqa: E402
from sim.config import load_config  # noqa: E402
from sim.space import n_cells_for_radius  # noqa: E402
from tests.conftest import ROOT  # noqa: E402

TINY = [ROOT / "config" / "default.yaml", ROOT / "tests" / "fixtures" / "tiny.yaml"]
N61 = n_cells_for_radius(4)
SLOT_S = 900.0


def _synthetic_orders(rng, n: int, n_slots: int, n_cells: int) -> pd.DataFrame:
    t = rng.uniform(0, n_slots * SLOT_S, n)
    status = rng.choice(["Completed", "Abandoned", "Cancelled"], size=n, p=[0.7, 0.2, 0.1])
    return pd.DataFrame({"request_time_s": t, "pu_cell": rng.integers(0, n_cells, n), "status": status,
                         "in_window": t >= 3600.0})


def _synthetic_snapshots(rng, n_slots: int, n_cells: int) -> pd.DataFrame:
    slot = np.repeat(np.arange(n_slots), n_cells)
    cell = np.tile(np.arange(n_cells), n_slots)
    return pd.DataFrame({"slot": slot, "cell": cell, "n_requests": rng.integers(0, 6, len(slot)),
                         "slack": rng.choice([0.0, 0.5, 1.5, np.inf], size=len(slot)),
                         "promo_on": rng.random(len(slot)) > 0.3})


def test_hex_centres_match_the_61_cells_of_radius_4():
    assert N61 == 61
    x, y = animation.hex_centres(4)
    assert x.shape == y.shape == (61,)
    assert np.isclose(np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :])[np.triu_indices(61, 1)].min(),
                      np.sqrt(3.0))                     # neighbouring centres are sqrt(3) apart (circumradius 1)


def test_pivots_have_the_grid_shape_and_keep_every_count():
    rng = np.random.default_rng(0)
    n_slots = 20
    orders = _synthetic_orders(rng, 3000, n_slots, N61)
    snaps = _synthetic_snapshots(rng, n_slots, N61)
    done = animation.completed_by_request_slot(orders, SLOT_S, n_slots, N61)
    done_w = animation.completed_by_request_slot(orders, SLOT_S, n_slots, N61, in_window_only=True)
    req = animation.requests_by_slot(snaps, n_slots, N61)
    slack = animation.field_by_slot(snaps, "slack", n_slots, N61)
    assert done.shape == done_w.shape == req.shape == slack.shape == (n_slots, 61)
    completed = orders[orders["status"] == "Completed"]
    assert done.sum() == len(completed)
    assert done_w.sum() == int(completed["in_window"].sum())
    assert req.sum() == snaps["n_requests"].sum()
    # a completed order requested at 7h10 is counted in slot 28 of its pickup cell, whatever its drop-off time
    one = pd.DataFrame({"request_time_s": [7 * 3600 + 600.0], "pu_cell": [17], "status": ["Completed"], "in_window": [True]})
    m = animation.completed_by_request_slot(one, SLOT_S, 96, N61)
    assert m[28, 17] == 1 and m.sum() == 1
    # rows outside the matrix are dropped, not wrapped
    late = pd.DataFrame({"request_time_s": [n_slots * SLOT_S + 5.0], "pu_cell": [0], "status": ["Completed"], "in_window": [True]})
    assert animation.completed_by_request_slot(late, SLOT_S, n_slots, N61).sum() == 0
    assert np.isinf(slack).any() and not np.isnan(slack).any()        # inf kept, every (slot, cell) published


@pytest.fixture(scope="module")
def tiny_pair(tmp_path_factory):
    """all_off and all_on on the tiny world, one seed each, no budget (same B = NaN), full log."""
    from sim.runner import evaluate
    out = tmp_path_factory.mktemp("anim")
    dirs = {}
    for name in ("all_off", "all_on"):
        cfg = load_config(TINY, [f"policy.name={name}", "sweep.n_seeds=1", "budget.enforce=false", "runner.n_procs=1"])
        evaluate(cfg, out / name, log_level="full")
        dirs[name] = out / name
    return dirs


def test_pair_totals_match_policy_results_and_gif_is_written(tiny_pair, tmp_path):
    pair = animation.load_pairs([tiny_pair["all_off"]], [tiny_pair["all_on"]])
    assert pair.n_cells == 7 and pair.seeds == [pair.old[0].seed]
    assert pair.d_req.shape == pair.d_done.shape == (pair.n_slots, 7)
    totals = animation.check_totals(pair)                                  # sums equal N(new) - N(old)
    assert len(totals) == 1
    row = totals.iloc[0]
    assert row["delta_N"] == row["delta_from_orders"] == pair.new[0].n_completed - pair.old[0].n_completed
    assert pair.d_done_by_seed[0][pair.new[0].done_window.sum(axis=1) > 0].shape[1] == 7
    # tiny window: 1 h warm-up then 2 h evaluated -> frames 1h..3h exist, 5h..9h do not
    slots = animation.frame_slots(pair, 1, 3)
    assert len(slots) == 8 and all(1 <= pair.hour_of_slot(int(s)) < 3 for s in slots)
    with pytest.raises(ValueError):
        animation.frame_slots(pair, 5, 9)
    cum = animation.cumulative_delta(pair, slots)
    assert cum["mean"].shape == (8,) and np.allclose(cum["mean"], cum["lo"])    # one seed: no band
    assert cum["mean"][-1] == pytest.approx(pair.d_done[slots].sum())
    assert animation.default_caption(pair, int(slots[0])).endswith("(trung bình seed).")
    gif = animation.animate(pair, slots, tmp_path / "06_test.gif", fps=4, hold_s=0.5,
                            captions={int(slots[0]): "khung đầu: chú thích riêng"})
    assert gif.exists() and gif.read_bytes()[:6] in (b"GIF87a", b"GIF89a")
    from PIL import Image
    with Image.open(gif) as im:                                             # Pillow merges identical frames into
        total_ms = 0                                                        # one longer frame, so check the duration
        for k in range(im.n_frames):
            im.seek(k)
            total_ms += im.info["duration"]
        assert im.n_frames <= 8 + 2
        assert total_ms == pytest.approx((8 + 2) * 250, abs=60)             # 8 slots + 0.5 s hold at 4 fps


def test_load_pairs_refuses_mismatched_runs(tiny_pair):
    with pytest.raises(ValueError, match="same number"):
        animation.load_pairs([tiny_pair["all_off"]], [])
    pair = animation.load_pairs([tiny_pair["all_off"]], [tiny_pair["all_on"]])
    assert animation.window_delta(pair)["n_seeds"] == 1 and np.isnan(animation.window_delta(pair)["se"])
