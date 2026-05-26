"""
Module 1.2: OpenSky Trino Fetcher
Reads a filtered Parquet list of flights, checks the global trajectory registry cache,
queries the OpenSky Trino database for uncached trajectory waypoints, and saves a consolidated Parquet file.
"""
import argparse
import pandas as pd
import logging
import time
from pathlib import Path
import hashlib
import json
import os

from pyopensky.trino import Trino
from pyopensky.schema import StateVectorsData4
from sqlalchemy import select

from src.common.config import BASE_DIR, FLIGHT_REGISTRY_DIR
from src.common.utils import split_route_string, setup_file_logger, update_global_registry

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

def fetch_with_backoff(trino_client, query, max_retries=10):
    """Executes a Trino query with exponential backoff on failure."""
    for attempt in range(max_retries):
        try:
            df = trino_client.query(query)
            return df
        except Exception as e:
            wait_time = 2 ** attempt
            logging.warning(f"Query failed (Attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)
            
    logging.error("Max retries reached. Query failed permanently.")
    return None

def filter_flight_list(df: pd.DataFrame, start_date=None, end_date=None, **kwargs) -> pd.DataFrame:
    """
    Filters a flight list DataFrame.
    Filters by:
    - start_date (inclusive, matches firstseen): df['firstseen'] >= pd.to_datetime(start_date)
    - end_date (inclusive, matches firstseen): df['firstseen'] <= pd.to_datetime(end_date)
    - **kwargs: Matches column-value equality. Standard columns like typecode, estdepartureairport, etc.
    """
    if df.empty:
        return df
        
    filtered_df = df.copy()
    
    if start_date is not None:
        start_dt = pd.to_datetime(start_date)
        if start_dt.tzinfo is None:
            start_dt = start_dt.tz_localize('UTC')
        else:
            start_dt = start_dt.tz_convert('UTC')
            
        firstseen_dt = pd.to_datetime(filtered_df['firstseen'])
        if firstseen_dt.dt.tz is None:
            firstseen_dt = firstseen_dt.dt.localize('UTC')
        else:
            firstseen_dt = firstseen_dt.dt.tz_convert('UTC')
            
        filtered_df = filtered_df[firstseen_dt >= start_dt]
        
    if end_date is not None:
        end_dt = pd.to_datetime(end_date)
        if end_dt.tzinfo is None:
            end_dt = end_dt.tz_localize('UTC')
        else:
            end_dt = end_dt.tz_convert('UTC')
            
        firstseen_dt = pd.to_datetime(filtered_df['firstseen'])
        if firstseen_dt.dt.tz is None:
            firstseen_dt = firstseen_dt.dt.localize('UTC')
        else:
            firstseen_dt = firstseen_dt.dt.tz_convert('UTC')
            
        filtered_df = filtered_df[firstseen_dt <= end_dt]
        
    for col, value in kwargs.items():
        if value is not None:
            if col in filtered_df.columns:
                if col == 'typecode' and isinstance(value, str):
                    val_to_use = value.upper()
                else:
                    val_to_use = value
                filtered_df = filtered_df[filtered_df[col] == val_to_use]
            else:
                logging.warning(f"Column '{col}' not found in flight list DataFrame. Skipping this filter.")
                
    return filtered_df

def fetch_trajectories(
    input_list_path: str,
    out_dir: str,
    sample_size: int = None,
    seed: int = 42,
    start_date: str = None,
    end_date: str = None,
    typecode: str = None
):
    start_time = time.time()
    out_dir_path = Path(out_dir)
    setup_file_logger(out_dir_path)
    
    logging.info(f"Loading filtered flight list from: {input_list_path}")
    
    if not Path(input_list_path).exists():
        logging.error(f"File not found: {input_list_path}")
        return False

    # Read the target population list
    target_flights = pd.read_parquet(input_list_path)
    logging.info(f"Found {len(target_flights)} flights in the list.")

    # Apply date and typecode filtering in-memory
    target_flights = filter_flight_list(target_flights, start_date=start_date, end_date=end_date, typecode=typecode)
    logging.info(f"Found {len(target_flights)} flights in the list after applying filters.")

    if target_flights.empty:
        logging.warning("No flights left in the list after filtering. Skipping corridor.")
        return False

    # Sample a random subset if requested
    if sample_size and sample_size > 0:
        if sample_size < len(target_flights):
            target_flights = target_flights.sample(n=sample_size, random_state=seed)
            logging.info(f"Randomly sampling {len(target_flights)} flights using seed {seed}...")
        else:
            logging.info(f"Sample size ({sample_size}) is >= total flights. Fetching all {len(target_flights)}.")

    # Generate cohort hash based on input flight list (deterministic, before querying)
    input_flight_ids = []
    for _, row in target_flights.iterrows():
        dep = row.get('estdepartureairport', 'UNK')
        arr = row.get('estarrivalairport', 'UNK')
        fs_val = row.get('firstseen')
        fs_str = pd.to_datetime(fs_val).strftime('%Y%m%d_%H%M') if pd.notna(fs_val) else 'UNK'
        input_flight_ids.append(f"{row['icao24']}_{row.get('callsign', 'UNK')}_{dep}-{arr}_{fs_str}")
        
    cohort_hash = hashlib.md5("".join(sorted(input_flight_ids)).encode('utf-8')).hexdigest()[:6]
    
    # Define output files
    base_name = Path(input_list_path).stem
    raw_dir = out_dir_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    out_filename = f"{base_name}_{cohort_hash}_raw.parquet"
    out_path = raw_dir / out_filename
    manifest_filename = f"{base_name}_{cohort_hash}_manifest.json"
    manifest_path = out_dir_path / manifest_filename
    
    if out_path.exists() and manifest_path.exists():
        logging.info(f"Output file already exists: {out_path}")
        logging.info(f"Skipping fetch and loading existing data.")
        combined_df = pd.read_parquet(out_path)
        logging.info(f"Loaded {len(combined_df):,} waypoints from existing file.")
        duration = time.time() - start_time
        logging.info(f"Fetch process for {Path(input_list_path).name} completed (cached) in {duration:.2f} seconds.")
        return True

    # Load Global Registry Cache Map
    registry_file = FLIGHT_REGISTRY_DIR / "global_trajectory_registry.parquet"
    cached_flights = {}
    if registry_file.exists():
        try:
            df_reg = pd.read_parquet(registry_file)
            cached_flights = dict(zip(df_reg['flight_id'], df_reg['file_path']))
            logging.info(f"Loaded global registry index containing {len(cached_flights):,} flight paths.")
        except Exception as e:
            logging.error(f"Error loading global trajectory registry: {e}")

    # Initialize OpenSky Trino Connection (lazily initialized on first Trino query)
    trino = None
    
    all_trajectories = []
    new_registry_entries = []
    successful_fetches = 0
    
    # Store relative path of final file for global manifest index
    out_rel_path = out_path.resolve().relative_to(BASE_DIR).as_posix()

    for i, (index, row) in enumerate(target_flights.iterrows(), 1):
        icao24 = row.get('icao24')
        typecode = row.get('typecode')
        callsign = row.get('callsign')
        firstseen = row.get('firstseen')
        lastseen = row.get('lastseen')
        dep = row.get('estdepartureairport', 'UNK')
        arr = row.get('estarrivalairport', 'UNK')

        if pd.isna(icao24) or pd.isna(firstseen) or pd.isna(lastseen):
            logging.debug(f"Skipping row {index}: Missing ICAO24 or time bounds.")
            continue

        try:
            firstseen_dt = pd.to_datetime(firstseen)
            lastseen_dt = pd.to_datetime(lastseen)
            fs_str = firstseen_dt.strftime('%Y%m%d_%H%M')
            
            firstseen_hour = firstseen_dt.floor('h').tz_localize(None).to_pydatetime()
            lastseen_hour = lastseen_dt.ceil('h').tz_localize(None).to_pydatetime()
        except Exception as e:
            logging.error(f"Time parsing error for {icao24}: {e}")
            continue

        # Generate unique flight_id
        flight_id = f"{icao24}_{callsign}_{dep}-{arr}_{fs_str}"
        logging.info(f"[{i}/{len(target_flights)}] Processing {flight_id}...")

        # 1. Local Cache Check
        if flight_id in cached_flights:
            cached_file_path = BASE_DIR / cached_flights[flight_id]
            if cached_file_path.exists():
                try:
                    logging.info(f"   -> Cache Hit: Loading flight waypoints from {cached_flights[flight_id]}")
                    df_cached = pd.read_parquet(cached_file_path)
                    flight_df = df_cached[df_cached['flight_id'] == flight_id].copy()
                    
                    if not flight_df.empty:
                        all_trajectories.append(flight_df)
                        new_registry_entries.append({"flight_id": flight_id, "file_path": out_rel_path})
                        successful_fetches += 1
                        logging.info(f"   -> Success: Retrieved {len(flight_df)} waypoints locally.")
                        continue
                    else:
                        logging.warning(f"   -> Cached file exists, but flight_id '{flight_id}' not found inside it.")
                except Exception as e:
                    logging.error(f"   -> Cache read failure for {flight_id}: {e}. Falling back to Trino...")
            else:
                logging.warning(f"   -> Cache index pointed to {cached_flights[flight_id]}, but file does not exist. Querying Trino...")

        # 2. Query Trino (Cache Miss)
        if trino is None:
            logging.info("Initializing pyopensky Trino client...")
            trino = Trino()

        # Construct SQLAlchemy Query
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
                StateVectorsData4.onground
            )
            .where(StateVectorsData4.icao24 == icao24)
            .where(StateVectorsData4.hour >= firstseen_hour)
            .where(StateVectorsData4.hour <= lastseen_hour)
        )
        
        if pd.notna(callsign):
            query = query.where(StateVectorsData4.callsign == callsign)

        # Execute query with exponential backoff
        flight_df = fetch_with_backoff(trino, query)

        if flight_df is not None and not flight_df.empty:
            # Inject metadata columns
            flight_df['icao24'] = icao24
            flight_df['callsign'] = callsign
            flight_df['typecode'] = typecode
            flight_df['estdepartureairport'] = dep
            flight_df['estarrivalairport'] = arr
            flight_df['firstseen'] = firstseen_dt
            flight_df['lastseen'] = lastseen_dt
            flight_df['flight_id'] = flight_id
            
            all_trajectories.append(flight_df)
            new_registry_entries.append({"flight_id": flight_id, "file_path": out_rel_path})
            successful_fetches += 1
            logging.info(f"   -> Success: Retrieved {len(flight_df)} waypoints from Trino.")
        else:
            logging.warning(f"   -> No trajectory data found/returned for {icao24} via Trino.")

    # Combine and save
    if all_trajectories:
        logging.info(f"Concatenating {successful_fetches} trajectories...")
        combined_df = pd.concat(all_trajectories, ignore_index=True)
        
        # Save Parquet
        combined_df.to_parquet(out_path, index=False)
        logging.info(f"COMPLETE: Saved {len(combined_df):,} total waypoints to {out_path}")
        
        # Save JSON Manifest File
        fetched_flight_ids = sorted(combined_df['flight_id'].unique().tolist())
        manifest_data = {
            "source_list": base_name,
            "cohort_hash": cohort_hash,
            "total_flights_requested": len(target_flights),
            "total_flights_fetched": len(fetched_flight_ids),
            "total_waypoints": len(combined_df),
            "flight_ids": fetched_flight_ids
        }
        with open(manifest_path, 'w') as f:
            json.dump(manifest_data, f, indent=4)
        logging.info(f"Manifest file saved to {manifest_path}")
        # 3. Update Global Trajectory Registry
        update_global_registry(registry_file, new_registry_entries)
                
        duration = time.time() - start_time
        logging.info(f"Fetch process for {Path(input_list_path).name} completed in {duration:.2f} seconds ({duration/60:.2f} minutes).")
        return True
    else:
        logging.error("No trajectories were successfully retrieved. Nothing to save.")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Trajectories from OpenSky Trino")
    parser.add_argument("--input-list", required=True, help="Path to the filtered target list Parquet file")
    parser.add_argument("--out-dir", required=True, help="Output directory for raw trajectories")
    parser.add_argument("--sample-size", type=int, default=None, help="Number of random flights to sample")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic cohort sampling")
    parser.add_argument("--start-date", default=None, help="Start bounds of flight departure window (ISO format)")
    parser.add_argument("--end-date", default=None, help="End bounds of flight departure window (ISO format)")
    parser.add_argument("--typecode", default=None, help="Aircraft model code (e.g. B738, A320)")

    args = parser.parse_args()
    
    fetch_trajectories(
        input_list_path=args.input_list,
        out_dir=args.out_dir,
        sample_size=args.sample_size,
        seed=args.seed,
        start_date=args.start_date,
        end_date=args.end_date,
        typecode=args.typecode
    )
