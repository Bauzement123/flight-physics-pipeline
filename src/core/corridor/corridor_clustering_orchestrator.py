from __future__ import annotations
import logging
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import os

import pandas as pd

from src.common.config import (
    BASE_DIR,
    GLOBAL_CLEAN_REGISTRY,
    MIN_FLIGHTS_FOR_CLUSTERING,
    CORRIDOR_CLUSTERING_THREADS_PER_WORKER,
)
from src.common.utils import (
    extract_target_routes,
    setup_file_logger,
)
from src.common.registry_utils import (
    load_model_registry,
    batch_register_corridors,
)
from src.core.corridor.corridor_clustering_worker import (
    _worker_init,
    cluster_route,
)

logger = logging.getLogger(__name__)


def _build_cohort_rows(
    df_registry: pd.DataFrame,
    route_id: str,
    require_pass: list[str],
) -> list[dict]:
    """
    Filters registry rows matching the route_id where all require_pass columns are True,
    and returns a list of dictionaries with flight_id and absolute/relative file_path.
    """
    # Match route_id: flight_id format is 'icao24_callsign_DEP-ARR_timestamp'
    pattern = f"_{route_id}_"
    matched = df_registry[df_registry["flight_id"].str.contains(pattern, na=False)]
    if matched.empty:
        return []

    # Apply require_pass filters
    for f in require_pass:
        col = f"{f}_pass"
        if col in matched.columns:
            # We enforce strict boolean check
            matched = matched[matched[col] == True]
        else:
            logger.warning(f"Filter column '{col}' not found in registry. Skipping filter check.")

    cohort_rows = []
    for _, row in matched.iterrows():
        fid = row["flight_id"]
        fpath = row["file_path"]
        p = Path(fpath)
        if not p.is_absolute():
            p = BASE_DIR / p
        cohort_rows.append({
            "flight_id": fid,
            "file_path": p.as_posix(),
        })
    return cohort_rows


