"""
cli.py — CLI entrypoint for the Physics Simulation Pipeline

Preserves all flags from clone_simulation.py argparse with their original
objectives. New flags added for sim_mode, model config, and step-down params.

Invoked via:
    python -m src.core.physics.cli --start-date 2025-01-01 --end-date 2025-01-31
"""

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from src.common.config import (
    CORRIDOR_PATHS_DIR,
    CORRIDOR_SIMULATIONS_DIR,
    EUR_BBOX,
    GLOBAL_CORRIDOR_MODEL_REGISTRY,
    WEATHER_DIR,
    BASE_DIR,
)
from src.common.utils import setup_file_logger
from src.core.physics import orchestrator

logger = logging.getLogger(__name__)


def _build_date_range(start_date: str, end_date: str) -> List[date]:
    """Build an inclusive list of calendar dates from YYYY-MM-DD strings."""
    from pandas import date_range as pd_date_range
    import pandas as pd
    dates = pd_date_range(start=start_date, end=end_date, freq="D")
    return [d.date() for d in dates]


def _build_corridors_map(
    registry_path: Path = None,
) -> Dict[Tuple[str, int], Path]:
    """Build corridors_map from GLOBAL_CORRIDOR_MODEL_REGISTRY.

    Reads the ``file_path`` and ``cluster_id`` columns from the registry
    to construct the ``(route_id, cluster_id) -> absolute Path`` mapping.
    This is the canonical source — do not scan the corridor directory directly.
    """
    import pandas as pd
    reg = registry_path or GLOBAL_CORRIDOR_MODEL_REGISTRY
    corridors_map: Dict[Tuple[str, int], Path] = {}

    if not Path(reg).exists():
        logger.warning("GLOBAL_CORRIDOR_MODEL_REGISTRY not found: %s", reg)
        return corridors_map

    df = pd.read_parquet(reg)
    for _, row in df.iterrows():
        route_id   = row["route_id"]
        cluster_id = int(row["cluster_id"])
        rel_path   = row["file_path"]
        abs_path   = BASE_DIR / rel_path
        if abs_path.exists():
            corridors_map[(route_id, cluster_id)] = abs_path
        else:
            logger.warning("Corridor file missing for %s c%d: %s", route_id, cluster_id, abs_path)

    logger.info("corridors_map: %d entry/entries loaded from registry.", len(corridors_map))
    return corridors_map


def parse_args(argv=None) -> argparse.Namespace:
    """Parse CLI arguments.  Flags preserve original clone_simulation.py objectives."""
    parser = argparse.ArgumentParser(
        prog="python -m src.core.physics.cli",
        description="Run the Delta-Lake physics simulation pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
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
        default=str(CORRIDOR_SIMULATIONS_DIR),
        metavar="DIR",
        help="Delta Lake root directory for simulation results.",
    )
    parser.add_argument(
        "--corridors-dir",
        default=str(CORRIDOR_PATHS_DIR),
        metavar="DIR",
        help="Directory containing cluster parquet trajectory files.",
    )

    # ── Physics parameters ───────────────────────────────────────────────── #
    parser.add_argument(
        "--max-age", "--age",
        type=int,
        default=48,
        dest="max_age",
        metavar="HOURS",
        help="Maximum contrail simulation/advection age in hours.",
    )
    parser.add_argument(
        "--clusters-per-flight", "-x",
        type=int,
        default=1,
        dest="clusters_per_flight",
        metavar="N",
        help="Number of cluster trajectories to simulate per flight.",
    )
    parser.add_argument(
        "--default-fl",
        type=float,
        default=350.0,
        dest="default_fl",
        metavar="FL",
        help="Default flight level (feet) when registry lookup returns no value.",
    )
    parser.add_argument(
        "--min-distance",
        type=float,
        default=0.0,
        dest="min_distance",
        metavar="KM",
        help="Minimum route distance in km; shorter routes are skipped.",
    )

    # ── Simulation mode ──────────────────────────────────────────────────── #
    parser.add_argument(
        "--sim-mode",
        default="O1",
        choices=["O1", "O2"],
        dest="sim_mode",
        help="Simulation mode: O1 standard or O2 step-down variational.",
    )
    parser.add_argument(
        "--model-config-id",
        default="kerosene",
        dest="model_config_id",
        metavar="ID",
        help="Fuel/model configuration identifier.",
    )
    parser.add_argument(
        "--step-size",
        type=float,
        default=1000.0,
        dest="step_size",
        metavar="FT",
        help="FL step-down increment in feet (O2 mode only).",
    )
    parser.add_argument(
        "--min-safe-fl",
        type=float,
        default=280.0,
        dest="min_safe_fl",
        metavar="FL",
        help="Minimum FL below which step-down is halted (O2 mode only).",
    )

    # ── Execution control ────────────────────────────────────────────────── #
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

    # Build corridors map from GLOBAL_CORRIDOR_MODEL_REGISTRY
    corridors_map = _build_corridors_map()

    # ── Rank filtering (mirrors clone_simulation.py behaviour) ──────────── #
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

    if ranks_to_process is not None:
        from src.common.utils import load_route_summary
        df_summary = load_route_summary()
        # route_summary uses 'LEPA -> LEBL' format; corridor registry uses 'LEPA-LEBL'
        allowed = df_summary[df_summary["rank"].isin(ranks_to_process)]["route"]
        allowed_ids = set(r.replace(" -> ", "-") for r in allowed)
        before = len(corridors_map)
        corridors_map = {k: v for k, v in corridors_map.items() if k[0] in allowed_ids}
        logger.info(
            "Ranks %s → %d corridor(s) after filtering (was %d).",
            ranks_to_process, len(corridors_map), before,
        )
        if not corridors_map:
            logger.error("No corridor files found for ranks %s — nothing to do.", ranks_to_process)
            sys.exit(1)

    # Resolve paths
    lake_path         = Path(args.out_dir)
    weather_cache_dir = Path(args.weather_cache)

    # Delegate to orchestrator
    orchestrator.run(
        date_range=date_range,
        sim_mode=args.sim_mode,
        model_config_id=args.model_config_id,
        corridors_map=corridors_map,
        lake_path=lake_path,
        weather_cache_dir=weather_cache_dir,
        max_age_hours=args.max_age,
        max_workers=args.max_workers,
        batch_size=args.batch_size,
        step_size=args.step_size,
        min_safe_fl=args.min_safe_fl,
        low_mem=args.low_mem,
        clusters_per_flight=args.clusters_per_flight,
        default_fl=args.default_fl,
        min_distance_km=args.min_distance,
        overwrite=args.overwrite,
        bbox=EUR_BBOX,
    )


if __name__ == "__main__":
    setup_file_logger(log_filename="simulation.log")
    main()
