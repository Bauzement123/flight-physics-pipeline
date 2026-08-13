from __future__ import annotations
import logging
import pandas as pd
from pathlib import Path
from typing import Any

from src.common.utils import setup_file_logger, resolve_airport_coordinates
from src.data_manager.io_utils import append_postfilter_batch
from .filter_result import FilterResult
from .trajectory_filters import (
    extract_horiz_velocity_metric,
    extract_vert_velocity_metric,
    extract_coord_horiz_velocity_metric,
    extract_coord_vert_velocity_metric,
    extract_acceleration_metric,
    extract_distance_metrics,
)

logger = logging.getLogger(__name__)

# Module-level airport cache preloaded once per worker process at init.
# Avoids re-reading the JSON cache file on every batch (O(1) per-batch lookup).
_AIRPORTS: dict[str, dict] = {}


def _worker_init() -> None:
    """Initialize process-level state for process pool workers."""
    global _AIRPORTS
    # Initialize the log handler for the worker process (idempotent/spawn-safe)
    setup_file_logger(log_filename="processing.log")
    # Preload the full airport coordinate cache into process memory once.
    # resolve_airport_coordinates([]) returns the full cache dict without
    # triggering any network/library fallbacks (all ICAOs already present).
    _AIRPORTS = resolve_airport_coordinates([])
    logger.debug(f"Worker initialized with {len(_AIRPORTS)} airports preloaded.")


def process_batch(
    batch: list[FilterResult],
    filters_to_run: list[str],
    lake_path: Path,
) -> list[FilterResult]:
    """
    Worker task: Processes a batch of flights by loading their trajectory Parquet files (clean or raw),
    running the specified metric extractors, and updating their FilterResult objects. Any I/O or
    schema errors are caught in a try-except block, leaving uncomputed metrics as NaN.
    """
    for fr in batch:
        try:
            # 1. Load clean trajectory file
            df = pd.read_parquet(fr.file_path)
            if df.empty:
                raise ValueError("Trajectory dataframe is empty")
                
            # 2. Extract selected metrics
            if "horiz_velocity" in filters_to_run:
                fr.metric_max_horiz_speed_mps = extract_horiz_velocity_metric(df)

            if "vert_velocity" in filters_to_run:
                fr.metric_max_vert_speed_mps = extract_vert_velocity_metric(df)

            if "coord_horiz_velocity" in filters_to_run:
                fr.metric_max_coord_horiz_speed_mps = extract_coord_horiz_velocity_metric(df)

            if "coord_vert_velocity" in filters_to_run:
                fr.metric_max_coord_vert_speed_mps = extract_coord_vert_velocity_metric(df)

            if "acceleration" in filters_to_run:
                fr.metric_max_acceleration_mps2 = extract_acceleration_metric(df)

            if any(f in filters_to_run for f in ("dep_horiz_dist", "dep_vert_dist", "arr_horiz_dist", "arr_vert_dist")):
                dep_icao = str(df["estdepartureairport"].iloc[0]).strip().upper() if "estdepartureairport" in df.columns and pd.notna(df["estdepartureairport"].iloc[0]) else None
                arr_icao = str(df["estarrivalairport"].iloc[0]).strip().upper() if "estarrivalairport" in df.columns and pd.notna(df["estarrivalairport"].iloc[0]) else None
                icaos = [ic for ic in (dep_icao, arr_icao) if ic]
                # Use preloaded in-process cache; only call resolver for true cache misses.
                cache_miss = [ic for ic in icaos if ic not in _AIRPORTS]
                if cache_miss:
                    _AIRPORTS.update(resolve_airport_coordinates(cache_miss))
                airports = {ic: _AIRPORTS[ic] for ic in icaos if ic in _AIRPORTS}
                dist_metrics = extract_distance_metrics(df, airports)
                fr.metric_dep_horiz_dist_m = dist_metrics.get("dep_horiz_dist_m")
                fr.metric_dep_vert_dist_m = dist_metrics.get("dep_vert_dist_m")
                fr.metric_arr_horiz_dist_m = dist_metrics.get("arr_horiz_dist_m")
                fr.metric_arr_vert_dist_m = dist_metrics.get("arr_vert_dist_m")
                
        except Exception as exc:
            logger.error(f"Error processing flight {fr.flight_id} from {fr.file_path}: {exc}")
            # Missing metrics will be caught by FilterResult.__post_init__ or as_dict and safely mapped to pd.NA

    # Append completed batch to Delta Lake crash buffer — lock-free concurrent write
    append_postfilter_batch(lake_path, batch)

    return batch

