import os
import argparse
import logging
from datetime import datetime
from pathlib import Path
import pandas as pd



from src.common.config import AIRCRAFT_DB_DIR, ALL_TARGET_FAMILIES, DEFAULT_AIRCRAFT_DB_PATH, DEFAULT_OPENAIRFRAMES_PATH, is_supported_typecode
from src.common.utils import setup_file_logger, log_skipped_aircraft

# Renaming maps for unifying database schemas
OPENAIRFRAMES_RENAME_MAP = {
    'icao': 'icao24',
    'r': 'registration',
    't': 'typecode',
    'ownOp': 'operator',
    'year': 'built',
    'desc': 'model',
    'aircraft_category': 'categoryDescription'
}

AIRCRAFT_DB_RENAME_MAP = {
    'icao24': 'icao24',
    'registration': 'registration',
    'typecode': 'typecode',
    'operator': 'operator',
    'built': 'built',
    'model': 'model',
    'categoryDescription': 'categoryDescription',
    'engines': 'engines'
}

def process_database_chunks(
    file_path: Path,
    rename_map: dict,
    target_typecodes: list,
    chunk_size: int = 250000,
    quotechar: str = None,
    quoting: int = None
) -> pd.DataFrame:
    """
    Streams a CSV/Gzip database in chunks, renames columns to a unified schema,
    filters by typecodes, and deduplicates based on icao24.
    """
    logging.info(f"Slicing database from: {file_path}")
    if not file_path.exists():
        raise FileNotFoundError(f"Database file not found at: {file_path}")

    filtered_chunks = []
    total_rows = 0
    seen_icaos = set()

    # Set up read_csv arguments dynamically based on file settings
    read_args = {
        'filepath_or_buffer': file_path,
        'usecols': list(rename_map.keys()),
        'dtype': str,
        'chunksize': chunk_size
    }
    if file_path.suffix == '.gz':
        read_args['compression'] = 'gzip'
    if quotechar is not None:
        read_args['quotechar'] = quotechar
    if quoting is not None:
        read_args['quoting'] = quoting

    chunk_iterator = pd.read_csv(**read_args)

    for i, chunk in enumerate(chunk_iterator):
        total_rows += len(chunk)
        
        # Rename columns to unified schema
        chunk = chunk.rename(columns=rename_map)
        
        # Clean typecode whitespace
        chunk['typecode'] = chunk['typecode'].astype(str).str.strip().str.upper()
        
        # Filter for the target typecodes
        chunk_filtered = chunk[chunk['typecode'].isin(target_typecodes)]
        if not chunk_filtered.empty:
            # Clean icao24 values (ensure lowercase for matching consistency)
            chunk_filtered = chunk_filtered.copy()
            chunk_filtered['icao24'] = chunk_filtered['icao24'].astype(str).str.strip().str.lower()
            
            # Deduplicate within the chunk
            chunk_filtered = chunk_filtered.drop_duplicates(subset=['icao24'], keep='first')
            
            # Filter out already seen ICAOs to optimize memory usage
            chunk_filtered = chunk_filtered[~chunk_filtered['icao24'].isin(seen_icaos)]
            
            if not chunk_filtered.empty:
                seen_icaos.update(chunk_filtered['icao24'])
                filtered_chunks.append(chunk_filtered)

        if i % 4 == 0:
            current_matches = sum(len(c) for c in filtered_chunks)
            logging.info(f"Processed {total_rows:,} rows... Found {current_matches:,} unique matches so far.")

    logging.info(f"Finished reading. Total rows processed: {total_rows:,}")
    
    if not filtered_chunks:
        return pd.DataFrame()

    # Combine chunks
    df_combined = pd.concat(filtered_chunks, ignore_index=True)

    # Final deduplication check
    initial_len = len(df_combined)
    df_combined = df_combined.drop_duplicates(subset=['icao24'], keep='first')
    logging.info(f"Deduplication: dropped {initial_len - len(df_combined)} duplicate airframes. Unique matches: {len(df_combined)}")
    
    return df_combined

