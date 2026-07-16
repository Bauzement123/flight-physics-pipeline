"""
Module 1.2: OpenSky Trino Fetcher (Worker)
Reads master flights table for a specific route, resolves trajectories against a 3-step cache
(Global Registry -> Concat Backup -> OpenSky Trino), and saves individual and consolidated Parquet files.
Every public function is strictly <= 50 LOC.
"""
import argparse
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.common.config import (
    BASE_DIR,
    FETCH_RUNS_DIRNAME,
    GLOBAL_TRAJECTORY_REGISTRY,
    LOGS_DIR,
    MASTER_FLIGHTS_FILE,
    MIN_DISTANCE_KM,
    RAW_CONCAT_SUFFIX,
    RAW_TRAJECTORY_DIRNAME,
    is_supported_typecode,
)
from src.common.registry_utils import load_trajectory_registry
from src.common.utils import retry_backoff, setup_file_logger, update_global_registry, log_skipped_aircraft
from src.core.fetching.helpers import (
    all_individual_files_exist,
    apply_flight_filters,
    compute_all_expected_paths,
    generate_route_run_id,
    load_master_flights_for_route,
    prepare_flight_records,
    sample_flights,
    write_parquet_atomic_PyOpenSky,
    write_parquet_atomic,
    write_run_manifest,
)
from src.core.fetching.models import FetchRunParams, FlightFetchOutcome, RouteFetchResult

logger = logging.getLogger(__name__)


def filter_flight_list(
    df: pd.DataFrame, start_date: str | None = None, end_date: str | None = None, **kwargs: Any
) -> pd.DataFrame:
    """Backward-compatible wrapper around apply_flight_filters."""
    return apply_flight_filters(df, start_date=start_date, end_date=end_date, typecode=kwargs.get('typecode'))


def label_flight_phase(df: pd.DataFrame) -> pd.DataFrame:
    """Labels flight records in df with a flight_phase column using openap.

    Normalizes PyArrow-typed timestamp columns to seconds (float) before
    passing to OpenAP to avoid a nanosecond integer CPU-freeze bug where
    FlightPhase.set_trajectory() iterates max(ts)//60 > 37 billion times.
    """
    if df.empty or any(col not in df.columns for col in ['baroaltitude', 'velocity', 'vertrate', 'time']):
        if 'flight_phase' not in df.columns:
            df['flight_phase'] = None
        return df
    icao24 = str(df.get('icao24', pd.Series([None])).iloc[0]) if 'icao24' in df.columns and not df.empty else 'UNK'
    typecode = str(df.get('typecode', pd.Series([None])).iloc[0]) if 'typecode' in df.columns and not df.empty else None
    if not is_supported_typecode(typecode):
        log_skipped_aircraft(icao24, typecode, "ERROR_FLAG: Missing, NaN, or non-target family aircraft typecode in fetcher phase labeling")
        if 'flight_phase' not in df.columns:
            df['flight_phase'] = None
        return df

    try:
        from openap.phase import FlightPhase
        from src.common.adapters import df_si_to_df_nautic

        df_sorted = df.sort_values('time')
        df_nautic = df_si_to_df_nautic(df_sorted)

        # Normalize timestamps: handle PyArrow extension types that produce
        # nanosecond integers instead of seconds when subtracted directly.
        ts_series = df_sorted['time'] - df_sorted['time'].iloc[0]
        ts = ts_series.dt.total_seconds().values if hasattr(ts_series, 'dt') else ts_series.values
        ts  = np.asarray(ts,  dtype=float)
        alt = np.asarray(df_nautic['altitude'].values,      dtype=float)
        spd = np.asarray(df_nautic['groundspeed'].values,   dtype=float)
        roc = np.asarray(df_nautic['vertical_rate'].values, dtype=float)

        fp = FlightPhase()
        fp.set_trajectory(ts, alt, spd, roc)
        df.loc[df_sorted.index, 'flight_phase'] = fp.phaselabel()
    except Exception as e:
        logger.debug(f"OpenAP phase labeling failed for {icao24} ({typecode}): {e}")
        df['flight_phase'] = None
        log_skipped_aircraft(icao24, typecode, f"ERROR_FLAG: Phase labeling failed: {e}")
    return df


