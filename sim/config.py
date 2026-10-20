"""Configuration loading.

YAML files are deep-merged in order (later files win), ``--set key.sub=value``
overrides are applied on top, and the result is validated into nested frozen
dataclasses. Fields deliberately have no defaults: ``config/default.yaml`` is the
single source of every parameter (CLAUDE.md, hard rule 1).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args, get_origin, get_type_hints

import yaml

SUPPORTED_CONFIG_VERSION = 1


class ConfigError(ValueError):
    """Unknown or missing key, wrong type, or out-of-range value."""


# ---------------------------------------------------------------------------
# Schema: one dataclass per YAML section; field names match the YAML keys.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetaConfig:
    config_version: int
    world_seed: int
    run_seed: int


@dataclass(frozen=True)
class SpaceConfig:
    variant: Literal["synthetic", "nyc"]
    grid_radius: int
    torus: bool
    edge_km: float
    detour_factor: float
    nn_const: float
    eta_gamma: float
    eta_floor_min: float
    intra_cell_dist_factor: float
    nyc_near_rings: int
    base_speed_kmh: float
    speed_factor_by_hour: tuple[float, ...]


@dataclass(frozen=True)
class TimeConfig:
    tick_s: int
    slot_min: int
    days_per_run: int
    window_min: Optional[int]
    warmup_min: int
    cooldown_max_min: int


@dataclass(frozen=True)
class DemandConfig:
    base_sessions_per_cell_h: float
    demand_scale: float
    cell_weight_sigma: float
    hour_profile: tuple[float, ...]
    dest_decay_rings: float
    n_riders: int
    home_cell_share: float
    trip_time_noise_sigma: float


@dataclass(frozen=True)
class RidersConfig:
    x_freq_gamma_shape: float
    x_tenure_max_months: float
    x_segment_probs: tuple[float, ...]
    alpha0: float
    alpha_freq: float
    alpha_u: float
    alpha_noise_sd: float
    beta_price_per_kvnd: float
    beta_price_seg_mult: tuple[float, ...]
    beta_price_noise_sd: float
    beta_eta_per_min: float
    beta_eta_noise_sd: float
    delta0: float
    delta_u: float
    delta_seg: tuple[float, ...]
    delta_noise_sd: float
    max_wait_mode_min: float
    max_wait_sigma: float


@dataclass(frozen=True)
class PricingConfig:
    open_fare_vnd: float
    open_km: float
    per_km_vnd: float
    per_min_vnd: float
    platform_fee_vnd: float
    commission_rate: float


@dataclass(frozen=True)
class VoucherConfig:
    pct_of_fare: float
    max_vnd: Optional[float]


@dataclass(frozen=True)
class BudgetConfig:
    enforce: bool
    mode: Literal["fraction_of_all_on", "fixed"]
    fraction: float
    fixed_vnd: Optional[float]
    pilot_seed_offset: int


@dataclass(frozen=True)
class LegacyPolicyConfig:
    slack_on: float
    epsilon_cell: float
    epsilon_p_on: float
    target_g0: float
    target_g_freq: float
    target_g_u: float
    explore_frac: float
    explore_p: float


@dataclass(frozen=True)
class ThresholdPolicyConfig:
    indicator: Literal["slack", "utilization", "eta"]
    scope: Literal["cell", "ring1", "city"]
    theta: float
    forecast: Literal["persistence", "ar"]
    ar_weights: tuple[float, float]
    hysteresis_h: float
    score_fn: str
    kappa: Union[Literal["auto"], float]
    kappa_max_iter: int


@dataclass(frozen=True)
class PolicyConfig:
    name: Literal["legacy", "experiment", "threshold", "all_on", "all_off"]
    legacy: LegacyPolicyConfig
    threshold: ThresholdPolicyConfig


@dataclass(frozen=True)
class MatchingConfig:
    max_pickup_eta_min: float
    max_ring: Optional[int]
    ring_tiebreak: Literal["lowest_id", "most_idle"]


@dataclass(frozen=True)
class CancelConfig:
    base_per_min: float
    slope_per_min2: float
    eta_free_min: float
    exact_time: bool


@dataclass(frozen=True)
class SupplyConfig:
    fleet_size: int
    shift_mode: Literal["schedule", "always_on"]
    shift_start_mixture: tuple[tuple[float, float, float], ...]
    shift_len_mean_h: float
    shift_len_sd_h: float
    shift_len_clip_h: tuple[float, float]
    early_exit_enabled: bool
    reservation_wage_vnd_h: float


@dataclass(frozen=True)
class RepositionConfig:
    enabled: bool
    mode: Literal["stay", "static_weights"]
    max_idle_min: float


@dataclass(frozen=True)
class ExperimentConfig:
    design: Literal["cluster_switchback", "global_switchback", "rider_ab"]
    cluster_level: Literal[1, 7, "all"]
    block_min: int
    burnin_min: int
    p_on: float
    budget_enforce: bool


@dataclass(frozen=True)
class DayDemandShockConfig:
    sigma: float


@dataclass(frozen=True)
class EventShockConfig:
    # one row per venue kind: [n_venues, p_per_day, end_from_h, end_to_h, surge_min, mult_venue, mult_ring1]
    venues: tuple[tuple[int, float, float, float, float, float, float], ...]


@dataclass(frozen=True)
class RainShockConfig:
    p_per_day: float
    start_hour_range: tuple[float, float]
    duration_h_range: tuple[float, float]
    demand_mult: float
    speed_mult: float
    driver_off_share: float


@dataclass(frozen=True)
class AttendanceShockConfig:
    p_work: float


@dataclass(frozen=True)
class ShiftJitterShockConfig:
    sd_min: float


@dataclass(frozen=True)
class ShocksConfig:
    enabled: bool
    day_demand: DayDemandShockConfig
    events: EventShockConfig
    rain: RainShockConfig
    attendance: AttendanceShockConfig
    shift_jitter: ShiftJitterShockConfig


@dataclass(frozen=True)
class MonitorConfig:
    slack_cap: float


@dataclass(frozen=True)
class SweepConfig:
    theta_grid: tuple[float, ...]
    n_seeds: int


@dataclass(frozen=True)
class GenerateConfig:
    days: int


@dataclass(frozen=True)
class GteConfig:
    n_seeds: int


@dataclass(frozen=True)
class ThroughputConfig:
    demand_scale_grid: tuple[float, ...]
    n_seeds: int
    reference_hour: int


@dataclass(frozen=True)
class RunnerConfig:
    n_procs: Optional[int]


@dataclass(frozen=True)
class CalibrationTargetsConfig:
    p_request_no_voucher: tuple[float, float]
    request_uplift_surplus: tuple[float, float]
    share_cellslots_slack_below_0_35: tuple[float, float]
    share_cellslots_slack_above_1: tuple[float, float]
    busy_rel_h6: tuple[float, float]
    busy_rel_h7: tuple[float, float]
    busy_rel_h8: tuple[float, float]
    mean_trip_km: tuple[float, float]
    trips_per_driver_day: tuple[float, float]


@dataclass(frozen=True)
class PerformanceConfig:
    max_seconds_per_sim_day: float


@dataclass(frozen=True)
class Config:
    meta: MetaConfig
    space: SpaceConfig
    time: TimeConfig
    demand: DemandConfig
    riders: RidersConfig
    pricing: PricingConfig
    voucher: VoucherConfig
    budget: BudgetConfig
    policy: PolicyConfig
    matching: MatchingConfig
    cancel: CancelConfig
    supply: SupplyConfig
    reposition: RepositionConfig
    experiment: ExperimentConfig
    shocks: ShocksConfig
    monitor: MonitorConfig
    sweep: SweepConfig
    generate: GenerateConfig
    gte: GteConfig
    throughput: ThroughputConfig
    runner: RunnerConfig
    calibration_targets: CalibrationTargetsConfig
    performance: PerformanceConfig


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

PathLike = Union[str, Path]


def load_config(paths: PathLike | Sequence[PathLike], overrides: Sequence[str] = ()) -> Config:
    """Load, merge, override and validate. ``paths`` may be one file or a list of layers."""
    return build_config(load_raw(paths, overrides))


def load_raw(paths: PathLike | Sequence[PathLike], overrides: Sequence[str] = ()) -> dict[str, Any]:
    """Merged plain dict before validation (layers in order, then ``--set`` overrides)."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    if not paths:
        raise ConfigError("no config file given")
    raw: dict[str, Any] = {}
    for path in paths:
        _deep_merge(raw, _read_yaml(Path(path)))
    for assignment in overrides:
        apply_override(raw, assignment)
    return raw


