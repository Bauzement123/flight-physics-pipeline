"""
Step 6: Unified Streaming Pipeline (Strategy B)
================================================
3-Pool Asynchronous Architecture connecting Trino fetching,
trajectory classification + delta-RSD stability checking + clustering,
and registry writes.

Pools
-----
Pool 1  ThreadPoolExecutor   I/O-bound Trino / cache fetching
Pool 2  ProcessPoolExecutor  CPU-bound classify -> delta-RSD -> cluster
Pool 3  Main thread          Single-writer registry flush (no concurrency)

The main thread acts as both dispatcher (submitting jobs to Pool 1 & 2,
handling requeues) and writer (flushing results to parquet registries).

Stability Resampling
--------------------
After each fetch+compute round the delta-RSD metric is computed by splitting
the current cohort in half and comparing the two halves.  If delta-RSD >= tau
and the route has not reached max_resample_rounds the job is re-submitted to
Pool 1 requesting STABILITY_RESAMPLE_MULTIPLIER * current_n flights.

Usage
-----
    python -m src.corridor_modeling.streaming_pipeline --lower-rank 1 --upper-rank 100
    python -m src.corridor_modeling.streaming_pipeline --ranks 1,5,12 --d-pca 13 --n-standard 65
    python -m src.corridor_modeling.streaming_pipeline --lower-rank 1 --upper-rank 10 --dry-run
"""

import argparse
import logging
import queue
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.common.config import (
    BASE_DIR,
    D_PCA,
    N_STANDARD,
    DELTA_CV_THRESHOLD,
    GLOBAL_TRAJECTORY_REGISTRY,
    CORRIDOR_TIME_GRID_SECONDS,
    STABILITY_MAX_RESAMPLE_ROUNDS,
    STABILITY_RESAMPLE_MULTIPLIER,
    FLIGHT_LISTS_DIR,
    MIN_FLIGHTS_FOR_CLUSTERING,
)
from src.common.utils import (
    load_route_summary,
    split_route_string,
    setup_file_logger,
    update_global_registry,
)
from src.common.registry_utils import (
    load_trajectory_registry,
    load_model_registry,
    batch_register_corridors,
)
from src.core.fetching import opensky_fetcher
from src.core.corridor.clustering_worker import (
    load_and_classify_cohort,
    cluster_from_prepared,
)
from src.core.corridor.pca_compressor import calculate_delta_cv

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job State
# ---------------------------------------------------------------------------

@dataclass
class RouteJobState:
    """Tracks per-route state across fetch/compute rounds."""
    route_id: str
    rank: int
    dep: str
    arr: str
    input_list_path: str       # path to sliced flight list parquet
    current_n: int             # flights to request this round
    round_index: int = 0       # resampling round (0 = initial)
    # Model params
    d_pca: int = 28
    n_standard: int = 140
    tau: float = 0.05
    stability_enabled: bool = True
    max_resample_rounds: int = 3
    time_grid_seconds: int = 60
    overwrite: bool = False
    # Fetch filters
    seed: int = 42
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    typecode: Optional[str] = None


# ---------------------------------------------------------------------------
# Pool 2 compute worker -- top-level picklable function
# ---------------------------------------------------------------------------

