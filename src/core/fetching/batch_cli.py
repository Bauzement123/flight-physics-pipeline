"""
CLI entry point for the OpenSky Fetcher Batch Orchestrator.
"""
import argparse
import logging
import sys
import time

from src.common.config import (
    MASTER_FLIGHTS_FILE,
    MIN_DISTANCE_KM,
    ROUTE_SUMMARY_PARQUET,
    init_runtime,
)
from src.common.utils import (
    extract_target_routes,
    generate_dataset_name,
    setup_file_logger,
)
from src.core.fetching.fetcher_orchestrator import (
    compute_fetch_targets,
    execute_batch_fetch,
)

logger = logging.getLogger(__name__)


def parse_cli_args() -> argparse.Namespace:
    """Parses CLI arguments for batch orchestrator execution."""
    def check_seed_range(val: str) -> int:
        try:
            ival = int(val)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Seed '{val}' is not a valid integer.")
        if ival < 0 or ival > 4294967295:
            raise argparse.ArgumentTypeError(f"Seed {ival} must be between 0 and 4294967295.")
        return ival

    parser = argparse.ArgumentParser(description="OpenSky Fetcher Orchestrator - Batch Trajectory Downloader")
    parser.add_argument("--route-summary", default=str(ROUTE_SUMMARY_PARQUET), help="Path to RouteSummary parquet file")
    parser.add_argument("--flight-source", default=str(MASTER_FLIGHTS_FILE), help="Path to master flights parquet")
    parser.add_argument("--format", choices=['oneway', 'roundtrip'], default='oneway', help="Fetch format directionality")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str, help="Comma-separated ranks list (e.g. '1,5,12')")
    group.add_argument("--lower-rank", type=int, help="Lower bound of corridor ranks")

    parser.add_argument("--upper-rank", type=int, help="Upper bound of corridor ranks")
    parser.add_argument("--strategy", choices=['fixed', 'percent', 'all'], default='fixed', help="Sampling strategy")
    parser.add_argument("--value", type=float, default=50.0, help="Value for fixed/percent strategies")
    parser.add_argument("--seed", type=check_seed_range, default=42, help="Seed value for randomized sampling state")
    parser.add_argument("--start-date", default=None, help="Start bounds of flight departure window (ISO format)")
    parser.add_argument("--end-date", default=None, help="End bounds of flight departure window (ISO format)")
    parser.add_argument("--typecode", default=None, help="Aircraft model code (e.g. B738, A320)")
    parser.add_argument("--min-distance", type=float, default=MIN_DISTANCE_KM, help="Min route distance in km")
    parser.add_argument("--resume", action="store_true", help="Resume batch fetch from previous runs")
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint for batch trajectory fetching."""
    init_runtime()
    setup_file_logger(log_filename="fetching.log")
    args = parse_cli_args()

    if args.ranks is not None and args.upper_rank is not None:
        sys.exit("--upper-rank cannot be used when --ranks is specified.")
    if args.lower_rank is not None and args.upper_rank is None:
        sys.exit("--upper-rank is required if --lower-rank is specified.")

    specific_ranks_list = None
    if args.ranks:
        try:
            specific_ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            sys.exit("--ranks must be a comma-separated list of integers.")

    dataset_name = generate_dataset_name(
        ranks=specific_ranks_list, lower_rank=args.lower_rank, upper_rank=args.upper_rank,
        strategy=args.strategy, value=args.value, seed=args.seed, fetch_format=args.format,
        start_date=args.start_date, end_date=args.end_date, typecode=args.typecode, min_distance=args.min_distance
    )
    logger.info(f"Generated dynamic dataset run ID: {dataset_name}")

    routes = extract_target_routes(
        summary_path=args.route_summary, lower=args.lower_rank, upper=args.upper_rank,
        specific_ranks=specific_ranks_list, fetch_format=args.format, min_distance=args.min_distance
    )
    if not routes.empty:
        plan = compute_fetch_targets(
            routes_df=routes, flight_source=args.flight_source, strategy=args.strategy, value=args.value,
            start_date=args.start_date, end_date=args.end_date, typecode=args.typecode
        )
        if plan:
            t0 = time.time()
            summary = execute_batch_fetch(
                execution_plan=plan, run_id=dataset_name, seed=args.seed, start_date=args.start_date,
                end_date=args.end_date, typecode=args.typecode, min_distance=args.min_distance,
                fetch_format=args.format, strategy=args.strategy, resume=args.resume, cli_params=vars(args)
            )
            logger.info(
                f"Batch fetch run completed in {round(time.time() - t0, 2)}s. "
                f"Total cumulative corridor duration: {round(summary.total_duration_seconds, 2)}s. "
                f"Cache hits: {summary.cache_hits}, restore from concat: {summary.restore_from_concat}, "
                f"fetch from trino: {summary.fetch_from_trino}, fails: {summary.fails}."
            )
        else:
            logger.error("No valid corridors available in the execution plan.")
    else:
        logger.error("No target corridors extracted matching the CLI parameters.")


if __name__ == "__main__":
    main()
