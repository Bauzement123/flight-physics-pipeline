"""
Pure and I/O-light helper functions for the Fetching module.
Every public function is strictly ≤ 50 LOC.
"""
import dataclasses
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import src.common.adapters as adapters
from src.common.config import (
    MASTER_FLIGHTS_FILE,
    RAW_TRAJECTORY_DIRNAME,
    RAW_TRAJECTORY_SUFFIX,
    is_supported_typecode,
    DEFAULT_PREFILTER_THRESHOLDS,
    ROUTE_SUMMARY_PARQUET,
)
from src.common.utils import to_project_relative, write_json_dataclass, log_skipped_aircraft
from src.core.fetching.models import FetchRunParams, RouteFetchResult

logger = logging.getLogger(__name__)


def load_master_flights_for_route(
    dep: str, arr: str, source: Path = MASTER_FLIGHTS_FILE
) -> pd.DataFrame:
    """Loads flight records for a specific corridor from the specified parquet source."""
    path = Path(source)
    if not path.exists():
        logger.error(f"Flight source file not found: {path}")
        return pd.DataFrame()
    try:
        filters = [('estdepartureairport', '==', dep), ('estarrivalairport', '==', arr)]
        df = pd.read_parquet(path, filters=filters)
        logger.info(f"Loaded {len(df)} master flights for route {dep}->{arr} from {path.name}")
        return df
    except Exception as e:
        logger.warning(f"Push-down filter failed on {path} ({e}); falling back to memory filter.")
        try:
            df = pd.read_parquet(path)
            df = df[(df['estdepartureairport'] == dep) & (df['estarrivalairport'] == arr)]
            logger.info(f"Loaded {len(df)} master flights for route {dep}->{arr} via memory filter.")
            return df
        except Exception as err:
            logger.error(f"Failed to load flights from {path}: {err}")
            return pd.DataFrame()


def _apply_metadata_prefilters(df: pd.DataFrame) -> pd.DataFrame:
    """Applies metadata pre-filters based on DEFAULT_PREFILTER_THRESHOLDS config."""
    active_thresholds = {k: v for k, v in DEFAULT_PREFILTER_THRESHOLDS.items() if v is not None}
    if not active_thresholds:
        return df

    res = df.copy()
    initial_count = len(res)

    route_medians_min = {}
    duration_check_active = (
        "max_duration_pct_above_median" in active_thresholds or
        "min_duration_pct_below_median" in active_thresholds
    )
    if duration_check_active:
        try:
            df_route_summary = pd.read_parquet(ROUTE_SUMMARY_PARQUET)
            if not df_route_summary.empty and "route" in df_route_summary.columns and "route_duration_median" in df_route_summary.columns:
                for _, row in df_route_summary.iterrows():
                    clean_route = str(row["route"]).replace(" -> ", "-").replace(" ", "")
                    route_medians_min[clean_route] = float(row["route_duration_median"])
        except Exception as e:
            logger.warning(f"Could not load route summary for duration pre-filter: {e}")

    # Compute duration in seconds if lastseen/firstseen columns are present and duration is not in columns
    if duration_check_active and 'duration_s' not in res.columns:
        try:
            res['duration_s'] = (pd.to_datetime(res['lastseen'], utc=True) - pd.to_datetime(res['firstseen'], utc=True)).dt.total_seconds()
        except Exception as e:
            logger.warning(f"Could not compute duration_s: {e}")

    # Apply distance and candidate filters
    # 1. max_dep_horiz_dist
    val = active_thresholds.get("max_dep_horiz_dist")
    if val is not None and "estdepartureairporthorizdistance" in res.columns:
        res = res[res["estdepartureairporthorizdistance"].isna() | (res["estdepartureairporthorizdistance"].astype(float) <= float(val))]

    # 2. max_dep_vert_dist
    val = active_thresholds.get("max_dep_vert_dist")
    if val is not None and "estdepartureairportvertdistance" in res.columns:
        res = res[res["estdepartureairportvertdistance"].isna() | (res["estdepartureairportvertdistance"].astype(float).abs() <= float(val))]

    # 3. max_arr_horiz_dist
    val = active_thresholds.get("max_arr_horiz_dist")
    if val is not None and "estarrivalairporthorizdistance" in res.columns:
        res = res[res["estarrivalairporthorizdistance"].isna() | (res["estarrivalairporthorizdistance"].astype(float) <= float(val))]

    # 4. max_arr_vert_dist
    val = active_thresholds.get("max_arr_vert_dist")
    if val is not None and "estarrivalairportvertdistance" in res.columns:
        res = res[res["estarrivalairportvertdistance"].isna() | (res["estarrivalairportvertdistance"].astype(float).abs() <= float(val))]

    # 5. max_dep_candidates
    val = active_thresholds.get("max_dep_candidates")
    if val is not None and "departureairportcandidatescount" in res.columns:
        res = res[res["departureairportcandidatescount"].isna() | (res["departureairportcandidatescount"].astype(float) <= float(val))]

    # 6. max_arr_candidates
    val = active_thresholds.get("max_arr_candidates")
    if val is not None and "arrivalairportcandidatescount" in res.columns:
        res = res[res["arrivalairportcandidatescount"].isna() | (res["arrivalairportcandidatescount"].astype(float) <= float(val))]

    # 7. max_duration_pct_above_median
    val = active_thresholds.get("max_duration_pct_above_median")
    if val is not None and "duration_s" in res.columns:
        def filter_duration_above(row):
            dep = row.get("estdepartureairport")
            arr = row.get("estarrivalairport")
            route_id = f"{dep}-{arr}" if pd.notna(dep) and pd.notna(arr) else ""
            med_min = route_medians_min.get(route_id, None)
            if med_min is None:
                return True
            max_s = med_min * 60.0 * (1.0 + float(val) / 100.0)
            dur = row.get("duration_s")
            return pd.isna(dur) or float(dur) <= max_s
        res = res[res.apply(filter_duration_above, axis=1)]

    # 8. min_duration_pct_below_median
    val = active_thresholds.get("min_duration_pct_below_median")
    if val is not None and "duration_s" in res.columns:
        def filter_duration_below(row):
            dep = row.get("estdepartureairport")
            arr = row.get("estarrivalairport")
            route_id = f"{dep}-{arr}" if pd.notna(dep) and pd.notna(arr) else ""
            med_min = route_medians_min.get(route_id, None)
            if med_min is None:
                return True
            min_s = med_min * 60.0 * (1.0 - float(val) / 100.0)
            dur = row.get("duration_s")
            return pd.isna(dur) or float(dur) >= min_s
        res = res[res.apply(filter_duration_below, axis=1)]

    filtered_count = len(res)
    if filtered_count < initial_count:
        logger.info(f"Metadata pre-filtering removed {initial_count - filtered_count} flights based on config. {filtered_count} remaining.")
    return res


