import os
import hashlib
import uuid
import time
import json
import dataclasses
from datetime import datetime, date
from typing import Any, Callable
import pandas as pd
from pathlib import Path
import logging

from src.common.config import (
    ROUTE_SUMMARY_PARQUET, TRAJECTORIES_DIR, BASE_DIR, LOGS_DIR,
    BACKOFF_MAX_RETRIES, BACKOFF_INITIAL_DELAY, BACKOFF_FACTOR, BACKOFF_MAX_DELAY,
    UNSUPPORTED_TYPECODE_FLAG, AIRPORTS_CACHE_PATH,
)
from src.common.exceptions import RetryError

logger = logging.getLogger(__name__)


def log_skipped_aircraft(
    flight_or_icao_id: str,
    typecode: Any,
    reason: str = "ERROR_FLAG: NaN/Missing or outside ALL_TARGET_FAMILIES"
) -> None:
    """
    Appends an entry to data/logs/skipped_aircraft.log when an aircraft or flight is skipped
    due to a NaN, missing, or non-target family typecode.
    Conforms to the global audit log format: id \t typecode \t reason.
    """
    skipped_log = LOGS_DIR / "skipped_aircraft.log"
    tc_str = str(typecode) if pd.notna(typecode) and typecode is not None and str(typecode).strip() != "" else UNSUPPORTED_TYPECODE_FLAG
    try:
        skipped_log.parent.mkdir(parents=True, exist_ok=True)
        with open(skipped_log, "a", encoding="utf-8") as f:
            f.write(f"{flight_or_icao_id}\t{tc_str}\t{reason}\n")
    except Exception as e:
        logger.error(f"Failed to append to skipped_aircraft.log for {flight_or_icao_id}: {e}")


def load_route_summary(summary_path: str | Path | None = None) -> pd.DataFrame:
    """
    Safely loads the RouteSummary file (supports parquet, pickle, csv) and returns a DataFrame.
    """
    if summary_path is None:
        summary_path = ROUTE_SUMMARY_PARQUET
        logger.info(f"Loading RouteSummary from default: {summary_path}")

    path = Path(summary_path)
    if not path.exists():
        logger.error(f"RouteSummary file not found at: {path}")
        return pd.DataFrame()

    suffix = path.suffix.lower()
    try:
        if suffix == '.parquet':
            df = pd.read_parquet(path)
        elif suffix in ('.pkl', '.pickle'):
            df = pd.read_pickle(path)
        elif suffix == '.csv':
            df = pd.read_csv(path)
        else:
            logger.warning(f"Unknown extension '{suffix}' for route summary path '{path}'. Trying multiple loaders...")
            try:
                df = pd.read_parquet(path)
            except Exception:
                try:
                    df = pd.read_pickle(path)
                except Exception:
                    df = pd.read_csv(path)
        return df
    except Exception as e:
        logger.error(f"Error loading RouteSummary file ({suffix}): {e}")
        return pd.DataFrame()


def haversine_distance_m(
    lat1: pd.Series | float,
    lon1: pd.Series | float,
    lat2: pd.Series | float,
    lon2: pd.Series | float,
) -> pd.Series | float:
    """
    Vectorized NumPy Haversine formula. Returns great-circle distance in metres.
    Accepts pd.Series or scalar floats. Replaces local haversine implementations.
    """
    import numpy as np
    R = 6_371_000.0  # volumetric mean Earth radius in metres
    lat1_r, lon1_r, lat2_r, lon2_r = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2.0) ** 2
    return 2.0 * R * np.arcsin(np.sqrt(a))


