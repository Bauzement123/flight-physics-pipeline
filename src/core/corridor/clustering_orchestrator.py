"""
Orchestrator: Stage 3 Clustering
=================================
CLI driver for the per-route clustering campaign.

Queries ``GLOBAL_STABILITY_REGISTRY`` for converged routes
(``needs_resample == False`` and ``is_uptodate == True``), dispatches
``cluster_route`` to a ``ProcessPoolExecutor`` worker pool, and flushes
results to ``GLOBAL_MODEL_REGISTRY`` in configurable batches.

Usage examples
--------------
    # Process a rank range with 4 worker processes
    python -m src.corridor_modeling.clustering_orchestrator \\
        --lower-rank 1 --upper-rank 500 --max-workers 4

    # Process specific ranks, overwrite existing records
    python -m src.corridor_modeling.clustering_orchestrator \\
        --ranks 1,5,12 --overwrite

    # Test on 3 routes with verbose logging
    python -m src.corridor_modeling.clustering_orchestrator \\
        --ranks 1,2,3 --max-workers 1 --batch-write-size 3

Design notes
------------
* ``cluster_route`` is a top-level picklable function — no state, no
  lambdas.  Mandatory for ``spawn`` context on Windows.
* Registry writes happen exclusively in the main process (single-writer)
  so batch sizes don't require a lock.
* The worker function is intentionally separate from this CLI so that a
  unified Option B streaming pipeline (Step 6) can import ``cluster_route``
  directly and submit it to a dedicated clustering executor without touching
  this orchestrator.
"""

import argparse
import logging
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.common.config import D_PCA, CORRIDOR_TIME_GRID_SECONDS
from src.common.utils import load_route_summary, split_route_string, setup_file_logger
from src.common.registry_utils import (
    load_trajectory_registry,
    load_stability_registry,
    load_model_registry,
    batch_register_corridors,
)
from src.corridor_modeling.clustering_worker import cluster_route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_config() -> None:
    if D_PCA <= 0:
        logger.error(
            f"D_PCA={D_PCA} is a sentinel value. "
            "Run the Phase A calibration script (Step 5) first."
        )
        sys.exit(1)


def _resolve_ranks_to_route_ids(
    ranks: list,
    df_summary: pd.DataFrame,
) -> list:
    route_ids = []
    for rank in ranks:
        row = df_summary[df_summary["rank"] == rank]
        if row.empty:
            logger.warning(f"Rank {rank} not found in RouteSummary. Skipping.")
            continue
        dep, arr = split_route_string(row["route"].iloc[0])
        if dep == "UNK" or arr == "UNK":
            logger.warning(f"Rank {rank}: could not parse airports. Skipping.")
            continue
        route_ids.append(f"{dep}-{arr}")
    return route_ids


def _format_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"


# ---------------------------------------------------------------------------
# Main orchestration function (importable for Option B integration)
# ---------------------------------------------------------------------------