def build_config(raw: Mapping[str, Any]) -> Config:
    cfg = _build(Config, raw, "")
    _check_ranges(cfg)
    return cfg


def apply_override(raw: dict[str, Any], assignment: str) -> None:
    """Apply one ``key.sub=value`` override in place; the value is parsed as YAML."""
    key, sep, text = assignment.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ConfigError(f"--set expects key.sub=value, got {assignment!r}")
    parts = key.split(".")
    node: Any = raw
    for depth, part in enumerate(parts[:-1]):
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise ConfigError(f"--set {key}: '{'.'.join(parts[: depth + 1])}' is not a config section")
    if parts[-1] not in node:
        raise ConfigError(f"--set {key}: unknown key")
    try:
        node[parts[-1]] = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"--set {key}: cannot parse value {text!r}: {exc}") from exc


def to_dict(cfg: Config) -> dict[str, Any]:
    """Plain nested dict with lists instead of tuples (JSON/YAML friendly)."""
    return _plain(dataclasses.asdict(cfg))


def eval_window_min(cfg: Config) -> int:
    """Length of the evaluation window in minutes.

    ``time.window_min`` if set, otherwise ``days_per_run * 1440``. Use this instead of
    reading either key directly.
    """
    t = cfg.time
    return t.window_min if t.window_min is not None else t.days_per_run * 1440


