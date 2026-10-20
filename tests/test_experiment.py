"""Experiment design (sim/experiment.py): clusters, blocks, switchback, rider A/B."""

import dataclasses

import numpy as np
import pytest

from sim import experiment as ex
from sim.config import load_config
from sim.rng import Rng
from sim.space import build_space


@pytest.fixture
def cfg(default_yaml):
    return load_config(default_yaml)


@pytest.fixture
def space(cfg):
    return build_space(cfg)


# --- clusters -----------------------------------------------------------------------


def test_cluster_levels_cover_every_cell_without_overlap(space):
    np.testing.assert_array_equal(ex.cluster_ids(space, 1), np.arange(space.n_cells))
    assert (ex.cluster_ids(space, "all") == 0).all() and ex.n_clusters(ex.cluster_ids(space, "all")) == 1
    ids = ex.cluster_ids(space, 7)
    assert ids.dtype == np.int16 and len(ids) == space.n_cells and ex.n_clusters(ids) == 7
    sizes = sorted(np.bincount(ids).tolist())
    assert sizes == [7, 7, 7, 8, 9, 11, 12]                             # R = 4; R = 3 gave 3–7
    centres = np.flatnonzero((space.cell_q.astype(int) + 5 * space.cell_r.astype(int)) % 7 == 0)
    assert (space.D[np.arange(space.n_cells), centres[ids]] <= 2).all()   # every cell within 2 rings of its centre
    for z in range(space.n_cells):                                      # nearest centre, ties to the smaller id
        d = space.D[z, centres]
        assert d[ids[z]] == d.min() and ids[z] == int(np.flatnonzero(d == d.min())[0])


def test_cluster_ids_on_tiny_grid_and_bad_level(tiny_cfg):
    sp = build_space(tiny_cfg)                                          # R = 1: the only centre is (0, 0)
    assert (ex.cluster_ids(sp, 7) == 0).all()
    with pytest.raises(ValueError):
        ex.cluster_ids(sp, 3)


def test_effective_level(cfg):
    assert ex.effective_level(cfg) == 7
    glob = dataclasses.replace(cfg, experiment=dataclasses.replace(cfg.experiment, design="global_switchback"))
    assert ex.effective_level(glob) == "all"


# --- blocks and burn-in ----------------------------------------------------------------


def test_block_and_burnin_flags(cfg):
    assert cfg.experiment.block_min == 60 and cfg.experiment.burnin_min == 15
    assert ex.block_of(cfg, 0.0) == 0 and ex.block_of(cfg, 3599.0) == 0 and ex.block_of(cfg, 3600.0) == 1
    np.testing.assert_array_equal(ex.block_of(cfg, np.array([0.0, 3600.0, 7199.0, 86400.0])), [0, 1, 1, 24])
    assert ex.in_burnin(cfg, 0.0) and ex.in_burnin(cfg, 899.0) and not ex.in_burnin(cfg, 900.0)
    assert ex.in_burnin(cfg, 3600.0) and not ex.in_burnin(cfg, 3599.0)
    np.testing.assert_array_equal(ex.in_burnin(cfg, np.array([0.0, 900.0, 4000.0, 4500.0])), [True, False, True, False])
    assert cfg.time.warmup_min % cfg.experiment.block_min == 0           # window starts on a block boundary


# --- switchback assignment (CELLSLOT stream) ------------------------------------------


def test_cluster_on_share_matches_p_on_and_is_deterministic(cfg, space):
    ids = ex.cluster_ids(space, 7)
    rng = Rng.from_config(cfg)
    n_blocks = 2000
    states = np.array([ex.cluster_on(rng, cfg, ids, 7, b) for b in range(n_blocks)])     # [blocks, N]
    per_cluster = np.array([[states[b, ids == c][0] for c in range(7)] for b in range(n_blocks)])
    assert abs(per_cluster.mean() - cfg.experiment.p_on) < 0.03                          # ±3%
    for b in (0, 1, 777):                                                               # cells share their cluster's state
        for c in range(7):
            assert len(set(states[b, ids == c].tolist())) == 1
    again = np.array([ex.cluster_on(Rng.from_config(cfg), cfg, ids, 7, b) for b in range(50)])
    np.testing.assert_array_equal(again, states[:50])
    other_seed = np.array([ex.cluster_on(Rng(run_seed=1, world_seed=cfg.meta.world_seed), cfg, ids, 7, b)
                           for b in range(50)])
    assert (other_seed != states[:50]).any()
    level_all = np.array([ex.cluster_on(rng, cfg, ex.cluster_ids(space, "all"), "all", b) for b in range(50)])
    assert all(len(set(row.tolist())) == 1 for row in level_all)                         # one cluster: one state
    assert (level_all[:, 0] != per_cluster[:50, 0]).any()                                # level code is part of the key


def test_cluster_on_p_on_extremes(cfg, space):
    ids = ex.cluster_ids(space, 1)
    rng = Rng.from_config(cfg)
    always = dataclasses.replace(cfg, experiment=dataclasses.replace(cfg.experiment, p_on=1.0))
    never = dataclasses.replace(cfg, experiment=dataclasses.replace(cfg.experiment, p_on=0.0))
    assert ex.cluster_on(rng, always, ids, 1, 3).all() and not ex.cluster_on(rng, never, ids, 1, 3).any()


# --- rider A/B (RIDER stream) -----------------------------------------------------------


def test_rider_arm_is_fixed_per_rider_and_has_share_p_on(cfg):
    rng = Rng.from_config(cfg)
    riders = np.arange(20000, dtype=np.int32)
    arm = ex.rider_arm(rng, cfg, riders)
    assert arm.dtype == bool and len(arm) == len(riders)
    assert abs(arm.mean() - cfg.experiment.p_on) < 0.02
    np.testing.assert_array_equal(ex.rider_arm(rng, cfg, riders[::7]), arm[::7])          # same rider, same arm
    np.testing.assert_array_equal(ex.rider_arm(Rng.from_config(cfg), cfg, [5, 5, 9]), [arm[5], arm[5], arm[9]])
    assert (ex.rider_arm(Rng(run_seed=1, world_seed=0), cfg, riders) != arm).any()         # run_seed changes the arms
    assert len(ex.rider_arm(rng, cfg, [])) == 0