def resolve_airport_coordinates(unique_icaos: list) -> dict:
    """
    Cache-backed resolver: ICAO code -> {"lat": float, "lon": float}.
    Checks AIRPORTS_CACHE_PATH first; resolves missing entries via airportsdata
    (primary) or traffic.data.airports (fallback); writes updates back to cache.
    """
    airports_db = {}
    cache_updated = False

    # 1. Try to load from local JSON cache first
    if AIRPORTS_CACHE_PATH.exists():
        logger.info(f"Loading airport coordinates from local cache: {AIRPORTS_CACHE_PATH}")
        try:
            with open(AIRPORTS_CACHE_PATH, 'r', encoding='utf-8') as f:
                airports_db = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read airport coordinates cache: {e}")

    # 2. Check if there are any missing ICAOs
    missing_icaos = [icao for icao in unique_icaos if icao not in airports_db]

    if missing_icaos:
        logger.info(f"Found {len(missing_icaos)} airports missing from local coordinates cache. Resolving them...")
        
        resolved_new = {}
        try:
            import airportsdata
            logger.info("Resolving missing airport coordinates using airportsdata library...")
            data = airportsdata.load()
            for icao in missing_icaos:
                if icao in data:
                    resolved_new[icao] = {"lat": data[icao]["lat"], "lon": data[icao]["lon"]}
        except ImportError:
            logger.warning("airportsdata not installed. Falling back to traffic library airports database...")
            try:
                from traffic.data import airports
                traffic_db = {
                    row.icao: {"lat": row.latitude, "lon": row.longitude}
                    for row in airports.data.dropna(subset=['icao']).itertuples()
                }
                for icao in missing_icaos:
                    if icao in traffic_db:
                        resolved_new[icao] = traffic_db[icao]
            except ImportError:
                logger.error("Could not import airportsdata or traffic. Airport coordinates cannot be resolved.")

        if resolved_new:
            airports_db.update(resolved_new)
            cache_updated = True
            logger.info(f"Successfully resolved {len(resolved_new)} new airport coordinate entries.")

    if cache_updated:
        try:
            AIRPORTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(AIRPORTS_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(airports_db, f, indent=4)
            logger.info(f"Saved updated airport coordinates cache to: {AIRPORTS_CACHE_PATH}")
        except Exception as e:
            logger.error(f"Failed to write airport coordinates cache: {e}")

    return airports_db


def split_route_string(route_str: str) -> tuple[str, str]:
    """
    Splits a route string of format 'DEP -> ARR' into (departure, arrival).
    Returns ('UNK', 'UNK') on failure.
    """
    if not isinstance(route_str, str) or ' -> ' not in route_str:
        return 'UNK', 'UNK'
    try:
        dep, arr = route_str.split(' -> ', 1)
        return dep.strip(), arr.strip()
    except Exception:
        return 'UNK', 'UNK'


def _filter_ranks(
    df_summary: pd.DataFrame, lower: int | None, upper: int | None, specific_ranks: list[int] | None
) -> pd.DataFrame:
    """Filters RouteSummary DataFrame by specific rank indices or rank range bounds."""
    if specific_ranks:
        logger.info(f"Filtering by specific ranks: {specific_ranks}")
        return df_summary[df_summary['rank'].isin(specific_ranks)].copy()
    if lower is not None and upper is not None:
        logger.info(f"Filtering by rank corridor: {lower} to {upper}")
        return df_summary[(df_summary['rank'] >= lower) & (df_summary['rank'] <= upper)].copy()
    logger.error("No valid filtering criteria provided.")
    return pd.DataFrame()


def _resolve_roundtrip_routes(df_summary: pd.DataFrame, filtered_df: pd.DataFrame) -> pd.DataFrame:
    """Resolves and appends inverse return flight corridors for roundtrip format requests."""
    logger.info("Roundtrip requested. Resolving inverse return flight paths...")
    inv_routes = []
    for r_str in filtered_df['route']:
        dep, arr = split_route_string(r_str)
        if dep != 'UNK':
            inv_routes.append(f"{arr} -> {dep}")
    return_df = df_summary[df_summary['route'].isin(inv_routes)].copy()
    before_cnt = len(filtered_df)
    res = pd.concat([filtered_df, return_df]).drop_duplicates(subset=['route'])
    logger.info(f"Resolved {len(res) - before_cnt} return routes. Total target routes: {len(res)}")
    return res


def extract_target_routes(
    summary_path: str | Path = ROUTE_SUMMARY_PARQUET,
    lower: int | None = None,
    upper: int | None = None,
    specific_ranks: list[int] | None = None,
    fetch_format: str = 'oneway',
    min_distance: float | None = None,
) -> pd.DataFrame:
    """Loads RouteSummary, applies rank/distance filters, and returns target corridor codes."""
    logger.info(f"Loading route metadata from summary: {summary_path}")
    df_summary = load_route_summary(summary_path)
    if df_summary.empty:
        logger.error("RouteSummary is empty.")
        return pd.DataFrame()

    filtered_df = _filter_ranks(df_summary, lower, upper, specific_ranks)
    if filtered_df.empty:
        logger.warning("No routes found matching the criteria.")
        return pd.DataFrame()

    if min_distance is not None:
        if 'distance_m' in filtered_df.columns:
            before_cnt = len(filtered_df)
            filtered_df = filtered_df[filtered_df['distance_m'] >= min_distance * 1000.0].copy()
            logger.info(f"Filtered by min distance >= {min_distance} km. Excluded {before_cnt - len(filtered_df)} routes.")
        else:
            logger.warning("Column 'distance_m' not found in RouteSummary. Skipping distance filtering.")

    if fetch_format == 'roundtrip':
        filtered_df = _resolve_roundtrip_routes(df_summary, filtered_df)

    deps, arrs = zip(*[split_route_string(r) for r in filtered_df['route']])
    filtered_df['dep'], filtered_df['arr'] = deps, arrs

    if 'no_of_flights' not in filtered_df.columns:
        cnt_cols = [c for c in filtered_df.columns if 'count' in c.lower() or 'flight' in c.lower()]
        if cnt_cols:
            filtered_df.rename(columns={cnt_cols[0]: 'no_of_flights'}, inplace=True)
        else:
            logger.error("Could not locate flight count column in RouteSummary!")
            return pd.DataFrame()

    return filtered_df[['rank', 'dep', 'arr', 'no_of_flights']]


def generate_dataset_name(
    ranks=None,
    lower_rank=None,
    upper_rank=None,
    strategy=None,
    value=None,
    seed=None,
    fetch_format=None,
    start_date=None,
    end_date=None,
    typecode=None,
    min_distance=None
) -> str:
    """
    Generates a unique dataset name incorporating all CLI parameters to make it human-readable,
    plus a deterministic parameter hash suffix.
    e.g., ranks_1-5_strat_fixed_val_50_seed_42_format_roundtrip_a9f1
    """
    parts = []

    # 1. Ranks / corridor part
    if ranks:
        if isinstance(ranks, list):
            ranks_str = "-".join(map(str, sorted(ranks)))
        else:
            ranks_str = str(ranks).replace(",", "-").replace(" ", "")
        parts.append(f"ranks_{ranks_str}")
    elif lower_rank is not None and upper_rank is not None:
        parts.append(f"ranks_{lower_rank}to{upper_rank}")
    else:
        parts.append("ranks_AllFlights")

    # 2. Strategy part
    if strategy is not None:
        parts.append(f"strat_{strategy}")

    # 3. Value part
    if value is not None:
        parts.append(f"val_{value}")

    # 4. Seed part
    if seed is not None:
        parts.append(f"seed_{seed}")

    # 5. Format part (oneway/roundtrip)
    if fetch_format is not None:
        parts.append(f"format_{fetch_format}")

    # 6. Start date part
    if start_date is not None:
        clean_start = str(start_date).replace(":", "-").replace(" ", "_")
        parts.append(f"start_{clean_start}")

    # 7. End date part
    if end_date is not None:
        clean_end = str(end_date).replace(":", "-").replace(" ", "_")
        parts.append(f"end_{clean_end}")

    # 8. Typecode part
    if typecode is not None:
        parts.append(f"type_{typecode}")

    # 9. Min distance part
    if min_distance is not None:
        parts.append(f"mindist_{min_distance}")

    base_prefix = "_".join(parts)

    # Compute a deterministic hash of the parameters to guarantee unique identification
    hash_suffix = hashlib.md5(base_prefix.encode('utf-8')).hexdigest()[:6]

    dataset_name = f"{base_prefix}_{hash_suffix}"
    return dataset_name


def setup_file_logger(
    out_dir: Path | str | None = None,
    log_filename: str = "extraction.log",
) -> logging.FileHandler:
    """
    Adds a FileHandler to the root logger to mirror console output to a log file.

    If ``out_dir`` is provided, writes the log into that directory instead of
    the global ``LOGS_DIR``.  Also ensures the root logger level is set to INFO
    and a single StreamHandler is present for console logging.

    Calling this function multiple times with the same log file is idempotent:
    duplicate handlers are never added.
    """
    # Handle legacy case where log_filename was passed as first positional argument
    if isinstance(out_dir, str) and out_dir.endswith(".log"):
        log_filename = out_dir
        out_dir = None

    target_dir = Path(out_dir) if out_dir is not None else LOGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    log_file = (target_dir / log_filename).resolve()

    root_logger = logging.getLogger()

    # Ensure root logger level is at least INFO
    if root_logger.level == logging.WARNING or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)

    # Check if handlers already exist.
    # IMPORTANT: logging.FileHandler is a subclass of logging.StreamHandler.
    # We must check FileHandler FIRST in this if/elif chain so that existing FileHandlers
    # are not mistakenly counted as console StreamHandlers.
    file_handler_exists = False
    stream_handler_exists = False

    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == log_file:
                    file_handler_exists = True
            except Exception:
                pass
        elif isinstance(handler, logging.StreamHandler):
            stream_handler_exists = True

    # Add StreamHandler for console output if missing
    if not stream_handler_exists:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s'))
        console_handler.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)

    # Add FileHandler if missing
    if not file_handler_exists:
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s - [%(name)s] - [%(levelname)s] - %(message)s'))
        file_handler.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
        return file_handler

    # Find and return the existing file handler
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == log_file:
                    return handler
            except Exception:
                pass


