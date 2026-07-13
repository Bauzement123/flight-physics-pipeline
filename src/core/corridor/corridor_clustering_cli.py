from __future__ import annotations
import argparse
import logging
import os
import sys
from pathlib import Path

# Project root on PATH for direct invocation
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from src.common.config import CORRIDOR_CLUSTERING_THREADS_PER_WORKER
from src.common.utils import setup_file_logger
from src.core.corridor.corridor_clustering_orchestrator import run_corridor_clustering

logger = logging.getLogger(__name__)


def main() -> None:
    """CLI entrypoint for the simplified corridor clustering pipeline."""
    parser = argparse.ArgumentParser(
        description="Simplified Corridor Clustering and Medoid Path Generation Pipeline."
    )

    # Scoping arguments
    parser.add_argument(
        "--ranks",
        type=int,
        nargs="+",
        help="Route volume ranks to process (e.g. --ranks 1 2 5).",
    )
    parser.add_argument(
        "--rank-range",
        type=int,
        nargs=2,
        help="Inclusive range of ranks to process (e.g. --rank-range 1 50).",
    )
    parser.add_argument(
        "--routes",
        type=str,
        nargs="+",
        help="Explicit corridor route strings to process (e.g. --routes EDDF-LIRF EGLL-BIKF).",
    )

    # Filtering options
    parser.add_argument(
        "--require-pass",
        type=str,
        nargs="+",
        choices=["velocity", "coordinate_velocity", "acceleration", "distance"],
        default=["velocity", "coordinate_velocity", "acceleration", "distance"],
        help="Post-filter checks that must be True for flights to be included (default: all four).",
    )

    # Concurrency and runtime options
    parser.add_argument(
        "--threads-per-worker",
        type=int,
        default=CORRIDOR_CLUSTERING_THREADS_PER_WORKER,
        help=f"Number of threads for BLAS operations in each worker (default: {CORRIDOR_CLUSTERING_THREADS_PER_WORKER}).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum parallel worker processes (default: cpu_count // threads_per_worker).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-process and overwrite routes already present in the model registry.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of completed routes between registry flushes (default: 50).",
    )

    args = parser.parse_args()

    # Validate target inputs
    if not (args.ranks or args.rank_range or args.routes):
        parser.error("At least one of --ranks, --rank-range, or --routes must be specified.")

    if args.rank_range and args.rank_range[0] > args.rank_range[1]:
        parser.error("Lower bound of --rank-range cannot be greater than upper bound.")

    # Determine effective workers
    threads = args.threads_per_worker
    workers = args.max_workers
    if workers is None:
        cpu_count = os.cpu_count() or 1
        workers = max(1, cpu_count // threads)

    logger.info(
        f"Starting corridor clustering CLI | "
        f"effective_max_workers={workers} | "
        f"threads_per_worker={threads} | "
        f"require_pass={args.require_pass}"
    )

    # Convert rank range to tuple format for orchestrator
    r_range = tuple(args.rank_range) if args.rank_range else None

    # Run orchestrator
    run_corridor_clustering(
        ranks=args.ranks,
        rank_range=r_range,
        routes=args.routes,
        require_pass=args.require_pass,
        threads_per_worker=threads,
        max_workers=workers,
        overwrite=args.overwrite,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    setup_file_logger(log_filename="corridor.log")
    main()
