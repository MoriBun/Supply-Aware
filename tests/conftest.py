from pathlib import Path

import pytest

from sim.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def default_yaml() -> Path:
    return ROOT / "config" / "default.yaml"


@pytest.fixture
def tiny_layers(default_yaml: Path) -> list[Path]:
    """default.yaml followed by the tiny overlay."""
    return [default_yaml, ROOT / "tests" / "fixtures" / "tiny.yaml"]


@pytest.fixture
def tiny_cfg(tiny_layers: list[Path]) -> Config:
    return load_config(tiny_layers)
