"""
cli.py — CLI entrypoint for the Physics Simulation Pipeline

Preserves all flags from clone_simulation.py argparse with their original
objectives. Pure CLI argument parsing layer — no file I/O, no registry reads.

Invoked via:
    python -m src.core.physics.cli --start-date 2025-01-01 --end-date 2025-01-31 --out-dir data/results/corridor_simulations
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import List

from src.common.config import (
    CORRIDOR_PATHS_DIR,
    EUR_BBOX,
    MIN_SAFE_FL,
    WEATHER_DIR,
)
from src.common.utils import setup_file_logger
from src.core.physics import orchestrator

logger = logging.getLogger(__name__)


def _build_date_range(start_date: str, end_date: str) -> List[date]:
    """Build an inclusive list of calendar dates from YYYY-MM-DD strings."""
    from pandas import date_range as pd_date_range
    dates = pd_date_range(start=start_date, end=end_date, freq="D")
    return [d.date() for d in dates]


def parse_args(argv=None) -> argparse.Namespace:
    """Parse CLI arguments. Flags preserve original clone_simulation.py objectives."""
    parser = argparse.ArgumentParser(
        prog="python -m src.core.physics.cli",
        description="Run the Delta-Lake physics simulation pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Simulation mode (O1 vs O2) ───────────────────────────────────────── #
    parser.add_argument(
        "--sim-mode",
        choices=["standard", "variational"],
        default="standard",
        dest="sim_mode",
        help="Simulation campaign mode: 'standard' (nominal FL baseline) or 'variational' (step-down optimization).",
    )

    # ── Physics model config identifier ─────────────────────────────────── #
    parser.add_argument(
        "--model-config-id",
        type=str,
        default="kerosene",
        dest="model_config_id",
        help="Identifier for the physics model configuration (e.g. 'kerosene', 'hydrogen').",
    )

    # ── Fuel type (Slot 3 injection) ────────────────────────────────────── #
    parser.add_argument(
        "--fuel",
        choices=["kerosene", "hydrogen"],
        default="kerosene",
        dest="fuel",
        help="Fuel type to attach to pycontrails Flight object in Slot 3 (default 'kerosene').",
    )

    # ── Step-down altitude method (Slot 3 trajectory transform) ─────────── #
    parser.add_argument(
        "--step-down-method",
        choices=["cap"],
        default=None,
        dest="step_down_method",
        help="Step-down altitude modification method in Slot 3 for variational mode (e.g. 'cap').",
    )

    # ── Date range ──────────────────────────────────────────────────────── #
    parser.add_argument(
        "--start-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="First calendar day to process (inclusive).",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Last calendar day to process (inclusive).",
    )

    # ── Corridor rank selection (matches old --ranks / --lower-rank) ─────── #
    rank_group = parser.add_mutually_exclusive_group()
    rank_group.add_argument(
        "--ranks",
        type=str,
        default=None,
        metavar="1,3",
        help="Comma-separated cluster ranks to process (e.g. '1,3').",
    )
    rank_group.add_argument(
        "--lower-rank",
        type=int,
        default=None,
        dest="lower_rank",
        metavar="N",
        help="Lower bound of corridor cluster rank (inclusive).",
    )
    parser.add_argument(
        "--upper-rank",
        type=int,
        default=None,
        dest="upper_rank",
        metavar="N",
        help="Upper bound of corridor cluster rank (inclusive).",
    )

    # ── Paths ────────────────────────────────────────────────────────────── #
    parser.add_argument(
        "--weather-cache",
        default=str(WEATHER_DIR),
        metavar="DIR",
        help="Directory containing hourly ERA5 .nc cache files.",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help=(
            "Delta Lake root directory for simulation results. Suggested targets: "
            "'data/results/corridor_simulations_kerosene' (Jet-A/kerosene) or "
            "'data/results/corridor_simulations_hydrogen' (hydrogen)."
        ),
    )
    parser.add_argument(
        "--corridors-dir",
        default=str(CORRIDOR_PATHS_DIR),
        metavar="DIR",
        help="Directory containing cluster parquet trajectory files.",
    )

    # ── Physics model parameters ─────────────────────────────────────────── #
    parser.add_argument(
        "--max-age",
        type=int,
        default=48,
        dest="max_age",
        metavar="HOURS",
        help="Max contrail segment age in hours passed to CoCiP (default 48).",
    )

    # ── Variational campaign parameters (used when --sim-mode variational) ─ #
    parser.add_argument(
        "--step-size",
        type=float,
        default=10.0,
        dest="step_size",
        metavar="FL",
        help="FL decrement step size in FL units (default 10.0 = 1000 ft).",
    )
    parser.add_argument(
        "--min-safe-fl",
        type=float,
        default=MIN_SAFE_FL,
        dest="min_safe_fl",
        metavar="FL",
        help=f"Minimum safe flight level for variational step-down (default {MIN_SAFE_FL}).",
    )

    # ── Flight sampling ─────────────────────────────────────────────────── #
    parser.add_argument(
        "--clusters-per-flight",
        type=int,
        default=1,
        dest="clusters_per_flight",
        metavar="K",
        help="Number of clusters to sample per flight from available set (default 1).",
    )
    parser.add_argument(
        "--min-distance",
        type=float,
        default=0.0,
        dest="min_distance",
        metavar="KM",
        help="Minimum great-circle flight distance in km (default 0).",
    )

    # ── Execution options ────────────────────────────────────────────────── #
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        dest="max_workers",
        metavar="N",
        help="Number of concurrent worker threads.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        dest="batch_size",
        metavar="N",
        help="Max flights per parallel batch; large groups are chunked (default 50).",
    )
    parser.add_argument(
        "--low-mem",
        action="store_true",
        dest="low_mem",
        help="Lazy-load ERA5 (skip eager .load()); reduces peak RAM at cost of speed.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-simulate tasks that already have results in the Delta Lake.",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        dest="test_mode",
        help="Limit run to a single day (2025-01-01) and 1 cluster per flight.",
    )

    return parser.parse_args(argv)


def main(argv=None) -> None:
    """Parse args and invoke the orchestrator."""
    args = parse_args(argv)

    # Test-mode overrides
    if args.test_mode:
        logger.info("=== TEST MODE ===")
        args.start_date = "2025-01-01"
        args.end_date   = "2025-01-01"
        args.clusters_per_flight = 1

    # Build date range
    date_range = _build_date_range(args.start_date, args.end_date)
    if not date_range:
        logger.error("Empty date range — nothing to do.")
        sys.exit(1)

    logger.info(
        "Date range: %s → %s (%d day(s)).",
        args.start_date, args.end_date, len(date_range),
    )

    # Mutual exclusion guard: --step-down-method ↔ --sim-mode variational
    if args.step_down_method is not None and args.sim_mode != "variational":
        logger.error("--step-down-method requires --sim-mode variational")
        sys.exit(2)
    if args.sim_mode == "variational" and args.step_down_method is None:
        logger.error("--sim-mode variational requires --step-down-method (e.g. --step-down-method cap)")
        sys.exit(2)

    # Parse rank parameters (pure argument processing)
    ranks_to_process = None
    if args.ranks:
        try:
            ranks_to_process = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            logger.error("--ranks must be a comma-separated list of integers.")
            sys.exit(1)
    elif args.lower_rank is not None:
        if args.upper_rank is None:
            logger.error("--upper-rank is required when --lower-rank is specified.")
            sys.exit(1)
        ranks_to_process = list(range(args.lower_rank, args.upper_rank + 1))

    # Resolve paths
    lake_path         = Path(args.out_dir)
    weather_cache_dir = Path(args.weather_cache)
    corridors_dir     = Path(args.corridors_dir)

    # Delegate to orchestrator
    orchestrator.run(
        date_range=date_range,
        sim_mode=args.sim_mode,
        model_config_id=args.model_config_id,
        ranks=ranks_to_process,
        lake_path=lake_path,
        weather_cache_dir=weather_cache_dir,
        corridors_dir=corridors_dir,
        fuel=args.fuel,
        step_down_method=args.step_down_method,
        max_age_hours=args.max_age,
        max_workers=args.max_workers,
        batch_size=args.batch_size,
        step_size=args.step_size,
        min_safe_fl=args.min_safe_fl,
        low_mem=args.low_mem,
        clusters_per_flight=args.clusters_per_flight,
        min_distance_km=args.min_distance,
        overwrite=args.overwrite,
        bbox=EUR_BBOX,
    )


if __name__ == "__main__":
    setup_file_logger(log_filename="simulation.log")
    main()