def apply_flight_filters(
    df: pd.DataFrame,
    start_date: str | None = None,
    end_date: str | None = None,
    typecode: str | None = None,
) -> pd.DataFrame:
    """Applies date, typecode, and config-based metadata pre-filters to a flight cohort DataFrame."""
    if df.empty:
        return df
    res = df.copy()

    if start_date is not None:
        try:
            s_dt = pd.to_datetime(start_date)
            if s_dt.tzinfo is None:
                s_dt = s_dt.tz_localize('UTC')
            else:
                s_dt = s_dt.tz_convert('UTC')
            fs_dt = pd.to_datetime(res['firstseen'], utc=True)
            res = res[fs_dt >= s_dt]
        except Exception as e:
            logger.error(f"Invalid start_date '{start_date}': {e}")
            raise ValueError(f"Invalid start_date '{start_date}': {e}") from e

    if end_date is not None:
        try:
            e_str = f"{end_date} 23:59:59" if len(str(end_date)) <= 10 else str(end_date)
            e_dt = pd.to_datetime(e_str)
            if e_dt.tzinfo is None:
                e_dt = e_dt.tz_localize('UTC')
            else:
                e_dt = e_dt.tz_convert('UTC')
            fs_dt = pd.to_datetime(res['firstseen'], utc=True)
            res = res[fs_dt <= e_dt]
        except Exception as e:
            logger.error(f"Invalid end_date '{end_date}': {e}")
            raise ValueError(f"Invalid end_date '{end_date}': {e}") from e

    if typecode is not None:
        if 'typecode' in res.columns:
            res = res[res['typecode'].str.upper() == str(typecode).upper()]
        else:
            logger.warning("Column 'typecode' not in DataFrame; skipping typecode filter.")
    elif 'typecode' in res.columns:
        supported_mask = res['typecode'].apply(is_supported_typecode)
        if not supported_mask.all():
            for _, bad_row in res[~supported_mask].iterrows():
                log_skipped_aircraft(build_flight_id(bad_row) or str(bad_row.get('icao24', 'UNK')), bad_row.get('typecode'), "ERROR_FLAG: Missing, NaN, or non-target family aircraft typecode in apply_flight_filters")
            res = res[supported_mask]

    # Apply configuration-driven metadata pre-filters
    res = _apply_metadata_prefilters(res)

    return res


