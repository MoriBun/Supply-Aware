"""M1 SpaceTime: hex grid, torus, distance matrix D, travel times T[a, b, h], pickup ETA.

Everything here is a pure function of ``cfg.space`` (plus the pickup limits of
``cfg.matching``), computed once per run by :func:`build_space`; the arrays are
read-only afterwards. ``SpaceTime.find_pickup`` is the single implementation of
the M5 search rule, shared by the quoted ETA (M1/M4) and matching (M5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from sim.config import Config

# Six axial neighbour directions.
HEX_DIRECTIONS: tuple[tuple[int, int], ...] = ((1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1))

# Torus images are searched over i, j in {-1, 0, 1}.
_SHIFTS: tuple[tuple[int, int], ...] = tuple((i, j) for i in (-1, 0, 1) for j in (-1, 0, 1))


def n_cells_for_radius(radius: int) -> int:
    return 3 * radius * radius + 3 * radius + 1


def hex_cells(radius: int) -> np.ndarray:
    """Axial coordinates ``(q, r)`` of the radius-``R`` hexagon, sorted by ``(q, r)`` (int16 [N, 2])."""
    cells = [
        (q, r)
        for q in range(-radius, radius + 1)
        for r in range(-radius, radius + 1)
        if max(abs(q), abs(r), abs(q + r)) <= radius
    ]
    return np.array(sorted(cells), dtype=np.int16)


def hex_area_km2(edge_km: float) -> float:
    """Area of a regular hexagon with side ``edge_km``."""
    return 3.0 * math.sqrt(3.0) / 2.0 * edge_km * edge_km


def hex_distance(dq: np.ndarray | int, dr: np.ndarray | int) -> np.ndarray | int:
    """Ring distance of an axial offset: ``(|dq| + |dr| + |dq + dr|) / 2``."""
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def torus_vectors(radius: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """Translation vectors ``a = (2R+1, -R)``, ``b = (R, R+1)``."""
    return (2 * radius + 1, -radius), (radius, radius + 1)


@dataclass
class SpaceTime:
    """Static geometry and travel times of the world. All arrays are read-only."""

    n_cells: int
    cell_q: np.ndarray        # int16 [N]
    cell_r: np.ndarray        # int16 [N]
    D: np.ndarray             # int16 [N, N], hex (torus) ring distance
    T: np.ndarray             # float32 [N, N, 24], travel minutes from a to b in hour h
    neighbors: np.ndarray     # int16 [N, 6] in HEX_DIRECTIONS order, -1 where a direction has no cell
    area_km2: float
    torus: bool
    radius: int
    rings: tuple[tuple[np.ndarray, ...], ...]  # rings[z][k-1]: cell ids at distance k from z, ascending
    center_dist_km: float     # s = sqrt(3) * edge_km, centre-to-centre distance of adjacent cells
    speed_kmh: np.ndarray     # float64 [24], base_speed_kmh * speed_factor_by_hour
    detour_factor: float
    nn_const: float
    eta_gamma: float
    eta_floor_min: float
    max_pickup_eta_min: float     # from cfg.matching, so the quote and M5 use the same limit
    max_ring: Optional[int]       # from cfg.matching; None = up to the whole grid
    ring_tiebreak: str = "lowest_id"  # from cfg.matching: lowest_id | most_idle
    is_stub: bool = False

    def eta_in(self, idle_count: int | np.ndarray, hour: int | np.ndarray) -> float | np.ndarray:
        """Expected pickup ETA (minutes) when ``idle_count >= 1`` drivers idle in the rider's cell.

        ``ETA_in(I, h) = max(eta_floor_min, detour * nn_const * (A / I)**gamma / v_h * 60)``.
        Vectorised over ``idle_count`` and ``hour``.
        """
        idle = np.asarray(idle_count)
        if np.any(idle < 1):
            raise ValueError(f"eta_in needs idle_count >= 1, got {idle_count!r}")
        v = self.speed_kmh[np.asarray(hour)]
        dist_km = self.detour_factor * self.nn_const * (self.area_km2 / idle) ** self.eta_gamma
        eta = np.maximum(self.eta_floor_min, dist_km / v * 60.0)
        return float(eta) if eta.ndim == 0 else eta

    def find_pickup(self, cell: int, hour: int, idle_count: np.ndarray) -> tuple[int, float]:
        """Source cell and pickup ETA for a rider in ``cell`` under the M5 search rule.

        ``idle_count[c]`` is the number of idle, on-shift drivers in cell ``c``.

        1. Idle drivers in the rider's cell: ETA = ``eta_in(idle_count[cell], hour)``.
        2. Otherwise scan rings k = 1, 2, ... until the ring's smallest ``T[., cell, h]``
           exceeds ``max_pickup_eta_min`` or k passes ``max_ring``. In the nearest ring
           with an idle driver, take the cell with the smallest ``T``. Ties (on the synthetic
           grid every cell of a ring ties) go by ``ring_tiebreak``: ``most_idle`` takes the cell
           with the most idle drivers, then the smallest id;
           ``lowest_id`` takes the smallest id.
        3. An ETA above ``max_pickup_eta_min`` counts as no pickup.

        Returns ``(source_cell, eta_min)``, or ``(-1, inf)`` if no pickup is possible.
        Does not modify ``idle_count``.
        """
        limit = self.max_pickup_eta_min
        n_here = int(idle_count[cell])
        if n_here >= 1:
            eta = self.eta_in(n_here, hour)
            return (cell, eta) if eta <= limit else (-1, math.inf)

        rings = self.rings[cell]
        n_rings = len(rings) if self.max_ring is None else min(len(rings), self.max_ring)
        for k in range(n_rings):
            ring = rings[k]
            t = self.T[ring, cell, hour]
            if t.min() > limit:
                break
            has_idle = idle_count[ring] > 0
            if has_idle.any():
                candidates = ring[has_idle]
                t_cand = t[has_idle]
                if self.ring_tiebreak == "most_idle":
                    nearest = np.flatnonzero(t_cand == t_cand.min())
                    j = int(nearest[np.argmax(idle_count[candidates[nearest]])])   # first max = smallest id
                else:
                    j = int(np.argmin(t_cand))  # ring ids are ascending, so ties go to the smallest id
                eta = float(t_cand[j])
                return (int(candidates[j]), eta) if eta <= limit else (-1, math.inf)
        return -1, math.inf

    def quote_eta(self, cell: int, hour: int, idle_count: np.ndarray) -> tuple[float, bool]:
        """Quoted ETA (minutes) and ``no_supply`` flag using the M5 search rule, without holding a driver.

        With no reachable idle driver it returns ``(max_pickup_eta_min, True)``.
        """
        source, eta = self.find_pickup(cell, hour, idle_count)
        if source < 0:
            return self.max_pickup_eta_min, True
        return eta, False


def build_space(cfg: Config) -> SpaceTime:
    """Build the grid, distances and travel-time table from ``cfg.space``."""
    sc = cfg.space
    if sc.variant != "synthetic":
        raise NotImplementedError(f"space.variant={sc.variant!r}: the NYC grid is not implemented")

    R = sc.grid_radius
    cells = hex_cells(R)
    n = len(cells)
    q = cells[:, 0].astype(np.int64)
    r = cells[:, 1].astype(np.int64)

    D = _distance_matrix(q, r, R, sc.torus)
    neighbors = _neighbor_table(q, r, R, sc.torus)
    rings = tuple(
        tuple(_frozen(np.flatnonzero(D[z] == k)) for k in range(1, int(D[z].max()) + 1)) for z in range(n)
    )

    area = hex_area_km2(sc.edge_km)
    s = math.sqrt(3.0) * sc.edge_km
    speed = sc.base_speed_kmh * np.asarray(sc.speed_factor_by_hour, dtype=np.float64)

    # Road distance (km) between cell centres; within a cell use the intra-cell trip length.
    dist_km = sc.detour_factor * D.astype(np.float64) * s
    np.fill_diagonal(dist_km, sc.detour_factor * sc.intra_cell_dist_factor * math.sqrt(area))
    if cfg.shocks.enabled:
        # Rain layer: hour index h + 24 is hour h with speeds x rain.speed_mult.
        speed = np.concatenate([speed, speed * cfg.shocks.rain.speed_mult])
    T = (dist_km[:, :, None] / speed[None, None, :] * 60.0).astype(np.float32)

    return SpaceTime(
        n_cells=n,
        cell_q=_frozen(cells[:, 0].copy()),
        cell_r=_frozen(cells[:, 1].copy()),
        D=_frozen(D.astype(np.int16)),
        T=_frozen(T),
        neighbors=_frozen(neighbors),
        area_km2=area,
        torus=sc.torus,
        radius=R,
        rings=rings,
        center_dist_km=s,
        speed_kmh=_frozen(speed),
        detour_factor=sc.detour_factor,
        nn_const=sc.nn_const,
        eta_gamma=sc.eta_gamma,
        eta_floor_min=sc.eta_floor_min,
        max_pickup_eta_min=cfg.matching.max_pickup_eta_min,
        max_ring=cfg.matching.max_ring,
        ring_tiebreak=cfg.matching.ring_tiebreak,
    )


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------


def _distance_matrix(q: np.ndarray, r: np.ndarray, R: int, torus: bool) -> np.ndarray:
    dq = q[None, :] - q[:, None]
    dr = r[None, :] - r[:, None]
    if not torus:
        return hex_distance(dq, dr)
    (aq, ar), (bq, br) = torus_vectors(R)
    return np.min([hex_distance(dq + i * aq + j * bq, dr + i * ar + j * br) for i, j in _SHIFTS], axis=0)


def _neighbor_table(q: np.ndarray, r: np.ndarray, R: int, torus: bool) -> np.ndarray:
    index = {(int(cq), int(cr)): i for i, (cq, cr) in enumerate(zip(q, r))}
    (aq, ar), (bq, br) = torus_vectors(R)
    out = np.full((len(index), len(HEX_DIRECTIONS)), -1, dtype=np.int16)
    for (cq, cr), i in index.items():
        for d, (dq, dr) in enumerate(HEX_DIRECTIONS):
            c = (cq + dq, cr + dr)
            if c in index:
                out[i, d] = index[c]
            elif torus:
                # wrap(c): exactly one image c - m*a - n*b lies inside the grid.
                images = [(c[0] - m * aq - n * bq, c[1] - m * ar - n * br) for m, n in _SHIFTS]
                inside = [index[img] for img in images if img in index]
                if len(inside) != 1:
                    raise AssertionError(f"torus wrap of {c} (R={R}) has {len(inside)} images in the grid")
                out[i, d] = inside[0]
    return out


def _frozen(a: np.ndarray) -> np.ndarray:
    a.setflags(write=False)
    return a
