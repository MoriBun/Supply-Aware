"""Hour-off rule as a score function.

A ``threshold`` policy with ``theta = 0`` (no cell is cut by tension) whose score is **NaN in the harmful
hours**, so those sessions are never offered (NaN = not offered, as for the oracle), and
elsewhere ranks riders either like ``random`` (the pre-drawn ``u_score``, so the budget binds the same way
as the ``random`` row of the policy table) or like the DR score per 1.000đ of world W0/W1
(``analysis.uplift``). This is the "fixed off at the hours where the voucher hurts" policy and, at the same
time, the ``hour`` tier (is the cell tier an hour-off rule in disguise?), so it runs once.

The harmful hours of a world come from :mod:`analysis.tension_curve` on the city-wide switchback run
(``tau(h)`` upper interval < 0) and are stored in ``analysis/models/hour_rule_<world>.json`` (committed,
written by the CLI below), so a score function loads deterministically in a spawned worker.

Score functions (``policy.threshold.score_fn``)::

    analysis.hour_rule:off_hours_random_w0   analysis.hour_rule:off_hours_dr_w0
    analysis.hour_rule:off_hours_random_w1   analysis.hour_rule:off_hours_dr_w1

CLI::

    python -m analysis.hour_rule set w0 --from runs/s9/w0/summary.json     # harmful_hours of tension_curve
    python -m analysis.hour_rule set w0 --hours 8 17 18 19                  # explicit list
    python -m analysis.hour_rule show
"""

from __future__ import annotations

import argparse
import json
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent / "models"
WORLDS = ("w0", "w1")
DR_SCORE = {"w0": "tau_x_dr_all_per_kvnd", "w1": "tau_x_dr_all_w1_per_kvnd"}


def rule_path(world: str, model_dir: Path | None = None) -> Path:
    return Path(MODEL_DIR if model_dir is None else model_dir) / f"hour_rule_{world}.json"   # MODEL_DIR read at call time


@lru_cache(maxsize=8)
def _load(world: str, model_dir: str) -> tuple[int, ...]:
    data = json.loads(rule_path(world, Path(model_dir)).read_text(encoding="utf-8"))
    return tuple(int(h) for h in data["harmful_hours"])


def load_hours(world: str, model_dir: Path | None = None) -> tuple[int, ...]:
    """Harmful hours of ``world`` (0–23), from the committed JSON."""
    return _load(world, str(rule_path(world, model_dir).parent))


def save_hours(world: str, hours, *, source: str, model_dir: Path | None = None) -> Path:
    hours = sorted({int(h) for h in hours})
    if any(h < 0 or h > 23 for h in hours):
        raise ValueError(f"hours must be in 0..23, got {hours}")
    path = rule_path(world, model_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"world": world, "harmful_hours": hours, "source": source,
                                "rule": "score NaN (never offered) in harmful_hours; theta = 0"},
                               indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _load.cache_clear()
    return path


def apply_rule(hour: np.ndarray, base: np.ndarray, hours) -> np.ndarray:
    """``base`` with NaN where ``hour`` is one of ``hours`` (float32)."""
    off = np.isin(np.asarray(hour, dtype=np.int64), np.asarray(list(hours), dtype=np.int64))
    return np.where(off, np.nan, np.asarray(base, dtype=np.float32)).astype(np.float32)


def _random_base(batch) -> np.ndarray:
    return batch.u_score.astype(np.float32)


def _dr_base(batch, s_hat: np.ndarray, world: str) -> np.ndarray:
    from analysis import uplift                      # LightGBM only when a DR variant is used
    return getattr(uplift, DR_SCORE[world])(batch, s_hat)


def _make(world: str, base: str):
    def score(batch, s_hat: np.ndarray) -> np.ndarray:
        b = _random_base(batch) if base == "random" else _dr_base(batch, s_hat, world)
        return apply_rule(batch.hour, b, load_hours(world))

    name = f"off_hours_{base}_{world}"
    score.__name__ = score.__qualname__ = name
    score.__doc__ = (f"NaN (not offered) in the harmful hours of {world.upper()}; elsewhere "
                     + ("the pre-drawn u_score, like ``random``." if base == "random"
                        else f"``analysis.uplift:{DR_SCORE[world]}`` (DR effect per 1.000đ)."))
    return score


SCORE_FUNCTIONS: dict[str, "callable"] = {}
for _world in WORLDS:
    for _base in ("random", "dr"):
        _fn = _make(_world, _base)
        globals()[_fn.__name__] = _fn
        SCORE_FUNCTIONS[_fn.__name__] = _fn
del _world, _base, _fn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hour-off rule: store the harmful hours of a world")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("world", choices=WORLDS)
    s.add_argument("--from", dest="source", type=Path, default=None, help="tension_curve summary.json")
    s.add_argument("--hours", type=int, nargs="*", default=None)
    sub.add_parser("show")
    args = ap.parse_args(argv)
    if args.cmd == "show":
        for w in WORLDS:
            p = rule_path(w)
            print(f"{w}: {load_hours(w) if p.exists() else '(no file)'}  {p}")
        return 0
    if (args.source is None) == (args.hours is None):
        ap.error("give exactly one of --from or --hours")
    if args.source is not None:
        hours = json.loads(args.source.read_text(encoding="utf-8"))["harmful_hours"]
        source = str(args.source)
    else:
        hours, source = args.hours, "explicit"
    path = save_hours(args.world, hours, source=source)
    print(f"{path}: harmful_hours = {load_hours(args.world)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