def _streaming_compute_worker(job_dict: dict, registry_df: pd.DataFrame) -> dict:
    """
    Pool 2 worker: load -> classify -> delta-RSD check -> cluster.

    Top-level function so ProcessPoolExecutor can pickle it (Windows spawn).
    Receives RouteJobState as a plain dict for pickle safety.

    Returns
    -------
    dict with keys:
        route_id, status ('needs_requery' | 'ok' | 'insufficient_data' | 'error'),
        needs_requery, delta_cv, corridors, n_flights_classified,
        optimal_k, silhouette_score, route_class, error_msg, timing_seconds
    """
    import time as _t
    import numpy as _np

    t0 = _t.perf_counter()
    route_id            = job_dict["route_id"]
    dep, arr            = route_id.split("-", 1)
    d_pca               = job_dict["d_pca"]
    round_index         = job_dict["round_index"]
    stability_enabled   = job_dict["stability_enabled"]
    tau                 = job_dict["tau"]
    max_resample_rounds = job_dict["max_resample_rounds"]
    time_grid_seconds   = job_dict["time_grid_seconds"]

    _NULL = dict(
        route_id=route_id, status="error", needs_requery=False,
        delta_cv=None, corridors=[], n_flights_classified=0,
        optimal_k=0, silhouette_score=None, route_class=0,
        error_msg=None, timing_seconds=0.0,
    )

    # Steps 1-2-3a: load, classify, vectorize, z-score
    prepared = load_and_classify_cohort(route_id, registry_df)
    if prepared is None:
        return dict(_NULL, status="insufficient_data",
                    error_msg="load_and_classify_cohort returned None",
                    timing_seconds=round(_t.perf_counter() - t0, 3))

    normalized_flights, is_clean_flags, X_scaled = prepared
    n_clean = len(normalized_flights)

    # Step 3b: delta-RSD stability check (split-half)
    delta_cv = None
    if stability_enabled and round_index < max_resample_rounds and n_clean >= 4:
        rng  = _np.random.default_rng(42 + round_index)
        idx  = rng.permutation(n_clean)
        half = n_clean // 2
        var_a = _np.var(X_scaled[idx[:half]], axis=0)
        var_b = _np.var(X_scaled[idx[half:]], axis=0)
        delta_cv = calculate_delta_cv(var_a, var_b)
        if delta_cv >= tau:
            return dict(_NULL,
                        status="needs_requery", needs_requery=True,
                        delta_cv=float(delta_cv), n_flights_classified=n_clean,
                        timing_seconds=round(_t.perf_counter() - t0, 3))

    # Steps 4-5: PCA + K-Means + medoid selection + corridor save
    cluster_result = cluster_from_prepared(
        route_id=route_id, dep=dep, arr=arr,
        normalized_flights=normalized_flights,
        is_clean_flags=is_clean_flags,
        X_scaled=X_scaled,
        d_pca=d_pca,
        time_grid_seconds=time_grid_seconds,
    )
    cluster_result["needs_requery"]        = False
    cluster_result["delta_cv"]             = float(delta_cv) if delta_cv is not None else None
    cluster_result["n_flights_classified"] = n_clean
    cluster_result["timing_seconds"]       = round(_t.perf_counter() - t0, 3)
    return cluster_result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class StreamingCorridorOrchestrator:
    """3-pool asynchronous state machine for the corridor pipeline."""

    def __init__(self, route_jobs: list, fetch_threads: int = 4,
                 compute_workers: int = 4, batch_write_size: int = 50,
                 out_dir: Optional[str] = None):
        self.route_jobs       = route_jobs
        self.fetch_threads    = fetch_threads
        self.compute_workers  = compute_workers
        self.batch_write_size = batch_write_size
        self.out_dir = out_dir or str(BASE_DIR / "data" / "trajectories")

        logger.info("Loading trajectory registry snapshot...")
        self._registry_df: pd.DataFrame = load_trajectory_registry()
        logger.info(f"Registry loaded: {len(self._registry_df):,} entries")

        # Done-callback queues (filled from any thread/process via add_done_callback)
        self._fetch_done:   queue.Queue = queue.Queue()
        self._compute_done: queue.Queue = queue.Queue()
        # Write buffers (drained by main thread only -- no lock needed)
        self._pending_writes:      list = []
        self._pending_reg_entries: list = []

        self._n_total        = len(route_jobs)
        self._n_done         = 0
        self._n_ok           = 0
        self._n_requeried    = 0
        self._n_insufficient = 0
        self._n_error        = 0
        self._t_start        = time.time()

    # -- submission helpers --------------------------------------------------

    def _submit_fetch(self, job: RouteJobState, pool: ThreadPoolExecutor) -> None:
        fut = pool.submit(self._do_fetch, job)
        fut.add_done_callback(lambda f: self._fetch_done.put((f, job)))

    def _do_fetch(self, job: RouteJobState) -> tuple:
        """Pool 1 thread: I/O only. Returns (success, new_registry_entries)."""
        success, new_entries = opensky_fetcher.fetch_trajectories(
            input_list_path=job.input_list_path,
            out_dir=self.out_dir,
            sample_size=job.current_n,
            seed=job.seed + job.round_index,   # different seed per round
            start_date=job.start_date,
            end_date=job.end_date,
            typecode=job.typecode,
        )
        return success, new_entries

    def _submit_compute(self, job: RouteJobState, pool: ProcessPoolExecutor) -> None:
        """Submits compute job with a snapshot of the current registry."""
        fut = pool.submit(
            _streaming_compute_worker,
            job.__dict__.copy(),   # plain dict -- pickle safe across spawn
            self._registry_df,     # read-only snapshot
        )
        fut.add_done_callback(lambda f: self._compute_done.put((f, job)))

    # -- write helpers (main thread only) ------------------------------------

    def _flush_trajectory_registry(self) -> None:
        if self._pending_reg_entries:
            update_global_registry(GLOBAL_TRAJECTORY_REGISTRY, self._pending_reg_entries)
            self._pending_reg_entries.clear()

    def _flush_corridor_registry(self) -> None:
        if self._pending_writes:
            batch_register_corridors(self._pending_writes)
            self._pending_writes.clear()

    def _log_progress(self) -> None:
        elapsed = time.time() - self._t_start
        rate = self._n_done / elapsed if elapsed > 0 else 0
        eta  = (self._n_total - self._n_done) / rate if rate > 0 else float("inf")
        logger.info(
            f"Progress: {self._n_done}/{self._n_total} | "
            f"ok={self._n_ok} requery={self._n_requeried} "
            f"insuf={self._n_insufficient} err={self._n_error} | "
            f"elapsed={elapsed/60:.1f}m ETA={eta/60:.1f}m"
        )

    # -- main event loop -----------------------------------------------------

    def run(self) -> None:
        """Dispatches all jobs and processes results until every route is done."""
        logger.info(
            f"Starting streaming pipeline: {self._n_total} routes | "
            f"fetch_threads={self.fetch_threads} compute_workers={self.compute_workers}"
        )
        in_flight_fetches  = 0
        in_flight_computes = 0

        with ThreadPoolExecutor(max_workers=self.fetch_threads) as fetch_pool, \
             ProcessPoolExecutor(max_workers=self.compute_workers) as compute_pool:

            # Seed Pool 1 with all initial fetch jobs
            for job in self.route_jobs:
                self._submit_fetch(job, fetch_pool)
                in_flight_fetches += 1

            while (self._n_done < self._n_total
                   or in_flight_fetches > 0
                   or in_flight_computes > 0):

                # ---- Drain completed fetch results (bounded drain per iteration) ----
                for _ in range(200):
                    try:
                        fut, job = self._fetch_done.get_nowait()
                    except queue.Empty:
                        break
                    in_flight_fetches -= 1
                    try:
                        success, new_entries = fut.result()
                    except Exception as exc:
                        logger.error(f"Fetch exception for {job.route_id}: {exc}")
                        self._n_done += 1
                        self._n_error += 1
                        continue

                    if not success:
                        logger.warning(f"Fetch returned False for {job.route_id} -- skipping.")
                        self._n_done += 1
                        self._n_error += 1
                        continue

                    # Update live in-memory registry so compute worker sees fresh flights
                    if new_entries:
                        self._pending_reg_entries.extend(new_entries)
                        new_df = pd.DataFrame(new_entries)
                        self._registry_df = (
                            pd.concat([self._registry_df, new_df], ignore_index=True)
                            .drop_duplicates(subset=["flight_id"], keep="last")
                        )
                    logger.info(
                        f"{job.route_id} | round {job.round_index} | "
                        f"fetched {len(new_entries)} entries -- submitting compute"
                    )
                    self._submit_compute(job, compute_pool)
                    in_flight_computes += 1

                # ---- Drain completed compute results ----
                for _ in range(200):
                    try:
                        fut, job = self._compute_done.get_nowait()
                    except queue.Empty:
                        break
                    in_flight_computes -= 1
                    try:
                        result = fut.result()
                    except Exception as exc:
                        logger.error(f"Compute exception for {job.route_id}: {exc}")
                        self._n_done += 1
                        self._n_error += 1
                        continue

                    status = result.get("status", "error")

                    if result.get("needs_requery") and job.round_index < job.max_resample_rounds:
                        # Not yet converged -- requeue with doubled sample size
                        job.current_n   = int(job.current_n * STABILITY_RESAMPLE_MULTIPLIER)
                        job.round_index += 1
                        dcv = result.get("delta_cv", "?")
                        logger.info(
                            f"{job.route_id} | round {job.round_index} | "
                            f"delta-RSD={dcv:.4f} >= tau={job.tau:.3f} | "
                            f"re-fetching {job.current_n} flights"
                        )
                        self._submit_fetch(job, fetch_pool)
                        in_flight_fetches += 1
                        self._n_requeried += 1
                    else:
                        # Terminal result -- route is done
                        self._n_done += 1
                        if status == "ok":
                            self._n_ok += 1
                            self._pending_writes.append(result)
                            dcv = result.get("delta_cv")
                            dcv_str = f"{dcv:.4f}" if dcv is not None else "N/A"
                            logger.info(
                                f"{job.route_id} | round {job.round_index} | "
                                f"delta-RSD={dcv_str} | "
                                f"ok k={result.get('optimal_k')} class={result.get('route_class')}"
                            )
                        elif status == "insufficient_data":
                            self._n_insufficient += 1
                            logger.warning(
                                f"{job.route_id} | insufficient_data: {result.get('error_msg')}"
                            )
                        else:
                            self._n_error += 1
                            logger.error(
                                f"{job.route_id} | error: {result.get('error_msg')}"
                            )

                # ---- Flush registries (main thread, single writer) ----
                self._flush_trajectory_registry()
                if len(self._pending_writes) >= self.batch_write_size:
                    self._flush_corridor_registry()
                    self._log_progress()

                time.sleep(0.05)  # prevent busy-waiting

        # Final flush
        self._flush_trajectory_registry()
        self._flush_corridor_registry()
        elapsed = time.time() - self._t_start
        logger.info(
            f"\n{'='*60}\n"
            f"STREAMING PIPELINE COMPLETE\n"
            f"  Routes targeted    : {self._n_total}\n"
            f"  Clustered (ok)     : {self._n_ok}\n"
            f"  Total requeues     : {self._n_requeried}\n"
            f"  Insufficient data  : {self._n_insufficient}\n"
            f"  Errors             : {self._n_error}\n"
            f"  Wall-clock         : {elapsed/3600:.2f}h ({elapsed/60:.1f}m)\n"
            f"{'='*60}"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 6: Unified Streaming Corridor Pipeline (Strategy B)"
    )

    # Route Selection
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str,
                       help="Comma-separated rank list e.g. '1,5,12'")
    group.add_argument("--lower-rank", type=int,
                       help="Lower bound of rank range (inclusive)")
    parser.add_argument("--upper-rank", type=int,
                       help="Upper bound of rank range (inclusive). Required with --lower-rank.")

    # Data Selection (retained from fetcher_orchestrator)
    parser.add_argument("--min-distance", type=float, default=0.0,
                        help="Minimum route distance km (default 0 = no filter)")
    parser.add_argument("--start-date", default=None,
                        help="Flight departure window start ISO e.g. 2025-01-01")
    parser.add_argument("--end-date", default=None,
                        help="Flight departure window end ISO e.g. 2025-12-31")
    parser.add_argument("--typecode", default=None,
                        help="Aircraft type filter e.g. A320, B738")
    parser.add_argument("--seed", type=int, default=42,
                        help="Base random sampling seed (default 42; incremented per requery round)")
    parser.add_argument("--format", choices=["oneway", "roundtrip"], default="oneway",
                        help="Route directionality (default oneway)")

    # Concurrency
    parser.add_argument("--fetch-threads", type=int, default=4,
                        help="Pool 1 thread count for Trino fetching (default 4)")
    parser.add_argument("--compute-workers", type=int, default=4,
                        help="Pool 2 process count for classify+cluster (default 4)")

    # Model Params (override config.py values)
    parser.add_argument("--d-pca", type=int, default=None,
                        help=f"PCA dimensionality (config default: {D_PCA})")
    parser.add_argument("--n-standard", type=int, default=None,
                        help=f"Initial fetch size N_standard (config default: {N_STANDARD})")
    parser.add_argument("--delta-cv-threshold", type=float, default=None,
                        help=f"delta-RSD convergence threshold tau (config default: {DELTA_CV_THRESHOLD})")
    parser.add_argument("--no-stability", action="store_true",
                        help="Disable delta-RSD stability resampling (single-pass fetch)")
    parser.add_argument("--max-resample-rounds", type=int, default=STABILITY_MAX_RESAMPLE_ROUNDS,
                        help=f"Max resampling rounds before forcing cluster (default {STABILITY_MAX_RESAMPLE_ROUNDS})")

    # Operations
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-cluster routes already in the model registry")
    parser.add_argument("--batch-write-size", type=int, default=50,
                        help="Routes between model registry flushes (default 50)")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory for raw trajectory parquets")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print execution plan and exit without fetching or clustering")

    args = parser.parse_args()

    # Validate rank args
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required with --lower-rank")
    if args.lower_rank is not None and args.lower_rank > args.upper_rank:
        parser.error("--lower-rank cannot be greater than --upper-rank")

    # Resolve effective params (CLI > config.py)
    d_pca_eff      = args.d_pca              if args.d_pca              is not None else D_PCA
    n_standard_eff = args.n_standard         if args.n_standard         is not None else N_STANDARD
    tau_eff        = args.delta_cv_threshold if args.delta_cv_threshold is not None else DELTA_CV_THRESHOLD

    if d_pca_eff <= 0:
        logger.error(f"D_PCA={d_pca_eff} invalid. Run Phase A calibration or pass --d-pca.")
        sys.exit(1)
    if n_standard_eff <= 0:
        logger.error(f"N_STANDARD={n_standard_eff} invalid.")
        sys.exit(1)

    # Resolve rank list
    if args.ranks:
        try:
            ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be comma-separated integers")
    else:
        ranks_list = list(range(args.lower_rank, args.upper_rank + 1))

    # Load route summary
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary empty or missing. Aborting.")
        sys.exit(1)

    # Apply min-distance filter
    if args.min_distance > 0 and "distance_m" in df_summary.columns:
        before = len(df_summary)
        df_summary = df_summary[df_summary["distance_m"] >= args.min_distance * 1000.0].copy()
        logger.info(f"min-distance filter: {before} -> {len(df_summary)} routes (>= {args.min_distance} km)")

    # Filter to target ranks
    df_target = df_summary[df_summary["rank"].isin(set(ranks_list))]
    if df_target.empty:
        logger.warning("No routes match the selected rank range. Exiting.")
        sys.exit(0)

    # Skip already-clustered routes unless --overwrite
    if not args.overwrite:
        model_df = load_model_registry()
        done_ids = set(model_df["route_id"].dropna()) if not model_df.empty else set()
    else:
        done_ids = set()

    # Build RouteJobState list
    route_jobs = []
    for _, row in df_target.sort_values("rank").iterrows():
        dep, arr = split_route_string(row["route"])
        if dep == "UNK" or arr == "UNK":
            logger.warning(f"Rank {row['rank']}: could not parse airports -- skipping.")
            continue
        route_id = f"{dep}-{arr}"
        if route_id in done_ids:
            logger.debug(f"Skipping {route_id} (already in model registry)")
            continue
        route_jobs.append(RouteJobState(
            route_id=route_id,
            rank=int(row["rank"]),
            dep=dep,
            arr=arr,
            input_list_path=str(FLIGHT_LISTS_DIR / f"{dep}-{arr}.parquet"),
            current_n=n_standard_eff,
            round_index=0,
            d_pca=d_pca_eff,
            n_standard=n_standard_eff,
            tau=tau_eff,
            stability_enabled=not args.no_stability,
            max_resample_rounds=args.max_resample_rounds,
            time_grid_seconds=CORRIDOR_TIME_GRID_SECONDS,
            overwrite=args.overwrite,
            seed=args.seed,
            start_date=args.start_date,
            end_date=args.end_date,
            typecode=args.typecode,
        ))

    if not route_jobs:
        logger.info("No routes to process (all done or filtered out). Exiting.")
        sys.exit(0)

    skipped = len(df_target) - len(route_jobs)
    logger.info(
        f"{len(route_jobs)} routes to process ({skipped} skipped) | "
        f"D_PCA={d_pca_eff} N_std={n_standard_eff} tau={tau_eff} "
        f"stability={'off' if args.no_stability else 'on (max_rounds=' + str(args.max_resample_rounds) + ')'}"
    )

    if args.dry_run:
        for job in route_jobs:
            print(f"  rank={job.rank:5d}  {job.route_id}  N={job.current_n}  d={job.d_pca}")
        logger.info("--dry-run: exiting without processing.")
        sys.exit(0)

    StreamingCorridorOrchestrator(
        route_jobs=route_jobs,
        fetch_threads=args.fetch_threads,
        compute_workers=args.compute_workers,
        batch_write_size=args.batch_write_size,
        out_dir=args.out_dir,
    ).run()


if __name__ == "__main__":
    setup_file_logger(log_filename="corridor.log")
    setup_file_logger(log_filename="streaming_pipeline.log")
    main()