def budget_period_min(cfg: Config) -> int:
    """Length of one budget period in minutes: ``min(1440, window)``."""
    return min(1440, eval_window_min(cfg))


def config_hash(cfg: Config) -> str:
    """sha1 of the normalized config, first 12 hex chars.

    Hashing the validated config (not the raw YAML) makes ``1`` and ``1.0`` in a
    float field produce the same hash.
    """
    text = json.dumps(to_dict(cfg), sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# YAML reading and merging
# ---------------------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of silently keeping the last one."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=_UniqueKeyLoader)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


# ---------------------------------------------------------------------------
# Typed construction
# ---------------------------------------------------------------------------


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _build(cls: type, data: Any, path: str) -> Any:
    where = path or "<root>"
    if not isinstance(data, Mapping):
        raise ConfigError(f"{where}: expected a mapping, got {data!r}")
    names = [f.name for f in dataclasses.fields(cls)]
    unknown = sorted(str(k) for k in data if k not in names)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}")
    missing = [n for n in names if n not in data]
    if missing:
        raise ConfigError(f"{where}: missing key(s) {missing}")
    hints = get_type_hints(cls)
    return cls(**{n: _convert(data[n], hints[n], _join(path, n)) for n in names})


def _convert(value: Any, tp: Any, path: str) -> Any:
    if dataclasses.is_dataclass(tp):
        return _build(tp, value, path)
    origin = get_origin(tp)
    if origin is Union:
        for arm in get_args(tp):
            if arm is type(None):
                if value is None:
                    return None
                continue
            try:
                return _convert(value, arm, path)
            except ConfigError:
                pass
        raise ConfigError(f"{path}: {value!r} does not match {tp}")
    if origin is Literal:
        # Compare types too, so that True does not match 1.
        for option in get_args(tp):
            if type(value) is type(option) and value == option:
                return value
        raise ConfigError(f"{path}: {value!r} is not one of {list(get_args(tp))}")
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{path}: expected a list, got {value!r}")
        args = get_args(tp)
        if len(args) == 2 and args[1] is Ellipsis:
            args = (args[0],) * len(value)
        elif len(value) != len(args):
            raise ConfigError(f"{path}: expected {len(args)} items, got {len(value)}")
        return tuple(_convert(v, a, f"{path}[{i}]") for i, (v, a) in enumerate(zip(value, args)))
    if tp is bool:
        if isinstance(value, bool):
            return value
    elif tp is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif tp is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isnan(value):
                raise ConfigError(f"{path}: NaN is not allowed")
            return float(value)
    elif tp is str:
        if isinstance(value, str):
            return value
    else:
        raise TypeError(f"unsupported config field type {tp!r} at {path}")
    raise ConfigError(f"{path}: expected {tp.__name__}, got {value!r}")


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Range checks: basic sanity only; semantics live in the modules.
# ---------------------------------------------------------------------------