def _query_trino_trajectory(trino_client: Any, record: dict[str, Any]) -> pd.DataFrame | None:
    """Executes Trino query for a single flight trajectory using retry_backoff."""
    from pyopensky.schema import StateVectorsData4
    from sqlalchemy import select

    query = (
        select(StateVectorsData4)
        .with_only_columns(
            StateVectorsData4.time,
            StateVectorsData4.lat,
            StateVectorsData4.lon,
            StateVectorsData4.velocity,
            StateVectorsData4.heading,
            StateVectorsData4.baroaltitude,
            StateVectorsData4.geoaltitude,
            StateVectorsData4.vertrate,
            StateVectorsData4.onground,
        )
        .where(StateVectorsData4.icao24 == record["icao24"])
        .where(StateVectorsData4.hour >= record["firstseen_hour"])
        .where(StateVectorsData4.hour <= record["lastseen_hour"])
    )
    if pd.notna(record.get("callsign")):
        query = query.where(StateVectorsData4.callsign == record["callsign"])

    try:
        return retry_backoff(trino_client.query, query)
    except Exception as e:
        logger.error(f"Trino query failed for {record['flight_id']}: {e}")
        return None


def _resolve_from_registry(
    record: dict[str, Any], cached_flights: dict[str, str]
) -> pd.DataFrame | None:
    """Step 1: Checks global registry index and local raw file."""
    fid = record["flight_id"]
    path = BASE_DIR / cached_flights[fid] if fid in cached_flights else record["raw_path"]
    if not path.exists():
        return None
    try:
        logger.info(f"   -> [Step 1: Registry/Disk] Hit for {fid} from {path.name}")
        df = pd.read_parquet(path)
        # Slice to this flight_id only — guards against multi-flight or concat files in registry
        if 'flight_id' in df.columns:
            df = df[df['flight_id'] == fid].copy()
        if df.empty:
            return None
        if 'flight_phase' not in df.columns:
            df = label_flight_phase(df)
            write_parquet_atomic(df, path)
        return df
    except Exception as e:
        logger.warning(f"   -> Registry/Disk read error for {fid}: {e}")
        return None


def _resolve_from_concat(
    record: dict[str, Any], concat_df: pd.DataFrame | None
) -> pd.DataFrame | None:
    """Step 2: Checks route-level concat file backup and restores individual file."""
    if concat_df is None or concat_df.empty or 'flight_id' not in concat_df.columns:
        return None
    fid = record["flight_id"]
    df_slice = concat_df[concat_df['flight_id'] == fid].copy()
    if df_slice.empty:
        return None
    try:
        logger.info(f"   -> [Step 2: Concat Backup] Recovered {fid} from concat backup.")
        if 'flight_phase' not in df_slice.columns:
            df_slice = label_flight_phase(df_slice)
        record["raw_path"].parent.mkdir(parents=True, exist_ok=True)
        write_parquet_atomic(df_slice, record["raw_path"])
        return df_slice
    except Exception as e:
        logger.warning(f"   -> Concat recovery error for {fid}: {e}")
        return None


def _resolve_from_trino(
    record: dict[str, Any], trino_client: Any
) -> pd.DataFrame | None:
    """Step 3: Queries Trino database for uncached trajectory waypoints."""
    logger.info(f"   -> [Step 3: Trino Query] Fetching {record['flight_id']} from OpenSky Trino...")
    df = _query_trino_trajectory(trino_client, record)
    if df is None or df.empty:
        return None
    try:
        df['icao24'] = record['icao24']
        df['callsign'] = record['callsign']
        df['typecode'] = record['typecode']
        df['estdepartureairport'] = record['dep']
        df['estarrivalairport'] = record['arr']
        df['flight_id'] = record['flight_id']
        # Restore schedule metadata columns expected by downstream processing
        df['firstseen'] = record['firstseen']
        df['lastseen'] = record['lastseen']
        df = label_flight_phase(df)
        record["raw_path"].parent.mkdir(parents=True, exist_ok=True)
        write_parquet_atomic_PyOpenSky(df, record["raw_path"])
        return df
    except Exception as e:
        logger.error(f"   -> Failed to process/save Trino df for {record['flight_id']}: {e}")
        return None