def update_global_registry(registry_file: Path, new_entries: list[dict[str, Any]]) -> None:
    """
    Appends new mapping entries to a global registry Parquet file.
    Deduplicates on flight_id to keep only the latest path and metrics.
    new_entries: list of dicts: [{"flight_id": str, "file_path": str, ...}, ...]
    """
    if not new_entries:
        return

    try:
        df_new = pd.DataFrame(new_entries)
        if registry_file.exists():
            df_reg = pd.read_parquet(registry_file)
            df_updated = pd.concat([df_reg, df_new]).drop_duplicates(subset=['flight_id'], keep='last')
        else:
            df_updated = df_new

        registry_file.parent.mkdir(parents=True, exist_ok=True)
        df_updated.to_parquet(registry_file, index=False)
        logger.info(f"Updated global registry {registry_file.name} with {len(new_entries)} entries.")
    except Exception as e:
        logger.error(f"Failed to update global registry {registry_file.name}: {e}")


def retry_backoff(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = BACKOFF_MAX_RETRIES,
    base_delay: float = BACKOFF_INITIAL_DELAY,
    factor: float = BACKOFF_FACTOR,
    max_delay: float = BACKOFF_MAX_DELAY,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    **kwargs: Any,
) -> Any:
    """
    Calls fn(*args, **kwargs) up to max_retries times with exponential back-off.
    Logs each failed attempt at WARNING via the root logger.
    Raises RetryError after all retries are exhausted.
    """
    delay = base_delay
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except exceptions as e:
            last_exc = e
            if attempt < max_retries:
                logger.warning(
                    f"Attempt {attempt}/{max_retries} failed for {getattr(fn, '__name__', str(fn))}: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                delay = min(delay * factor, max_delay)
            else:
                logger.error(
                    f"All {max_retries} attempts exhausted for {getattr(fn, '__name__', str(fn))}. Last error: {e}"
                )
    raise RetryError(f"Operation {getattr(fn, '__name__', str(fn))} failed after {max_retries} retries: {last_exc}") from last_exc


def to_project_relative(path: Path) -> str:
    """
    Returns a POSIX-style path relative to BASE_DIR.
    Raises ValueError if path is not under BASE_DIR.
    Used for every flight_id -> file_path entry written to GLOBAL_TRAJECTORY_REGISTRY.
    """
    p_res = Path(path).resolve()
    b_res = BASE_DIR.resolve()
    try:
        rel = p_res.relative_to(b_res)
        return rel.as_posix()
    except ValueError:
        # On Windows, try case-insensitive path comparison if standard relative_to fails
        p_str = p_res.as_posix()
        b_str = b_res.as_posix()
        if p_str.lower().startswith(b_str.lower().rstrip('/') + '/'):
            rel_str = p_str[len(b_str.rstrip('/')) + 1:]
            return rel_str
        raise ValueError(f"Path {path} is not under BASE_DIR ({BASE_DIR})")


class _DataclassJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


def write_json_dataclass(path: Path, obj: Any) -> None:
    """
    Serializes obj (dataclass or dict) to JSON at path.
    Handles Path, datetime, and dataclass instances in the custom encoder.
    Writes atomically via a sibling temp file + os.replace().
    Creates parent directories if missing.
    Propagates exceptions to caller — fetching callers must catch and log as non-fatal.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via temp file in the same directory
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                data = dataclasses.asdict(obj)
            else:
                data = obj
            json.dump(data, f, cls=_DataclassJSONEncoder, indent=2)
        os.replace(temp_path, path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise
