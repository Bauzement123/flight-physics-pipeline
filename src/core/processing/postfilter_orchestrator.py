from __future__ import annotations
import concurrent.futures
import logging
import multiprocessing as mp
from pathlib import Path
from typing import Generator

import pandas as pd

from src.common.config import (
    BASE_DIR,
    POSTFILTER_BATCH_SIZE_DEFAULT,
    PROCESSING_DEFAULT_MAX_WORKERS,
    POSTFILTER_COL_VELOCITY_PASS,
    POSTFILTER_COL_VELOCITY_REASON,
    POSTFILTER_COL_COORD_VEL_PASS,
    POSTFILTER_COL_COORD_VEL_REASON,
    POSTFILTER_COL_ACCEL_PASS,
    POSTFILTER_COL_ACCEL_REASON,
    POSTFILTER_COL_DISTANCE_PASS,
    POSTFILTER_COL_DISTANCE_REASON,
)
from .filter_result import FilterResult
from .postfilter_worker import _worker_init, process_batch

logger = logging.getLogger(__name__)

# Maps filter name → (pass_column, reason_column) in the clean registry
FILTER_COL_MAP: dict[str, tuple[str, str]] = {
    "velocity":            (POSTFILTER_COL_VELOCITY_PASS,  POSTFILTER_COL_VELOCITY_REASON),
    "coordinate_velocity": (POSTFILTER_COL_COORD_VEL_PASS, POSTFILTER_COL_COORD_VEL_REASON),
    "acceleration":        (POSTFILTER_COL_ACCEL_PASS,     POSTFILTER_COL_ACCEL_REASON),
    "distance":            (POSTFILTER_COL_DISTANCE_PASS,  POSTFILTER_COL_DISTANCE_REASON),
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _chunks(lst: list, n: int) -> Generator[list, None, None]:
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _load_registry(registry_path: Path, filters_to_run: list[str]) -> pd.DataFrame:
    """Read the clean registry, set flight_id index, and add missing filter columns."""
    if not registry_path.exists():
        raise FileNotFoundError(f"Registry file not found: {registry_path}")
    df = pd.read_parquet(registry_path)
    df.set_index("flight_id", drop=False, inplace=True)
    for f in filters_to_run:
        pass_col, reason_col = FILTER_COL_MAP[f]
        if pass_col not in df.columns:
            df[pass_col] = pd.NA
        if reason_col not in df.columns:
            df[reason_col] = pd.NA
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
            all_filled = all(
                not (pd.isna(row[FILTER_COL_MAP[f][0]]))
                for f in filters_to_run
            )
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
            pass_col, reason_col = FILTER_COL_MAP[f]
            df.loc[fr.flight_id, pass_col] = result[pass_col]
            df.loc[fr.flight_id, reason_col] = result[reason_col]


def _run_pool(
    df: pd.DataFrame,
    batches: list[list[FilterResult]],
    filters_to_run: list[str],
    thresholds: dict[str, float],
    tmp_path: Path,
    n_workers: int,
) -> None:
    """Submit batches to the process pool, merge results, and flush after each batch."""
    ctx = mp.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=n_workers,
        mp_context=ctx,
        initializer=_worker_init,
        initargs=(thresholds,),
    ) as executor:
        futures = [executor.submit(process_batch, batch, filters_to_run) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            _merge_results(df, future.result(), filters_to_run)
            df.reset_index(drop=True).to_parquet(tmp_path, index=False)


def _log_summary(df: pd.DataFrame, filters_to_run: list[str]) -> None:
    """Log passed / failed / missing counts per requested filter."""
    for f in filters_to_run:
        pass_col, _ = FILTER_COL_MAP[f]
        col = df[pass_col]
        passed  = int(col.eq(True).sum())
        failed  = int(col.eq(False).sum())
        missing = int(col.isna().sum())
        logger.info(f"Filter [{f}] → Passed: {passed}, Failed: {failed}, Missing/Skipped: {missing}")


# ---------------------------------------------------------------------------
# Public orchestrator entry point
# ---------------------------------------------------------------------------

def run_postfilters(
    registry_path: Path,
    filters_to_run: list[str],
    thresholds: dict[str, float],
    batch_size: int = POSTFILTER_BATCH_SIZE_DEFAULT,
    overwrite: bool = False,
    max_workers: int | None = None,
    target_flight_ids: set[str] | None = None,
) -> None:
    """Orchestrate the post-filtering pipeline on the clean registry."""
    logger.info(f"Starting post-filter run — filters: {filters_to_run}")

    df = _load_registry(registry_path, filters_to_run)
    work_list, skipped = _build_work_list(df, filters_to_run, overwrite, target_flight_ids)

    logger.info(
        f"Registry rows: {len(df)} | Target: {len(work_list) + skipped} | "
        f"To process: {len(work_list)} | Skipped: {skipped}"
    )
    if not work_list:
        logger.info("No flights require processing. Exiting.")
        return

    batches = list(_chunks(work_list, batch_size))
    tmp_path = registry_path.with_suffix(".tmp.parquet")
    n_workers = max(1, min(max_workers or PROCESSING_DEFAULT_MAX_WORKERS, len(batches)))

    try:
        _run_pool(df, batches, filters_to_run, thresholds, tmp_path, n_workers)
        df.reset_index(drop=True).to_parquet(registry_path, index=False)
        tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.error(f"Orchestrator crashed — snapshot preserved at: {tmp_path} ({exc})")
        raise

    _log_summary(df, filters_to_run)