def sample_flights(
    df: pd.DataFrame, sample_size: int | None = None, seed: int = 42
) -> pd.DataFrame:
    """Deterministically samples up to sample_size rows from df."""
    if df.empty or sample_size is None or sample_size <= 0 or sample_size >= len(df):
        return df
    return df.sample(n=sample_size, random_state=seed)


def build_flight_id(row: Any) -> str | None:
    """Constructs unique flight_id: {icao24}_{callsign}_{dep}-{arr}_{fs_str}."""
    try:
        if isinstance(row, dict):
            icao24 = row.get('icao24')
            callsign = row.get('callsign')
            dep = row.get('estdepartureairport', 'UNK')
            arr = row.get('estarrivalairport', 'UNK')
            fs_val = row.get('firstseen')
        else:
            icao24 = row.get('icao24') if 'icao24' in row else None
            callsign = row.get('callsign') if 'callsign' in row else None
            dep = row.get('estdepartureairport', 'UNK') if 'estdepartureairport' in row else 'UNK'
            arr = row.get('estarrivalairport', 'UNK') if 'estarrivalairport' in row else 'UNK'
            fs_val = row.get('firstseen') if 'firstseen' in row else None

        if pd.isna(icao24) or pd.isna(fs_val):
            return None

        clean_cs = str(callsign).strip() if pd.notna(callsign) and str(callsign).strip() != "" else "UNK"
        fs_dt = pd.to_datetime(fs_val)
        if fs_dt.tzinfo is not None:
            fs_dt = fs_dt.tz_localize(None)
        fs_str = fs_dt.strftime('%Y%m%d_%H%M')
        return f"{icao24}_{clean_cs}_{dep}-{arr}_{fs_str}"
    except Exception as e:
        logger.debug(f"Failed to build flight_id: {e}")
        return None


def parse_time_bounds(
    firstseen: Any, lastseen: Any
) -> tuple[datetime, datetime, datetime, datetime, str] | None:
    """Parses firstseen/lastseen into naive UTC datetimes, hourly partition bounds, and fs_str."""
    if pd.isna(firstseen) or pd.isna(lastseen):
        return None
    try:
        fs_dt = pd.to_datetime(firstseen)
        if fs_dt.tzinfo is not None:
            fs_dt = fs_dt.tz_localize(None)
        ls_dt = pd.to_datetime(lastseen)
        if ls_dt.tzinfo is not None:
            ls_dt = ls_dt.tz_localize(None)

        fs_hour = fs_dt.floor('h').to_pydatetime()
        ls_hour = ls_dt.ceil('h').to_pydatetime()
        fs_str = fs_dt.strftime('%Y%m%d_%H%M')
        return fs_dt, ls_dt, fs_hour, ls_hour, fs_str
    except Exception as e:
        logger.debug(f"Time parsing error: {e}")
        return None


def build_expected_raw_path(
    route_dir: Path, icao24: str, callsign: Any, fs_str: str
) -> tuple[Path, str]:
    """Derives individual raw file path and project-relative string."""
    clean_cs = str(callsign).strip() if pd.notna(callsign) and str(callsign).strip() != "" else "UNK"
    raw_dir = route_dir / RAW_TRAJECTORY_DIRNAME
    filename = f"{icao24}_{clean_cs}_{fs_str}{RAW_TRAJECTORY_SUFFIX}"
    path = raw_dir / filename
    rel_path = to_project_relative(path)
    return path, rel_path


def prepare_flight_records(df: pd.DataFrame, route_dir: Path) -> list[dict[str, Any]]:
    """Converts cohort DataFrame into a clean list of validated flight record dicts."""
    records = []
    for _, row in df.iterrows():
        fid = build_flight_id(row)
        if not fid:
            continue
        tc = row.get('typecode', None)
        if not is_supported_typecode(tc):
            log_skipped_aircraft(fid, tc, "ERROR_FLAG: Missing, NaN, or non-target family aircraft typecode in prepare_flight_records")
            continue
        tb = parse_time_bounds(row.get('firstseen'), row.get('lastseen'))
        if not tb:
            continue
        fs_dt, ls_dt, fs_hour, ls_hour, fs_str = tb
        path, rel = build_expected_raw_path(
            route_dir, str(row.get('icao24')), row.get('callsign'), fs_str
        )
        records.append({
            "flight_id": fid,
            "icao24": str(row.get('icao24')),
            "callsign": row.get('callsign'),
            "typecode": row.get('typecode'),
            "dep": row.get('estdepartureairport', 'UNK'),
            "arr": row.get('estarrivalairport', 'UNK'),
            "firstseen": fs_dt,
            "lastseen": ls_dt,
            "firstseen_hour": fs_hour,
            "lastseen_hour": ls_hour,
            "fs_str": fs_str,
            "raw_path": path,
            "rel_path": rel,
            "raw_row": row.to_dict(),
        })
    return records


def compute_all_expected_paths(records: list[dict[str, Any]]) -> list[Path]:
    """Returns list of expected raw file paths for all cohort records."""
    return [r["raw_path"] for r in records if "raw_path" in r]


def all_individual_files_exist(paths: list[Path]) -> bool:
    """Checks if every expected raw trajectory file already exists on disk."""
    if not paths:
        return False
    return all(p.exists() for p in paths)


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """Generic atomic writer – does not enforce Golden Schema.

    Writes any DataFrame to a Parquet file using a FUSE-safe atomic pattern.
    On Windows Google Drive (FUSE mount), directly overwriting an existing file
    causes lock contention and 4-byte footer truncation errors.  This helper
    unlinks the existing file first, writes to a UUID-named temp file in the
    same directory, then atomically replaces the target.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    temp_path = path.with_suffix(f".tmp.{uuid.uuid4().hex}.parquet")
    try:
        df.to_parquet(temp_path, index=False)
        os.replace(temp_path, path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def write_parquet_atomic_PyOpenSky(df: pd.DataFrame, path: Path) -> None:
    """Write raw PyOpenSky/OpenSky Trino data atomically after Golden Schema conversion.

    This writer is reserved for raw OpenSky query outputs whose input columns
    may use PyOpenSky aliases such as lat, lon, baroaltitude, velocity, and
    vertrate. It converts them through adapters.PyOpenSky_df_to_PyContrails_df()
    before performing the same FUSE-safe temp-file and os.replace() sequence as
    write_parquet_atomic().
    """

    # ---> ENFORCE GOLDEN SCHEMA BEFORE WRITING <---
    df_golden = adapters.PyOpenSky_df_to_PyContrails_df(df)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass

    temp_path = path.with_suffix(f".tmp.{uuid.uuid4().hex}.parquet")

    try:
        # Save the unified Golden Schema dataframe to disk
        df_golden.to_parquet(temp_path, index=False)
        os.replace(temp_path, path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def write_run_manifest(
    manifest_path: Path, result: RouteFetchResult, params: FetchRunParams
) -> None:
    """Writes combined execution metadata and outcome statistics to a JSON manifest."""
    payload = {
        "run_id": result.run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "params": dataclasses.asdict(params),
        "result": {
            "success": result.success,
            "dep": result.dep,
            "arr": result.arr,
            "requested": result.requested,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "skipped": result.skipped,
            "registry_hits": result.registry_hits,
            "concat_recoveries": result.concat_recoveries,
            "trino_fetches": result.trino_fetches,
            "duration_seconds": result.duration_seconds,
            "failed_flight_ids": result.failed_flight_ids,
            "concat_path": str(result.concat_path),
        }
    }
    write_json_dataclass(manifest_path, payload)


def generate_route_run_id(params: FetchRunParams) -> str:
    """Derives a deterministic run_id string from FetchRunParams."""
    parts = [f"route_{params.dep}-{params.arr}"]
    if params.rank is not None:
        parts.append(f"rank_{params.rank}")
    if params.strategy:
        parts.append(f"strat_{params.strategy}")
    if params.sample_size is not None:
        parts.append(f"val_{params.sample_size}")
    if params.seed is not None:
        parts.append(f"seed_{params.seed}")
    if params.fetch_format:
        parts.append(f"fmt_{params.fetch_format}")
    if params.start_date:
        parts.append(f"start_{str(params.start_date).replace(':', '-').replace(' ', '_')}")
    if params.end_date:
        parts.append(f"end_{str(params.end_date).replace(':', '-').replace(' ', '_')}")
    if params.typecode:
        parts.append(f"type_{params.typecode}")

    base_prefix = "_".join(parts)
    hash_suffix = hashlib.md5(base_prefix.encode('utf-8')).hexdigest()[:6]
    return f"{base_prefix}_{hash_suffix}"
