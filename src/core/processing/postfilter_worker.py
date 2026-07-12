from __future__ import annotations
import logging
import pandas as pd
from typing import Any

import airportsdata

from src.common.utils import setup_file_logger
from .filter_result import FilterResult
from .trajectory_filters import (
    check_velocity,
    check_coordinate_velocity,
    check_acceleration,
    check_distance,
)

logger = logging.getLogger(__name__)

# Module-level globals for worker processes
_airports: dict[str, dict[str, Any]] = {}
_thresholds: dict[str, float] = {}

def _worker_init(thresholds: dict[str, float]) -> None:
    """Initialize process-level state for process pool workers."""
    # Initialize the log handler for the worker process (idempotent/spawn-safe)
    setup_file_logger(log_filename="processing.log")
    
    global _airports, _thresholds
    try:
        _airports = airportsdata.load()
    except Exception as e:
        logger.error(f"Failed to load airportsdata in worker process: {e}")
        _airports = {}
        
    _thresholds = thresholds
    logger.debug("Worker process initialized successfully.")

def process_batch(
    batch: list[FilterResult],
    filters_to_run: list[str],
) -> list[FilterResult]:
    """
    Worker task: Processes a batch of flights by loading their clean trajectory Parquet files,
    running the specified filters, and updating their FilterResult objects.
    """
    for fr in batch:
        try:
            # 1. Load clean trajectory file
            df = pd.read_parquet(fr.file_path)
            if df.empty:
                raise ValueError("Trajectory dataframe is empty")
                
            # 2. Run selected filters
            if "velocity" in filters_to_run:
                fr.velocity_pass, fr.velocity_reject_reason = check_velocity(df, _thresholds)
                
            if "coordinate_velocity" in filters_to_run:
                fr.coordinate_velocity_pass, fr.coordinate_velocity_reject_reason = check_coordinate_velocity(df, _thresholds)
                
            if "acceleration" in filters_to_run:
                fr.acceleration_pass, fr.acceleration_reject_reason = check_acceleration(df, _thresholds)
                
            if "distance" in filters_to_run:
                fr.distance_pass, fr.distance_reject_reason = check_distance(df, _airports, _thresholds)
                
        except Exception as exc:
            err_msg = f"LOAD_ERROR: {str(exc)}"
            logger.error(f"Error processing flight {fr.flight_id} from {fr.file_path}: {exc}")
            
            # Fail all requested filters with the load error reason
            if "velocity" in filters_to_run:
                fr.velocity_pass = False
                fr.velocity_reject_reason = err_msg
            if "coordinate_velocity" in filters_to_run:
                fr.coordinate_velocity_pass = False
                fr.coordinate_velocity_reject_reason = err_msg
            if "acceleration" in filters_to_run:
                fr.acceleration_pass = False
                fr.acceleration_reject_reason = err_msg
            if "distance" in filters_to_run:
                fr.distance_pass = False
                fr.distance_reject_reason = err_msg

    return batch