def resolve_flight(
    record: dict[str, Any],
    cached_flights: dict[str, str],
    concat_df: pd.DataFrame | None,
    trino_box: list[Any],
) -> tuple[FlightFetchOutcome, pd.DataFrame | None]:
    """Orchestrates the 3-step cache lookup (Registry -> Concat -> Trino)."""
    fid = record["flight_id"]
    
    df = _resolve_from_registry(record, cached_flights)
    if df is not None:
        return FlightFetchOutcome(fid, "registry", record["raw_path"], record["rel_path"]), df

    df = _resolve_from_concat(record, concat_df)
    if df is not None:
        return FlightFetchOutcome(fid, "concat", record["raw_path"], record["rel_path"]), df

    if trino_box[0] is None:
        from pyopensky.trino import Trino
        logger.info("Initializing pyopensky Trino client...")
        trino_box[0] = Trino()

    df = _resolve_from_trino(record, trino_box[0])
    if df is not None:
        return FlightFetchOutcome(fid, "trino", record["raw_path"], record["rel_path"]), df

    return FlightFetchOutcome(fid, "failed", error="Not found in cache or Trino"), None


def update_raw_concat(concat_path: Path, new_dfs: list[pd.DataFrame]) -> None:
    """Incrementally updates or creates the route-level consolidated parquet file."""
    if not new_dfs:
        return
    try:
        df_new = pd.concat(new_dfs, ignore_index=True)
        if concat_path.exists():
            df_old = pd.read_parquet(concat_path)
            if 'flight_id' in df_old.columns and 'flight_id' in df_new.columns:
                existing_fids = set(df_old['flight_id'].unique())
                df_new = df_new[~df_new['flight_id'].isin(existing_fids)]
            df_combined = pd.concat([df_old, df_new], ignore_index=True) if not df_new.empty else df_old
        else:
            df_combined = df_new
        if not df_combined.empty:
            write_parquet_atomic(df_combined, concat_path)
            logger.info(f"Updated concat file {concat_path.name} (total rows: {len(df_combined):,}).")
    except Exception as e:
        logger.error(f"Failed to update concat file {concat_path.name}: {e}")


def _prepare_cohort(
    dep: str, arr: str, source: Path, sample_size: int | None, seed: int,
    start_date: str | None, end_date: str | None, typecode: str | None
) -> pd.DataFrame:
    """Loads, filters, and samples target flight cohort for a route."""
    df = load_master_flights_for_route(dep, arr, source=source)
    if df.empty:
        return df
    df = apply_flight_filters(df, start_date=start_date, end_date=end_date, typecode=typecode)
    return sample_flights(df, sample_size=sample_size, seed=seed)


def _load_cached_registry_map() -> dict[str, str]:
    """Loads global trajectory registry into a flight_id -> file_path dict."""
    try:
        df_reg = load_trajectory_registry()
        if not df_reg.empty:
            return dict(zip(df_reg['flight_id'], df_reg['file_path']))
    except Exception as e:
        logger.error(f"Error loading global trajectory registry: {e}")
    return {}


