"""
Reformat Route Summary Registry
Injects distance_km from filtered_routes_byDistance.csv into master_flights_RouteSummary.pkl.
Generates an unfiltered master_flights_RouteSummary_unfiltered.pkl and a filtered master_flights_RouteSummary.pkl.
"""

import pandas as pd
import pickle
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger(__name__)

def main():
    csv_path = Path("data/flight_registry/filtered_routes_byDistance.csv")
    pkl_path = Path("data/flight_registry/master_flights_RouteSummary.pkl")
    unfiltered_pkl_path = Path("data/flight_registry/master_flights_RouteSummary_unfiltered.pkl")
    
    if not csv_path.exists():
        logger.error(f"New CSV file not found at: {csv_path}")
        return
        
    if not pkl_path.exists():
        logger.error(f"Old RouteSummary file not found at: {pkl_path}")
        return
        
    # 1. Read CSV and map airport_connection to route string (e.g. BIKF-EBBR -> BIKF -> EBBR)
    logger.info(f"Reading new CSV: {csv_path.name}")
    df_csv = pd.read_csv(csv_path)
    df_csv['route'] = df_csv['airport_connection'].str.replace('-', ' -> ', regex=False)
    
    # 2. Load old RouteSummary pickle
    logger.info(f"Reading old RouteSummary pickle: {pkl_path.name}")
    df_old = pd.read_pickle(pkl_path)
    logger.info(f"Old RouteSummary has {len(df_old):,} entries.")
    
    # 3. Inject distance_km into old DataFrame
    logger.info("Injecting distance_km column...")
    # Create mapping dictionary
    dist_map = dict(zip(df_csv['route'], df_csv['distance_km']))
    
    # Map the distance column
    df_old['distance_km'] = df_old['route'].map(dist_map)
    
    # 4. Save the merged DataFrame as the unfiltered pickle file
    logger.info(f"Saving merged unfiltered RouteSummary to {unfiltered_pkl_path.name}")
    df_old.to_pickle(unfiltered_pkl_path)
    
    # Also save as CSV for easy inspection
    unfiltered_csv_path = unfiltered_pkl_path.with_suffix('.csv')
    df_old.to_csv(unfiltered_csv_path, index=False)
    logger.info(f"Saved unfiltered CSV copy to {unfiltered_csv_path.name}")
    
    # 5. Filter to retain only routes for which the distance column exists
    logger.info("Filtering routes to keep only those with valid distance values...")
    df_filtered = df_old[df_old['distance_km'].notna()].copy()
    logger.info(f"Filtered RouteSummary has {len(df_filtered):,} entries.")
    
    # 6. Re-sort by total_route_count descending and re-calculate ranks
    logger.info("Re-sorting by traffic count and re-calculating ranks...")
    df_filtered = df_filtered.sort_values(by='total_route_count', ascending=False)
    df_filtered['rank'] = range(1, len(df_filtered) + 1)
    
    # 7. Overwrite the main master_flights_RouteSummary.pkl with the filtered DataFrame
    logger.info(f"Overwriting main RouteSummary pickle: {pkl_path.name}")
    df_filtered.to_pickle(pkl_path)
    
    # Also save a CSV copy for validation
    filtered_csv_path = pkl_path.with_suffix('.csv')
    df_filtered.to_csv(filtered_csv_path, index=False)
    logger.info(f"Saved filtered CSV copy to {filtered_csv_path.name}")
    
    logger.info("✅ RouteSummary reformatting and integration complete!")
    
if __name__ == "__main__":
    main()
