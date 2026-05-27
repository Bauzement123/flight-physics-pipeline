"""
Module 2.3: Synthesized Trajectory Generator
Aggregates a cohort of cleaned EKF trajectories for a given route rank
into a single idealized 'synthesized' route, mapped onto a uniform temporal grid
starting at a fixed baseline date (2025-01-01 00:00:00 UTC).
"""

import argparse
import logging
from pathlib import Path
import pandas as pd
import numpy as np
from scipy.interpolate import interp1d
from collections import Counter

from pycontrails import Flight
from src.common.config import BASE_DIR, FLIGHT_REGISTRY_DIR, SYNTHESIZED_FLIGHT_PATHS_DIR
from src.common.utils import load_route_summary, split_route_string
from src.common.adapters import read_flights_from_parquet, write_flights_to_parquet

# Configure logging to match pipeline standards
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger(__name__)

# --- 1. Helper Functions ---

def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculates the great-circle distance between two points in nautical miles (NM)."""
    R = 3440.065
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    
    a = np.sin(dphi/2)**2 + np.cos(phi1)*np.cos(phi2)*np.sin(dlambda/2)**2
    return 2 * R * np.arctan2(np.sqrt(a), np.sqrt(1 - a))

def update_synthesized_registry(registry_file: Path, route: str, rank: int, file_path: str):
    """
    Appends a new synthesized flight mapping to the global registry Parquet.
    Deduplicates on route to ensure only one synthesized path per route.
    """
    new_entry = {
        "route": route,
        "rank": rank,
        "file_path": file_path
    }
    df_new = pd.DataFrame([new_entry])
    if registry_file.exists():
        try:
            df_reg = pd.read_parquet(registry_file)
            df_updated = pd.concat([df_reg, df_new]).drop_duplicates(subset=['route'], keep='last')
        except Exception as e:
            logger.warning(f"Could not read existing synthesized registry, overwriting: {e}")
            df_updated = df_new
    else:
        df_updated = df_new
    
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    df_updated.to_parquet(registry_file, index=False)
    logger.info(f"Updated global synthesized registry {registry_file.name} for route {route}.")

# --- 2. Main Processing Pipeline ---

def create_synthesized_trajectory(rank: int, output_parquet: str, time_grid_seconds: int = 60) -> str:
    # 1. Resolve Rank to Route
    logger.info(f"Resolving rank {rank} to route...")
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing.")
        return None
        
    route_row = df_summary[df_summary['rank'] == rank]
    if route_row.empty:
        logger.error(f"Rank {rank} not found in RouteSummary.")
        return None
        
    route_str = route_row['route'].iloc[0]
    dep, arr = split_route_string(route_str)
    if dep == 'UNK' or arr == 'UNK':
        logger.error(f"Failed to parse departure/arrival airports from route '{route_str}'.")
        return None
        
    logger.info(f"Rank {rank} resolved to route: {dep} -> {arr}")
    
    # 2. Query global_clean_registry.parquet to find trajectory files
    clean_registry_file = FLIGHT_REGISTRY_DIR / "global_clean_registry.parquet"
    if not clean_registry_file.exists():
        logger.error(f"Global clean registry does not exist at: {clean_registry_file}. Run EKF processing first.")
        return None
        
    df_clean_reg = pd.read_parquet(clean_registry_file)
    route_pattern = f"_{dep}-{arr}_"
    
    # Find all matching flight entries in the registry
    matching_flights = df_clean_reg[df_clean_reg['flight_id'].str.contains(route_pattern, na=False)]
    if matching_flights.empty:
        logger.error(f"No cleaned flights found in registry matching route pattern '{route_pattern}'.")
        return None
        
    # Retrieve unique clean trajectory Parquet files containing these flights
    unique_file_paths = matching_flights['file_path'].unique()
    logger.info(f"Found {len(matching_flights)} matching flights across {len(unique_file_paths)} clean parquet files.")
    
    # 3. Load flights from Parquet files
    spatial_grid_size = 1000
    normalized_distance_grid = np.linspace(0, 1, spatial_grid_size)
    interpolated_flights = []
    typecodes = []
    
    for rel_path in unique_file_paths:
        abs_path = BASE_DIR / rel_path
        if not abs_path.exists():
            logger.warning(f"File listed in registry not found: {abs_path}")
            continue
            
        try:
            flights_dict = read_flights_from_parquet(str(abs_path))
            for flight_id, fl in flights_dict.items():
                if route_pattern in flight_id:
                    df_fl = fl.to_dataframe()
                    df_fl = df_fl.sort_values('time').reset_index(drop=True)
                    df_fl['elapsed_time'] = (df_fl['time'] - df_fl['time'].iloc[0]).dt.total_seconds()
                    
                    typecode = fl.attrs.get('aircraft_type', 'B738')
                    typecodes.append(typecode)
                    
                    lats = df_fl['latitude'].values
                    lons = df_fl['longitude'].values
                    alts = df_fl['altitude'].values
                    
                    dists = haversine_distance(lats[:-1], lons[:-1], lats[1:], lons[1:])
                    cum_dist = np.insert(np.cumsum(dists), 0, 0)
                    
                    if cum_dist[-1] == 0:
                        continue
                        
                    norm_cum_dist = cum_dist / cum_dist[-1]
                    
                    # Ensure strictly increasing for interpolation
                    valid_idx = np.concatenate(([True], np.diff(norm_cum_dist) > 0))
                    norm_cum_dist = norm_cum_dist[valid_idx]
                    
                    interp_lat = interp1d(norm_cum_dist, lats[valid_idx], kind='linear')(normalized_distance_grid)
                    interp_lon = interp1d(norm_cum_dist, lons[valid_idx], kind='linear')(normalized_distance_grid)
                    interp_alt = interp1d(norm_cum_dist, alts[valid_idx], kind='linear')(normalized_distance_grid)
                    interp_time = interp1d(norm_cum_dist, df_fl['elapsed_time'].values[valid_idx], kind='linear')(normalized_distance_grid)
                    
                    interpolated_flights.append({
                        'lat': interp_lat, 'lon': interp_lon, 'alt': interp_alt, 'time': interp_time
                    })
        except Exception as e:
            logger.error(f"Failed to load flights from {abs_path}: {e}")
            
    if not interpolated_flights:
        logger.error("No valid flights successfully interpolated.")
        return None
        
    # 4. Check if altitude reaches FL250 threshold
    max_alt_val = max(np.max(f['alt']) for f in interpolated_flights)
    # EKF output in clean_si.parquet is strictly in meters.
    min_threshold_m = 25000 / 3.28084
    
    if max_alt_val < min_threshold_m:
        logger.warning(f"Trajectory aborted: Flights do not reach contrail-relevant altitudes (< 25,000 ft / {min_threshold_m:,.1f} meters).")
        return None

    # --- 3. Initial Aggregation ---
    lat_stack = np.vstack([f['lat'] for f in interpolated_flights])
    lon_stack = np.vstack([f['lon'] for f in interpolated_flights])
    alt_stack = np.vstack([f['alt'] for f in interpolated_flights])
    time_stack = np.vstack([f['time'] for f in interpolated_flights])
    
    synthesized_lat = np.median(lat_stack, axis=0)
    synthesized_lon = np.median(lon_stack, axis=0)
    synthesized_time = np.mean(time_stack, axis=0)
    synthesized_alt = np.percentile(alt_stack, 75, axis=0)
    
    # --- 4. The Climb/Descent "Straight Line" Override ---
    max_synthesized_alt = np.max(synthesized_alt)
    if max_synthesized_alt < min_threshold_m:
        logger.warning(f"Trajectory aborted: Aggregated cruise profile < 25,000 ft.")
        return None

    # Define TOC and TOD dynamically: 95% of the max altitude achieved
    cruise_threshold = max_synthesized_alt * 0.95
    above_threshold_indices = np.where(synthesized_alt >= cruise_threshold)[0]
    toc_idx = above_threshold_indices[0]
    tod_idx = above_threshold_indices[-1]
    
    # Overwrite Climb Segment (0 to toc_idx) with a straight line
    synthesized_lat[:toc_idx] = np.linspace(synthesized_lat[0], synthesized_lat[toc_idx], toc_idx)
    synthesized_lon[:toc_idx] = np.linspace(synthesized_lon[0], synthesized_lon[toc_idx], toc_idx)
    synthesized_alt[:toc_idx] = np.linspace(synthesized_alt[0], synthesized_alt[toc_idx], toc_idx)
    synthesized_time[:toc_idx] = np.linspace(synthesized_time[0], synthesized_time[toc_idx], toc_idx)

    # Overwrite Descent Segment (tod_idx to end) with a straight line
    desc_len = spatial_grid_size - tod_idx
    synthesized_lat[tod_idx:] = np.linspace(synthesized_lat[tod_idx], synthesized_lat[-1], desc_len)
    synthesized_lon[tod_idx:] = np.linspace(synthesized_lon[tod_idx], synthesized_lon[-1], desc_len)
    synthesized_alt[tod_idx:] = np.linspace(synthesized_alt[tod_idx], synthesized_alt[-1], desc_len)
    synthesized_time[tod_idx:] = np.linspace(synthesized_time[tod_idx], synthesized_time[-1], desc_len)

    # --- 5. Temporal Resampling ---
    max_time = np.max(synthesized_time)
    equal_time_grid = np.arange(0, max_time, time_grid_seconds)
    
    final_lat = interp1d(synthesized_time, synthesized_lat, kind='linear', fill_value="extrapolate")(equal_time_grid)
    final_lon = interp1d(synthesized_time, synthesized_lon, kind='linear', fill_value="extrapolate")(equal_time_grid)
    final_alt = interp1d(synthesized_time, synthesized_alt, kind='linear', fill_value="extrapolate")(equal_time_grid)
    
    # Assign fixed baseline date: 2025-01-01 00:00:00 UTC
    baseline_time = pd.Timestamp("2025-01-01 00:00:00", tz="UTC")
    time_series = baseline_time + pd.to_timedelta(equal_time_grid, unit='s')
    
    synthesized_df = pd.DataFrame({
        'time': time_series,
        'latitude': final_lat,
        'longitude': final_lon,
        'altitude': final_alt
    })
    
    # Find most common typecode to use as representative type
    representative_typecode = Counter(typecodes).most_common(1)[0][0] if typecodes else "B738"
    
    # Construct PyContrails Flight object
    synthesized_flight = Flight(
        data=synthesized_df,
        flight_id=f"{dep}-{arr}_synthesized",
        aircraft_type=representative_typecode,
        icao24="SYNTH",
        callsign="SYNTH",
        crs="EPSG:4326"
    )
    
    # Save using the shared adapter write logic
    out_path = Path(output_parquet)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_flights_to_parquet([synthesized_flight], out_path)
    
    # Update global synthesized registry manifest
    synthesized_registry_file = FLIGHT_REGISTRY_DIR / "global_synthesized_registry.parquet"
    rel_path_to_save = out_path.resolve().relative_to(BASE_DIR).as_posix()
    update_synthesized_registry(synthesized_registry_file, route=f"{dep}-{arr}", rank=rank, file_path=rel_path_to_save)
    
    logger.info(f"Synthesized trajectory created and registered: {out_path.name}")
    return str(out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a Synthesized Trajectory from registry cohorts.")
    parser.add_argument("--rank", type=int, required=True, help="Route rank from RouteSummary to process.")
    parser.add_argument("--out-dir", default=str(SYNTHESIZED_FLIGHT_PATHS_DIR), help="Output directory for the synthesized trajectory.")
    parser.add_argument("--grid-seconds", type=int, default=60, help="Time grid resolution in seconds (default: 60).")
    
    args = parser.parse_args()
    
    # Resolve route to verify name
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing.")
        exit(1)
        
    route_row = df_summary[df_summary['rank'] == args.rank]
    if route_row.empty:
        logger.error(f"Rank {args.rank} not found in RouteSummary.")
        exit(1)
        
    route_str = route_row['route'].iloc[0]
    dep, arr = split_route_string(route_str)
    
    out_file = Path(args.out_dir) / f"{dep}-{arr}_synthesized.parquet"
    
    create_synthesized_trajectory(args.rank, str(out_file), time_grid_seconds=args.grid_seconds)