def fetch_trajectories(
    dep: str,
    arr: str,
    out_dir: str | Path,
    flight_source: str | Path = MASTER_FLIGHTS_FILE,
    sample_size: int | None = None,
    seed: int = 42,
    start_date: str | None = None,
    end_date: str | None = None,
    typecode: str | None = None,
    min_distance: float = MIN_DISTANCE_KM,
    run_id: str | None = None,
    rank: int | None = None,
    strategy: str | None = None,
    fetch_format: str | None = None,
    update_concat: bool = True,
) -> RouteFetchResult:
    """Main orchestration function for retrieving flight trajectories on a single route."""
    start_time = time.time()
    out_path = Path(out_dir)
    raw_dir = out_path / RAW_TRAJECTORY_DIRNAME
    concat_path = out_path / f"{out_path.name}{RAW_CONCAT_SUFFIX}"
    params = FetchRunParams(
        dep, arr, rank, strategy, sample_size, seed, start_date, end_date, typecode, min_distance, fetch_format
    )
    eff_run_id = run_id or generate_route_run_id(params)
    manifest_path = out_path / FETCH_RUNS_DIRNAME / f"{eff_run_id}.json"
    df_cohort = _prepare_cohort(dep, arr, Path(flight_source), sample_size, seed, start_date, end_date, typecode)
    records = prepare_flight_records(df_cohort, out_path)

    if not records:
        logger.warning(f"No valid flight records to fetch for route {dep}->{arr}.")
        res = RouteFetchResult(False, dep, arr, eff_run_id, 0, 0, 0, 0, 0, 0, 0, out_path, raw_dir, concat_path, [], [], manifest_path, 0.0)
        write_run_manifest(manifest_path, res, params)
        return res

    # Fast-cache path: all individual files already present and concat exists
    expected_paths = compute_all_expected_paths(records)
    if all_individual_files_exist(expected_paths) and concat_path.exists():
        logger.info(f"Fast cache hit for {dep}->{arr}: all {len(records)} files already on disk.")
        res = RouteFetchResult(True, dep, arr, eff_run_id, len(records), len(records), 0, 0,
                          len(records), 0, 0, out_path, raw_dir, concat_path, [], [], manifest_path,
                          round(time.time() - start_time, 2))
        write_run_manifest(manifest_path, res, params)
        return res

    cached_flights = _load_cached_registry_map()
    concat_df = pd.read_parquet(concat_path) if concat_path.exists() else None
    trino_box = [None]

    outcomes, new_dfs, new_reg_entries, failed_fids = [], [], [], []
    reg_hits = concat_rec = trino_f = 0

    for idx, rec in enumerate(records, 1):
        logger.info(f"[{idx}/{len(records)}] Processing {rec['flight_id']}...")
        outcome, df = resolve_flight(rec, cached_flights, concat_df, trino_box)
        outcomes.append(outcome)
        if df is not None:
            new_dfs.append(df)
            if outcome.source == "registry": reg_hits += 1
            elif outcome.source == "concat": concat_rec += 1
            elif outcome.source == "trino": trino_f += 1
            if (outcome.source in ("concat", "trino") or rec["flight_id"] not in cached_flights) and outcome.registry_rel_path:
                new_reg_entries.append({"flight_id": rec["flight_id"], "file_path": outcome.registry_rel_path})
        else:
            failed_fids.append(rec["flight_id"])
            
    update_global_registry(GLOBAL_TRAJECTORY_REGISTRY, new_reg_entries)
    if update_concat and (concat_rec > 0 or trino_f > 0 or not concat_path.exists()):
        update_raw_concat(concat_path, new_dfs)

    succeeded = reg_hits + concat_rec + trino_f
    res = RouteFetchResult(
        succeeded > 0, dep, arr, eff_run_id, len(records), succeeded, len(failed_fids), 0,
        reg_hits, concat_rec, trino_f, out_path, raw_dir, concat_path, new_reg_entries, failed_fids,
        manifest_path, round(time.time() - start_time, 2)
    )
    write_run_manifest(manifest_path, res, params)
    logger.info(f"Route {dep}->{arr} fetch complete in {res.duration_seconds}s (Success: {res.success}).")
    return res


def parse_cli_args() -> argparse.Namespace:
    """Parses CLI arguments for single-route worker execution."""
    parser = argparse.ArgumentParser(description="Fetch Trajectories from OpenSky Trino (Worker)")
    parser.add_argument("--dep", required=True, help="Departure airport ICAO code (e.g. EDDF)")
    parser.add_argument("--arr", required=True, help="Arrival airport ICAO code (e.g. EGLL)")
    parser.add_argument("--out-dir", required=True, help="Output directory for route trajectories")
    parser.add_argument("--flight-source", default=str(MASTER_FLIGHTS_FILE), help="Path to master flights parquet")
    parser.add_argument("--sample-size", type=int, default=None, help="Number of random flights to sample")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic cohort sampling")
    parser.add_argument("--start-date", default=None, help="Start bounds of flight departure window (ISO format)")
    parser.add_argument("--end-date", default=None, help="End bounds of flight departure window (ISO format)")
    parser.add_argument("--typecode", default=None, help="Aircraft model code (e.g. B738, A320)")
    parser.add_argument("--min-distance", type=float, default=MIN_DISTANCE_KM,
                        help="Minimum corridor distance in km. Metadata-only for single-route worker "
                             "runs; route distance filtering is applied by fetcher_orchestrator.")
    parser.add_argument("--run-id", default=None, help="Optional run identifier for checkpoints")
    parser.add_argument("--rank", type=int, default=None, help="Corridor rank index")
    parser.add_argument("--strategy", default=None, help="Sampling strategy name")
    parser.add_argument("--fetch-format", default=None, help="Format name (e.g. roundtrip)")
    return parser.parse_args()


if __name__ == "__main__":
    from src.common.config import init_runtime
    init_runtime()
    setup_file_logger(log_filename="fetching.log")
    args = parse_cli_args()
    fetch_trajectories(
        dep=args.dep,
        arr=args.arr,
        out_dir=args.out_dir,
        flight_source=args.flight_source,
        sample_size=args.sample_size,
        seed=args.seed,
        start_date=args.start_date,
        end_date=args.end_date,
        typecode=args.typecode,
        min_distance=args.min_distance,
        run_id=args.run_id,
        rank=args.rank,
        strategy=args.strategy,
        fetch_format=args.fetch_format,
    )
