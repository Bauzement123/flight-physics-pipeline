"""
Pipeline Module: Route Summary Distance Enrichment
Enriches the aggregated master flights RouteSummary pickle/CSV file with geodesic great-circle distances.
Uses traffic's embedded OurAirports database and a vectorized NumPy Haversine formula.
"""

import sys
import os
import pickle
import logging
from pathlib import Path
import pandas as pd
import numpy as np
from traffic.data import airports

# Add project root to path if needed for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from src.common.config import FLIGHT_REGISTRY_DIR

# Configure logging
logger = logging.getLogger(__name__)

def calculate_haversine_numpy(
    lon1: pd.Series, 
    lat1: pd.Series, 
    lon2: pd.Series, 
    lat2: pd.Series
) -> pd.Series:
    """
    Computes great-circle distances in meters between arrays of coordinates.
    Uses vectorized NumPy trigonometric math.
    """
    # Convert degrees to radians
    lon1_rad, lat1_rad, lon2_rad, lat2_rad = map(np.radians, [lon1, lat1, lon2, lat2])
    
    # Deltas
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    
    # Haversine formula
    a = np.sin(dlat / 2.0)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon / 2.0)**2
    c = 2.0 * np.arcsin(np.sqrt(a))
    
    # Volumetric mean radius of the Earth in meters
    EARTH_RADIUS_METERS = 6371000.0
    return c * EARTH_RADIUS_METERS

def enrich_route_summary(
    summary_pkl_path: Path,
    summary_csv_path: Path
):
    logger.info(f"Loading RouteSummary from: {summary_pkl_path}")
    if not summary_pkl_path.exists():
        logger.error(f"RouteSummary pickle file not found at {summary_pkl_path}")
        return False
        
    try:
        with open(summary_pkl_path, 'rb') as f:
            df = pickle.load(f)
    except Exception as e:
        logger.error(f"Failed to load RouteSummary pickle: {e}")
        return False

    if df.empty:
        logger.warning("RouteSummary is empty. Nothing to enrich.")
        return False

    if 'route' not in df.columns:
        logger.error("Required column 'route' is missing from the DataFrame schema.")
        return False

    logger.info("Parsing origin and destination ICAOs from route codes...")
    # Handle variable whitespace around '->' delimiter
    parsed_routes = df['route'].str.split(r"\s*->\s*", expand=True)
    if parsed_routes.shape[1] < 2:
        logger.error("Failed to parse origin/destination ICAOs. 'route' values must be like 'DEP -> ARR'")
        return False
        
    df['origin_icao'] = parsed_routes[0].str.strip()
    df['destination_icao'] = parsed_routes[1].str.strip()

    logger.info("Building unique airport coordinates registry cache using traffic...")
    unique_icaos = pd.concat([df['origin_icao'], df['destination_icao']]).dropna().unique()
    logger.info(f"Found {len(unique_icaos)} unique airports to map coordinates for.")

    # Populate coordinates registry cache
    coordinate_registry = {}
    for icao in unique_icaos:
        try:
            airport_entry = airports[icao]
            if airport_entry is not None:
                coordinate_registry[icao] = (float(airport_entry.longitude), float(airport_entry.latitude))
            else:
                coordinate_registry[icao] = (np.nan, np.nan)
        except Exception:
            coordinate_registry[icao] = (np.nan, np.nan)

    logger.info("Mapping coordinates to route endpoints...")
    df['orig_lon'] = df['origin_icao'].map(lambda x: coordinate_registry.get(x, (np.nan, np.nan))[0])
    df['orig_lat'] = df['origin_icao'].map(lambda x: coordinate_registry.get(x, (np.nan, np.nan))[1])
    df['dest_lon'] = df['destination_icao'].map(lambda x: coordinate_registry.get(x, (np.nan, np.nan))[0])
    df['dest_lat'] = df['destination_icao'].map(lambda x: coordinate_registry.get(x, (np.nan, np.nan))[1])

    logger.info("Calculating great-circle route distances (vectorized Haversine)...")
    df['distance_m'] = calculate_haversine_numpy(
        df['orig_lon'], df['orig_lat'],
        df['dest_lon'], df['dest_lat']
    )

    # Clean up temporary scratch processing columns
    scratch_columns = ['origin_icao', 'destination_icao', 'orig_lon', 'orig_lat', 'dest_lon', 'dest_lat']
    df = df.drop(columns=scratch_columns)

    # Save enriched files
    logger.info(f"Saving enriched RouteSummary pickle to: {summary_pkl_path}")
    try:
        with open(summary_pkl_path, 'wb') as f:
            pickle.dump(df, f)
        logger.info("Pickle saved successfully.")
    except Exception as e:
        logger.error(f"Failed to write RouteSummary pickle: {e}")
        return False

    logger.info(f"Saving enriched RouteSummary CSV to: {summary_csv_path}")
    try:
        df.to_csv(summary_csv_path, index=False)
        logger.info("CSV saved successfully.")
    except Exception as e:
        logger.error(f"Failed to write RouteSummary CSV: {e}")
        return False

    logger.info("Distance enrichment completed successfully.")
    return True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - [DISTANCE ENRICHMENT] - %(message)s")
    pkl_file = FLIGHT_REGISTRY_DIR / "master_flights_route_summary.pkl"
    csv_file = FLIGHT_REGISTRY_DIR / "master_flights_route_summary.csv"
    enrich_route_summary(pkl_file, csv_file)
