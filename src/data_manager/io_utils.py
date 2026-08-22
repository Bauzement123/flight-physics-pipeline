import functools
import logging
import operator
from pathlib import Path
from typing import Collection, Dict, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
from deltalake import write_deltalake, DeltaTable

from src.data_manager.schemas import (
    MasterFlightQuery,
    RouteSummaryQuery,
    SimResultQuery,
    CorridorCluster,
    SIM_LAKE_FIXED_COLUMNS,
    SIM_LAKE_STR_COLUMNS,
    SimTask,
)
from src.common.config import (
    BASE_DIR,
    MASTER_FLIGHTS_FILE,
    ROUTE_SUMMARY_PARQUET,
    GLOBAL_CORRIDOR_MODEL_REGISTRY,
    GLOBAL_TRAJECTORY_REGISTRY,
    GLOBAL_CLEAN_REGISTRY,
    DELTA_LAKE_TARGET_FILE_SIZE_BYTES,
)

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


def _build_master_flights_filter(
    query: Optional[MasterFlightQuery] = None,
    schema_names: Optional[List[str]] = None,
    ignore_routes: bool = False,
) -> Optional[ds.Expression]:
    """Build PyArrow expression filter from MasterFlightQuery."""
    if query is None:
        return None

    exprs = []
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

    if query.routes and not ignore_routes:
        normalized_routes = [r.replace(" -> ", "-") for r in query.routes]
        if schema_names is not None and "route" in schema_names:
            # Fast path: native single-column C++ Parquet predicate pushdown
            exprs.append(ds.field("route").cast(pa.string()).isin(normalized_routes))

    if not exprs:
        return None
    return functools.reduce(operator.and_, exprs)


def read_master_flights(
    query: Optional[MasterFlightQuery] = None,
    columns: Optional[List[str]] = None,
    dataset: Optional[ds.Dataset] = None,
) -> pd.DataFrame:
    """Reads master_flights via PyArrow dataset with predicate pushdown.

    Column names match the actual master_flights schema:
    - date filtering uses ``firstseen`` (epoch seconds / timestamp).
    - airport filtering uses ``estdepartureairport`` / ``estarrivalairport``.
    - route filtering dynamically checks if precomputed ``route`` column exists
      for O(1) single-column pushdown, otherwise falls back to chunked streaming
      OR predicates over exact (departure, arrival) pairs to prevent recursion limits.

    Returns a pandas DataFrame. Raises FileNotFoundError if MASTER_FLIGHTS_FILE
    does not exist.
    """
    if dataset is None:
        if not Path(MASTER_FLIGHTS_FILE).exists():
            raise FileNotFoundError(f"master_flights file not found: {MASTER_FLIGHTS_FILE}")
        dataset = ds.dataset(str(MASTER_FLIGHTS_FILE))

    scan_columns = list(columns) if columns is not None else None
    has_route = "route" in dataset.schema.names
    needs_pair_fallback = query is not None and bool(query.routes) and not has_route

    if needs_pair_fallback:
        # Fallback for legacy schemas lacking 'route' column:
        # Chunk routes into batches of 50 to avoid AST expression tree recursion limits,
        # constructing exact disjunctions: ((estdepartureairport == d) & (estarrivalairport == a)) | ...
        base_filter = _build_master_flights_filter(
            query, schema_names=dataset.schema.names, ignore_routes=True
        )
        normalized_routes = [r.replace(" -> ", "-") for r in query.routes]
        chunk_size = 50
        route_chunks = [
            normalized_routes[i : i + chunk_size]
            for i in range(0, len(normalized_routes), chunk_size)
        ]

        tables = []
        for chunk in route_chunks:
            pair_exprs = []
            for r in chunk:
                if "-" in r:
                    d, a = r.split("-", 1)
                    pair_exprs.append(
                        ds.field("estdepartureairport").isin([d]) & ds.field("estarrivalairport").isin([a])
                    )
            if not pair_exprs:
                continue
            chunk_or_expr = functools.reduce(operator.or_, pair_exprs)
            chunk_filter = (
                (base_filter & chunk_or_expr) if base_filter is not None else chunk_or_expr
            )
            tbl = dataset.scanner(filter=chunk_filter, columns=scan_columns).to_table()
            if tbl.num_rows > 0:
                tables.append(tbl)

        if not tables:
            empty_schema = dataset.schema
            if scan_columns is not None:
                empty_schema = pa.schema([f for f in dataset.schema if f.name in scan_columns])
            return pa.Table.from_batches([], schema=empty_schema).to_pandas()

        df = pa.concat_tables(tables).to_pandas()
        return df.reset_index(drop=True)

    combined = _build_master_flights_filter(query, schema_names=dataset.schema.names)
    df = dataset.to_table(filter=combined, columns=scan_columns).to_pandas()
    return df


