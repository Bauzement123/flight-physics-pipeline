"""
Module 1.2b: OpenSky Fetcher Orchestrator
Batch-processing orchestration engine. Coordinates fetching trajectories for ranked corridors
from Trino/local cache into dynamically generated dataset namespace directories.
Every public function is strictly <= 50 LOC.
"""
import dataclasses
import logging
import math
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
    load_route_summary,
    setup_file_logger,
    split_route_string,
    write_json_dataclass,
)
from src.core.fetching import opensky_fetcher
from src.core.fetching.helpers import (
    apply_flight_filters,
    load_master_flights_for_route,
    prepare_flight_records,
    sample_flights,
)
from src.core.fetching.models import RouteFetchResult, RouteFetchSummary, BatchFetchSummary

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


def _plan_route(
    row: pd.Series,
    flight_source: Path,
    strategy: str,
    value: float,
    start_date: str | None,
    end_date: str | None,
    typecode: str | None,
) -> dict[str, Any] | None:
    """Plans fetch target for a single corridor, returning pre-prepared records."""
    rank, dep, arr = row['rank'], row['dep'], row['arr']
    df_flights = load_master_flights_for_route(dep, arr, source=flight_source)
    if df_flights.empty:
        logger.warning(f"No master flights found for route {dep}->{arr}. Skipping corridor.")
        return None

    try:
        df_filtered = apply_flight_filters(df_flights, start_date=start_date, end_date=end_date, typecode=typecode)
        capacity = len(df_filtered)
        logger.info(f"Corridor {dep} -> {arr}: filtered from {len(df_flights)} to {capacity} flights.")
    except Exception as e:
        logger.error(f"Error filtering flight list for {dep}->{arr}: {e}")
        return None

    if capacity == 0:
        logger.warning(f"Rank {rank} ({dep}->{arr}) has 0 flights matching filters. Skipping.")
        return None

    target = _calculate_target_quota(capacity, strategy, value)
    df_sampled = sample_flights(df_filtered, sample_size=target, seed=42)
    route_dir_name = f"{dep}-{arr}"
    out_path = TRAJECTORIES_DIR / route_dir_name
    records = prepare_flight_records(df_sampled, out_path)

    return {
        'rank': rank,
        'dep': dep,
        'arr': arr,
        'flight_source': str(flight_source),
        'target': target,
        'capacity': capacity,
        'filename': f"{dep}-{arr}.parquet",
        'records': records,
    }


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
        route_plan = _plan_route(row, source_path, strategy, value, start_date, end_date, typecode)
        if route_plan is not None:
            plan.append(route_plan)
    return plan


def print_batch_plan(execution_plan: list[dict[str, Any]], run_id: str) -> None:
    """Prints formatted batch execution plan table to console."""
    print("\n" + "=" * 70)
    print(f"BATCH FETCH PLAN - RUN ID: {run_id}")
    print("=" * 70)
    for i, item in enumerate(execution_plan, 1):
        print(f"{i:02d}.  Rank {item['rank']:03d} | {item['dep']} -> {item['arr']} | Sample Size: {item['target']}/{item['capacity']}")
    print("=" * 70 + "\n", flush=True)


def print_batch_summary(results: list[RouteFetchSummary]) -> None:
    """Prints formatted batch completion summary table to console."""
    print("\n" + "=" * 70)
    print("BATCH FETCH SUMMARY")
    print("=" * 70)
    succ = sum(1 for r in results if r.success)
    print(f"Total Corridors Processed: {len(results)} | Successful: {succ} | Failed: {len(results) - succ}")
    for r in results:
        status = "SUCCESS" if r.success else "FAILED"
        print(f"  [{status}] Rank {r.rank:03d} ({r.dep}->{r.arr}): {r.succeeded}/{r.requested} flights retrieved.")
    print("=" * 70 + "\n", flush=True)


def write_orchestrator_manifest(
    manifest_path: Path, summary: BatchFetchSummary
) -> None:
    """Writes aggregate orchestrator execution metadata to a JSON manifest."""
    write_json_dataclass(manifest_path, summary)
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
) -> list[RouteFetchSummary]:
    """Executes the batch fetching loop sequentially across all planned corridors."""
    results: list[RouteFetchSummary] = []
    total = len(execution_plan)

    for i, item in enumerate(execution_plan, 1):
        logger.info(f"Processing [{i}/{total}] - Rank {item['rank']} | {item['dep']} -> {item['arr']}")
        route_dir_name = f"{item['dep']}-{item['arr']}"
        item_out_dir = TRAJECTORIES_DIR / route_dir_name
        checkpoint_path = item_out_dir / FETCH_RUNS_DIRNAME / f"{run_id}.json"

        if resume and checkpoint_path.exists():
            try:
                import json
                with open(checkpoint_path, encoding='utf-8') as _f:
                    _data = json.load(_f)
                _was_success = _data.get("result", {}).get("success", False)
                _duration = _data.get("result", {}).get("duration_seconds", 0.0)
            except Exception:
                _was_success = False
                _duration = 0.0
            if _was_success:
                logger.info(f"Resuming: skipping completed rank {item['rank']} ({item['dep']}->{item['arr']}) based on checkpoint.")
                results.append(RouteFetchSummary.from_resumed(
                    rank=item['rank'],
                    dep=item['dep'],
                    arr=item['arr'],
                    target=item['target'],
                    duration_seconds=_duration,
                ))
                continue

        try:
            res = opensky_fetcher.fetch_trajectories(
                dep=item['dep'], arr=item['arr'], out_dir=item_out_dir, flight_source=item['flight_source'],
                sample_size=item['target'], seed=seed, start_date=start_date, end_date=end_date,
                typecode=typecode, min_distance=min_distance, run_id=run_id, rank=item['rank'],
                strategy=strategy, fetch_format=fetch_format,
                update_concat=True,
                records=item.get("records"),
            )
            results.append(RouteFetchSummary.from_fetch_result(
                rank=item['rank'],
                dep=item['dep'],
                arr=item['arr'],
                res=res,
            ))
        except Exception as e:
            logger.error(f"CRITICAL ERROR fetching trajectories for {item['dep']}->{item['arr']}: {e}")
            results.append(RouteFetchSummary.from_error(
                rank=item['rank'],
                dep=item['dep'],
                arr=item['arr'],
                target=item['target'],
                error=e,
            ))

        import gc
        gc.collect()

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
) -> BatchFetchSummary:
    """Orchestrates batch plan printing, sequential execution, summary reporting, and manifest saving."""
    if not execution_plan:
        logger.error("Execution plan is empty. Aborting batch fetch.")
        raise ValueError("Execution plan is empty.")

    print_batch_plan(execution_plan, run_id)
    results = run_batch(execution_plan, run_id, seed, start_date, end_date, typecode, min_distance, fetch_format, strategy, resume)
    print_batch_summary(results)

    summary = BatchFetchSummary(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        cli_params=cli_params or {},
        corridor_results=results
    )

    manifest_path = TRAJECTORIES_DIR / FETCH_RUNS_DIRNAME / f"{run_id}_orchestrator.json"
    write_orchestrator_manifest(manifest_path, summary)
    return summary
