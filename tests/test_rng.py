"""CRN streams (sim/rng.py) and hard rule 4 (no global randomness)."""

import ast
from pathlib import Path

import numpy as np
import pytest

from sim.rng import Rng, Stream, hash64

SIM_DIR = Path(__file__).resolve().parents[1] / "sim"


def draws(rng, stream, *key, n=5):
    return rng.rng_for(stream, *key).random(n)


def test_same_key_same_numbers():
    a, b = Rng(run_seed=3, world_seed=7), Rng(run_seed=3, world_seed=7)
    for stream in Stream:
        np.testing.assert_array_equal(draws(a, stream, 1, 2), draws(b, stream, 1, 2))


def test_different_key_different_numbers():
    rng = Rng(run_seed=0, world_seed=0)
    base = draws(rng, Stream.SESSION, 12345)
    assert not np.array_equal(base, draws(rng, Stream.SESSION, 12346))
    # Same values in a different key layout must not collide either.
    assert not np.array_equal(draws(rng, Stream.DEMAND, 1, 2), draws(rng, Stream.DEMAND, 2, 1))
    assert not np.array_equal(draws(rng, Stream.DEMAND, 1, 2), draws(rng, Stream.DEMAND, 1, 2, 0))


def test_different_stream_different_numbers():
    rng = Rng(run_seed=0, world_seed=0)
    seen = {tuple(draws(rng, stream, 42)) for stream in Stream}
    assert len(seen) == len(Stream)


def test_draws_do_not_depend_on_call_order():
    # CRN: what other sessions draw (and in which order) must not matter.
    rng = Rng(run_seed=1, world_seed=1)
    first = draws(rng, Stream.SESSION, 7)
    for sid in range(100):
        rng.rng_for(Stream.SESSION, sid).random(10)
    np.testing.assert_array_equal(first, draws(rng, Stream.SESSION, 7))


def test_world_stream_ignores_run_seed():
    a, b = Rng(run_seed=0, world_seed=99), Rng(run_seed=1, world_seed=99)
    np.testing.assert_array_equal(draws(a, Stream.WORLD, 0), draws(b, Stream.WORLD, 0))
    assert not np.array_equal(draws(a, Stream.SESSION, 0), draws(b, Stream.SESSION, 0))


def test_world_seed_only_changes_world_stream():
    a, b = Rng(run_seed=0, world_seed=1), Rng(run_seed=0, world_seed=2)
    assert not np.array_equal(draws(a, Stream.WORLD, 0), draws(b, Stream.WORLD, 0))
    np.testing.assert_array_equal(draws(a, Stream.SESSION, 0), draws(b, Stream.SESSION, 0))


def test_numpy_integer_keys_equal_python_ints():
    rng = Rng(run_seed=0, world_seed=0)
    np.testing.assert_array_equal(
        draws(rng, Stream.DRIVER, 5, 2), draws(rng, Stream.DRIVER, np.int64(5), np.int16(2))
    )


@pytest.mark.parametrize("key", [(1.0,), ("1",), (2**63,)])
def test_bad_keys_rejected(key):
    with pytest.raises((TypeError, ValueError)):
        Rng(0, 0).rng_for(Stream.SESSION, *key)


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        Rng(0, 0).rng_for(Stream.SESSION)


def test_from_config(tiny_cfg):
    rng = Rng.from_config(tiny_cfg)
    assert (rng.run_seed, rng.world_seed) == (tiny_cfg.meta.run_seed, tiny_cfg.meta.world_seed)


def test_hash64_is_pinned():
    # Changing the hash silently changes every random number, so generated
    # datasets would no longer be reproducible. Update only on purpose.
    assert hash64(0, 3, 42) == 16375947467241191322
    assert hash64(20260930, 1, 0) == 12381375145982931506


# --- session ids ------------------------------------------------------------------


def test_ticks_per_day():
    from sim.rng import ticks_per_day
    assert ticks_per_day(60) == 1440 and ticks_per_day(30) == 2880
    with pytest.raises(ValueError):
        ticks_per_day(7)


def test_session_id_formula_and_roundtrip():
    from sim.rng import make_session_id, split_session_id
    sid = make_session_id(2, 100, 5, 7, n_cells=37, ticks_per_day=1440)
    assert sid == ((2 * 1440 + 100) * 37 + 5) * 1000 + 7     # with ticks_per_day = 1440
    assert split_session_id(sid, n_cells=37, ticks_per_day=1440) == (2, 100, 5, 7)
    later = make_session_id(2, 101, 0, 0, n_cells=37, ticks_per_day=1440)
    assert later > make_session_id(2, 100, 36, 999, n_cells=37, ticks_per_day=1440)   # time order is id order


@pytest.mark.parametrize("args", [(0, 0, 0, 1000), (0, 0, 37, 0), (0, 1440, 0, 0), (-1, 0, 0, 0)])
def test_session_id_range_checks(args):
    from sim.rng import make_session_id
    with pytest.raises(ValueError):
        make_session_id(*args, n_cells=37, ticks_per_day=1440)


