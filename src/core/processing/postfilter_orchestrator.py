from __future__ import annotations
import concurrent.futures
import logging
import multiprocessing as mp
from pathlib import Path
import sqlite3
from typing import Generator

import pandas as pd

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
from src.common.registry_utils import join_flight_registries
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


def _init_sqlite_db(db_path: Path) -> None:
    """Initialize SQLite database table and WAL mode for fast crash-safe upserts."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # On Linux, stale .db-wal / .db-shm sidecar files left by a prior crashed run
    # put the database into WAL recovery state. PRAGMA journal_mode=WAL; requires an
    # exclusive lock to execute, which Linux POSIX fcntl locking denies while stale
    # sidecars exist. Deleting them resets the WAL lock state. The committed data
    # in the .db file itself is unaffected; any uncommitted data from the crash is
    # already irrecoverable and is recovered via merge_only mode instead.
    for sidecar in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        sidecar.unlink(missing_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                flight_id TEXT PRIMARY KEY,
                metric_max_horiz_speed_mps REAL,
                metric_max_vert_speed_mps REAL,
                metric_max_coord_horiz_speed_mps REAL,
                metric_max_coord_vert_speed_mps REAL,
                metric_max_acceleration_mps2 REAL,
                metric_dep_horiz_dist_m REAL,
                metric_dep_vert_dist_m REAL,
                metric_arr_horiz_dist_m REAL,
                metric_arr_vert_dist_m REAL
            );
        """)


def _upsert_batch_sqlite(db_path: Path, completed_batch: list[FilterResult]) -> None:
    """Upsert a completed batch into SQLite in a single transaction (O(batch_size))."""
    rows = []
    for fr in completed_batch:
        d = fr.as_dict()
        rows.append((
            d.get("flight_id"),
            d.get("metric_max_horiz_speed_mps"),
            d.get("metric_max_vert_speed_mps"),
            d.get("metric_max_coord_horiz_speed_mps"),
            d.get("metric_max_coord_vert_speed_mps"),
            d.get("metric_max_acceleration_mps2"),
            d.get("metric_dep_horiz_dist_m"),
            d.get("metric_dep_vert_dist_m"),
            d.get("metric_arr_horiz_dist_m"),
            d.get("metric_arr_vert_dist_m"),
        ))
    sql = """
        INSERT OR REPLACE INTO results (
            flight_id,
            metric_max_horiz_speed_mps,
            metric_max_vert_speed_mps,
            metric_max_coord_horiz_speed_mps,
            metric_max_coord_vert_speed_mps,
            metric_max_acceleration_mps2,
            metric_dep_horiz_dist_m,
            metric_dep_vert_dist_m,
            metric_arr_horiz_dist_m,
            metric_arr_vert_dist_m
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """
    with sqlite3.connect(db_path) as conn:
        conn.executemany(sql, rows)


def _load_registry(
    registry_path: Path,
    quality_registry_path: Path,
    filters_to_run: list[str]
) -> pd.DataFrame:
    """Read the base registry locator and join quality metrics, set flight_id index, add missing filter columns."""
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
    recheck_flags: bool = False,
) -> tuple[list[FilterResult], int]:
    """Build the list of FilterResult stubs to process, applying skip logic."""
    if recheck_flags:
        return [], 0

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
    db_path: Path,
    n_workers: int,
) -> None:
    """Submit batches to the process pool, upsert results to SQLite db_path, and log progress milestones."""
    import time
    _init_sqlite_db(db_path)
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
            _upsert_batch_sqlite(db_path, completed_batch)

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


def _merge_sqlite(
    df: pd.DataFrame,
    db_path: Path,
    filters_to_run: list[str],
) -> int:
    """Read all records from SQLite db_path and merge them back into in-memory DataFrame.

    Uses df.update() for vectorized index-aligned merging — only overwrites cells
    where the SQLite row has a non-NA value, leaving existing metrics intact.
    """
    if not db_path.exists():
        return 0

    logger.info(f"Merging SQLite database from {db_path}...")
    with sqlite3.connect(db_path) as conn:
        dumped_df = pd.read_sql_query("SELECT * FROM results", conn)

    if dumped_df.empty:
        return 0

    dumped_df.set_index("flight_id", inplace=True)

    # Only update the metric columns relevant to the requested filters
    cols_to_update = [mc for f in filters_to_run for mc in FILTER_METRIC_MAP[f]]
    cols_present = [c for c in cols_to_update if c in dumped_df.columns]
    df.update(dumped_df[cols_present])  # Vectorized; skips NaN; aligns on index

    return int(df.index.isin(dumped_df.index).sum())


def evaluate_thresholds(
    df: pd.DataFrame, 
    filters_to_run: list[str], 
    thresholds: dict[str, float],
    target_flight_ids: set[str] | None = None
) -> None:
    """Vectorized evaluation of metric columns against thresholds to update pass/reason columns."""
    if target_flight_ids is not None:
        eval_mask = df.index.isin(target_flight_ids)
    else:
        all_metric_cols = [m for sublist in FILTER_METRIC_MAP.values() for m in sublist]
        eval_mask = df[all_metric_cols].notna().any(axis=1)

    for f in filters_to_run:
        pass_col, reason_col = FILTER_COL_MAP[f]
        
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

        for mcol in FILTER_METRIC_MAP[f]:
            mask_na = eval_mask & (df[pass_col] == True) & df[mcol].isna()
            df.loc[mask_na, pass_col] = False
            df.loc[mask_na, reason_col] = "METRIC_EXTRACTION_FAILED"


