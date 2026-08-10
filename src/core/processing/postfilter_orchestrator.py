from __future__ import annotations
import concurrent.futures
import logging
import multiprocessing as mp
from pathlib import Path
from typing import Generator

import pandas as pd
import numpy as np

from src.common.config import (
    BASE_DIR,
    MPS_TO_KT,
    MPS_TO_FPM,
    POSTFILTER_BATCH_SIZE_DEFAULT,
    PROCESSING_DEFAULT_MAX_WORKERS,
    POSTFILTER_COL_HORIZ_VEL_PASS,
    POSTFILTER_COL_HORIZ_VEL_REASON,
    POSTFILTER_COL_VERT_VEL_PASS,
    POSTFILTER_COL_VERT_VEL_REASON,
    POSTFILTER_COL_COORD_HORIZ_VEL_PASS,
    POSTFILTER_COL_COORD_HORIZ_VEL_REASON,
    POSTFILTER_COL_COORD_VERT_VEL_PASS,
    POSTFILTER_COL_COORD_VERT_VEL_REASON,
    POSTFILTER_COL_ACCEL_PASS,
    POSTFILTER_COL_ACCEL_REASON,
    POSTFILTER_COL_DISTANCE_PASS,
    POSTFILTER_COL_DISTANCE_REASON,
    METRIC_COL_MAX_HORIZ_VEL,
    METRIC_COL_MAX_VERT_VEL,
    METRIC_COL_MAX_COORD_HORIZ_VEL,
    METRIC_COL_MAX_COORD_VERT_VEL,
    METRIC_COL_MAX_ACCEL,
    METRIC_COL_DEP_HORIZ_DIST,
    METRIC_COL_DEP_VERT_DIST,
    METRIC_COL_ARR_HORIZ_DIST,
    METRIC_COL_ARR_VERT_DIST,
    _LEGACY_VELOCITY_COLS,
    GLOBAL_CLEAN_REGISTRY,
    GLOBAL_CLEAN_QUALITY_REGISTRY,
    GLOBAL_TRAJECTORY_REGISTRY,
    GLOBAL_RAW_QUALITY_REGISTRY,
)
from src.common.registry_utils import load_clean_cohort, load_raw_cohort, join_flight_registries
from .filter_result import FilterResult
from .postfilter_worker import _worker_init, process_batch

logger = logging.getLogger(__name__)

# Maps filter name → (pass_column, reason_column) in the clean registry
FILTER_COL_MAP: dict[str, tuple[str, str]] = {
    "horiz_velocity":       (POSTFILTER_COL_HORIZ_VEL_PASS,       POSTFILTER_COL_HORIZ_VEL_REASON),
    "vert_velocity":        (POSTFILTER_COL_VERT_VEL_PASS,        POSTFILTER_COL_VERT_VEL_REASON),
    "coord_horiz_velocity": (POSTFILTER_COL_COORD_HORIZ_VEL_PASS, POSTFILTER_COL_COORD_HORIZ_VEL_REASON),
    "coord_vert_velocity":  (POSTFILTER_COL_COORD_VERT_VEL_PASS,  POSTFILTER_COL_COORD_VERT_VEL_REASON),
    "acceleration":         (POSTFILTER_COL_ACCEL_PASS,           POSTFILTER_COL_ACCEL_REASON),
    "distance":             (POSTFILTER_COL_DISTANCE_PASS,        POSTFILTER_COL_DISTANCE_REASON),
}

FILTER_METRIC_MAP: dict[str, list[str]] = {
    "horiz_velocity":       [METRIC_COL_MAX_HORIZ_VEL],
    "vert_velocity":        [METRIC_COL_MAX_VERT_VEL],
    "coord_horiz_velocity": [METRIC_COL_MAX_COORD_HORIZ_VEL],
    "coord_vert_velocity":  [METRIC_COL_MAX_COORD_VERT_VEL],
    "acceleration":         [METRIC_COL_MAX_ACCEL],
    "distance":             [
        METRIC_COL_DEP_HORIZ_DIST, METRIC_COL_DEP_VERT_DIST,
        METRIC_COL_ARR_HORIZ_DIST, METRIC_COL_ARR_VERT_DIST
    ],
}

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------



