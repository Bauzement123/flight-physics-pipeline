"""
Module 1.2b: OpenSky Fetcher Orchestrator
Batch-processing orchestration engine. Coordinates fetching trajectories for ranked corridors
from Trino/local cache into dynamically generated dataset namespace directories.
Every public function is strictly <= 50 LOC.
"""
import argparse
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.common.config import (
    FETCH_RUNS_DIRNAME,
    MASTER_FLIGHTS_FILE,
    MIN_DISTANCE_KM,
    ROUTE_SUMMARY_PARQUET,
    TRAJECTORIES_DIR,
)
from src.common.utils import (
    extract_target_routes,
    generate_dataset_name,
    load_route_summary,
    setup_file_logger,
    split_route_string,
    write_json_dataclass,
)
from src.core.fetching import opensky_fetcher
from src.core.fetching.helpers import apply_flight_filters, load_master_flights_for_route

logger = logging.getLogger(__name__)


def _calculate_target_quota(capacity: int, strategy: str, value: float) -> int:
    """Calculates target sample size based on sampling strategy and cohort capacity."""
    if strategy == 'all':
        return capacity
    if strategy == 'fixed':
        return min(int(value), capacity)
    if strategy == 'percent':
        return min(math.ceil(capacity * (value / 100.0)), capacity)
    return capacity


def compute_fetch_targets(
    routes_df: pd.DataFrame,
    flight_source: str | Path = MASTER_FLIGHTS_FILE,
    strategy: str = 'fixed',
    value: float = 50.0,
    start_date: str | None = None,
    end_date: str | None = None,
    typecode: str | None = None,
) -> list[dict[str, Any]]:
    """Loads flights in memory from flight_source, applies filters, and calculates sample quotas."""
    plan = []
    source_path = Path(flight_source)
    logger.info(f"Scanning master flight list ({source_path.name}) and calculating sample quotas...")

    for _, row in routes_df.iterrows():
        rank, dep, arr = row['rank'], row['dep'], row['arr']
        df_flights = load_master_flights_for_route(dep, arr, source=source_path)
        if df_flights.empty:
            logger.warning(f"No master flights found for route {dep}->{arr}. Skipping corridor.")
            continue

        try:
            df_filtered = apply_flight_filters(df_flights, start_date=start_date, end_date=end_date, typecode=typecode)
            capacity = len(df_filtered)
            logger.info(f"Corridor {dep} -> {arr}: filtered from {len(df_flights)} to {capacity} flights.")
        except Exception as e:
            logger.error(f"Error filtering flight list for {dep}->{arr}: {e}")
            continue

        if capacity == 0:
            logger.warning(f"Rank {rank} ({dep}->{arr}) has 0 flights matching filters. Skipping.")
            continue

        target = _calculate_target_quota(capacity, strategy, value)
        plan.append({
            'rank': rank,
            'dep': dep,
            'arr': arr,
            'flight_source': str(source_path),
            'target': target,
            'capacity': capacity,
            'filename': f"{dep}-{arr}.parquet",
        })
    return plan


def print_batch_plan(execution_plan: list[dict[str, Any]], run_id: str) -> None:
    """Prints formatted batch execution plan table to console."""
    print("\n" + "=" * 70)
    print(f"BATCH FETCH PLAN - RUN ID: {run_id}")
    print("=" * 70)
    for i, item in enumerate(execution_plan, 1):
        print(f"{i:02d}.  Rank {item['rank']:03d} | {item['dep']} -> {item['arr']} | Sample Size: {item['target']}/{item['capacity']}")
    print("=" * 70 + "\n")


def print_batch_summary(results: list[dict[str, Any]]) -> None:
    """Prints formatted batch completion summary table to console."""
    print("\n" + "=" * 70)
    print("BATCH FETCH SUMMARY")
    print("=" * 70)
    succ = sum(1 for r in results if r["success"])
    print(f"Total Corridors Processed: {len(results)} | Successful: {succ} | Failed: {len(results) - succ}")
    for r in results:
        status = "SUCCESS" if r["success"] else "FAILED"
        print(f"  [{status}] Rank {r['rank']:03d} ({r['dep']}->{r['arr']}): {r['succeeded']}/{r['requested']} flights retrieved.")
    print("=" * 70 + "\n")