def _log_summary(df: pd.DataFrame, filters_to_run: list[str], target_flight_ids: set[str] | None = None) -> None:
    """Log passed / failed / missing counts per requested filter."""
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
    recheck_flags: bool = False,
    max_workers: int | None = None,
    target_flight_ids: set[str] | None = None,
    quality_registry_path: Path | None = None,
    merge_only: bool = False,
) -> None:
    """Orchestrate the post-filtering pipeline on clean or raw trajectory registries."""
    if overwrite and recheck_flags:
        raise ValueError("--overwrite and --recheck-flags are mutually exclusive.")

    if filters_to_run is None:
        filters_to_run = list(FILTER_COL_MAP.keys())
    if thresholds is None:
        thresholds = {}

    if quality_registry_path is None:
        if registry_path.name == GLOBAL_TRAJECTORY_REGISTRY.name:
            quality_registry_path = GLOBAL_RAW_QUALITY_REGISTRY
        else:
            quality_registry_path = GLOBAL_CLEAN_QUALITY_REGISTRY

    db_path = BASE_DIR / "data" / "temp" / "postfilter_tmp" / f"{quality_registry_path.stem}.db"

    work_list: list[FilterResult] = []

    if merge_only:
        logger.info(f"Running MERGE-ONLY mode — scanning SQLite database: {db_path}")
        df = _load_registry(registry_path, quality_registry_path, filters_to_run)
        merged_count = _merge_sqlite(df, db_path, filters_to_run)
        logger.info(f"Merged {merged_count} flight record(s) from SQLite database.")
    elif recheck_flags:
        logger.info(
            f"Running RECHECK-FLAGS mode — registry: {registry_path.name}, "
            f"quality target: {quality_registry_path.name}, filters: {filters_to_run}"
        )
        df = _load_registry(registry_path, quality_registry_path, filters_to_run)
    else:
        logger.info(
            f"Starting post-filter run — registry: {registry_path.name}, "
            f"quality target: {quality_registry_path.name}, filters: {filters_to_run}"
        )

        df = _load_registry(registry_path, quality_registry_path, filters_to_run)
        work_list, skipped = _build_work_list(df, filters_to_run, overwrite, target_flight_ids, recheck_flags)

        logger.info(
            f"Registry rows: {len(df)} | Target: {len(work_list) + skipped} | "
            f"To process (metrics extraction): {len(work_list)} | Skipped (metrics already exist): {skipped}"
        )
        
        if work_list:
            batches = list(_chunks(work_list, batch_size))
            n_workers = max(1, min(max_workers or PROCESSING_DEFAULT_MAX_WORKERS, len(batches)))

            try:
                _run_pool(df, batches, filters_to_run, db_path, n_workers)
            except Exception as exc:
                logger.error(f"Orchestrator crashed — SQLite database preserved at: {db_path} ({exc})")
                raise
        else:
            # No work needed — all metrics already present. Merge any partial
            # DB left behind by a prior crashed run, if one exists.
            if db_path.exists():
                _merge_sqlite(df, db_path, filters_to_run)

    # 3. Evaluate thresholds and update boolean pass columns vectorized
    evaluate_thresholds(df, filters_to_run, thresholds, target_flight_ids)
    
    # 4. Save to disk (Quality metrics delta upsert)
    quality_cols = [c for c in df.columns if c != "file_path"]

    if recheck_flags or merge_only:
        if target_flight_ids is not None:
            save_ids = target_flight_ids
        else:
            all_metric_cols = [m for sublist in FILTER_METRIC_MAP.values() for m in sublist]
            save_ids = set(df.index[df[all_metric_cols].notna().any(axis=1)].tolist())
    else:
        save_ids = {fr.flight_id for fr in work_list}

    if save_ids:
        delta_df = df.loc[df.index.isin(save_ids), quality_cols].reset_index(drop=True)
        if quality_registry_path.exists():
            existing = pd.read_parquet(quality_registry_path)
            merged = pd.concat([existing, delta_df]).drop_duplicates(subset=['flight_id'], keep='last')
        else:
            merged = delta_df
        
        quality_registry_path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(quality_registry_path, index=False)
        logger.info(
            f"Successfully updated quality manifest ({len(delta_df):,} delta rows upserted, "
            f"total manifest: {len(merged):,} rows) → {quality_registry_path.name}"
        )
    else:
        logger.info("No rows modified; quality manifest left unchanged.")

    # Clean up SQLite temporary file on successful completion
    if db_path.exists():
        for p in (db_path, db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
            try:
                p.unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"Could not remove temporary file {p}: {e}")

    _log_summary(df, filters_to_run, target_flight_ids)


