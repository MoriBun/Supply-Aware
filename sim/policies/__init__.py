"""Voucher policies."""

from __future__ import annotations

from sim.config import Config
from sim.policies.base import LegacyHiddenView, OracleHiddenView, Policy
from sim.policies.experiment import ExperimentPolicy
from sim.policies.fixed import FixedPolicy
from sim.policies.legacy import LegacyPolicy
from sim.policies.scores import is_oracle, make_oracle_score
from sim.policies.threshold import ThresholdPolicy
from sim.rng import Rng


def make_policy(cfg: Config, world, *, rng: Rng | None = None, theta: float | None = None,
                kappa: float | None = None) -> Policy:
    """Build the policy named by ``cfg.policy.name``.

    ``theta`` and ``kappa`` override ``policy.threshold`` for sweeps and kappa-auto
    (ignored by the other policies). ``rng`` defaults to ``Rng.from_config(cfg)``;
    LegacyPolicy (CELLSLOT) and ExperimentPolicy (CELLSLOT, RIDER) draw from it.
    """
    name = cfg.policy.name
    if name == "all_on":
        return FixedPolicy(True, world.n_cells)
    if name == "all_off":
        return FixedPolicy(False, world.n_cells)
    if rng is None:
        rng = Rng.from_config(cfg)
    if name == "legacy":
        return LegacyPolicy(cfg, LegacyHiddenView.from_world(world), rng, world.n_cells)
    if name == "threshold":
        score_fn = None
        if is_oracle(cfg.policy.threshold.score_fn):          # the only place the oracle gate is opened
            score_fn = make_oracle_score(cfg, OracleHiddenView.from_world(world))
        return ThresholdPolicy(cfg, world.n_cells, theta=theta, kappa=kappa, neighbors=world.space.neighbors,
                               score_fn=score_fn)
    if name == "experiment":
        return ExperimentPolicy(cfg, world, rng)
    raise ValueError(f"unknown policy {name!r}")


HIDDEN_VIEWS = ("none", "legacy", "oracle")


def hidden_view(cfg: Config) -> str:
    """Which gate to hidden variables the policy of ``cfg`` opens: ``"legacy"`` (``LegacyHiddenView``,
    ``u_latent``), ``"oracle"`` (``OracleHiddenView``, threshold with the oracle score) or ``"none"``.
    Written to ``meta/run_metadata.hidden_view``."""
    name = cfg.policy.name
    if name == "legacy":
        return "legacy"
    if name == "threshold" and is_oracle(cfg.policy.threshold.score_fn):
        return "oracle"
    return "none"
