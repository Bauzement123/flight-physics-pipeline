import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from sqlalchemy import select, or_

# Attempt import from pyopensky (assumed to be in environment)
try:
    from pyopensky.trino import Trino
    from pyopensky.schema import FlightsData4
except ImportError as e:
    # Fallback placeholder to allow code compilation and local mocking/testing
    Trino = None
    FlightsData4 = None
    logging.warning(f"Could not import pyopensky modules: {e}. trino_client calls will fail.")

from src.common.config import (
    MASTER_FLIGHTS_DB_DIR, 
    DEFAULT_AIRPORT_PREFIXES, 
    AIRPORTS_CACHE_PATH, 
    EUR_LAT_MIN, 
    EUR_LAT_MAX, 
    EUR_LON_MIN, 
    EUR_LON_MAX
)
from src.common.utils import setup_file_logger

def fetch_daily_flights_with_backoff(trino_client, query, day_str, max_retries=10):
    """
    Executes the Trino daily flights query with exponential backoff on failure.
    """
    import time
    for attempt in range(max_retries):
        try:
            logging.info(f"Querying Trino partition day={day_str} (Attempt {attempt + 1}/{max_retries})...")
            df_daily = trino_client.query(query)
            logging.info(f"Retrieved {len(df_daily)} records for {day_str}.")
            return df_daily
        except Exception as e:
            wait_time = 2 ** attempt
            logging.warning(f"Trino query failed for partition {day_str} on attempt {attempt + 1}: {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)
            
    logging.error(f"Max retries ({max_retries}) reached. Trino query failed permanently for partition {day_str}.")
    return None

def fetch_daily_flights(trino_client, day_str, dep_prefixes, arr_prefixes):
    """
    Queries Trino FlightsData4 for a single day, filtering departure and arrival
    airports by the starting letters in prefixes.
    """
    if Trino is None or FlightsData4 is None:
        raise RuntimeError("pyopensky is required to execute Trino database queries.")

    query = (
        select(FlightsData4)
        .with_only_columns(
            FlightsData4.icao24,
            FlightsData4.firstseen,
            FlightsData4.estdepartureairport,
            FlightsData4.lastseen,
            FlightsData4.estarrivalairport,
            FlightsData4.callsign,
            FlightsData4.estdepartureairporthorizdistance,
            FlightsData4.estdepartureairportvertdistance,            
            FlightsData4.estarrivalairporthorizdistance,
            FlightsData4.estarrivalairportvertdistance,
            FlightsData4.departureairportcandidatescount,
            FlightsData4.arrivalairportcandidatescount,
            FlightsData4.otherdepartureairportcandidates,
            FlightsData4.otherarrivalairportcandidates,
        )
        .where(FlightsData4.day == day_str)
    )

    # Conditionally apply departure and arrival airport filters
    if dep_prefixes:
        query = query.where(or_(*(FlightsData4.estdepartureairport.startswith(p) for p in dep_prefixes)))
    if arr_prefixes:
        query = query.where(or_(*(FlightsData4.estarrivalairport.startswith(p) for p in arr_prefixes)))

    return fetch_daily_flights_with_backoff(trino_client, query, day_str)

def apply_geographic_bbox_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filters flights in df using coordinates from AIRPORTS_CACHE_PATH and the
    European bounding box limits in config.py.
    """
    if not AIRPORTS_CACHE_PATH.exists():
        logging.warning(f"Airport coordinates cache not found at {AIRPORTS_CACHE_PATH}. Bounding box filtering skipped.")
        return df

    import json
    import numpy as np
    try:
        with open(AIRPORTS_CACHE_PATH, "r", encoding="utf-8") as f:
            airports_db = json.load(f)
    except Exception as e:
        logging.error(f"Failed to load airport coordinate cache: {e}. Bounding box filtering skipped.")
        return df

    initial_len = len(df)
    
    # Map coordinates
    dep_lats = df['estdepartureairport'].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    dep_lons = df['estdepartureairport'].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))
    arr_lats = df['estarrivalairport'].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    arr_lons = df['estarrivalairport'].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))

    in_box_mask = (
        (dep_lats.between(EUR_LAT_MIN, EUR_LAT_MAX)) &
        (dep_lons.between(EUR_LON_MIN, EUR_LON_MAX)) &
        (arr_lats.between(EUR_LAT_MIN, EUR_LAT_MAX)) &
        (arr_lons.between(EUR_LON_MIN, EUR_LON_MAX))
    )
    
    df_filtered = df[in_box_mask].copy()
    dropped = initial_len - len(df_filtered)
    logging.info(f"Geographic Bounding Box Filter complete. Dropped {dropped:,} of {initial_len:,} flights. Remaining: {len(df_filtered):,}")
    return df_filtered

def build_master_population(
    start_date: datetime,
    end_date: datetime,
    dep_prefixes: list,
    arr_prefixes: list,
    output_path: Path,
    apply_bbox: bool = False,
    resume: bool = False
):
    """
    Loops daily from start_date to end_date to download flight data,
    uses intermediate parquets as daily cache for crash resilience and resumption,
    deduplicates on (icao24, firstseen), and saves the final file if all days succeed.
    """
    logging.info(f"Starting master population fetch: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    
    # Define cache directories based on output_path or centralized database dir
    cache_dir = MASTER_FLIGHTS_DB_DIR / "daily_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Unique prefix key strings to make caches param-specific
    dep_key = "-".join(sorted(dep_prefixes)) if dep_prefixes else "ALL"
    arr_key = "-".join(sorted(arr_prefixes)) if arr_prefixes else "ALL"
    
    trino_client = None
    daily_dfs = []
    
    successful_days = []
    failed_days = []
    cache_hits = []
    
    current_date = start_date
    total_days = (end_date - start_date).days + 1
    
    while current_date <= end_date:
        day_str = current_date.strftime('%Y-%m-%d')
        cache_file = cache_dir / f"{day_str}_dep_{dep_key}_arr_{arr_key}.parquet"
        
        # 1. Cache hit logic
        if resume and cache_file.exists():
            try:
                logging.info(f"   -> Cache Hit: Loading partition {day_str} from {cache_file.name}")
                df_day = pd.read_parquet(cache_file)
                daily_dfs.append(df_day)
                cache_hits.append(day_str)
                current_date += timedelta(days=1)
                continue
            except Exception as e:
                logging.warning(f"   -> Cache corrupted or unreadable for {day_str}: {e}. Re-fetching...")
                
        # 2. Cache miss: Fetch from Trino
        if trino_client is None:
            trino_client = Trino()
            
        try:
            df_day = fetch_daily_flights(trino_client, day_str, dep_prefixes, arr_prefixes)
            if df_day is not None:
                # Save daily slice immediately
                df_day.to_parquet(cache_file, index=False)
                daily_dfs.append(df_day)
                successful_days.append(day_str)
            else:
                # Mark as failed if fetch returned None (exhausted retries)
                failed_days.append(day_str)
                logging.error(f"Permanent failure fetching data for {day_str}. Moving to next day.")
        except Exception as e:
            failed_days.append(day_str)
            logging.error(f"Unexpected error querying data for partition {day_str}: {e}. Moving to next day.")
            
        current_date += timedelta(days=1)
        
    # Print execution summary
    logging.info("\n" + "="*50)
    logging.info("INGESTION RUN SUMMARY")
    logging.info("="*50)
    logging.info(f"Total days processed: {total_days}")
    logging.info(f"  - Loaded from cache (Hits): {len(cache_hits)}")
    logging.info(f"  - Successfully fetched (New): {len(successful_days)}")
    logging.info(f"  - Failed partitions: {len(failed_days)}")
    if failed_days:
        logging.error(f"    Failed dates list: {failed_days}")
    logging.info("="*50 + "\n")
    
    # 3. Handle errors and gate saving
    if failed_days:
        # Save failure info to JSON for external tools/checkers
        import json
        failed_log_path = MASTER_FLIGHTS_DB_DIR / "failed_dates.json"
        try:
            with open(failed_log_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "failed_dates": failed_days,
                    "parameters": {"dep_prefixes": dep_prefixes, "arr_prefixes": arr_prefixes},
                    "timestamp": datetime.now().isoformat()
                }, f, indent=4)
            logging.info(f"Failed dates logged to {failed_log_path}")
        except Exception as e:
            logging.error(f"Failed to write failed dates JSON: {e}")
            
        logging.critical("CRITICAL: Some partitions failed to download. Gating final concat/save.")
        logging.critical("Please run the script again with the '--resume' flag once network/Trino access is restored.")
        import sys
        sys.exit(1)
        
    if not daily_dfs:
        logging.warning("No flights retrieved for the specified dates and prefixes.")
        return
        
    logging.info("Combining all daily partitions...")
    df_all = pd.concat(daily_dfs, ignore_index=True)
    
    # Deduplicate
    initial_len = len(df_all)
    df_all = df_all.drop_duplicates(subset=['icao24', 'firstseen'], keep='first')
    logging.info(f"Deduplication: dropped {initial_len - len(df_all)} duplicates. Remaining: {len(df_all)}")
    
    if apply_bbox:
        df_all = apply_geographic_bbox_filter(df_all)

    # Save output (Support both CSV and Parquet depending on suffix)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == '.parquet':
        df_all.to_parquet(output_path, index=False)
    else:
        df_all.to_csv(output_path, index=False)
    
    # Clear any previous failed_dates.json log on successful complete
    failed_log_path = MASTER_FLIGHTS_DB_DIR / "failed_dates.json"
    if failed_log_path.exists():
        try:
            failed_log_path.unlink()
        except OSError:
            pass
            
    logging.info(f"SUCCESS: Saved unified master population ({len(df_all):,} flights) to: {output_path}")

    # 4. Resolve coordinates for all unique airports in this master population
    try:
        logging.info("Building deduplicated list of airports from master population...")
        dep_airports = df_all['estdepartureairport'].dropna().unique()
        arr_airports = df_all['estarrivalairport'].dropna().unique()
        unique_airports = list(set(dep_airports) | set(arr_airports))
        # Filter to valid 4-letter ICAOs
        unique_airports = [a for a in unique_airports if isinstance(a, str) and len(a.strip()) == 4]
        
        logging.info(f"Found {len(unique_airports)} unique airports. Auto-enriching coordinates cache...")
        from src.analysis.verification.summarize_population import resolve_airport_coordinates
        resolve_airport_coordinates(unique_airports)
    except Exception as e:
        logging.error(f"Failed to auto-resolve airport coordinates: {e}")

if __name__ == "__main__":
    setup_file_logger(log_filename="acquisition.log")
    parser = argparse.ArgumentParser(description="Build Flight Master Population from Trino")
    parser.add_argument("--start-date", default="2025-01-01", help="Inclusive start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default="2025-01-31", help="Inclusive end date (YYYY-MM-DD)")
    parser.add_argument("--dep_prefixes", default="", help="Comma-separated airport ICAO initials, empty = no filter")
    parser.add_argument("--arr_prefixes", default="", help="Comma-separated airport ICAO initials, empty = no filter")
    parser.add_argument("--apply-bbox-filter", action="store_true", help="Apply European lat/lon bounding box filter after Trino fetch")
    parser.add_argument("--resume", action="store_true", help="Resume fetch using daily cache files, skipping completed days")
    parser.add_argument("--output", help="Path to save output (defaults to data/databases/master_flights/master_flights.parquet)")

    args = parser.parse_args()
    
    start_dt = datetime.strptime(args.start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(args.end_date, "%Y-%m-%d")
    dep_prefix_list = [p.strip().upper() for p in args.dep_prefixes.split(",") if p.strip()]
    arr_prefix_list = [p.strip().upper() for p in args.arr_prefixes.split(",") if p.strip()]
    
    if args.output:
        out_path = Path(args.output)
    else:
        dep_str = "-".join(dep_prefix_list) if dep_prefix_list else "ALL"
        arr_str = "-".join(arr_prefix_list) if arr_prefix_list else "ALL"
        out_path = MASTER_FLIGHTS_DB_DIR / f"ParentPopulation_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}_{dep_str}_to_{arr_str}.parquet"
        
    build_master_population(
        start_dt, 
        end_dt, 
        dep_prefix_list, 
        arr_prefix_list, 
        out_path, 
        apply_bbox=args.apply_bbox_filter,
        resume=args.resume
    )
