"""
CLI entry point for single-route worker executions.
"""
import argparse
import logging

from src.common.config import MASTER_FLIGHTS_FILE, MIN_DISTANCE_KM, init_runtime
from src.common.utils import setup_file_logger
from src.core.fetching.opensky_fetcher import fetch_trajectories

logger = logging.getLogger(__name__)


def parse_cli_args() -> argparse.Namespace:
    """Parses CLI arguments for single-route worker execution."""
    parser = argparse.ArgumentParser(description="Fetch Trajectories from OpenSky Trino (Worker)")
    parser.add_argument("--dep", required=True, help="Departure airport ICAO code (e.g. EDDF)")
    parser.add_argument("--arr", required=True, help="Arrival airport ICAO code (e.g. EGLL)")
    parser.add_argument("--out-dir", required=True, help="Output directory for route trajectories")
    parser.add_argument("--flight-source", default=str(MASTER_FLIGHTS_FILE), help="Path to master flights parquet")
    parser.add_argument("--sample-size", type=int, default=None, help="Number of random flights to sample")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic cohort sampling")
    parser.add_argument("--start-date", default=None, help="Start bounds of flight departure window (ISO format)")
    parser.add_argument("--end-date", default=None, help="End bounds of flight departure window (ISO format)")
    parser.add_argument("--typecode", default=None, help="Aircraft model code (e.g. B738, A320)")
    parser.add_argument("--min-distance", type=float, default=MIN_DISTANCE_KM,
                        help="Minimum corridor distance in km.")
    parser.add_argument("--run-id", default=None, help="Optional run identifier for checkpoints")
    parser.add_argument("--rank", type=int, default=None, help="Corridor rank index")
    parser.add_argument("--strategy", default=None, help="Sampling strategy name")
    parser.add_argument("--fetch-format", default=None, help="Format name (e.g. roundtrip)")
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint for single-route trajectory worker execution."""
    init_runtime()
    setup_file_logger(log_filename="fetching.log")
    args = parse_cli_args()
    fetch_trajectories(
        dep=args.dep,
        arr=args.arr,
        out_dir=args.out_dir,
        flight_source=args.flight_source,
        sample_size=args.sample_size,
        seed=args.seed,
        start_date=args.start_date,
        end_date=args.end_date,
        typecode=args.typecode,
        min_distance=args.min_distance,
        run_id=args.run_id,
        rank=args.rank,
        strategy=args.strategy,
        fetch_format=args.fetch_format,
    )


if __name__ == "__main__":
    main()