def write_orchestrator_manifest(
    manifest_path: Path, run_id: str, plan: list[dict[str, Any]], results: list[dict[str, Any]], cli_params: dict[str, Any]
) -> None:
    """Writes aggregate orchestrator execution metadata to a JSON manifest."""
    payload = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cli_params": cli_params,
        "total_corridors_requested": len(plan),
        "total_corridors_succeeded": sum(1 for r in results if r["success"]),
        "total_corridors_failed": sum(1 for r in results if not r["success"]),
        "total_trajectories_requested": sum(r["requested"] for r in results),
        "total_trajectories_succeeded": sum(r["succeeded"] for r in results),
        "total_trajectories_failed": sum(r["failed"] for r in results),
        "corridor_results": results,
    }
    write_json_dataclass(manifest_path, payload)
    logger.info(f"Orchestrator manifest saved to {manifest_path}")


def run_batch(
    execution_plan: list[dict[str, Any]],
    run_id: str,
    seed: int,
    start_date: str | None = None,
    end_date: str | None = None,
    typecode: str | None = None,
    min_distance: float = MIN_DISTANCE_KM,
    fetch_format: str | None = None,
    strategy: str | None = None,
    resume: bool = False,
) -> list[dict[str, Any]]:
    """Executes the batch fetching loop sequentially across all planned corridors."""
    results = []
    total = len(execution_plan)

    for i, item in enumerate(execution_plan, 1):
        logger.info(f"Processing [{i}/{total}] - Rank {item['rank']} | {item['dep']} -> {item['arr']}")
        rank_dir_name = f"rank_{item['rank']:03d}_{item['dep']}-{item['arr']}"
        item_out_dir = TRAJECTORIES_DIR / rank_dir_name
        checkpoint_path = item_out_dir / FETCH_RUNS_DIRNAME / f"{run_id}.json"

        if resume and checkpoint_path.exists():
            # Only skip if the existing manifest explicitly records a successful run
            try:
                import json
                with open(checkpoint_path, encoding='utf-8') as _f:
                    _data = json.load(_f)
                _was_success = _data.get("result", {}).get("success", False)
            except Exception:
                _was_success = False
            if _was_success:
                logger.info(f"Resuming: skipping completed rank {item['rank']} ({item['dep']}->{item['arr']}) based on checkpoint.")
                results.append({
                    "rank": item['rank'],
                    "dep": item['dep'],
                    "arr": item['arr'],
                    "success": True,
                    "requested": item['target'],
                    "succeeded": item['target'],
                    "failed": 0,
                    "resumed": True,
                    "cache_hits": 0,
                    "restore_from_concat": 0,
                    "fetch_from_trino": 0,
                    "fails": 0,
                })
                continue

        try:
            res = opensky_fetcher.fetch_trajectories(
                dep=item['dep'], arr=item['arr'], out_dir=item_out_dir, flight_source=item['flight_source'],
                sample_size=item['target'], seed=seed, start_date=start_date, end_date=end_date,
                typecode=typecode, min_distance=min_distance, run_id=run_id, rank=item['rank'],
                strategy=strategy, fetch_format=fetch_format,
                update_concat=False,  # Pass 1: individual files only; concat rebuilt in Pass 2
            )
            results.append({
                "rank": item['rank'],
                "dep": item['dep'],
                "arr": item['arr'],
                "success": res.success,
                "requested": res.requested,
                "succeeded": res.succeeded,
                "failed": res.failed,
                "resumed": False,
                "new_dfs": res.failed_flight_ids,
                "concat_path": str(res.concat_path),
                "cache_hits": res.registry_hits,
                "restore_from_concat": res.concat_recoveries,
                "fetch_from_trino": res.trino_fetches,
                "fails": res.failed,
            })
        except Exception as e:
            logger.error(f"CRITICAL ERROR fetching trajectories for {item['dep']}->{item['arr']}: {e}")
            results.append({
                "rank": item['rank'],
                "dep": item['dep'],
                "arr": item['arr'],
                "success": False,
                "requested": item['target'],
                "succeeded": 0,
                "failed": item['target'],
                "error": str(e),
                "resumed": False,
                "cache_hits": 0,
                "restore_from_concat": 0,
                "fetch_from_trino": 0,
                "fails": item['target'],
            })
            continue

    # Pass 2: Rebuild route-level concat files after all FUSE file handles have flushed
    logger.info("Pass 2: Rebuilding route-level concat files after batch completion...")
    for item in execution_plan:
        rank_dir_name = f"rank_{item['rank']:03d}_{item['dep']}-{item['arr']}"
        item_out_dir = TRAJECTORIES_DIR / rank_dir_name
        concat_path = item_out_dir / f"{item_out_dir.name}{opensky_fetcher.RAW_CONCAT_SUFFIX if hasattr(opensky_fetcher, 'RAW_CONCAT_SUFFIX') else '_all_raw.parquet'}"
        try:
            from src.common.config import RAW_CONCAT_SUFFIX, RAW_TRAJECTORY_DIRNAME
            concat_path = item_out_dir / f"{item_out_dir.name}{RAW_CONCAT_SUFFIX}"
            raw_dir = item_out_dir / RAW_TRAJECTORY_DIRNAME
            if raw_dir.exists():
                parquet_files = list(raw_dir.glob("*_raw.parquet"))
                if parquet_files:
                    new_dfs = [pd.read_parquet(p) for p in parquet_files]
                    opensky_fetcher.update_raw_concat(concat_path, new_dfs)
        except Exception as e:
            logger.error(f"Pass 2 concat rebuild failed for {item['dep']}->{item['arr']}: {e}")

    return results


