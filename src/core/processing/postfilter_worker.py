from __future__ import annotations
import logging
import pandas as pd
from typing import Any

import airportsdata

from src.common.utils import setup_file_logger
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

# Module-level globals for worker processes
_airports: dict[str, dict[str, Any]] = {}

def _worker_init() -> None:
    """Initialize process-level state for process pool workers."""
    # Initialize the log handler for the worker process (idempotent/spawn-safe)
    setup_file_logger(log_filename="processing.log")
    
    global _airports
    try:
        _airports = airportsdata.load()
    except Exception as e:
        logger.error(f"Failed to load airportsdata in worker process: {e}")
        _airports = {}
        
    logger.debug("Worker process initialized successfully.")

def process_batch(
    batch: list[FilterResult],
    filters_to_run: list[str],
) -> list[FilterResult]:
    """
    Worker task: Processes a batch of flights by loading their clean trajectory Parquet files,
    running the specified metric extractors, and updating their FilterResult objects.
    """
    for fr in batch:
        try:
            # 1. Load clean trajectory file
            df = pd.read_parquet(fr.file_path)
            if df.empty:
                raise ValueError("Trajectory dataframe is empty")
                
            # 2. Extract selected metrics
            if "horiz_velocity" in filters_to_run:
                fr.metric_max_horiz_speed_kt = extract_horiz_velocity_metric(df)

            if "vert_velocity" in filters_to_run:
                fr.metric_max_vert_speed_fpm = extract_vert_velocity_metric(df)

            if "coord_horiz_velocity" in filters_to_run:
                fr.metric_max_coord_horiz_speed_kt = extract_coord_horiz_velocity_metric(df)

            if "coord_vert_velocity" in filters_to_run:
                fr.metric_max_coord_vert_speed_fpm = extract_coord_vert_velocity_metric(df)

            if "acceleration" in filters_to_run:
                fr.metric_max_acceleration_mps2 = extract_acceleration_metric(df)

            if "distance" in filters_to_run:
                dist_metrics = extract_distance_metrics(df, _airports)
                fr.metric_dep_horiz_dist_m = dist_metrics.get("dep_horiz_dist_m")
                fr.metric_dep_vert_dist_m = dist_metrics.get("dep_vert_dist_m")
                fr.metric_arr_horiz_dist_m = dist_metrics.get("arr_horiz_dist_m")
                fr.metric_arr_vert_dist_m = dist_metrics.get("arr_vert_dist_m")
                
        except Exception as exc:
            logger.error(f"Error processing flight {fr.flight_id} from {fr.file_path}: {exc}")
            # Missing metrics will be caught by FilterResult.__post_init__ or as_dict and safely mapped to pd.NA

    return batch
