import argparse
import logging
from datetime import datetime
from pathlib import Path
import pandas as pd

from src.common.config import MASTER_FLIGHTS_DB_DIR, AIRCRAFT_DB_DIR, ALL_TARGET_FAMILIES
from src.common.utils import setup_file_logger

def find_latest_file(directory: Path, pattern: str) -> Path:
    """
    Finds the latest file matching pattern in directory recursively (based on modification time).
    """
    files = list(directory.rglob(pattern))
    if not files:
        return None
    # Sort files by modification time
    return max(files, key=lambda p: p.stat().st_mtime)

def main():
    setup_file_logger(log_filename="acquisition.log")
    
    parser = argparse.ArgumentParser(description="Merge Flight Population with Sliced Fleet Registry")
    parser.add_argument("--flights", help="Path to input flight population file (CSV or Parquet)")
    parser.add_argument("--fleet", help="Path to input enriched fleet file (CSV or Parquet)")
    parser.add_argument("--output", help="Path to write final merged parquet file")
    parser.add_argument("--skip-fleet-join", action="store_true", help="Output raw flights without merging fleet registry (produces unfiltered list)")
    parser.add_argument("--fleet-filter-typecodes", default=",".join(ALL_TARGET_FAMILIES), help="Comma-separated typecodes to keep after fleet join")
    
    args = parser.parse_args()
    
    # 1. Resolve Flights Path
    if args.flights:
        flights_path = Path(args.flights)
    else:
        # Auto-find latest ParentPopulation parquet file, fallback to CSV
        flights_path = find_latest_file(MASTER_FLIGHTS_DB_DIR, "ParentPopulation_*.parquet")
        if not flights_path:
            flights_path = find_latest_file(MASTER_FLIGHTS_DB_DIR, "ParentPopulation_*.csv")
            
    if not flights_path or not flights_path.exists():
        raise FileNotFoundError(f"Flight population file not found. Please specify it using --flights.")
        
    # 2. Resolve Fleet Path (only if not skipping fleet join)
    fleet_path = None
    if not args.skip_fleet_join:
        if args.fleet:
            fleet_path = Path(args.fleet)
        else:
            # Auto-find latest Enriched_Fleet parquet file, fallback to CSV
            fleet_path = find_latest_file(AIRCRAFT_DB_DIR, "*_Enriched_Fleet.parquet")
            if not fleet_path:
                fleet_path = find_latest_file(AIRCRAFT_DB_DIR, "*_Enriched_Fleet.csv")
                
        if not fleet_path or not fleet_path.exists():
            raise FileNotFoundError(f"Enriched fleet file not found. Please specify it using --fleet.")
        
    # 3. Resolve Output Path
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = MASTER_FLIGHTS_DB_DIR / "master_flights.parquet"
        
    logging.info(f"Using flights file: {flights_path}")
    if fleet_path:
        logging.info(f"Using fleet file: {fleet_path}")
    
    # 4. Load Datasets
    logging.info("Loading flight population...")
    if flights_path.suffix.lower() == ".parquet":
        df_flights = pd.read_parquet(flights_path)
    else:
        df_flights = pd.read_csv(flights_path, dtype=str)
        
    logging.info(f"Flights database size: {len(df_flights):,} rows")

    # 5. Handle Join or Skip Join
    if args.skip_fleet_join:
        logging.info("Skipping fleet join as requested. Output will contain empty 'typecode' and 'engines' columns.")
        df_merged = df_flights.copy()
        df_merged['typecode'] = None
        df_merged['engines'] = None
    else:
        logging.info("Loading fleet registry...")
        if fleet_path.suffix.lower() == ".parquet":
            df_fleet = pd.read_parquet(fleet_path)
        else:
            df_fleet = pd.read_csv(fleet_path, dtype=str)
            
        logging.info(f"Fleet database size: {len(df_fleet):,} unique aircraft")
        
        # Clean merge keys
        df_flights['icao24'] = df_flights['icao24'].astype(str).str.strip().str.lower()
        df_fleet['icao24'] = df_fleet['icao24'].astype(str).str.strip().str.lower()
        
        # Ensure typecode column in fleet is clean
        if 'typecode' in df_fleet.columns:
            df_fleet['typecode'] = df_fleet['typecode'].astype(str).str.strip().str.upper()
        
        # Perform Inner Join on icao24
        logging.info("Performing inner join on icao24...")
        df_merged = pd.merge(df_flights, df_fleet[['icao24', 'typecode', 'engines']], on='icao24', how='inner')
        
        # Filter by typecode
        typecode_filter = [t.strip().upper() for t in args.fleet_filter_typecodes.split(",") if t.strip()]
        if typecode_filter:
            initial_len = len(df_merged)
            df_merged = df_merged[df_merged['typecode'].astype(str).str.strip().str.upper().isin(typecode_filter)].copy()
            logging.info(f"Filtered by typecode: kept {len(df_merged):,} of {initial_len:,} flights matching typecodes.")
    
    # 7. Select & Order exactly the 16 columns matching master_flights.parquet
    desired_cols = [
        'icao24', 'firstseen', 'estdepartureairport', 'lastseen', 'estarrivalairport', 'callsign',
        'estdepartureairporthorizdistance', 'estdepartureairportvertdistance',
        'estarrivalairporthorizdistance', 'estarrivalairportvertdistance',
        'departureairportcandidatescount', 'arrivalairportcandidatescount',
        'otherdepartureairportcandidates', 'otherarrivalairportcandidates',
        'typecode', 'engines'
    ]
    
    # Filter to only keep columns that actually exist in the merged dataframe
    cols_to_use = [c for c in desired_cols if c in df_merged.columns]
    missing_cols = [c for c in desired_cols if c not in cols_to_use]
    if missing_cols:
        logging.warning(f"Missing expected columns in merge: {missing_cols}")
        for c in missing_cols:
            df_merged[c] = None
            
    df_final = df_merged[desired_cols]
    
    logging.info(f"Merge completed. Output contains {len(df_final):,} flights.")
    
    # 8. Save Output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info(f"Saving merged output to {out_path}...")
    if out_path.suffix.lower() == '.csv':
        df_final.to_csv(out_path, index=False)
    else:
        df_final.to_parquet(out_path, index=False)
        
    logging.info("Successfully completed master merger!")

if __name__ == "__main__":
    main()
