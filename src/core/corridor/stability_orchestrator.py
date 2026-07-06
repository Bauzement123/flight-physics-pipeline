"""
Orchestrator: Stage 2 Stability Sampling
=========================================
CLI driver for the per-route trajectory stability sampling campaign.

For each target route (directional city pair), dispatches ``process_route``
to a ``ProcessPoolExecutor`` worker pool.  Results are collected in the main
process and flushed to ``GLOBAL_STABILITY_REGISTRY`` in configurable batches.

Usage examples
--------------
    # Process a rank range with 6 worker processes
    python -m src.corridor_modeling.stability_orchestrator \\
        --lower-rank 1 --upper-rank 500 --max-workers 6

    # Process specific ranks, overwrite existing records
    python -m src.corridor_modeling.stability_orchestrator \\
        --ranks 1,5,12 --overwrite

    # Test on 3 routes with verbose logging
    python -m src.corridor_modeling.stability_orchestrator \\
        --ranks 1,2,3 --max-workers 1 --batch-write-size 3

Design notes
------------
* Uses ``multiprocessing.get_context("spawn")`` — mandatory on Windows where
  ``fork`` is unavailable.  Worker processes re-import the module from scratch,
  so ``stability_worker.py`` must have no side-effects at import time.
* The global trajectory registry DataFrame is loaded ONCE in the main process
  and passed as an argument to each worker (picklable via pandas).  This avoids
  repeated disk reads across 30 k worker invocations.
* Registry writes happen exclusively in the main process (single-writer) to
  avoid race conditions.
"""

import argparse
import logging
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

# Project root on PATH for direct invocation
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.common.config import (
    BASE_DIR,
    D_PCA,
    N_STANDARD,
    DELTA_CV_THRESHOLD,
    STABILITY_RESAMPLE_MULTIPLIER,
    STABILITY_MAX_RESAMPLE_ROUNDS,
)
from src.common.utils import load_route_summary, split_route_string, setup_file_logger
from src.common.registry_utils import (
    load_trajectory_registry,
    load_stability_registry,
    batch_update_stability_registry,
)
from src.core.corridor.stability_worker import process_route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_config() -> None:
    """Aborts early if Phase A/B calibration has not been run yet."""
    errors = []
    if D_PCA <= 0:
        errors.append(
            f"D_PCA={D_PCA} is a sentinel value. "
            "Run the Phase A calibration script (Step 5) first."
        )
    if N_STANDARD <= 0:
        errors.append(
            f"N_STANDARD={N_STANDARD} is a sentinel value. "
            "Run the Phase A calibration script (Step 5) first."
        )
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)


def _resolve_ranks_to_route_ids(
    ranks: list,
    df_summary: pd.DataFrame,
) -> list:
    """
    Maps rank integers to ``DEP-ARR`` route_id strings.

    Routes with unparseable airport codes are skipped with a warning.
    """
    route_ids = []
    for rank in ranks:
        row = df_summary[df_summary["rank"] == rank]
        if row.empty:
            logger.warning(f"Rank {rank} not found in RouteSummary. Skipping.")
            continue
        route_str = row["route"].iloc[0]
        dep, arr = split_route_string(route_str)
        if dep == "UNK" or arr == "UNK":
            logger.warning(
                f"Rank {rank}: could not parse airports from '{route_str}'. Skipping."
            )
            continue
        route_ids.append(f"{dep}-{arr}")
    return route_ids


def _format_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"


# ---------------------------------------------------------------------------
# Main orchestration function
# ---------------------------------------------------------------------------

