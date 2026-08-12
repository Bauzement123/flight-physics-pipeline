import logging
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
from deltalake import write_deltalake, DeltaTable

from src.data_manager.schemas import MasterFlightQuery, RouteSummaryQuery, SimResultQuery
from src.common.config import MASTER_FLIGHTS_FILE, ROUTE_SUMMARY_PARQUET

if TYPE_CHECKING:
    from src.core.processing.filter_result import FilterResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Postfilter crash-buffer helpers (Delta Lake)
# ---------------------------------------------------------------------------

# Canonical schema for the postfilter temp lake — mirrors FilterResult metric fields.
_POSTFILTER_SCHEMA = pa.schema([
    pa.field("flight_id",                      pa.string()),
    pa.field("metric_max_horiz_speed_mps",      pa.float64()),
    pa.field("metric_max_vert_speed_mps",       pa.float64()),
    pa.field("metric_max_coord_horiz_speed_mps",pa.float64()),
    pa.field("metric_max_coord_vert_speed_mps", pa.float64()),
    pa.field("metric_max_acceleration_mps2",    pa.float64()),
    pa.field("metric_dep_horiz_dist_m",         pa.float64()),
    pa.field("metric_dep_vert_dist_m",          pa.float64()),
    pa.field("metric_arr_horiz_dist_m",         pa.float64()),
    pa.field("metric_arr_vert_dist_m",          pa.float64()),
])


def append_postfilter_batch(
    lake_path: Path,
    batch: "list[FilterResult]",
) -> None:
    """Append a completed worker batch to the postfilter Delta Lake crash buffer.

    Called directly from each worker process after processing a batch. Delta Lake
    uses optimistic concurrency over an atomic JSON transaction log — no fcntl file
    locks are involved, so concurrent appends from multiple worker processes are
    safe without any coordination.

    Deduplication by flight_id is deferred to merge time (merge_postfilter_lake).
    If a worker retries a batch, the duplicate rows are harmless and the last
    append wins during the merge.

    Parameters
    ----------
    lake_path:
        Path to the Delta Lake directory for this run's crash buffer
        (e.g. data/temp/postfilter_tmp/global_raw_quality_registry/).
    batch:
        Completed FilterResult objects with metric fields populated.
    """
    if not batch:
        return

    rows = [fr.as_dict() for fr in batch]
    # Drop file_path — not part of the quality schema
    for r in rows:
        r.pop("file_path", None)

    table = pa.Table.from_pylist(rows, schema=_POSTFILTER_SCHEMA)
    write_deltalake(
        str(lake_path),
        table,
        mode="append",
        schema_mode="merge",
    )


def merge_postfilter_lake(
    lake_path: Path,
    df: pd.DataFrame,
    metric_cols: list[str],
) -> int:
    """Read the postfilter Delta Lake crash buffer and merge results into df.

    Used by the merge_only recovery path. Reads all records from the lake,
    deduplicates by flight_id (keep='last' so the most recent append wins),
    then calls df.update() which is index-aligned and skips NaN values so
    existing metrics in df are only overwritten where the lake has a real value.

    Parameters
    ----------
    lake_path:
        Path to the Delta Lake directory.
    df:
        In-memory registry DataFrame indexed on flight_id.
    metric_cols:
        Metric column names to merge (subset of _POSTFILTER_SCHEMA fields).

    Returns
    -------
    int
        Number of flight_ids from the lake that were found in df's index.
    """
    if not lake_path.exists():
        logger.info("merge_postfilter_lake: no lake at %s — nothing to merge.", lake_path)
        return 0

    try:
        dt = DeltaTable(str(lake_path))
    except Exception as exc:
        logger.warning("merge_postfilter_lake: could not open lake at %s: %s", lake_path, exc)
        return 0

    lake_df = dt.to_pandas()
    if lake_df.empty:
        logger.info("merge_postfilter_lake: lake at %s is empty — nothing to merge.", lake_path)
        return 0

    # Dedup: keep last append per flight_id (most recent worker result wins)
    lake_df = lake_df.drop_duplicates(subset="flight_id", keep="last")
    lake_df = lake_df.set_index("flight_id")

    cols_present = [c for c in metric_cols if c in lake_df.columns]
    df.update(lake_df[cols_present])  # index-aligned, skips NaN

    matched = int(df.index.isin(lake_df.index).sum())
    logger.info(
        "merge_postfilter_lake: merged %d flight record(s) from %s.",
        matched, lake_path,
    )
    return matched