def slice_openairframes_db(input_gz_path: Path, target_typecodes: list, chunk_size: int = 250000) -> pd.DataFrame:
    """
    Slices the OpenAirframes database in chunks, filtering for specific aircraft typecodes.
    """
    # quoting=3 represents csv.QUOTE_NONE (handles malformed double quotes robustly)
    return process_database_chunks(
        file_path=input_gz_path,
        rename_map=OPENAIRFRAMES_RENAME_MAP,
        target_typecodes=target_typecodes,
        chunk_size=chunk_size,
        quoting=3
    )

def slice_aircraft_db(input_csv_path: Path, target_typecodes: list, chunk_size: int = 250000) -> pd.DataFrame:
    """
    Slices the local OpenSky aircraft database CSV in chunks, filtering for target typecodes.
    """
    return process_database_chunks(
        file_path=input_csv_path,
        rename_map=AIRCRAFT_DB_RENAME_MAP,
        target_typecodes=target_typecodes,
        chunk_size=chunk_size,
        quotechar="'"
    )

def load_aircraft_db_from_traffic(target_typecodes: list) -> pd.DataFrame:
    """
    Loads and filters the aircraft database from the traffic library as a fallback.
    """
    try:
        from traffic.data import aircraft
    except ImportError as e:
        logging.warning(f"traffic library is not available: {e}. Skipping fallback.")
        return pd.DataFrame()

    logging.info("Loading aircraft database from traffic library...")
    try:
        opensky_df = aircraft.data
        opensky_df['typecode'] = opensky_df['typecode'].astype(str).str.strip().str.upper()
        df_filtered = opensky_df[opensky_df['typecode'].isin(target_typecodes)].copy()
        
        # Rename/align columns to unified schema
        df_filtered = df_filtered.rename(columns=AIRCRAFT_DB_RENAME_MAP)
        
        # Keep only the columns present in unified schema
        cols_to_keep = [col for col in AIRCRAFT_DB_RENAME_MAP.values() if col in df_filtered.columns]
        df_filtered = df_filtered[cols_to_keep]
        
        df_filtered['icao24'] = df_filtered['icao24'].astype(str).str.strip().str.lower()
        df_filtered = df_filtered.drop_duplicates(subset=['icao24'], keep='first')
        return df_filtered
    except Exception as e:
        logging.error(f"Failed to load/filter aircraft database from traffic: {e}")
        return pd.DataFrame()

def merge_and_enrich_fleets(df_openairframes: pd.DataFrame, df_opensky: pd.DataFrame) -> pd.DataFrame:
    """
    Combines the OpenAirframes and OpenSky fleet databases using a full outer merge on icao24,
    coalescing metadata columns (preferring OpenAirframes values where present).
    """
    if df_openairframes.empty and df_opensky.empty:
        logging.warning("Both fleet DataFrames are empty.")
        return pd.DataFrame()
    if df_openairframes.empty:
        logging.info("OpenAirframes dataset is empty. Using OpenSky data only.")
        return df_opensky
    if df_opensky.empty:
        logging.info("OpenSky dataset is empty. Using OpenAirframes data only.")
        if 'engines' not in df_openairframes.columns:
            df_openairframes['engines'] = None
        return df_openairframes

    logging.info(f"Merging OpenAirframes ({len(df_openairframes)} records) and OpenSky ({len(df_opensky)} records)...")
    
    # Full outer merge on icao24
    df_combined = pd.merge(df_openairframes, df_opensky, on='icao24', how='outer', suffixes=('_oa', '_os'))
    
    # Coalesce metadata columns
    metadata_cols = ['registration', 'typecode', 'operator', 'built', 'model', 'categoryDescription']
    for col in metadata_cols:
        col_oa = f"{col}_oa"
        col_os = f"{col}_os"
        
        if col_oa in df_combined.columns and col_os in df_combined.columns:
            df_combined[col] = df_combined[col_oa].combine_first(df_combined[col_os])
            df_combined = df_combined.drop(columns=[col_oa, col_os])
        elif col_oa in df_combined.columns:
            df_combined[col] = df_combined[col_oa]
            df_combined = df_combined.drop(columns=[col_oa])
        elif col_os in df_combined.columns:
            df_combined[col] = df_combined[col_os]
            df_combined = df_combined.drop(columns=[col_os])
            
    # Handle engines column (only present in OpenSky)
    if 'engines_os' in df_combined.columns:
        df_combined['engines'] = df_combined['engines_os']
        df_combined = df_combined.drop(columns=['engines_os'])
    elif 'engines' not in df_combined.columns:
        df_combined['engines'] = None

    # Clean up any leftover columns
    leftovers = [c for c in df_combined.columns if c.endswith('_oa') or c.endswith('_os')]
    if leftovers:
        df_combined = df_combined.drop(columns=leftovers)

    logging.info(f"Merge complete. Combined dataset has {len(df_combined)} unique airframes.")
    return df_combined