def _chunks(lst: list, n: int) -> Generator[list, None, None]:
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _load_registry(
    registry_path: Path,
    quality_registry_path: Path,
    filters_to_run: list[str]
) -> pd.DataFrame:
    """Read the base registry locator and join quality metrics, set flight_id index, add missing filter columns, and
    drop any legacy 3-D combined velocity columns left over from the old filter logic."""
    if not registry_path.exists():
        raise FileNotFoundError(f"Registry file not found: {registry_path}")
        
    df = join_flight_registries([registry_path, quality_registry_path], how="left")
    df.set_index("flight_id", drop=False, inplace=True)

    # Drop legacy combined-velocity columns if present
    legacy_present = [c for c in _LEGACY_VELOCITY_COLS if c in df.columns]
    if legacy_present:
        df.drop(columns=legacy_present, inplace=True)
        logger.info(f"Dropped {len(legacy_present)} legacy velocity column(s): {legacy_present}")

    for f in filters_to_run:
        pass_col, reason_col = FILTER_COL_MAP[f]
        if pass_col not in df.columns:
            df[pass_col] = pd.NA
        if reason_col not in df.columns:
            df[reason_col] = pd.NA
            
        for metric_col in FILTER_METRIC_MAP[f]:
            if metric_col not in df.columns:
                df[metric_col] = pd.NA
    return df


def _build_work_list(
    df: pd.DataFrame,
    filters_to_run: list[str],
    overwrite: bool,
    target_flight_ids: set[str] | None,
) -> tuple[list[FilterResult], int]:
    """Build the list of FilterResult stubs to process, applying skip logic."""
    ids_to_check = (
        df.index if target_flight_ids is None
        else [fid for fid in df.index if fid in target_flight_ids]
    )
    work_list: list[FilterResult] = []
    skipped = 0

    for fid in ids_to_check:
        row = df.loc[fid]
        if not overwrite:
            all_filled = True
            for f in filters_to_run:
                for metric_col in FILTER_METRIC_MAP[f]:
                    if pd.isna(row[metric_col]):
                        all_filled = False
                        break
                if not all_filled:
                    break
                    
            if all_filled:
                skipped += 1
                continue

        abs_path = Path(row["file_path"])
        if not abs_path.is_absolute():
            abs_path = BASE_DIR / abs_path

        work_list.append(FilterResult(flight_id=fid, file_path=str(abs_path)))

    return work_list, skipped


def _merge_results(
    df: pd.DataFrame,
    completed_batch: list[FilterResult],
    filters_to_run: list[str],
) -> None:
    """Merge a completed batch back into the in-memory DataFrame."""
    for fr in completed_batch:
        if fr.flight_id not in df.index:
            continue
        result = fr.as_dict()
        for f in filters_to_run:
            for metric_col in FILTER_METRIC_MAP[f]:
                df.loc[fr.flight_id, metric_col] = result[metric_col]


def _run_pool(
    df: pd.DataFrame,
    batches: list[list[FilterResult]],
    filters_to_run: list[str],
    tmp_path: Path,
    n_workers: int,
) -> None:
    """Submit batches to the process pool, merge results, flush snapshot, and log progress milestones."""
    import time
    ctx = mp.get_context("spawn")
    total_flights = sum(len(b) for b in batches)
    total_batches = len(batches)
    processed_flights = 0
    completed_batches = 0

    log_interval_seconds = 10.0
    last_log_time = time.time()
    last_log_pct = 0.0

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=n_workers,
        mp_context=ctx,
        initializer=_worker_init,
    ) as executor:
        futures = {executor.submit(process_batch, batch, filters_to_run): len(batch) for batch in batches}
        for future in concurrent.futures.as_completed(futures):
            batch_size = futures[future]
            completed_batch = future.result()
            _merge_results(df, completed_batch, filters_to_run)
            df.reset_index(drop=True).to_parquet(tmp_path, index=False)

            processed_flights += batch_size
            completed_batches += 1
            now = time.time()
            current_pct = (processed_flights / total_flights) * 100.0 if total_flights > 0 else 100.0

            if (now - last_log_time >= log_interval_seconds) or (current_pct - last_log_pct >= 10.0) or (completed_batches == total_batches):
                logger.info(
                    f"Progress: {processed_flights:,}/{total_flights:,} flights processed "
                    f"({current_pct:.1f}%) | Batches: {completed_batches}/{total_batches}"
                )
                last_log_time = now
                last_log_pct = current_pct