def count_master_flights(
    query: Optional[MasterFlightQuery] = None,
    dataset: Optional[ds.Dataset] = None,
) -> int:
    """Counts matching master_flights records without loading table data into memory.

    Uses PyArrow dataset scanner count_rows() metadata pushdown with chunked OR fallback.
    """
    if dataset is None:
        if not Path(MASTER_FLIGHTS_FILE).exists():
            raise FileNotFoundError(f"master_flights file not found: {MASTER_FLIGHTS_FILE}")
        dataset = ds.dataset(str(MASTER_FLIGHTS_FILE))

    has_route = "route" in dataset.schema.names
    needs_pair_fallback = query is not None and bool(query.routes) and not has_route

    if needs_pair_fallback:
        base_filter = _build_master_flights_filter(
            query, schema_names=dataset.schema.names, ignore_routes=True
        )
        normalized_routes = [r.replace(" -> ", "-") for r in query.routes]
        chunk_size = 50
        route_chunks = [
            normalized_routes[i : i + chunk_size]
            for i in range(0, len(normalized_routes), chunk_size)
        ]
        total_count = 0
        for chunk in route_chunks:
            pair_exprs = []
            for r in chunk:
                if "-" in r:
                    d, a = r.split("-", 1)
                    pair_exprs.append(
                        ds.field("estdepartureairport").isin([d]) & ds.field("estarrivalairport").isin([a])
                    )
            if not pair_exprs:
                continue
            chunk_or_expr = functools.reduce(operator.or_, pair_exprs)
            chunk_filter = (
                (base_filter & chunk_or_expr) if base_filter is not None else chunk_or_expr
            )
            total_count += dataset.scanner(filter=chunk_filter).count_rows()
        return total_count

    combined = _build_master_flights_filter(query, schema_names=dataset.schema.names)
    return dataset.scanner(filter=combined).count_rows()


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
        if query.ranks:
            exprs.append(ds.field("rank").isin(query.ranks))
        if query.dep_airports:
            exprs.append(ds.field("dep").isin(query.dep_airports))
        if query.arr_airports:
            exprs.append(ds.field("arr").isin(query.arr_airports))
        if query.min_distance_km is not None and query.min_distance_km > 0:
            if "distance_m" in dataset.schema.names:
                exprs.append(ds.field("distance_m") >= (query.min_distance_km * 1000.0))
            elif "distance_km" in dataset.schema.names:
                exprs.append(ds.field("distance_km") >= query.min_distance_km)

    if not exprs:
        combined = None
    else:
        combined = functools.reduce(operator.and_, exprs)

    return dataset.to_table(filter=combined, columns=columns).to_pandas()


