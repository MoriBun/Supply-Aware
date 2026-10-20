"""Helpers for the result notebooks in ``notebooks/``.

A notebook only calls functions of ``analysis/``; this module gives every notebook the
same first cell::

    from analysis.notebook import require_runs, setup
    from analysis.plots import save_figure

    plt = setup()
    sweep_dir, gte_dir = require_runs("runs/s5/sweep_ring1_dr", "runs/b7a/gte")

``setup`` makes paths independent of where the notebook runs from (``notebooks/``):
it moves to the repository root, so ``runs/...`` and ``config/...`` resolve as on the
command line. ``require_runs`` fails at once, naming every missing input, instead of
in the middle of the notebook; ``runs/`` is never committed, each input is rebuilt
with its command in README.md, section "Tái lập kết quả".
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASETS_DOC = 'README.md, section "Tái lập kết quả"'


def setup():
    """Move to the repository root, set the plot style and a wide pandas display; return ``pyplot``."""
    import pandas as pd

    from analysis.plots import pyplot

    os.chdir(ROOT)
    pd.set_option("display.max_columns", 60)
    pd.set_option("display.width", 200)
    return pyplot()


def require_runs(*paths: str | Path) -> list[Path]:
    """Absolute paths of the data a notebook needs (run directories or files, relative to the repository root).

    Raises ``FileNotFoundError`` listing every missing one, with the pointer :data:`DATASETS_DOC`.
    """
    resolved = [Path(p) if Path(p).is_absolute() else ROOT / p for p in paths]
    missing = [str(p) for p, r in zip(paths, resolved) if not r.exists()]
    if missing:
        raise FileNotFoundError(f"missing data: {', '.join(missing)}. "
                                f"Rebuild each one with its command in {DATASETS_DOC}; runs/ is not committed.")
    return resolved