def run_stability_campaign(
    ranks: list,
    max_workers: int = 4,
    batch_write_size: int = 500,
    overwrite: bool = False,
) -> None:
    """
    Runs the stability sampling campaign for the given list of ranks.

    Parameters
    ----------
    ranks           : list[int]   route ranks to process
    max_workers     : int         ProcessPoolExecutor pool size
    batch_write_size: int         routes between stability registry flushes
    overwrite       : bool        re-process routes already in stability registry
    """
    _validate_config()

    # ------------------------------------------------------------------
    # 1. Load shared data in the main process
    # ------------------------------------------------------------------
    logger.info("Loading route summary...")
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing. Aborting.")
        sys.exit(1)

    logger.info("Loading global trajectory registry...")
    registry_df = load_trajectory_registry()
    if registry_df.empty:
        logger.error("Global trajectory registry is empty or missing. Aborting.")
        sys.exit(1)
    logger.info(f"Registry loaded: {len(registry_df):,} flight entries.")

    # ------------------------------------------------------------------
    # 2. Resolve ranks → route_ids
    # ------------------------------------------------------------------
    route_ids = _resolve_ranks_to_route_ids(ranks, df_summary)
    if not route_ids:
        logger.error("No valid routes resolved from the provided ranks. Aborting.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 3. Filter already-processed routes (unless overwrite)
    # ------------------------------------------------------------------
    if not overwrite:
        stability_df = load_stability_registry()
        done_ids = set(stability_df["route_id"].dropna()) if not stability_df.empty else set()
        before = len(route_ids)
        route_ids = [r for r in route_ids if r not in done_ids]
        skipped_existing = before - len(route_ids)
        if skipped_existing:
            logger.info(
                f"Skipping {skipped_existing} routes already in stability registry "
                f"(use --overwrite to reprocess)."
            )
    else:
        skipped_existing = 0

    if not route_ids:
        logger.info("All target routes are already processed. Nothing to do.")
        return

    logger.info(
        f"Targeting {len(route_ids)} routes | "
        f"max_workers={max_workers} | "
        f"D_PCA={D_PCA} | N_STANDARD={N_STANDARD} | "
        f"ΔCV_threshold={DELTA_CV_THRESHOLD}"
    )

    # ------------------------------------------------------------------
    # 4. Dispatch to worker pool
    # ------------------------------------------------------------------
    pending_results: list = []
    n_ok = 0
    n_error = 0
    n_insufficient = 0
    n_resampled = 0
    start_time = time.time()

    # spawn context: mandatory on Windows (no fork); re-imports module in
    # each worker so stability_worker.py must be side-effect-free at import.
    ctx = mp.get_context("spawn")

    with ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx) as pool:
        futures = {
            pool.submit(
                process_route,
                route_id,
                registry_df,
                N_STANDARD,
                D_PCA,
                DELTA_CV_THRESHOLD,
                STABILITY_RESAMPLE_MULTIPLIER,
                STABILITY_MAX_RESAMPLE_ROUNDS,
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
                # Worker should never raise (catches internally), but guard anyway
                logger.error(
                    f"Route {route_id}: unhandled exception from worker: {exc}",
                    exc_info=True,
                )
                result = dict(
                    route_id=route_id,
                    status="error",
                    N_current=0,
                    pca_mean_vector=None,
                    pca_variance=None,
                    delta_cv=None,
                    needs_resample=True,
                    is_uptodate=False,
                    resample_rounds=0,
                    error_msg=str(exc),
                )

            # Tally
            status = result.get("status", "error")
            if status == "ok":
                n_ok += 1
            elif status == "insufficient_data":
                n_insufficient += 1
            else:
                n_error += 1
            if result.get("resample_rounds", 0) > 0:
                n_resampled += 1

            pending_results.append(result)
            completed += 1

            # Progress log every 50 routes
            if completed % 50 == 0 or completed == len(futures):
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(futures) - completed) / rate if rate > 0 else 0
                logger.info(
                    f"Progress: {completed}/{len(futures)} routes | "
                    f"ok={n_ok} insufficient={n_insufficient} err={n_error} | "
                    f"elapsed={_format_duration(elapsed)} | "
                    f"ETA={_format_duration(eta)}"
                )

            # Periodic flush — avoids accumulating 30k results in RAM
            if len(pending_results) >= batch_write_size:
                logger.info(
                    f"Flushing {len(pending_results)} results to stability registry..."
                )
                batch_update_stability_registry(pending_results)
                pending_results.clear()

    # Final flush
    if pending_results:
        logger.info(
            f"Final flush: writing {len(pending_results)} results to stability registry..."
        )
        batch_update_stability_registry(pending_results)
        pending_results.clear()

    # ------------------------------------------------------------------
    # 5. Summary
    # ------------------------------------------------------------------
    total_time = time.time() - start_time
    logger.info(
        f"\n{'='*60}\n"
        f"STABILITY CAMPAIGN COMPLETE\n"
        f"  Total routes targeted  : {len(route_ids)}\n"
        f"  Skipped (existing)     : {skipped_existing}\n"
        f"  Converged (ok)         : {n_ok}\n"
        f"  Insufficient data      : {n_insufficient}\n"
        f"  Errors                 : {n_error}\n"
        f"  Routes resampled       : {n_resampled}\n"
        f"  Total time             : {_format_duration(total_time)}\n"
        f"{'='*60}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stage 2 Stability Sampling Orchestrator — "
            "computes ΔCV stability for each route's trajectory cohort."
        )
    )

    # Rank selection (mutually exclusive)
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

    # Processing options
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Number of parallel worker processes (default: 4).",
    )
    parser.add_argument(
        "--batch-write-size",
        type=int,
        default=500,
        help="Number of completed routes between stability registry flushes (default: 500).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-process routes that already have a stability record.",
    )

    args = parser.parse_args()

    # Validate rank args
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required when --lower-rank is specified.")
    if args.lower_rank is not None and args.lower_rank > args.upper_rank:
        parser.error("--lower-rank cannot be greater than --upper-rank.")

    # Resolve ranks list
    if args.ranks:
        try:
            ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")
    else:
        ranks_list = list(range(args.lower_rank, args.upper_rank + 1))

    run_stability_campaign(
        ranks=ranks_list,
        max_workers=args.max_workers,
        batch_write_size=args.batch_write_size,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    setup_file_logger(log_filename="corridor.log")
    setup_file_logger(log_filename="stability_orchestrator.log")
    main()