def run_clustering_campaign(
    route_ids: list,
    registry_df: pd.DataFrame,
    max_workers: int = 4,
    batch_write_size: int = 200,
    overwrite: bool = False,
) -> None:
    """
    Runs the clustering campaign for the given list of route_ids.

    This function is intentionally separated from the CLI arg-parsing so
    that Option B (Step 6 unified pipeline) can call it directly — or
    more likely call ``cluster_route`` per-route on its own executor.

    Parameters
    ----------
    route_ids       : list[str]   route_ids to cluster
    registry_df     : pd.DataFrame  global trajectory registry (read-only)
    max_workers     : int         ProcessPoolExecutor pool size
    batch_write_size: int         routes between model registry flushes
    overwrite       : bool        re-cluster already-registered routes
    """
    if not route_ids:
        logger.info("No route_ids supplied. Nothing to do.")
        return

    # Filter already-done unless overwrite
    if not overwrite:
        model_df = load_model_registry()
        done_ids = set(model_df["route_id"].dropna()) if not model_df.empty else set()
        before = len(route_ids)
        route_ids = [r for r in route_ids if r not in done_ids]
        skipped = before - len(route_ids)
        if skipped:
            logger.info(
                f"Skipping {skipped} routes already in model registry "
                f"(use --overwrite to reprocess)."
            )
    else:
        skipped = 0

    if not route_ids:
        logger.info("All target routes already clustered. Nothing to do.")
        return

    logger.info(
        f"Clustering {len(route_ids)} routes | "
        f"max_workers={max_workers} | D_PCA={D_PCA}"
    )

    pending: list = []
    n_ok = 0
    n_error = 0
    n_insufficient = 0
    total_timing = 0.0
    start_time = time.time()

    ctx = mp.get_context("spawn")

    with ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx) as pool:
        futures = {
            pool.submit(
                cluster_route,
                route_id,
                registry_df,
                D_PCA,
                CORRIDOR_TIME_GRID_SECONDS,
                overwrite,
            ): route_id
            for route_id in route_ids
        }

        completed = 0
        for future in as_completed(futures):
            route_id = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                logger.error(
                    f"Route {route_id}: unhandled exception from worker: {exc}",
                    exc_info=True,
                )
                result = dict(
                    route_id=route_id,
                    status="error",
                    optimal_k=0,
                    silhouette_score=None,
                    route_class=0,
                    corridors=[],
                    error_msg=str(exc),
                    timing_seconds=0.0,
                )

            status = result.get("status", "error")
            if status == "ok":
                n_ok += 1
            elif status == "insufficient_data":
                n_insufficient += 1
            else:
                n_error += 1
            total_timing += result.get("timing_seconds", 0.0)

            # Only queue results that produced corridors for registry write
            if result.get("corridors"):
                pending.append(result)

            completed += 1
            if completed % 50 == 0 or completed == len(futures):
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(futures) - completed) / rate if rate > 0 else 0
                avg_ms = (total_timing / n_ok * 1000) if n_ok > 0 else 0
                logger.info(
                    f"Progress: {completed}/{len(futures)} | "
                    f"ok={n_ok} insuf={n_insufficient} err={n_error} | "
                    f"avg={avg_ms:.0f}ms/route | "
                    f"elapsed={_format_duration(elapsed)} ETA={_format_duration(eta)}"
                )

            if len(pending) >= batch_write_size:
                logger.info(f"Flushing {len(pending)} results to model registry...")
                batch_register_corridors(pending)
                pending.clear()

    if pending:
        logger.info(f"Final flush: {len(pending)} results to model registry...")
        batch_register_corridors(pending)
        pending.clear()

    total_wall = time.time() - start_time
    avg_route_ms = (total_timing / n_ok * 1000) if n_ok > 0 else 0
    logger.info(
        f"\n{'='*60}\n"
        f"CLUSTERING CAMPAIGN COMPLETE\n"
        f"  Routes targeted        : {len(route_ids)}\n"
        f"  Skipped (existing)     : {skipped}\n"
        f"  Clustered (ok)         : {n_ok}\n"
        f"  Insufficient data      : {n_insufficient}\n"
        f"  Errors                 : {n_error}\n"
        f"  Avg clustering time    : {avg_route_ms:.0f} ms/route\n"
        f"  Total wall-clock time  : {_format_duration(total_wall)}\n"
        f"{'='*60}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stage 3 Clustering Orchestrator — "
            "clusters converged routes and selects corridor medoids."
        )
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--ranks",
        type=str,
        help="Comma-separated list of route ranks (e.g. '1,5,12').",
    )
    group.add_argument(
        "--lower-rank",
        type=int,
        help="Lower bound of a route rank range (inclusive).",
    )
    parser.add_argument(
        "--upper-rank",
        type=int,
        help="Upper bound of a route rank range (inclusive). Required with --lower-rank.",
    )
    parser.add_argument(
        "--max-workers", type=int, default=4,
        help="Number of parallel worker processes (default: 4).",
    )
    parser.add_argument(
        "--batch-write-size", type=int, default=200,
        help="Routes between model registry flushes (default: 200).",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-cluster routes already in the model registry.",
    )

    args = parser.parse_args()

    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required when --lower-rank is specified.")
    if args.lower_rank is not None and args.lower_rank > args.upper_rank:
        parser.error("--lower-rank cannot be greater than --upper-rank.")

    if args.ranks:
        try:
            ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")
    else:
        ranks_list = list(range(args.lower_rank, args.upper_rank + 1))

    _validate_config()

    # Load shared data in the main process
    logger.info("Loading route summary...")
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary empty or missing. Aborting.")
        sys.exit(1)

    logger.info("Loading trajectory registry...")
    registry_df = load_trajectory_registry()
    if registry_df.empty:
        logger.error("Trajectory registry empty or missing. Aborting.")
        sys.exit(1)
    logger.info(f"Registry loaded: {len(registry_df):,} flight entries.")

    # Filter stability registry for converged routes
    logger.info("Loading stability registry...")
    stability_df = load_stability_registry()
    if not stability_df.empty:
        converged = stability_df[
            (stability_df["needs_resample"] == False) &  # noqa: E712
            (stability_df["is_uptodate"] == True)        # noqa: E712
        ]["route_id"].tolist()
        converged_set = set(converged)
    else:
        logger.warning("Stability registry is empty — clustering all resolved routes.")
        converged_set = None  # Skip filter below

    # Resolve ranks → route_ids, then intersect with converged set
    route_ids = _resolve_ranks_to_route_ids(ranks_list, df_summary)
    if converged_set is not None:
        not_converged = [r for r in route_ids if r not in converged_set]
        if not_converged:
            logger.warning(
                f"{len(not_converged)} routes not yet converged in stability registry "
                f"(run stability_orchestrator first). Skipping: {not_converged[:5]}..."
            )
        route_ids = [r for r in route_ids if r in converged_set]

    if not route_ids:
        logger.error("No converged routes to cluster. Aborting.")
        sys.exit(1)

    run_clustering_campaign(
        route_ids=route_ids,
        registry_df=registry_df,
        max_workers=args.max_workers,
        batch_write_size=args.batch_write_size,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - [CLUSTERING] - %(levelname)s - %(message)s",
    )
    setup_file_logger(log_filename="clustering_orchestrator.log")
    main()