def execute_batch_fetch(
    execution_plan: list[dict[str, Any]],
    run_id: str,
    seed: int,
    start_date: str | None = None,
    end_date: str | None = None,
    typecode: str | None = None,
    min_distance: float = MIN_DISTANCE_KM,
    fetch_format: str | None = None,
    strategy: str | None = None,
    resume: bool = False,
    cli_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Orchestrates batch plan printing, sequential execution, summary reporting, and manifest saving."""
    if not execution_plan:
        logger.error("Execution plan is empty. Aborting batch fetch.")
        return []

    print_batch_plan(execution_plan, run_id)
    results = run_batch(execution_plan, run_id, seed, start_date, end_date, typecode, min_distance, fetch_format, strategy, resume)
    print_batch_summary(results)

    manifest_path = TRAJECTORIES_DIR / FETCH_RUNS_DIRNAME / f"{run_id}_orchestrator.json"
    write_orchestrator_manifest(manifest_path, run_id, execution_plan, results, cli_params or {})
    return results


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


if __name__ == "__main__":
    from src.common.config import init_runtime
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
            results = execute_batch_fetch(
                execution_plan=plan, run_id=dataset_name, seed=args.seed, start_date=args.start_date,
                end_date=args.end_date, typecode=args.typecode, min_distance=args.min_distance,
                fetch_format=args.format, strategy=args.strategy, resume=args.resume, cli_params=vars(args)
            )
            cache_hits = sum(r.get("cache_hits", 0) for r in results)
            restore_from_concat = sum(r.get("restore_from_concat", 0) for r in results)
            fetch_from_trino = sum(r.get("fetch_from_trino", 0) for r in results)
            fails = sum(r.get("fails", 0) for r in results)
            logger.info(
                f"Batch fetch run completed in {round(time.time() - t0, 2)}s. "
                f"Cache hits: {cache_hits}, restore from concat: {restore_from_concat}, "
                f"fetch from trino: {fetch_from_trino}, fails: {fails}."
            )
        else:
            logger.error("No valid corridors available in the execution plan.")
    else:
        logger.error("No target corridors extracted matching the CLI parameters.")