def main():
    setup_file_logger(log_filename="acquisition.log")
    
    parser = argparse.ArgumentParser(description="Slice & Combine OpenAirframes and OpenSky Databases")
    parser.add_argument("--openairframes", default=str(DEFAULT_OPENAIRFRAMES_PATH), help="Path to openairframes_adsb_*.csv.gz file")
    parser.add_argument("--aircraft-db", default=str(DEFAULT_AIRCRAFT_DB_PATH), help="Path to OpenSky aircraft database CSV file")
    parser.add_argument("--typecodes", default=",".join(ALL_TARGET_FAMILIES), help="Comma-separated typecodes to filter")
    parser.add_argument("--output-dir", help="Directory to save output files (defaults to data/aircraft_db/)")
    parser.add_argument("--chunk-size", type=int, default=250000, help="Pandas chunk size for reading")

    args = parser.parse_args()

    target_typecodes = [t.strip().upper() for t in args.typecodes.split(",") if t.strip()]

    # 1. Process OpenAirframes Database
    df_openairframes = pd.DataFrame()
    openairframes_path = Path(args.openairframes)
    if openairframes_path.exists():
        try:
            df_openairframes = slice_openairframes_db(openairframes_path, target_typecodes, args.chunk_size)
        except Exception as e:
            logging.error(f"Error processing OpenAirframes database: {e}")
    else:
        logging.warning(f"OpenAirframes database not found at: {openairframes_path}")

    # 2. Process OpenSky Aircraft Database
    df_opensky = pd.DataFrame()
    aircraft_db_path = Path(args.aircraft_db)
    if aircraft_db_path.exists():
        try:
            df_opensky = slice_aircraft_db(aircraft_db_path, target_typecodes, args.chunk_size)
        except Exception as e:
            logging.error(f"Error processing local aircraft database: {e}")
    else:
        logging.info(f"Local aircraft database CSV not found at: {aircraft_db_path}")
        df_opensky = load_aircraft_db_from_traffic(target_typecodes)

    # 3. Combine Databases
    df_final = merge_and_enrich_fleets(df_openairframes, df_opensky)
    if not df_final.empty and 'typecode' in df_final.columns:
        valid_mask = df_final['typecode'].apply(is_supported_typecode)
        if not valid_mask.all():
            for _, bad_row in df_final[~valid_mask].iterrows():
                log_skipped_aircraft(str(bad_row.get('icao24', 'UNK')), bad_row.get('typecode'), "ERROR_FLAG: Dropped airframe in fleet_builder due to missing or non-target family typecode after merge")
            df_final = df_final[valid_mask].copy()

    # 4. Save Output
    if not df_final.empty:
        # Reorder columns to a clean layout
        desired_cols = ['icao24', 'registration', 'typecode', 'operator', 'built', 'model', 'categoryDescription', 'engines']
        cols_to_use = [c for c in desired_cols if c in df_final.columns]
        extra_cols = [c for c in df_final.columns if c not in cols_to_use]
        df_final = df_final[cols_to_use + extra_cols]

        out_dir = Path(args.output_dir) if args.output_dir else AIRCRAFT_DB_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = out_dir / f"{timestamp}_Enriched_Fleet.csv"
        parquet_path = out_dir / f"{timestamp}_Enriched_Fleet.parquet"

        df_final.to_csv(csv_path, index=False)
        df_final.to_parquet(parquet_path, index=False)
        
        logging.info(f"Successfully saved combined fleet database to:")
        logging.info(f"  CSV: {csv_path}")
        logging.info(f"  Parquet: {parquet_path}")
    else:
        logging.warning("No fleet data compiled. Output files were not saved.")

if __name__ == "__main__":
    main()