def _check_ranges(cfg: Config) -> None:
    problems: list[str] = []

    def need(ok: bool, where: str, rule: str) -> None:
        if not ok:
            problems.append(f"{where}: {rule}")

    def positive(where: str, x: float) -> None:
        need(x > 0, where, f"must be > 0, got {x}")

    def nonneg(where: str, x: float) -> None:
        need(x >= 0, where, f"must be >= 0, got {x}")

    def prob(where: str, x: float) -> None:
        need(0.0 <= x <= 1.0, where, f"must be in [0, 1], got {x}")

    def interval(where: str, pair: tuple[float, float]) -> None:
        need(pair[0] <= pair[1], where, f"lower bound must be <= upper bound, got {list(pair)}")

    need(cfg.meta.config_version == SUPPORTED_CONFIG_VERSION, "meta.config_version",
         f"must be {SUPPORTED_CONFIG_VERSION}, got {cfg.meta.config_version}")

    s = cfg.space
    need(s.grid_radius >= 1, "space.grid_radius", f"must be >= 1, got {s.grid_radius}")
    positive("space.edge_km", s.edge_km)
    need(s.detour_factor >= 1, "space.detour_factor", f"must be >= 1, got {s.detour_factor}")
    positive("space.nn_const", s.nn_const)
    positive("space.eta_gamma", s.eta_gamma)
    nonneg("space.eta_floor_min", s.eta_floor_min)
    positive("space.intra_cell_dist_factor", s.intra_cell_dist_factor)
    nonneg("space.nyc_near_rings", s.nyc_near_rings)
    positive("space.base_speed_kmh", s.base_speed_kmh)
    need(len(s.speed_factor_by_hour) == 24, "space.speed_factor_by_hour",
         f"must have 24 values, got {len(s.speed_factor_by_hour)}")
    need(all(v > 0 for v in s.speed_factor_by_hour), "space.speed_factor_by_hour", "values must be > 0")

    t = cfg.time
    need(t.tick_s > 0 and 86400 % t.tick_s == 0, "time.tick_s", f"must divide 86400, got {t.tick_s}")
    need(t.slot_min > 0 and 1440 % t.slot_min == 0, "time.slot_min", f"must divide 1440, got {t.slot_min}")
    if t.tick_s > 0:
        need((t.slot_min * 60) % t.tick_s == 0, "time.slot_min", "slot length must be a whole number of ticks")
    need(t.days_per_run >= 1, "time.days_per_run", f"must be >= 1, got {t.days_per_run}")
    if t.window_min is not None:
        # Evaluation window must be a whole number of slots.
        need(t.window_min > 0 and t.slot_min > 0 and t.window_min % t.slot_min == 0, "time.window_min",
             f"must be a positive multiple of time.slot_min ({t.slot_min}) or null, got {t.window_min}")
        # Budget periods have length min(1440, window); a longer window that is
        # not whole days would end in a short period that still gets a full B.
        need(t.window_min <= 1440 or t.window_min % 1440 == 0, "time.window_min",
             f"must be <= 1440 or a multiple of 1440, got {t.window_min}")
    nonneg("time.warmup_min", t.warmup_min)
    nonneg("time.cooldown_max_min", t.cooldown_max_min)

    d = cfg.demand
    nonneg("demand.base_sessions_per_cell_h", d.base_sessions_per_cell_h)
    nonneg("demand.demand_scale", d.demand_scale)
    nonneg("demand.cell_weight_sigma", d.cell_weight_sigma)
    need(len(d.hour_profile) == 24, "demand.hour_profile", f"must have 24 values, got {len(d.hour_profile)}")
    need(all(v >= 0 for v in d.hour_profile), "demand.hour_profile", "values must be >= 0")
    positive("demand.dest_decay_rings", d.dest_decay_rings)
    need(d.n_riders >= 1, "demand.n_riders", f"must be >= 1, got {d.n_riders}")
    prob("demand.home_cell_share", d.home_cell_share)
    nonneg("demand.trip_time_noise_sigma", d.trip_time_noise_sigma)

    r = cfg.riders
    positive("riders.x_freq_gamma_shape", r.x_freq_gamma_shape)
    positive("riders.x_tenure_max_months", r.x_tenure_max_months)
    need(all(p >= 0 for p in r.x_segment_probs) and abs(sum(r.x_segment_probs) - 1.0) < 1e-9,
         "riders.x_segment_probs", "must be >= 0 and sum to 1")
    n_seg = len(r.x_segment_probs)
    need(len(r.beta_price_seg_mult) == n_seg, "riders.beta_price_seg_mult", f"must have {n_seg} values")
    need(all(v >= 0 for v in r.beta_price_seg_mult), "riders.beta_price_seg_mult", "values must be >= 0")
    need(len(r.delta_seg) == n_seg, "riders.delta_seg", f"must have {n_seg} values")
    for name in ("alpha_noise_sd", "beta_price_per_kvnd", "beta_price_noise_sd", "beta_eta_per_min",
                 "beta_eta_noise_sd", "delta_noise_sd", "max_wait_sigma"):
        nonneg(f"riders.{name}", getattr(r, name))
    positive("riders.max_wait_mode_min", r.max_wait_mode_min)

    p = cfg.pricing
    nonneg("pricing.open_fare_vnd", p.open_fare_vnd)
    nonneg("pricing.open_km", p.open_km)
    nonneg("pricing.per_km_vnd", p.per_km_vnd)
    nonneg("pricing.per_min_vnd", p.per_min_vnd)
    nonneg("pricing.platform_fee_vnd", p.platform_fee_vnd)
    prob("pricing.commission_rate", p.commission_rate)

    prob("voucher.pct_of_fare", cfg.voucher.pct_of_fare)
    if cfg.voucher.max_vnd is not None:
        positive("voucher.max_vnd", cfg.voucher.max_vnd)

    b = cfg.budget
    prob("budget.fraction", b.fraction)
    if b.fixed_vnd is not None:
        nonneg("budget.fixed_vnd", b.fixed_vnd)
    need(b.mode != "fixed" or b.fixed_vnd is not None, "budget.fixed_vnd", "required when budget.mode = fixed")
    nonneg("budget.pilot_seed_offset", b.pilot_seed_offset)

    lg = cfg.policy.legacy
    nonneg("policy.legacy.slack_on", lg.slack_on)
    for name in ("epsilon_cell", "epsilon_p_on", "explore_frac", "explore_p"):
        prob(f"policy.legacy.{name}", getattr(lg, name))

    th = cfg.policy.threshold
    nonneg("policy.threshold.theta", th.theta)
    nonneg("policy.threshold.hysteresis_h", th.hysteresis_h)
    need(bool(th.score_fn.strip()), "policy.threshold.score_fn", "must not be empty")
    nonneg("policy.threshold.kappa_max_iter", th.kappa_max_iter)

    positive("matching.max_pickup_eta_min", cfg.matching.max_pickup_eta_min)
    if cfg.matching.max_ring is not None:
        need(cfg.matching.max_ring >= 1, "matching.max_ring", f"must be >= 1 or null, got {cfg.matching.max_ring}")

    for name in ("base_per_min", "slope_per_min2", "eta_free_min"):
        nonneg(f"cancel.{name}", getattr(cfg.cancel, name))

    sp = cfg.supply
    need(sp.fleet_size >= 1, "supply.fleet_size", f"must be >= 1, got {sp.fleet_size}")
    need(len(sp.shift_start_mixture) >= 1, "supply.shift_start_mixture", "must not be empty")
    for i, (lo, hi, w) in enumerate(sp.shift_start_mixture):
        need(0 <= lo <= hi <= 24, f"supply.shift_start_mixture[{i}]", f"need 0 <= from <= to <= 24, got {[lo, hi]}")
        nonneg(f"supply.shift_start_mixture[{i}] weight", w)
    need(sum(row[2] for row in sp.shift_start_mixture) > 0, "supply.shift_start_mixture", "weights must not all be 0")
    positive("supply.shift_len_mean_h", sp.shift_len_mean_h)
    nonneg("supply.shift_len_sd_h", sp.shift_len_sd_h)
    lo, hi = sp.shift_len_clip_h
    need(0 < lo <= hi <= 24, "supply.shift_len_clip_h", f"need 0 < lo <= hi <= 24, got {[lo, hi]}")
    nonneg("supply.reservation_wage_vnd_h", sp.reservation_wage_vnd_h)

    positive("reposition.max_idle_min", cfg.reposition.max_idle_min)

    e = cfg.experiment
    # A switchback block is a whole number of slots.
    need(e.block_min > 0 and t.slot_min > 0 and e.block_min % t.slot_min == 0, "experiment.block_min",
         f"must be a positive multiple of time.slot_min ({t.slot_min}), got {e.block_min}")
    need(0 <= e.burnin_min < e.block_min, "experiment.burnin_min",
         f"need 0 <= burnin_min < block_min, got {e.burnin_min}")
    # Blocks count from the start of the run, so the evaluation window
    # starts on a block boundary only if warm-up is whole blocks.
    if e.block_min > 0:
        need(t.warmup_min % e.block_min == 0, "time.warmup_min",
             f"must be a multiple of experiment.block_min ({e.block_min}), got {t.warmup_min}")
    prob("experiment.p_on", e.p_on)

    sh = cfg.shocks
    nonneg("shocks.day_demand.sigma", sh.day_demand.sigma)
    for i, (n, p_day, end_lo, end_hi, surge_min, mult_v, mult_r) in enumerate(sh.events.venues):
        where = f"shocks.events.venues[{i}]"
        need(n >= 0, where, f"number of venues must be >= 0, got {n}")
        prob(f"{where} p_per_day", p_day)
        need(0 <= end_lo <= end_hi <= 24, where, f"need 0 <= end_from <= end_to <= 24, got {[end_lo, end_hi]}")
        nonneg(f"{where} surge_min", surge_min)
        nonneg(f"{where} mult_venue", mult_v)
        nonneg(f"{where} mult_ring1", mult_r)
    rn = sh.rain
    prob("shocks.rain.p_per_day", rn.p_per_day)
    need(0 <= rn.start_hour_range[0] <= rn.start_hour_range[1] <= 24, "shocks.rain.start_hour_range",
         f"need 0 <= from <= to <= 24, got {list(rn.start_hour_range)}")
    need(0 <= rn.duration_h_range[0] <= rn.duration_h_range[1] <= 24, "shocks.rain.duration_h_range",
         f"need 0 <= lo <= hi <= 24, got {list(rn.duration_h_range)}")
    nonneg("shocks.rain.demand_mult", rn.demand_mult)
    positive("shocks.rain.speed_mult", rn.speed_mult)
    prob("shocks.rain.driver_off_share", rn.driver_off_share)
    prob("shocks.attendance.p_work", sh.attendance.p_work)
    nonneg("shocks.shift_jitter.sd_min", sh.shift_jitter.sd_min)

    positive("monitor.slack_cap", cfg.monitor.slack_cap)

    need(len(cfg.sweep.theta_grid) >= 1, "sweep.theta_grid", "must not be empty")
    need(all(v >= 0 for v in cfg.sweep.theta_grid), "sweep.theta_grid", "values must be >= 0")
    need(cfg.sweep.n_seeds >= 1, "sweep.n_seeds", f"must be >= 1, got {cfg.sweep.n_seeds}")
    need(cfg.generate.days >= 1, "generate.days", f"must be >= 1, got {cfg.generate.days}")
    need(cfg.gte.n_seeds >= 1, "gte.n_seeds", f"must be >= 1, got {cfg.gte.n_seeds}")

    tp = cfg.throughput
    need(len(tp.demand_scale_grid) >= 1, "throughput.demand_scale_grid", "must not be empty")
    need(all(v > 0 for v in tp.demand_scale_grid), "throughput.demand_scale_grid", "values must be > 0")
    need(tp.n_seeds >= 1, "throughput.n_seeds", f"must be >= 1, got {tp.n_seeds}")
    need(0 <= tp.reference_hour <= 23, "throughput.reference_hour", f"must be in 0..23, got {tp.reference_hour}")
    if cfg.runner.n_procs is not None:
        need(cfg.runner.n_procs >= 1, "runner.n_procs", f"must be >= 1 or null, got {cfg.runner.n_procs}")

    for f in dataclasses.fields(CalibrationTargetsConfig):
        interval(f"calibration_targets.{f.name}", getattr(cfg.calibration_targets, f.name))

    positive("performance.max_seconds_per_sim_day", cfg.performance.max_seconds_per_sim_day)

    if problems:
        raise ConfigError("invalid config:\n  " + "\n  ".join(problems))