def evaluate_thresholds(
    df: pd.DataFrame, 
    filters_to_run: list[str], 
    thresholds: dict[str, float],
    target_flight_ids: set[str] | None = None
) -> None:
    """Vectorized evaluation of metric columns against thresholds to update pass/reason columns."""
    
    # Determine which rows to evaluate. If specific flights were targeted, evaluate only those.
    # Otherwise, evaluate all flights that have been processed (have non-NA in any metric column).
    if target_flight_ids is not None:
        eval_mask = df.index.isin(target_flight_ids)
    else:
        # If no specific target, evaluate rows that have at least one metric populated
        all_metric_cols = [m for sublist in FILTER_METRIC_MAP.values() for m in sublist]
        eval_mask = df[all_metric_cols].notna().any(axis=1)

    for f in filters_to_run:
        pass_col, reason_col = FILTER_COL_MAP[f]
        
        # Reset columns for the filter, BUT ONLY for the evaluated rows
        df.loc[eval_mask, pass_col] = True
        df.loc[eval_mask, reason_col] = "PASSED"
        
        if f == "horiz_velocity":
            limit = thresholds.get("max_horiz_velocity_mps", thresholds.get("max_horiz_velocity_kt", 800.0) / MPS_TO_KT)
            metric_col = METRIC_COL_MAX_HORIZ_VEL
            mask = eval_mask & (df[metric_col] > limit)
            df.loc[mask, pass_col] = False
            df.loc[mask, reason_col] = "max horiz speed > limit"
            
        elif f == "vert_velocity":
            limit = thresholds.get("max_vert_velocity_mps", thresholds.get("max_vert_velocity_fpm", 7000.0) / MPS_TO_FPM)
            metric_col = METRIC_COL_MAX_VERT_VEL
            mask = eval_mask & (df[metric_col] > limit)
            df.loc[mask, pass_col] = False
            df.loc[mask, reason_col] = "max vert speed > limit"
            
        elif f == "coord_horiz_velocity":
            limit = thresholds.get("max_coord_horiz_velocity_mps", thresholds.get("max_coord_horiz_velocity_kt", 800.0) / MPS_TO_KT)
            metric_col = METRIC_COL_MAX_COORD_HORIZ_VEL
            mask = eval_mask & (df[metric_col] > limit)
            df.loc[mask, pass_col] = False
            df.loc[mask, reason_col] = "max coord horiz speed > limit"
            
        elif f == "coord_vert_velocity":
            limit = thresholds.get("max_coord_vert_velocity_mps", thresholds.get("max_coord_vert_velocity_fpm", 7000.0) / MPS_TO_FPM)
            metric_col = METRIC_COL_MAX_COORD_VERT_VEL
            mask = eval_mask & (df[metric_col] > limit)
            df.loc[mask, pass_col] = False
            df.loc[mask, reason_col] = "max coord vert speed > limit"
            
        elif f == "acceleration":
            limit = thresholds.get("max_acceleration_mps2", 10.0)
            metric_col = METRIC_COL_MAX_ACCEL
            mask = eval_mask & (df[metric_col] > limit)
            df.loc[mask, pass_col] = False
            df.loc[mask, reason_col] = "max 3D acceleration > limit"
            
        elif f == "distance":
            # For distance we check 4 limits
            limits = {
                METRIC_COL_DEP_HORIZ_DIST: (thresholds.get("max_dep_horiz_dist"), "DEP_HORIZ"),
                METRIC_COL_DEP_VERT_DIST: (thresholds.get("max_dep_vert_dist"), "DEP_VERT"),
                METRIC_COL_ARR_HORIZ_DIST: (thresholds.get("max_arr_horiz_dist"), "ARR_HORIZ"),
                METRIC_COL_ARR_VERT_DIST: (thresholds.get("max_arr_vert_dist"), "ARR_VERT"),
            }
            
            for mcol, (lim, name) in limits.items():
                if lim is not None:
                    mask = eval_mask & (df[pass_col] == True) & (df[mcol] > lim)
                    df.loc[mask, pass_col] = False
                    df.loc[mask, reason_col] = f"{name}_DIST > limit"

        # Handle NaNs from metric extraction (e.g. empty trajectories, missing airports)
        for mcol in FILTER_METRIC_MAP[f]:
            mask_na = eval_mask & (df[pass_col] == True) & df[mcol].isna()
            df.loc[mask_na, pass_col] = False
            df.loc[mask_na, reason_col] = "METRIC_EXTRACTION_FAILED"