def read_corridor_model_registry(
    routes: Optional[List[str]] = None,
    columns: Optional[List[str]] = None,
    registry_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Reads GLOBAL_CORRIDOR_MODEL_REGISTRY via PyArrow dataset with predicate pushdown.

    Parameters
    ----------
    routes : List[str], optional
        List of route identifiers (e.g. ``['EDDF-EGLL', 'LEPA-LEBL']`` or ``['EDDF -> EGLL']``).
    columns : List[str], optional
        List of columns to project.
    registry_path : Path, optional
        Path override for the corridor model registry parquet. Defaults to GLOBAL_CORRIDOR_MODEL_REGISTRY.

    Returns
    -------
    pd.DataFrame
        Filtered corridor model registry DataFrame.
    """
    reg_path = Path(registry_path) if registry_path is not None else Path(GLOBAL_CORRIDOR_MODEL_REGISTRY)
    if not reg_path.exists():
        logger.warning("Corridor model registry not found: %s", reg_path)
        cols = columns if columns is not None else ["route_id", "cluster_id", "file_path", "fl"]
        return pd.DataFrame(columns=cols)

    dataset = ds.dataset(str(reg_path))
    exprs = []

    if routes:
        # Accept both dash form ('EDDF-EGLL') and arrow form ('EDDF -> EGLL') from callers.
        # The registry column may store either. Build an isin set covering both representations.
        dash_forms  = {r.replace(" -> ", "-") for r in routes}
        arrow_forms = {f"{d} -> {a}" for r in dash_forms for d, _, a in [r.partition("-")]}
        normalized_routes = list(dash_forms | arrow_forms)
        route_col = "route_id" if "route_id" in dataset.schema.names else "route"
        if route_col in dataset.schema.names:
            exprs.append(ds.field(route_col).cast(pa.string()).isin(normalized_routes))

    combined = functools.reduce(operator.and_, exprs) if exprs else None
    return dataset.to_table(filter=combined, columns=columns).to_pandas()


def validate_sim_trajectory_df(df: pd.DataFrame) -> None:
    """Ensure incoming trajectory DataFrame satisfies the 14-column fixed metadata contract.

    Raises
    ------
    ValueError
        If mandatory metadata columns are missing or primary key columns contain NULLs.
    """
    missing = [col for col in SIM_LAKE_FIXED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(
            f"Trajectory DataFrame missing mandatory metadata columns: {missing}"
        )

    null_cols = [
        col for col in ["SIM_FID", "model_config_id", "fuel", "route", "dep_date", "FL"]
        if df[col].isnull().any()
    ]
    if null_cols:
        raise ValueError(
            f"Trajectory DataFrame contains NULL values in primary metadata columns: {null_cols}"
        )


def read_corridors_map(
    ranks: Optional[List[int]] = None,
    registry_path: Optional[Path] = None,
    corridors_dir: Optional[Path] = None,
    min_distance_km: float = 0.0,
) -> Dict[Tuple[str, int], CorridorCluster]:
    """[DEPRECATED] Build the (route_id, cluster_id) -> CorridorCluster map from registry.

    Deprecated: Prefer using ``src.core.physics.slots.slot1_flightlist_gen.build_corridors_map()``
    which encapsulates corridor selection business logic.
    """
    from src.core.physics.slots.slot1_flightlist_gen import build_corridors_map
    return build_corridors_map(
        ranks=ranks,
        registry_path=registry_path,
        corridors_dir=corridors_dir,
        min_distance_km=min_distance_km,
    )


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
        filters.append(ds.field("SIM_FID").cast(pa.string()).isin(query.sim_fids))
    if query.routes:
        filters.append(ds.field("route").cast(pa.string()).isin(query.routes))
    if query.ef_gt is not None:
        filters.append(ds.field("EF_total") > query.ef_gt)
    if query.fl_lte is not None:
        filters.append(ds.field("FL") <= query.fl_lte)
    if query.model_config_id is not None:
        filters.append(ds.field("model_config_id").cast(pa.string()) == query.model_config_id)
    if query.fuel is not None:
        filters.append(ds.field("fuel").cast(pa.string()) == query.fuel)

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
    """Append a trajectory DataFrame into the simulation Delta Lake.

    Normal mode (overwrite=False)
    ------------------------------
    - Deduplicates incoming data on ``(SIM_FID, time)`` with ``keep='last'``.
    - Validates mandatory 14-column metadata schema and non-null primary keys.
    - Appends rows directly via ``write_deltalake(mode='append')``.
    - Deduplication against existing lake data is handled upstream by the
      Slot 2 skip-gate (``read_existing_sim_fids`` / ``filter_and_batch``).
    - If incoming DataFrame introduces new physics columns not yet in the
      Delta schema, uses ``schema_mode='merge'`` to allow schema evolution.

    Overwrite mode (overwrite=True)
    ---------------------------------
    - DELETE all existing rows matching incoming ``SIM_FID``s, then INSERT fresh rows
      via append with ``schema_mode='merge'`` to guarantee clean trajectory replacement
      without leaving orphaned waypoints.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        raise TypeError("df must be a non-empty pandas DataFrame.")

    # 1. Enforce strict 14-column metadata contract
    validate_sim_trajectory_df(df)

    # 2. Pre-deduplicate on (SIM_FID, time) with keep='last'
    df = df.drop_duplicates(subset=["SIM_FID", "time"], keep="last")

    # 3. Convert any timedelta64 columns to float seconds (Delta Lake does not support Duration type)
    for col in df.select_dtypes(include=["timedelta64", "timedelta"]).columns:
        df[col] = df[col].dt.total_seconds()

    # 4. Strict typecasting for known metadata string columns to avoid string_view issues
    for col in SIM_LAKE_STR_COLUMNS:
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
        write_deltalake(path_str, df, mode="append", schema_mode="merge")
        logger.info("Appended %d waypoint row(s) to Delta Lake at %s", len(df), lake_path)


# ---------------------------------------------------------------------------
# Unified simulation lake IO engine
# ---------------------------------------------------------------------------

def read_sim_lake_metadata(
    lake_path: str | Path,
    tasks: List[SimTask],
    columns: List[str],
) -> pd.DataFrame:
    """Read one waypoint-0 row per SIM_FID from the simulation Delta Lake.

    The unified IO primitive shared by all Slot 2 paths (standard skip-gate,
    variational EF lookup, future extensions). Uses a 4-stage pipeline:

    **On disk** (Acero scanner):

    1. ``dep_date.isin()`` — file-level skip via Z-ORDER min/max stats.
    2. ``& firstseen.isin()`` — row-level: 84% of flights unique on firstseen;
       eliminates ~84% of non-target rows. Decoded for filter only, not in output.
    3. ``& waypoint.isin([0])`` — selects exactly 1 row per SIM_FID on disk.
       Verified 100% coverage (8483/8483 SIM_FIDs). Caps RAM at ≪1.2×N_tasks rows
       regardless of trajectory length (long-haul safe).
    Column projection: ``["SIM_FID"] + columns`` only. ``firstseen`` and
    ``waypoint`` are decoded for filter evaluation but never materialised.

    **In RAM** (pure Python, no C++ kernel):

    4. ``SIM_FID.rsplit("_", n=2)[0].isin(target_base_keys)`` — exact match on
       ``(icao24, callsign, dep-arr, YYYYMMDD, HHMM)`` which is unique per
       master flight.

    Parameters
    ----------
    lake_path : str | Path
        Root directory of the simulation Delta Lake.
    tasks : List[SimTask]
        Candidate tasks from Slot 1. Drive all filter sets.
    columns : List[str]
        Metadata columns to return (e.g. ``["SIM_FID"]`` or
        ``["SIM_FID", "FL", "EF_total"]``). ``SIM_FID`` is always included.

    Returns
    -------
    pd.DataFrame
        One row per matched SIM_FID with the requested columns.
        Empty DataFrame if the lake does not exist or no tasks match.
    """
    path_str = str(lake_path)
    if not Path(path_str).exists() or not tasks:
        return pd.DataFrame(columns=list(dict.fromkeys(["SIM_FID"] + columns)))

    try:
        dt = DeltaTable(path_str)
    except Exception:
        return pd.DataFrame(columns=list(dict.fromkeys(["SIM_FID"] + columns)))

    # --- Build filter sets from tasks ---
    target_dep_dates: List[int] = list({
        int(pd.Timestamp(t.firstseen, unit="s").strftime("%Y%m%d")) for t in tasks
    })
    target_firstseens: List[pd.Timestamp] = list({
        pd.Timestamp(t.firstseen, unit="s") for t in tasks
    })

    # Stage 1+2+3: file-skip, row selectivity, 1-row-per-SIM_FID on disk
    expr = (
        ds.field("dep_date").isin(target_dep_dates)
        & ds.field("firstseen").isin(target_firstseens)
        & ds.field("waypoint").isin([0])
    )

    # Ensure SIM_FID is always present; deduplicate column list preserving order
    scan_cols = list(dict.fromkeys(["SIM_FID"] + [c for c in columns if c != "SIM_FID"]))

    try:
        pyarrow_ds = dt.to_pyarrow_dataset()
        pruned = pyarrow_ds.to_table(columns=scan_cols, filter=expr)
    except Exception as exc:
        logger.warning("read_sim_lake_metadata failed to scan %s: %s", lake_path, exc)
        return pd.DataFrame(columns=scan_cols)

    if pruned.num_rows == 0:
        return pd.DataFrame(columns=scan_cols)

    df = pruned.to_pandas()

    # Stage 4: exact Base_FID match in Python on the already-tiny DataFrame.
    # base_key = SIM_FID with cluster_id AND FL stripped (rsplit n=2).
    # This matches (icao24, callsign, dep-arr, YYYYMMDD, HHMM) — unique per master flight.
    import re as _re
    target_base_keys: frozenset[str] = frozenset(
        f"{t.icao24}_{_re.sub(r'[^A-Z0-9]', '', (t.callsign or '').upper())}"
        f"_{t.dep}-{t.arr}"
        f"_{pd.Timestamp(t.firstseen, unit='s').strftime('%Y%m%d_%H%M')}"
        for t in tasks
    )
    bk = df["SIM_FID"].str.rsplit("_", n=2).str[0]
    df = df[bk.isin(target_base_keys)].reset_index(drop=True)

    return df


def delete_sim_lake_rows(
    lake_path: str | Path,
    sim_fids: Collection[str],
) -> None:
    """Delete all rows whose SIM_FID is in ``sim_fids`` from the Delta Lake.

    Uses a single SQL ``SIM_FID IN (...)`` predicate. Safe for daily-loop
    overwrite: the per-day task count bounds the list to a few thousand strings
    at most. Delta-rs streams files during deletion; Z-ORDER on ``dep_date``
    means only the relevant day's files are rewritten (peak RAM ≈ X_lake/Y_days).

    Parameters
    ----------
    lake_path : str | Path
        Root directory of the simulation Delta Lake.
    sim_fids : Collection[str]
        Exact SIM_FID strings to delete.
    """
    fids = list(sim_fids)
    if not fids or not Path(str(lake_path)).exists():
        return
    try:
        dt = DeltaTable(str(lake_path))
        quoted = ", ".join(repr(f) for f in fids)
        dt.delete(f"SIM_FID IN ({quoted})")
        logger.info(
            "delete_sim_lake_rows: deleted %d SIM_FID(s) from %s.",
            len(fids), lake_path,
        )
    except Exception as exc:
        logger.warning("delete_sim_lake_rows failed for %s: %s", lake_path, exc)


def read_existing_sim_fids(
    lake_path: str | Path,
    tasks: List[SimTask],
) -> frozenset[str]:
    """Return the frozenset of SIM_FIDs already present in the lake for these tasks.

    Thin wrapper over :func:`read_sim_lake_metadata`.
    """
    df = read_sim_lake_metadata(lake_path, tasks, columns=["SIM_FID"])
    return frozenset(df["SIM_FID"].tolist())



def vacuum_sim_lake(
    lake_path: Path,
    retention_hours: int = 0,
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
    target_size: Optional[int] = DELTA_LAKE_TARGET_FILE_SIZE_BYTES,
) -> None:
    """Run Delta Lake compaction and Z-ordering on the simulation lake.

    Parameters
    ----------
    lake_path : Path
        Path to the simulation Delta Lake directory.
    z_order_cols : list[str], optional
        Columns to Z-order on. Default is ``["dep_date", "route", "EF_total"]``.
    target_size : int, optional
        Target file size in bytes for bin-packed compaction chunks.
        Default is ``DELTA_LAKE_TARGET_FILE_SIZE_BYTES`` (512 MB).
    """
    path_str = str(lake_path)
    if not Path(path_str).exists():
        return

    if z_order_cols is None:
        z_order_cols = ["dep_date", "route", "EF_total"]

    try:
        dt = DeltaTable(path_str)
        # Step 1: Compact small per-batch parquet files into larger chunks
        dt.optimize.compact(target_size=target_size)
        # Step 2: Z-order data on key query columns for fast skipping
        dt.optimize.z_order(z_order_cols, target_size=target_size)
        logger.info(
            "optimize_sim_lake: compacted and Z-ordered %s on %s (target_size=%d bytes)",
            lake_path, z_order_cols, target_size or 0,
        )
    except Exception as exc:
        logger.warning("optimize_sim_lake failed for %s: %s — continuing.", lake_path, exc)


def read_ef_by_base_key(
    lake_path: Path,
    tasks: List[SimTask],
) -> "dict[str, list[tuple[float, float]]]":
    """Return ``{cluster_fid: [(fl, ef_total), ...]}`` for all matched tasks.

    Thin wrapper over :func:`read_sim_lake_metadata`. Groups by CLUSTER_FID
    (SIM_FID with the ``_{FL}`` suffix stripped, i.e. one entry per FL variant)
    so the variational step-down logic sees all simulated FL results per
    ``(flight, cluster)`` pair.
    """
    df = read_sim_lake_metadata(lake_path, tasks, columns=["SIM_FID", "FL", "EF_total"])
    result: dict[str, list[tuple[float, float]]] = {}
    for sim_fid, fl, ef in zip(df["SIM_FID"], df["FL"], df["EF_total"]):
        cluster_fid = sim_fid.rsplit("_", 1)[0]  # strip only _{FL} → CLUSTER_FID
        result.setdefault(cluster_fid, []).append((float(fl), float(ef)))
    return result


def read_flight_filepaths(
    flight_ids: Optional[List[str] | set[str]] = None,
    registry_path: Optional[Path] = None,
    columns: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Reads trajectory registry via PyArrow dataset with predicate pushdown to resolve filepaths for FIDs.

    Parameters
    ----------
    flight_ids : list or set of str, optional
        Flight IDs to look up. If None, returns all entries from the registry.
    registry_path : Path, optional
        Path to registry parquet. Defaults to GLOBAL_TRAJECTORY_REGISTRY.
    columns : list of str, optional
        Columns to load. Defaults to ['flight_id', 'file_path'].

    Returns
    -------
    pd.DataFrame
        DataFrame containing resolved flight_id and file_path mappings.
    """
    path = registry_path if registry_path is not None else GLOBAL_TRAJECTORY_REGISTRY
    cols = columns or ["flight_id", "file_path"]

    if not Path(path).exists():
        logger.warning("read_flight_filepaths: registry not found at %s", path)
        return pd.DataFrame(columns=cols)

    dataset = ds.dataset(str(path))
    expr = None
    if flight_ids is not None:
        fid_list = list(flight_ids)
        if not fid_list:
            return pd.DataFrame(columns=cols)
        expr = ds.field("flight_id").isin(fid_list)

    table = dataset.to_table(filter=expr, columns=cols)
    return table.to_pandas()