def read_master_flights(
    query: Optional[MasterFlightQuery] = None,
    columns: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Reads master_flights via PyArrow dataset with predicate pushdown.

    Column names match the actual master_flights schema:
    - date filtering uses ``firstseen`` (epoch seconds).
    - airport filtering uses ``estdepartureairport`` / ``estarrivalairport``.
    - route filtering expects a pre-built ``"{dep}-{arr}"`` string column if present,
      otherwise falls back to individual airport filters.

    Returns a pandas DataFrame. Raises FileNotFoundError if MASTER_FLIGHTS_FILE
    does not exist.
    """
    if not Path(MASTER_FLIGHTS_FILE).exists():
        raise FileNotFoundError(f"master_flights file not found: {MASTER_FLIGHTS_FILE}")

    dataset = ds.dataset(str(MASTER_FLIGHTS_FILE))
    exprs = []

    if query is not None:
        if query.dep_date_start is not None:
            exprs.append(ds.field("firstseen") >= query.dep_date_start)
        if query.dep_date_end is not None:
            exprs.append(ds.field("firstseen") <= query.dep_date_end)
        if query.typecodes:
            exprs.append(ds.field("typecode").isin(query.typecodes))
        if query.icao24s:
            exprs.append(ds.field("icao24").isin(query.icao24s))
        if query.callsigns:
            exprs.append(ds.field("callsign").isin(query.callsigns))
        if query.dep_airports:
            exprs.append(ds.field("estdepartureairport").isin(query.dep_airports))
        if query.arr_airports:
            exprs.append(ds.field("estarrivalairport").isin(query.arr_airports))

    combined = exprs[0] if exprs else None
    for e in exprs[1:]:
        combined = combined & e

    return dataset.to_table(filter=combined, columns=columns).to_pandas()

def read_route_summary(
    query: Optional[RouteSummaryQuery] = None,
    columns: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Reads route_summary via PyArrow dataset with predicate pushdown.

    Raises FileNotFoundError if ROUTE_SUMMARY_PARQUET does not exist.
    """
    if not Path(ROUTE_SUMMARY_PARQUET).exists():
        raise FileNotFoundError(f"route_summary file not found: {ROUTE_SUMMARY_PARQUET}")

    dataset = ds.dataset(str(ROUTE_SUMMARY_PARQUET))
    exprs = []

    if query is not None:
        if query.routes:
            exprs.append(ds.field("route").isin(query.routes))
        if query.dep_airports:
            exprs.append(ds.field("dep").isin(query.dep_airports))
        if query.arr_airports:
            exprs.append(ds.field("arr").isin(query.arr_airports))

    combined = exprs[0] if exprs else None
    for e in exprs[1:]:
        combined = combined & e

    return dataset.to_table(filter=combined, columns=columns).to_pandas()

def read_sim_lake(lake_path: str | Path, query: SimResultQuery) -> pd.DataFrame:
    """Reads the simulation Delta Lake based on the provided query."""
    path_str = str(lake_path)
    if not Path(path_str).exists():
        raise FileNotFoundError(f"Delta lake not found at {path_str}")
        
    try:
        dt = DeltaTable(path_str)
    except Exception as e:
        raise FileNotFoundError(f"Delta lake not found at {path_str}: {e}")
        
    filters = []
    if query.sim_fids:
        filters.append(ds.field("SIM_FID").isin(query.sim_fids))
    if query.routes:
        filters.append(ds.field("route").isin(query.routes))
    if query.ef_gt is not None:
        filters.append(ds.field("EF") > query.ef_gt)
    if query.fl_lte is not None:
        filters.append(ds.field("FL") <= query.fl_lte)
    if query.model_config_id is not None:
        filters.append(ds.field("model_config_id") == query.model_config_id)
        
    pyarrow_ds = dt.to_pyarrow_dataset()
    
    if not filters:
        df = pyarrow_ds.to_table().to_pandas()
    else:
        combined_filter = filters[0]
        for f in filters[1:]:
            combined_filter = combined_filter & f
        df = pyarrow_ds.to_table(filter=combined_filter).to_pandas()
        
    return df

def append_sim_lake(lake_path: str | Path, df: pd.DataFrame, overwrite: bool = False) -> None:
    """Upsert (or overwrite) a DataFrame into the simulation Delta Lake on (SIM_FID, model_config_id).

    Normal mode (overwrite=False)
    ------------------------------
    MERGE on (SIM_FID, model_config_id):
    - matched rows are updated in-place
    - unmatched rows are inserted
    Guarantees no duplicates for clean lakes.

    Overwrite mode (overwrite=True)
    ---------------------------------
    DELETE all existing rows whose SIM_FID is in the incoming batch, then
    INSERT the fresh rows via append.  This handles dirty lakes that already
    contain duplicate SIM_FIDs from a previous failed run.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        raise TypeError("df must be a non-empty pandas DataFrame.")

    path_str = str(lake_path)

    if not Path(path_str).exists():
        write_deltalake(path_str, df, mode="overwrite")
        logger.info("Created Delta Lake with %d row(s) at %s", len(df), lake_path)
        return

    try:
        dt = DeltaTable(path_str)
    except Exception:
        write_deltalake(path_str, df, mode="overwrite")
        logger.info("Re-created Delta Lake with %d row(s) at %s", len(df), lake_path)
        return

    if overwrite:
        # Build an IN-list predicate and delete all matching rows first
        fids = df["SIM_FID"].tolist()
        quoted = ", ".join(f"'{f}'" for f in fids)
        dt.delete(f"SIM_FID IN ({quoted})")
        write_deltalake(path_str, df, mode="append")
        logger.info("Overwrote %d row(s) in Delta Lake at %s", len(df), lake_path)
    else:
        (
            dt.merge(
                source=df,
                predicate=(
                    "target.SIM_FID = source.SIM_FID "
                    "AND target.model_config_id = source.model_config_id"
                ),
                source_alias="source",
                target_alias="target",
            )
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute()
        )
        logger.info("Upserted %d row(s) into Delta Lake at %s", len(df), lake_path)

def sim_fid_exists(lake_path: str | Path, sim_fid: str) -> bool:
    """Checks if a SIM_FID exists in the Delta Lake without loading all columns."""
    path_str = str(lake_path)
    if not Path(path_str).exists():
        return False
        
    try:
        dt = DeltaTable(path_str)
    except Exception:
        return False
        
    pyarrow_ds = dt.to_pyarrow_dataset()
    filter_expr = ds.field("SIM_FID") == sim_fid
    
    table = pyarrow_ds.to_table(columns=["SIM_FID"], filter=filter_expr)
    return len(table) > 0


def vacuum_sim_lake(
    lake_path: Path,
    retention_hours: int = 168,
) -> None:
    """Run Delta Lake VACUUM to remove orphaned data files beyond the retention window.

    Should be called once per day after all batches for that day complete,
    before loading the next day's weather. Without periodic vacuuming, old
    intermediate parquet files accumulate indefinitely even when no longer
    referenced by the transaction log.

    Parameters
    ----------
    lake_path : Path
        Path to the simulation Delta Lake directory.
    retention_hours : int, optional
        Files older than this many hours are eligible for deletion.
        Default is 168 (7 days) — the Delta Lake standard retention window.

    Notes
    -----
    If the lake does not yet exist this function is a no-op (logs INFO and returns).
    Any vacuum error is logged at WARNING level and swallowed so that the daily
    cleanup step never aborts the overall run.
    """
    path_str = str(lake_path)
    if not Path(path_str).exists():
        logger.info("vacuum_sim_lake: lake not yet created at %s — skipping.", lake_path)
        return

    try:
        dt = DeltaTable(path_str)
        deleted = dt.vacuum(retention_hours=retention_hours, dry_run=False, enforce_retention_duration=False)
        logger.info(
            "vacuum_sim_lake: removed %d stale file(s) from %s (retention=%dh).",
            len(deleted), lake_path, retention_hours,
        )
    except Exception as exc:
        logger.warning("vacuum_sim_lake failed for %s: %s — continuing.", lake_path, exc)
