"""Command line: ``python -m sim run --mode <mode> --config <yaml> [--set a.b=c ...] [--out DIR]``."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from sim import runner
from sim.config import ConfigError, config_hash, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sim", description="Ride-hailing simulator.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one of the modes (README, section \"Các mode chạy\")")
    run.add_argument("--mode", required=True, choices=runner.MODES)
    run.add_argument("--config", action="append", required=True, metavar="YAML",
                     help="config file; repeat to layer files (later files override earlier ones)")
    run.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                     help="override one key, value parsed as YAML (e.g. policy.threshold.theta=0.4)")
    run.add_argument("--out", type=Path, default=None,
                     help="output directory (default runs/<mode>-<config_hash>)")
    run.add_argument("--log-level", choices=("minimal", "full"), default="minimal",
                     help="full also keeps session and order tables in evaluate/sweep_theta/gte")
    run.add_argument("--profile", action="store_true", help="print time spent per engine step")
    return parser


def summarize(mode: str, table: pd.DataFrame) -> str:
    """Console summary of a mode's headline table."""
    if mode == "throughput_curve":
        return runner.summarize_throughput(table).to_string(index=False)
    if mode == "sweep_theta":
        return table.to_string(index=False)
    if mode == "gte":
        s = runner.gte_summary(table)
        return (f"N_on={s['N_on']:.1f} N_off={s['N_off']:.1f} GTE={s['GTE']:.1f} "
                f"(se={s['GTE_se']:.1f}, n_seeds={s['n_seeds']})")
    if mode == "calibrate_budget":
        row = table.iloc[0]
        return f"budget_B_vnd={row['budget_B_vnd']:.2f} per period (mode={row['mode']}, fraction={row['fraction']})"
    if mode in ("evaluate", "generate"):
        n = len(table)
        se = table["N_completed"].std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
        line = (f"policy={table['policy'].iloc[0]} n_seeds={n} N_mean={table['N_completed'].mean():.1f} "
                f"(se={se:.1f}) V_mean={table['V_profit_vnd'].mean():.1f} "
                f"spent_mean={table['voucher_spent_vnd'].mean():.1f} B={table['budget_B_vnd'].iloc[0]}")
        if mode == "generate":
            line += "\nwritten: observed/, market/, hidden/, results/, meta/ (docs/schema.md)"
        return line
    return table.to_string(index=False)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config, args.overrides)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    digest = config_hash(cfg)
    print(f"mode={args.mode} policy={cfg.policy.name} config_hash={digest}")
    out_dir = args.out if args.out is not None else Path("runs") / f"{args.mode}-{digest}"
    try:
        table = runner.run_mode(args.mode, cfg, out_dir, log_level=args.log_level, profile=args.profile)
    except NotImplementedError as exc:
        print(f"mode '{args.mode}' is not implemented for this config ({exc})", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"out={out_dir}")
    with pd.option_context("display.width", 160, "display.max_columns", 20, "display.precision", 3):
        print(summarize(args.mode, table))
    return 0