def run_corridor_clustering(
    ranks: list[int] | None = None,
    rank_range: tuple[int, int] | None = None,
    routes: list[str] | None = None,
    require_pass: list[str] | None = None,
    threads_per_worker: int = CORRIDOR_CLUSTERING_THREADS_PER_WORKER,
    max_workers: int | None = None,
    overwrite: bool = False,
    batch_size: int = 50,
) -> None:
    """
    Runs the corridor clustering orchestrator. Loads target routes based on rank, rank-range,
    or explicit routes list, filters their cohorts based on require_pass columns, and dispatches
    to the process pool.

    Parameters
    ----------
    ranks : list[int], optional
        List of specific route ranks to process.
    rank_range : tuple[int, int], optional
        Inclusive rank range bounds (lower, upper).
    routes : list[str], optional
        List of route ID strings (e.g. ['EDDF-LIRF', 'EGLL-BIKF']).
    require_pass : list[str], optional
        List of post-filter check names that must be True (e.g. ['velocity', 'coordinate_velocity']).
        Defaults to all four standard post-filters if None.
    threads_per_worker : int, default 2
        Number of threads per worker process for BLAS operations.
    max_workers : int, optional
        Maximum number of process pool workers to spawn. Defaults to max(1, CPU count // threads_per_worker).
    overwrite : bool, default False
        Whether to re-process and overwrite routes already in the model registry.
    batch_size : int, default 50
        Number of processed routes to accumulate before flushing registry updates to disk.
    """
    t_start = time.perf_counter()

    if require_pass is None:
        require_pass = ["velocity", "coordinate_velocity", "acceleration", "distance"]

    # 1. Load target routes
    logger.info("Resolving target corridors...")
    targets = []
    
    # Check if explicit routes are provided
    if routes:
        for r in routes:
            r_clean = r.strip().upper()
            if "-" in r_clean:
                targets.append((r_clean, 999))  # Default rank of 999 for manual route specifications
            else:
                logger.warning(f"Skipping malformed route argument: {r} (expected DEP-ARR format)")

    # Check if ranks or rank ranges are specified
    if ranks or rank_range:
        routes_df = extract_target_routes(
            specific_ranks=ranks,
            lower=rank_range[0] if rank_range else None,
            upper=rank_range[1] if rank_range else None,
        )
        if routes_df.empty:
            logger.warning("No routes resolved from ranks/rank-range.")
        else:
            for _, row in routes_df.iterrows():
                dep, arr = row["dep"], row["arr"]
                rank = int(row["rank"])
                targets.append((f"{dep}-{arr}", rank))

    if not targets:
        logger.error("No target corridors specified or resolved. Aborting.")
        return

    # Deduplicate targets while preserving order
    seen = set()
    deduped_targets = []
    for r_id, r_rank in targets:
        if r_id not in seen:
            seen.add(r_id)
            deduped_targets.append((r_id, r_rank))
    targets = deduped_targets

    logger.info(f"Resolved {len(targets)} unique target corridors.")

    # 2. Check model registry to see what can be skipped if not overwrite
    if not overwrite:
        try:
            model_df = load_model_registry()
            if not model_df.empty:
                done_routes = set(model_df["route_id"].dropna().unique())
            else:
                done_routes = set()
        except Exception as e:
            logger.warning(f"Could not load model registry to check for existing records: {e}")
            done_routes = set()
    else:
        done_routes = set()

    # 3. Load GLOBAL_CLEAN_REGISTRY
    if not GLOBAL_CLEAN_REGISTRY.exists():
        logger.critical(f"Clean trajectory registry does not exist at {GLOBAL_CLEAN_REGISTRY}. Aborting.")
        sys.exit(1)

    logger.info(f"Loading clean trajectory registry from {GLOBAL_CLEAN_REGISTRY}...")
    try:
        df_registry = pd.read_parquet(GLOBAL_CLEAN_REGISTRY)
    except Exception as exc:
        logger.critical(f"Failed to load clean registry: {exc}")
        sys.exit(1)
    logger.info(f"Registry loaded: {len(df_registry):,} flight entries.")

    # 4. Filter and build cohort rows for each target route
    eligible_routes = []
    n_skipped_existing = 0
    n_skipped_insufficient = 0

    for route_id, rank in targets:
        if route_id in done_routes:
            n_skipped_existing += 1
            logger.info(f"Route {route_id} (Rank {rank}): already in model registry. Skipping (overwrite=False).")
            continue

        cohort_rows = _build_cohort_rows(df_registry, route_id, require_pass)
        if len(cohort_rows) < MIN_FLIGHTS_FOR_CLUSTERING:
            n_skipped_insufficient += 1
            logger.warning(
                f"Route {route_id} (Rank {rank}): cohort has {len(cohort_rows)} qualifying flights, "
                f"which is less than MIN_FLIGHTS_FOR_CLUSTERING ({MIN_FLIGHTS_FOR_CLUSTERING}). Skipping."
            )
            continue

        eligible_routes.append((route_id, rank, cohort_rows))

    logger.info(
        f"Pre-filtering summary: {len(eligible_routes)} eligible routes, "
        f"{n_skipped_existing} skipped (existing), {n_skipped_insufficient} skipped (insufficient data)."
    )

    if not eligible_routes:
        logger.info("No routes remaining to process. Exiting.")
        return

    # 5. Set up worker concurrency
    cpu_count = os.cpu_count() or 1
    if max_workers is None:
        max_workers = max(1, cpu_count // threads_per_worker)

    logger.info(
        f"Spawning ProcessPoolExecutor with max_workers={max_workers} "
        f"(threads_per_worker={threads_per_worker}, system CPU count={cpu_count})"
    )

    # 6. Dispatch tasks to worker pool
    pending = []
    n_processed = 0
    n_failed = 0
    n_insufficient_at_runtime = 0

    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=ctx,
        initializer=_worker_init,
        initargs=(threads_per_worker,),
    ) as pool:
        futures = {
            pool.submit(cluster_route, route_id, rank, cohort_rows): route_id
            for route_id, rank, cohort_rows in eligible_routes
        }

        for future in as_completed(futures):
            route_id = futures[future]
            try:
                res = future.result()
                status = res.get("status", "error")
                if status == "ok":
                    pending.append(res)
                    n_processed += 1
                elif status == "insufficient_data":
                    n_insufficient_at_runtime += 1
                    logger.warning(f"Route {route_id}: worker detected insufficient data: {res.get('error_msg')}")
                else:
                    n_failed += 1
                    logger.error(f"Route {route_id}: worker failed with error: {res.get('error_msg')}")
            except Exception as exc:
                n_failed += 1
                logger.error(f"Route {route_id}: unhandled worker exception: {exc}", exc_info=True)

            # Flush batch to registry files
            if len(pending) >= batch_size:
                logger.info(f"Flushing batch of {len(pending)} completed routes to registries...")
                try:
                    batch_register_corridors(pending)
                except Exception as e:
                    logger.error(f"Failed to save batch registries: {e}", exc_info=True)
                pending.clear()

        # Flush final batch
        if pending:
            logger.info(f"Flushing final batch of {len(pending)} completed routes to registries...")
            try:
                batch_register_corridors(pending)
            except Exception as e:
                logger.error(f"Failed to save final batch registries: {e}", exc_info=True)
            pending.clear()

    duration = time.perf_counter() - t_start
    h, rem = divmod(int(duration), 3600)
    m, s = divmod(rem, 60)
    duration_str = f"{h:02d}h {m:02d}m {s:02d}s"

    logger.info(
        f"Corridor clustering campaign complete. "
        f"Completed: {n_processed} | Failed: {n_failed} | "
        f"Runtime insufficient: {n_insufficient_at_runtime} | "
        f"Total duration: {duration_str}."
    )