def _log_summary(df: pd.DataFrame, filters_to_run: list[str], target_flight_ids: set[str] | None = None) -> None:
    """Log passed / failed / missing counts per requested filter."""
    
    # Restrict summary to the target scope if specified
    if target_flight_ids is not None:
        df_target = df[df.index.isin(target_flight_ids)]
    else:
        df_target = df
        
    for f in filters_to_run:
        pass_col, _ = FILTER_COL_MAP[f]
        col = df_target[pass_col]
        passed  = int(col.eq(True).sum())
        failed  = int(col.eq(False).sum())
        missing = int(col.isna().sum())
        logger.info(f"Filter [{f}] → Passed: {passed}, Failed: {failed}, Missing/Skipped: {missing}")


# ---------------------------------------------------------------------------
# Public orchestrator entry point
# ---------------------------------------------------------------------------

def run_postfilters(
    registry_path: Path = GLOBAL_CLEAN_REGISTRY,
    filters_to_run: list[str] = None,
    thresholds: dict[str, float] = None,
    batch_size: int = POSTFILTER_BATCH_SIZE_DEFAULT,
    overwrite: bool = False,
    max_workers: int | None = None,
    target_flight_ids: set[str] | None = None,
    quality_registry_path: Path | None = None,
) -> None:
    """Orchestrate the post-filtering pipeline on clean or raw trajectory registries."""
    if filters_to_run is None:
        filters_to_run = list(FILTER_COL_MAP.keys())
    if thresholds is None:
        thresholds = {}

    if quality_registry_path is None:
        if registry_path.name == GLOBAL_TRAJECTORY_REGISTRY.name:
            quality_registry_path = GLOBAL_RAW_QUALITY_REGISTRY
        else:
            quality_registry_path = GLOBAL_CLEAN_QUALITY_REGISTRY

    logger.info(
        f"Starting post-filter run — registry: {registry_path.name}, "
        f"quality target: {quality_registry_path.name}, filters: {filters_to_run}"
    )

    df = _load_registry(registry_path, quality_registry_path, filters_to_run)
    work_list, skipped = _build_work_list(df, filters_to_run, overwrite, target_flight_ids)

    logger.info(
        f"Registry rows: {len(df)} | Target: {len(work_list) + skipped} | "
        f"To process (metrics extraction): {len(work_list)} | Skipped (metrics already exist): {skipped}"
    )
    
    tmp_path = quality_registry_path.with_suffix(".tmp.parquet")
    
    if work_list:
        batches = list(_chunks(work_list, batch_size))
        n_workers = max(1, min(max_workers or PROCESSING_DEFAULT_MAX_WORKERS, len(batches)))

        try:
            _run_pool(df, batches, filters_to_run, tmp_path, n_workers)
        except Exception as exc:
            logger.error(f"Orchestrator crashed — snapshot preserved at: {tmp_path} ({exc})")
            raise

    # 3. Evaluate thresholds and update boolean pass columns vectorized
    evaluate_thresholds(df, filters_to_run, thresholds, target_flight_ids)
    
    # 4. Save to disk (Quality metrics only!)
    # We drop file_path since it belongs in the core index, not the quality registry
    quality_cols = [c for c in df.columns if c != "file_path"]
    quality_df = df[quality_cols].reset_index(drop=True)
    if quality_registry_path.exists():
        existing = pd.read_parquet(quality_registry_path)
        merged = pd.concat([existing, quality_df]).drop_duplicates(subset=['flight_id'], keep='last')
    else:
        merged = quality_df
    
    quality_registry_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(quality_registry_path, index=False)
    tmp_path.unlink(missing_ok=True)

    _log_summary(df, filters_to_run, target_flight_ids)