def test_session_ids_for_tick():
    from sim.rng import make_session_id, session_ids_for_tick
    ids = session_ids_for_tick(1, 5, 3, 4, n_cells=7, ticks_per_day=1440)
    assert ids.dtype == np.int64 and ids.tolist() == [
        make_session_id(1, 5, 3, k, n_cells=7, ticks_per_day=1440) for k in range(4)
    ]
    assert session_ids_for_tick(1, 5, 3, 0, n_cells=7, ticks_per_day=1440).shape == (0,)
    with pytest.raises(ValueError):
        session_ids_for_tick(1, 5, 3, 1001, n_cells=7, ticks_per_day=1440)


# --- pre-drawn numbers ----------------------------------------------------------------


def test_session_draw_order_is_pinned():
    from sim.rng import SESSION_DRAW_ORDER, SessionScalars
    assert SESSION_DRAW_ORDER == ("rider", "dest", "u_book", "u_target", "u_explore", "u_explore_arm",
                                  "trip_noise", "e_cancel", "u_score")
    assert SessionScalars._fields == SESSION_DRAW_ORDER[2:]


def test_draw_session_scalars():
    from sim.rng import draw_session_scalars
    rng = Rng(run_seed=0, world_seed=0)
    a = draw_session_scalars(rng.rng_for(Stream.SESSION, 123), 0.15)
    b = draw_session_scalars(rng.rng_for(Stream.SESSION, 123), 0.15)
    assert a == b
    for u in (a.u_book, a.u_target, a.u_explore, a.u_explore_arm, a.u_score):
        assert 0.0 <= u < 1.0
    assert a.trip_noise > 0 and a.e_cancel >= 0
    assert len({a.u_book, a.u_target, a.u_explore, a.u_explore_arm, a.u_score}) == 5
    assert draw_session_scalars(rng.rng_for(Stream.SESSION, 1), 0.0).trip_noise == 1.0


# --- CELLSLOT kinds ----------------------------------------------------------------------


def test_cellslot_kinds_and_cluster_codes():
    from sim.rng import CellSlotKind, cluster_level_code
    assert int(CellSlotKind.LEGACY_EPS) == 1 and int(CellSlotKind.SWITCHBACK) == 2
    assert cluster_level_code(1) == 1 and cluster_level_code(7) == 7 and cluster_level_code("all") == 0
    with pytest.raises(ValueError):
        cluster_level_code(3)
    rng = Rng(0, 0)
    rng.rng_for(Stream.CELLSLOT, CellSlotKind.SWITCHBACK, cluster_level_code("all"), 0, 5)   # accepted


# --- hard rule 4: no global numpy / stdlib randomness in sim/ ----------------

_ALLOWED_NP_RANDOM = {"Generator"}          # type hints anywhere
_ALLOWED_NP_RANDOM_IN_RNG = {"Philox"}      # only sim/rng.py builds bit generators


def random_violations(source: str, is_rng_module: bool = False) -> list[str]:
    allowed = _ALLOWED_NP_RANDOM | (_ALLOWED_NP_RANDOM_IN_RNG if is_rng_module else set())
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names
                      if a.name == "random" or a.name.startswith("numpy.random")]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "random" or module.startswith("numpy.random") or (
                module == "numpy" and any(a.name == "random" for a in node.names)
            ):
                found.append(f"from {module} import ...")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "random"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in {"np", "numpy"}
            and node.attr not in allowed
        ):
            found.append(f"np.random.{node.attr}")
    return found


def test_checker_catches_violations():
    bad = (
        "import random\n"
        "from numpy.random import default_rng\n"
        "import numpy as np\n"
        "x = np.random.rand(3)\n"
        "g = np.random.default_rng(0)\n"
        "np.random.seed(1)\n"
    )
    assert len(random_violations(bad)) == 5
    assert random_violations("import numpy as np\ndef f(g: np.random.Generator): ...\n") == []
    assert random_violations("import numpy as np\nb = np.random.Philox(key=1)\n") == ["np.random.Philox"]


@pytest.mark.parametrize("path", sorted(SIM_DIR.rglob("*.py")), ids=lambda p: p.relative_to(SIM_DIR).as_posix())
def test_no_global_randomness_in_sim(path):
    is_rng = path == SIM_DIR / "rng.py"
    assert random_violations(path.read_text(encoding="utf-8"), is_rng) == [], (
        "use rng.rng_for(stream, *key) (CLAUDE.md hard rule 4)"
    )


def test_shock_stream_and_kind_codes_are_fixed():
    # Codes are part of the CRN contract; never renumber.
    from sim.rng import ShockKind
    assert Stream.SHOCK == 8 and [s.value for s in Stream] == list(range(1, 9))
    assert {k.name: k.value for k in ShockKind} == {"DAY_DEMAND": 1, "EVENT": 2, "RAIN": 3, "ATTEND": 4, "JITTER": 5}
