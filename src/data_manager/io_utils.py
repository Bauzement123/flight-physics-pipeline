import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd
import pyarrow.dataset as ds
from deltalake import write_deltalake, DeltaTable

from src.data_manager.schemas import MasterFlightQuery, RouteSummaryQuery, SimResultQuery
from src.common.config import MASTER_FLIGHTS_FILE, ROUTE_SUMMARY_PARQUET

logger = logging.getLogger(__name__)

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
