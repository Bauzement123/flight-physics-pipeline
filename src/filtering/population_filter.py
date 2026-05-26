"""
Module 1.1: Population Corridor Filter
Filters the master flights population CSV/Parquet registry into targeted, route-specific subsets
and saves them as lightweight Parquet lists in the central flight lists directory.
"""
import argparse
import pandas as pd
import logging
from pathlib import Path

from src.common.config import FLIGHT_REGISTRY_DIR, FLIGHT_LISTS_DIR

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

def filter_population(
    file_path: str,
    out_dir: str,
    start_date: str = None,
    end_date: str = None,
    typecode: str = None,
    origin: str = None,
    dest: str = None
):
    logging.info(f"Loading master file from: {file_path}")
    
    # Check if file exists
    if not Path(file_path).exists():
        logging.error(f"File not found: {file_path}")
        return

    # Build filename parts dynamically based on filter parameters
    name_parts = []
    if typecode:
        name_parts.append(typecode)
    
    if origin or dest:
        orig_str = origin if origin else "Any"
        dest_str = dest if dest else "Any"
        name_parts.append(f"{orig_str}-{dest_str}")
        
    if not name_parts:
        name_parts.append("AllFlights")

    # Add temporal string
    date_str = ""
    if start_date or end_date:
        sd = start_date.replace('-', '') if start_date else "Start"
        ed = end_date.replace('-', '') if end_date else "End"
        date_str = f"_{sd}-{ed}"
        
    out_filename = f"{'_'.join(name_parts)}{date_str}.parquet"
    out_path = Path(out_dir) / out_filename

    # If the output file already exists, raise an error to allow skipping in orchestrators
    if out_path.exists():
        raise FileExistsError(f"Filtered file already exists: {out_path}.")
    
    # Load the registry database
    if file_path.endswith('.parquet'):
        df = pd.read_parquet(file_path)
        initial_count = len(df)
        logging.info(f"Loaded {initial_count:,} rows from Parquet registry.")
        # Ensure tz-awareness
        if 'firstseen' in df.columns and df['firstseen'].dt.tz is None:
            df['firstseen'] = df['firstseen'].dt.tz_localize('UTC')
        if 'lastseen' in df.columns and df['lastseen'].dt.tz is None:
            df['lastseen'] = df['lastseen'].dt.tz_localize('UTC')
    else:
        df = pd.read_csv(file_path)
        initial_count = len(df)
        logging.info(f"Loaded {initial_count:,} rows from CSV registry.")
        # Convert timestamp columns to datetime objects
        if 'firstseen' in df.columns:
            df['firstseen'] = pd.to_datetime(df['firstseen'], utc=True)
        if 'lastseen' in df.columns:
            df['lastseen'] = pd.to_datetime(df['lastseen'], utc=True)

    # Apply filters iteratively
    df_filtered = df.copy()

    if start_date:
        start_dt = pd.to_datetime(start_date, utc=True)
        df_filtered = df_filtered[df_filtered['firstseen'] >= start_dt]
        logging.info(f"Filtered by start date >= {start_date}. Remaining: {len(df_filtered):,}")

    if end_date:
        end_dt_str = f"{end_date} 23:59:59" if len(end_date) <= 10 else end_date
        end_dt = pd.to_datetime(end_dt_str, utc=True)
        df_filtered = df_filtered[df_filtered['firstseen'] <= end_dt]
        logging.info(f"Filtered by end date <= {end_dt_str}. Remaining: {len(df_filtered):,}")

    if typecode:
        df_filtered = df_filtered[df_filtered['typecode'] == typecode]
        logging.info(f"Filtered by typecode == {typecode}. Remaining: {len(df_filtered):,}")

    if origin:
        orig_col = 'estdepartureairport' if 'estdepartureairport' in df.columns else 'estdepatureairport'
        df_filtered = df_filtered[df_filtered[orig_col] == origin]
        logging.info(f"Filtered by origin == {origin}. Remaining: {len(df_filtered):,}")

    if dest:
        df_filtered = df_filtered[df_filtered['estarrivalairport'] == dest]
        logging.info(f"Filtered by destination == {dest}. Remaining: {len(df_filtered):,}")

    if df_filtered.empty:
        logging.warning("Filter resulted in 0 flights. No file will be saved.")
        return

    # Validate columns required for the OpenSky Trino queries
    req_cols = ['icao24', 'callsign', 'firstseen', 'lastseen']
    missing_cols = [c for c in req_cols if c not in df_filtered.columns]
    if missing_cols:
        logging.warning(f"Warning: The following required columns are missing for Trino fetching: {missing_cols}")

    # Save to Parquet
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    df_filtered.to_parquet(out_path, index=False)
    logging.info(f"Successfully saved {len(df_filtered):,} flights to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Filter Master Flight Population Registry")
    parser.add_argument("--csv", "--file", dest="file_path", default=str(FLIGHT_REGISTRY_DIR / "master_flights.parquet"), help="Path to the master CSV or Parquet registry")
    parser.add_argument("--out-dir", default=str(FLIGHT_LISTS_DIR), help="Output directory for sliced flight lists")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--typecode", help="Aircraft Typecode (e.g., B777)")
    parser.add_argument("--origin", help="Origin ICAO (e.g., EGLL)")
    parser.add_argument("--dest", help="Destination ICAO (e.g., KJFK)")

    args = parser.parse_args()
    
    try:
        filter_population(
            file_path=args.file_path,
            out_dir=args.out_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            typecode=args.typecode,
            origin=args.origin,
            dest=args.dest
        )
    except FileExistsError as e:
        logging.info(str(e))
