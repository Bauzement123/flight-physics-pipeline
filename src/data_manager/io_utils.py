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
    - exact route filtering uses OR-chained (dep == X & arr == Y) expressions.

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
        if query.routes:
            pair_exprs = []
            for r in query.routes:
                if "-" in r:
                    dep, arr = r.split("-", 1)
                    pair_exprs.append(
                        (ds.field("estdepartureairport") == dep)
                        & (ds.field("estarrivalairport") == arr)
                    )
            if pair_exprs:
                route_filter = pair_exprs[0]
                for p_expr in pair_exprs[1:]:
                    route_filter = route_filter | p_expr
                exprs.append(route_filter)

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


_STR_METADATA_COLS = [
    "SIM_FID", "model_config_id", "fuel", "route", "icao24", "callsign", "typecode"
]


def read_sim_lake(lake_path: str | Path, query: SimResultQuery) -> pd.DataFrame:
    """Reads the simulation Delta Lake based on the provided query.

    Returns full per-waypoint trajectory records. Callers requiring flight-level
    metadata only may drop duplicates on ``SIM_FID``.
    """
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
        filters.append(ds.field("EF_total") > query.ef_gt)
    if query.fl_lte is not None:
        filters.append(ds.field("FL") <= query.fl_lte)
    if query.model_config_id is not None:
        filters.append(ds.field("model_config_id") == query.model_config_id)
    if query.fuel is not None:
        filters.append(ds.field("fuel") == query.fuel)

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
    """Upsert (or overwrite) a trajectory DataFrame into the simulation Delta Lake.

    Normal mode (overwrite=False)
    ------------------------------
    - Deduplicates incoming data on ``(SIM_FID, time)`` with ``keep='last'``.
    - If incoming DataFrame introduces new physics columns not yet in the Delta schema,
      falls back to delete-then-append with ``schema_mode='merge'`` to allow schema evolution.
    - Otherwise, performs atomic MERGE on ``(SIM_FID, model_config_id, time)``.

    Overwrite mode (overwrite=True)
    ---------------------------------
    - DELETE all existing rows matching incoming ``SIM_FID``s, then INSERT fresh rows
      via append with ``schema_mode='merge'`` to guarantee clean trajectory replacement
      without leaving orphaned waypoints.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        raise TypeError("df must be a non-empty pandas DataFrame.")

    # 1. Pre-deduplicate on (SIM_FID, time) with keep='last'
    df = df.drop_duplicates(subset=["SIM_FID", "time"], keep="last")

    # 2. Convert any timedelta64 columns to float seconds (Delta Lake does not support Duration type)
    for col in df.select_dtypes(include=["timedelta64", "timedelta"]).columns:
        df[col] = df[col].dt.total_seconds()

    # 3. Strict typecasting for known metadata string columns to avoid string_view issues
    for col in _STR_METADATA_COLS:
        if col in df.columns:
            df[col] = df[col].astype(str)

    path_str = str(lake_path)

    if not Path(path_str).exists():
        write_deltalake(path_str, df, mode="overwrite", schema_mode="merge")
        logger.info("Created Delta Lake with %d waypoint row(s) at %s", len(df), lake_path)
        return

    try:
        dt = DeltaTable(path_str)
    except Exception:
        write_deltalake(path_str, df, mode="overwrite", schema_mode="merge")
        logger.info("Re-created Delta Lake with %d waypoint row(s) at %s", len(df), lake_path)
        return

    existing_cols = set(dt.schema().to_arrow().names)
    has_new_cols = bool(set(df.columns) - existing_cols)

    if overwrite or has_new_cols:
        fids = df["SIM_FID"].unique().tolist()
        quoted = ", ".join(f"'{f}'" for f in fids)
        dt.delete(f"SIM_FID IN ({quoted})")
        write_deltalake(path_str, df, mode="append", schema_mode="merge")
        logger.info(
            "%s %d waypoint row(s) in Delta Lake at %s (schema evolution=%s)",
            "Overwrote" if overwrite else "Appended", len(df), lake_path, has_new_cols,
        )
    else:
        (
            dt.merge(
                source=df,
                predicate=(
                    "target.SIM_FID = source.SIM_FID "
                    "AND target.model_config_id = source.model_config_id "
                    "AND target.time = source.time"
                ),
                source_alias="source",
                target_alias="target",
            )
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute()
        )
        logger.info("Upserted %d waypoint row(s) into Delta Lake at %s", len(df), lake_path)


def read_existing_sim_fids(
    lake_path: str | Path,
    routes: Optional[List[str]] = None,
    dep_date: Optional[int] = None,
) -> frozenset[str]:
    """Bulk-read existing SIM_FIDs from the Delta Lake for the given routes/dep_date.

    Scans the lake using predicate pushdown and returns a frozenset of unique
    ``SIM_FID`` strings. Used by Slot 2 to filter out already simulated tasks
    in O(1) time before batch formation.
    """
    path_str = str(lake_path)
    if not Path(path_str).exists():
        return frozenset()

    try:
        dt = DeltaTable(path_str)
    except Exception:
        return frozenset()

    filters = []
    if routes:
        filters.append(ds.field("route").isin(routes))
    if dep_date is not None:
        filters.append(ds.field("dep_date") == dep_date)

    combined = None
    if filters:
        combined = filters[0]
        for f in filters[1:]:
            combined = combined & f

    pyarrow_ds = dt.to_pyarrow_dataset()
    try:
        if combined is not None:
            table = pyarrow_ds.to_table(columns=["SIM_FID"], filter=combined)
        else:
            table = pyarrow_ds.to_table(columns=["SIM_FID"])
    except Exception as exc:
        logger.warning("read_existing_sim_fids failed scan for %s: %s", lake_path, exc)
        return frozenset()

    df = table.to_pandas()
    if df.empty:
        return frozenset()
    return frozenset(df["SIM_FID"].unique())


def vacuum_sim_lake(
    lake_path: Path,
    retention_hours: int = 168,
) -> None:
    """Run Delta Lake VACUUM to remove orphaned data files beyond the retention window."""
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


def optimize_sim_lake(
    lake_path: Path,
    z_order_cols: Optional[List[str]] = None,
) -> None:
    """Run Delta Lake compaction and Z-ordering on the simulation lake.

    Parameters
    ----------
    lake_path : Path
        Path to the simulation Delta Lake directory.
    z_order_cols : list[str], optional
        Columns to Z-order on. Default is ``["dep_date", "route", "EF_total"]``.
    """
    path_str = str(lake_path)
    if not Path(path_str).exists():
        return

    if z_order_cols is None:
        z_order_cols = ["dep_date", "route", "EF_total"]

    try:
        dt = DeltaTable(path_str)
        # Step 1: Compact small per-batch parquet files into larger chunks
        dt.optimize.compact()
        # Step 2: Z-order data on key query columns for fast skipping
        dt.optimize.z_order(z_order_cols)
        logger.info("optimize_sim_lake: compacted and Z-ordered %s on %s", lake_path, z_order_cols)
    except Exception as exc:
        logger.warning("optimize_sim_lake failed for %s: %s — continuing.", lake_path, exc)


def read_ef_by_base_key(
    lake_path: Path,
    base_keys: "set[str]",
    model_config_id: Optional[str] = None,
    routes: Optional[List[str]] = None,
    dep_dates: Optional[List[int]] = None,
) -> "dict[str, list[tuple[float, float]]]":
    """Batch-read EF_total and FL for all SIM_FIDs matching the given base keys.

    Parameters
    ----------
    lake_path : Path
        Root directory of the simulation Delta Lake.
    base_keys : set[str]
        Set of base key strings to query.
    model_config_id : str, optional
        If provided, filter to rows with this model_config_id.
    routes : list[str], optional
        Route strings to push down for Z-order scanning.
    dep_dates : list[int], optional
        Departure date integers (YYYYMMDD) to push down for Z-order scanning.

    Returns
    -------
    dict[str, list[tuple[float, float]]]
        Mapping of ``base_key → [(fl, ef_total), ...]``.
    """
    path_str = str(lake_path)
    if not Path(path_str).exists():
        return {}

    try:
        dt = DeltaTable(path_str)
    except Exception:
        return {}

    pyarrow_ds = dt.to_pyarrow_dataset()

    filters = []
    if model_config_id is not None:
        filters.append(ds.field("model_config_id") == model_config_id)
    if routes:
        filters.append(ds.field("route").isin(routes))
    if dep_dates:
        filters.append(ds.field("dep_date").isin(dep_dates))

    combined = None
    if filters:
        combined = filters[0]
        for f in filters[1:]:
            combined = combined & f

    try:
        if combined is not None:
            table = pyarrow_ds.to_table(columns=["SIM_FID", "FL", "EF_total"], filter=combined)
        else:
            table = pyarrow_ds.to_table(columns=["SIM_FID", "FL", "EF_total"])
    except Exception as exc:
        logger.warning("read_ef_by_base_key failed to scan %s: %s", lake_path, exc)
        return {}

    df = table.to_pandas()
    if df.empty:
        return {}

    # Deduplicate by SIM_FID to obtain 1 row per flight simulation
    df = df.drop_duplicates(subset=["SIM_FID"])

    # Derive base key from SIM_FID by stripping the last "_<FL>" segment
    df["base_key"] = df["SIM_FID"].str.rsplit("_", n=1).str[0]

    # Keep only rows whose base_key is in the requested set
    df = df[df["base_key"].isin(base_keys)]

    result: dict[str, list[tuple[float, float]]] = {}
    for row in df.itertuples(index=False):
        key = row.base_key
        result.setdefault(key, []).append((float(row.FL), float(row.EF_total)))

    return result

