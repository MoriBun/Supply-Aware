"""Helpers of the result notebooks (analysis/notebook.py, analysis.plots.save_figure)."""

import os
import sys

import pytest

from analysis import notebook


def test_require_runs_resolves_from_the_repository_root_whatever_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                                         # e.g. a notebook started in notebooks/
    (path,) = notebook.require_runs("config/default.yaml")
    assert path == notebook.ROOT / "config" / "default.yaml" and path.exists()
    assert notebook.require_runs(path) == [path]                        # absolute paths pass through


def test_require_runs_names_every_missing_input_and_the_datasets_doc():
    with pytest.raises(FileNotFoundError) as err:
        notebook.require_runs("config/default.yaml", "runs/no_such_a", "runs/no_such_b")
    msg = str(err.value)
    assert "runs/no_such_a" in msg and "runs/no_such_b" in msg and "config/default.yaml" not in msg
    assert notebook.DATASETS_DOC in msg


def test_setup_moves_to_the_root_and_returns_pyplot(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    monkeypatch.chdir(tmp_path)
    plt = notebook.setup()
    assert os.path.samefile(os.getcwd(), notebook.ROOT) and hasattr(plt, "subplots")


def test_save_figure_writes_a_png_keeps_the_figure_open_and_checks_the_name(tmp_path):
    pytest.importorskip("matplotlib")
    from analysis.plots import pyplot, save_figure

    plt = pyplot()
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    path = save_figure(fig, "04_test_line", figures_dir=tmp_path)
    assert path == tmp_path / "04_test_line.png" and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert plt.fignum_exists(fig.number)                               # still shown by the notebook
    for bad in ("regret", "4_regret", "04-regret", "04_Regret", "04_regret.png"):
        with pytest.raises(ValueError, match="figure name"):
            save_figure(fig, bad, figures_dir=tmp_path)
    plt.close(fig)


def test_plot_style_keeps_the_notebook_backend(monkeypatch):
    matplotlib = pytest.importorskip("matplotlib")
    from analysis import plots

    calls = []
    monkeypatch.setattr(matplotlib, "use", lambda backend, *a, **k: calls.append(backend))
    monkeypatch.setitem(sys.modules, "ipykernel", object())                   # running inside a kernel
    plots.pyplot()
    assert calls == []
    monkeypatch.delitem(sys.modules, "ipykernel")                             # command line / tests
    plots.pyplot()
    assert calls == ["Agg"]
