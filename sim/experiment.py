"""M10 ExperimentDesigner: clusters (1 / 7 / all), switchback blocks, rider A/B, burn-in flags.

Everything here is a pure function of the config, the grid and the keyed streams:
the state of a (cluster, block) comes from CELLSLOT keyed by
``(SWITCHBACK, level_code, cluster_id, block)`` and a rider's arm from RIDER keyed
by ``rider_id``, so an assignment never depends on the policy or on
what happened earlier in the run.
"""

from __future__ import annotations

import numpy as np

from sim.config import Config
from sim.rng import CellSlotKind, Rng, Stream, cluster_level_code
from sim.space import SpaceTime


def effective_level(cfg: Config):
    """Cluster level of the design: ``global_switchback`` is a switchback with one cluster."""
    return "all" if cfg.experiment.design == "global_switchback" else cfg.experiment.cluster_level


def cluster_ids(space: SpaceTime, level) -> np.ndarray:
    """Cluster of every cell (int16 [N]), ids 0..n_clusters-1.

    Level 1: each cell its own cluster. ``all``: one cluster. Level 7: centres are the
    cells with ``(q + 5r) mod 7 == 0``; every cell joins the nearest centre by (torus)
    distance, ties to the smaller cell id; the cluster id is the centre's rank.
    """
    n = space.n_cells
    if level == 1:
        return np.arange(n, dtype=np.int16)
    if level == "all":
        return np.zeros(n, dtype=np.int16)
    if level == 7:
        q, r = space.cell_q.astype(np.int64), space.cell_r.astype(np.int64)
        centres = np.flatnonzero((q + 5 * r) % 7 == 0)
        if len(centres) == 0:
            raise ValueError("no cluster centre on this grid")
        nearest = np.argmin(space.D[:, centres], axis=1)   # first minimum = smallest centre id
        return nearest.astype(np.int16)
    raise ValueError(f"unknown cluster_level {level!r}")


def n_clusters(ids: np.ndarray) -> int:
    return int(ids.max()) + 1


def block_s(cfg: Config) -> int:
    return cfg.experiment.block_min * 60


def block_of(cfg: Config, t) -> int | np.ndarray:
    """``floor(t / (block_min * 60))`` from the start of the run."""
    b = np.floor_divide(np.asarray(t, dtype=np.float64), block_s(cfg)).astype(np.int64)
    return int(b) if b.ndim == 0 else b


def in_burnin(cfg: Config, t) -> bool | np.ndarray:
    """True in the first ``burnin_min`` minutes of a block."""
    tt = np.asarray(t, dtype=np.float64)
    flag = (tt - np.floor_divide(tt, block_s(cfg)) * block_s(cfg)) < cfg.experiment.burnin_min * 60
    return bool(flag) if flag.ndim == 0 else flag


def cluster_on(rng: Rng, cfg: Config, ids: np.ndarray, level, block: int) -> np.ndarray:
    """Promotion state of every cell in ``block`` (bool [N]): ``Uniform < p_on`` per cluster."""
    code = cluster_level_code(level)
    p_on = cfg.experiment.p_on
    on = np.fromiter(
        (rng.rng_for(Stream.CELLSLOT, CellSlotKind.SWITCHBACK, code, c, int(block)).random() < p_on
         for c in range(n_clusters(ids))),
        dtype=bool, count=n_clusters(ids),
    )
    return on[ids]


def rider_arm(rng: Rng, cfg: Config, rider_ids) -> np.ndarray:
    """Treatment arm of riders (bool [n]), fixed for the whole run: ``Uniform < p_on`` keyed by rider."""
    p_on = cfg.experiment.p_on
    ids = np.asarray(rider_ids).tolist()
    return np.fromiter((rng.rng_for(Stream.RIDER, int(r)).random() < p_on for r in ids), dtype=bool, count=len(ids))
