import pandas as pd
import numpy as np
from pathlib import Path
import logging
from src.common.utils import setup_file_logger
from src.common.registry_utils import load_clean_cohort
from src.core.fetching.helpers import build_flight_id
from src.common.config import BASE_DIR

import argparse

logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-registry', type=str, help='Path to merged registry')
    args = parser.parse_args()

    setup_file_logger(log_filename="calibration.log")
    logger.info("Starting Stage 6: Prefilter Value Verification")

    # 1. Load clean cohort
    if args.input_registry:
        df_clean = pd.read_parquet(args.input_registry)
    else:
        df_clean = load_clean_cohort(require_metrics=True)
        
    if df_clean.empty:
        logger.error("Clean cohort is empty.")
        return
        
    logger.info(f"Loaded {len(df_clean)} flights from clean cohort.")
    
    # 2. Map clean to canonical route for grouping
    def get_canon(fid):
        parts = str(fid).split('_')
        if len(parts) > 2:
            route_part = parts[2]
            if '-' in route_part:
                dep, arr = route_part.split('-', 1)
                return "-".join(sorted([dep[:2], arr[:2]]))
        return None
        
    df_clean['Canonical_Route'] = df_clean['flight_id'].apply(get_canon)
    
    # 3. Extract join keys from clean flight_id
    # Format: {icao24}_{callsign}_{dep}-{arr}_{YYYYMMDD_HHMM}
    logger.info("Extracting join keys from clean registry flight_ids...")
    df_clean['icao24'] = df_clean['flight_id'].str.split('_').str[0]
    # Handle the fact that some callsigns have underscores or missing?
    # Actually, split('_') might be tricky if callsign has an underscore. But standard is no underscore.
    df_clean['callsign'] = df_clean['flight_id'].str.split('_').str[1]
    route_part = df_clean['flight_id'].str.split('_').str[2]
    df_clean['estdepartureairport'] = route_part.str.split('-').str[0]
    df_clean['estarrivalairport'] = route_part.str.split('-').str[1]
    
    # 4. Load master flights
    master_path = BASE_DIR / "data" / "databases" / "master_flights" / "master_flights.parquet"
    logger.info("Loading master flights database for matching...")
    df_raw = pd.read_parquet(master_path, columns=[
        'icao24', 'callsign', 'estdepartureairport', 'estarrivalairport',
        'estdepartureairporthorizdistance', 'estdepartureairportvertdistance', 
        'estarrivalairporthorizdistance', 'estarrivalairportvertdistance'
    ])
    
    # Clean up raw columns for joining
    df_raw['callsign'] = df_raw['callsign'].astype(str).str.strip().replace('', 'UNK')
    df_raw['callsign'] = df_raw['callsign'].fillna('UNK')
    df_raw['estdepartureairport'] = df_raw['estdepartureairport'].astype(str).str.strip().replace('', 'UNK')
    df_raw['estarrivalairport'] = df_raw['estarrivalairport'].astype(str).str.strip().replace('', 'UNK')
    
    # Join datasets on the extracted indices
    join_keys = ['icao24', 'callsign', 'estdepartureairport', 'estarrivalairport']
    
    # Drop duplicates in raw to avoid exploding the join (if a plane flies the same route twice)
    df_raw = df_raw.drop_duplicates(subset=join_keys, keep='last')
    
    df_joined = df_clean.merge(df_raw, on=join_keys, how='inner')
    logger.info(f"Successfully joined {len(df_joined)} flights between clean registry and master database.")
    
    # 5. Compute Deltas (Clean Engine Value - Raw OpenSky Value)
    metrics_map = {
        'dep_horiz': ('metric_dep_horiz_dist_m', 'estdepartureairporthorizdistance'),
        'dep_vert': ('metric_dep_vert_dist_m', 'estdepartureairportvertdistance'),
        'arr_horiz': ('metric_arr_horiz_dist_m', 'estarrivalairporthorizdistance'),
        'arr_vert': ('metric_arr_vert_dist_m', 'estarrivalairportvertdistance'),
    }
    
    for prefix, (clean_col, raw_col) in metrics_map.items():
        if clean_col in df_joined.columns and raw_col in df_joined.columns:
            df_joined[f'delta_{prefix}'] = df_joined[clean_col] - df_joined[raw_col]
            
    # 6. Group by Canonical Route and compute Median Absolute Error (MedAE) and Median Delta
    results = []
    
    # Add Global Average
    global_res = {'Canonical_Route': 'GLOBAL_AVERAGE', 'Count': len(df_joined)}
    for prefix in metrics_map.keys():
        delta_col = f'delta_{prefix}'
        if delta_col in df_joined.columns:
            global_res[f'{prefix}_MedAE'] = df_joined[delta_col].abs().median()
            global_res[f'{prefix}_MedianBias'] = df_joined[delta_col].median()
    results.append(global_res)
    
    # Add Regional Averages (for routes with >= 10 flights)
    route_counts = df_joined['Canonical_Route'].value_counts()
    valid_routes = route_counts[route_counts >= 10].index
    
    for route in valid_routes:
        route_df = df_joined[df_joined['Canonical_Route'] == route]
        res = {'Canonical_Route': route, 'Count': len(route_df)}
        for prefix in metrics_map.keys():
            delta_col = f'delta_{prefix}'
            if delta_col in route_df.columns:
                res[f'{prefix}_MedAE'] = route_df[delta_col].abs().median()
                res[f'{prefix}_MedianBias'] = route_df[delta_col].median()
        results.append(res)
        
    final_df = pd.DataFrame(results)
    
    out_dir = Path("data/calibration/PostFilter_callibration/stage6")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "stage6_prefilter_verification.csv"
    final_df.to_csv(out_file, index=False)
    
    logger.info(f"Stage 6 complete. Results saved to {out_file}")
    
    # Print summary to console
    print("\n=== GLOBAL VERIFICATION SUMMARY ===")
    print(final_df[final_df['Canonical_Route'] == 'GLOBAL_AVERAGE'].to_string(index=False))
    print("\n=== TOP 5 HIGHEST DISCREPANCY REGIONS (Arr Horiz MedAE) ===")
    if 'arr_horiz_MedAE' in final_df.columns:
        print(final_df[final_df['Canonical_Route'] != 'GLOBAL_AVERAGE'].sort_values('arr_horiz_MedAE', ascending=False).head(5)[['Canonical_Route', 'Count', 'arr_horiz_MedAE', 'arr_horiz_MedianBias']].to_string(index=False))

if __name__ == "__main__":
    main()
