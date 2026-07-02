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

    # Conditionally apply departure and arrival airport filters to avoid empty or_()
    # statements, which are deprecated and will fail in future SQLAlchemy versions.
    if dep_prefixes:
        query = query.where(or_(*(FlightsData4.estdepartureairport.startswith(p) for p in dep_prefixes)))
    if arr_prefixes:
        query = query.where(or_(*(FlightsData4.estarrivalairport.startswith(p) for p in arr_prefixes)))

    logging.info(f"Querying Trino partition day={day_str} with airport prefixes={dep_prefixes} and {arr_prefixes}...")
    df_daily = trino_client.query(query)
    logging.info(f"Retrieved {len(df_daily)} records for {day_str}.")
    return df_daily

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

def build_master_population(start_date: datetime, end_date: datetime, dep_prefixes: list, arr_prefixes: list, output_path: Path, apply_bbox: bool = False):
    """
    Loops daily from start_date to end_date to download flight data,
    deduplicates on (icao24, firstseen), and saves the final file.
    """
    logging.info(f"Starting master population fetch from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    
    trino_client = Trino()
    daily_dfs = []
    
    current_date = start_date
    while current_date <= end_date:
        day_str = current_date.strftime('%Y-%m-%d')
        try:
            df_day = fetch_daily_flights(trino_client, day_str, dep_prefixes, arr_prefixes)
            if not df_day.empty:
                daily_dfs.append(df_day)
        except Exception as e:
            logging.error(f"Error fetching data for partition {day_str}: {e}")
        
        current_date += timedelta(days=1)
        
    if not daily_dfs:
        logging.warning("No flights retrieved for the specified dates and prefixes.")
        return
        
    logging.info("Combining daily partitions...")
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
    
    logging.info(f"Successfully saved master population to: {output_path}")

if __name__ == "__main__":
    setup_file_logger(log_filename="acquisition.log")
    parser = argparse.ArgumentParser(description="Build Flight Master Population from Trino")
    parser.add_argument("--start-date", default="2025-01-01", help="Inclusive start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default="2025-01-31", help="Inclusive end date (YYYY-MM-DD)")
    parser.add_argument("--dep_prefixes", default="", help="Comma-separated airport ICAO initials, empty = no filter")
    parser.add_argument("--arr_prefixes", default="", help="Comma-separated airport ICAO initials, empty = no filter")
    parser.add_argument("--apply-bbox-filter", action="store_true", help="Apply European lat/lon bounding box filter after Trino fetch")
    parser.add_argument("--output", help="Path to save output (defaults to data/flight_registry/master_flights.parquet)")

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
        
    build_master_population(start_dt, end_dt, dep_prefix_list, arr_prefix_list, out_path, apply_bbox=args.apply_bbox_filter)
