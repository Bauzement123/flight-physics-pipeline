"""
Module 2.1: Trajectory Processing & Filtering
Reads raw OpenSky trajectories, uses the `traffic` library to drop ground data,
applies an Extended Kalman Filter (EKF) for 6D kinematic smoothing, resamples 
to a 1-minute frequency, and saves a clean Parquet file.
"""

import pandas as pd
import logging
from pathlib import Path
import warnings
from traffic.core import Flight
from traffic.algorithms.filters.ekf import EKF
from src.common.adapters import (
    dataframe_to_pycontrails,
    write_flights_to_parquet,
    traffic_to_pycontrails,
    parquet_to_pycontrails,
    pycontrails_to_traffic
)
from src.common.utils import setup_file_logger, update_global_registry
from pyproj import CRS, Transformer, Geod
from src.common.config import GLOBAL_CLEAN_REGISTRY, BASE_DIR
from src.common.registry_utils import load_trajectory_registry


# Threshold in meters (100 km). Gaps larger than this will use geodesic interpolation.
GEODESIC_DISTANCE_THRESHOLD_M = 100000.0

# Suppress pandas FutureWarnings to keep console output clean
warnings.simplefilter(action='ignore', category=FutureWarning)

# Logging is configured inside __main__ block to avoid polluting imports

def clean_trajectories(input_file: str, out_dir: str):
    logging.info(f"Loading raw trajectories from: {input_file}")
    df = pd.read_parquet(input_file)
    if df.empty: 
        return

    # Set up processing log
    setup_file_logger(log_filename="processing.log")
    out_dir_path = Path(out_dir).resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)

    # Load clean EKF registry to support flight-level cache hits
    cached_clean_flights = {}
    try:
        df_reg = load_trajectory_registry(GLOBAL_CLEAN_REGISTRY)
        if not df_reg.empty and 'flight_id' in df_reg.columns and 'file_path' in df_reg.columns:
            cached_clean_flights = dict(zip(df_reg['flight_id'], df_reg['file_path']))
            logging.info(f"Loaded clean registry index containing {len(cached_clean_flights):,} cleaned flights.")
    except Exception as e:
        logging.warning(f"Could not load clean registry index: {e}")

    try:
        flights_dict = parquet_to_pycontrails(input_file)
    except Exception as exc:
        logging.error(f"Failed to read raw trajectories: {exc}")
        return

    pc_flights = []
    
    logging.info(f"Processing {len(flights_dict)} flights...")
    
    # Initialize the EKF object once
    ekf = EKF(smooth=True)
    logging.info(f"EKF initialized with smooth=True (RTS backward pass enabled)")
    
    for flight_id, pyc_flight in flights_dict.items():
        # Check if flight has already been cleaned and is in the cache
        if flight_id in cached_clean_flights:
            cached_file_path = BASE_DIR / cached_clean_flights[flight_id]
            if cached_file_path.exists():
                try:
                    df_cached = pd.read_parquet(cached_file_path)
                    df_flight_cached = df_cached[df_cached['flight_id'] == flight_id].copy()
                    if not df_flight_cached.empty:
                        pc_flight = dataframe_to_pycontrails(df_flight_cached)
                        if pc_flight:
                            pc_flights.append(pc_flight)
                            msg = f"Flight {flight_id}: Cache Hit (loaded from EKF clean cache)"
                            logging.info(msg)
                            continue
                except Exception as e:
                    logging.warning(f"Failed to load cached flight {flight_id} from {cached_clean_flights[flight_id]}: {e}. Falling back to EKF smoothing.")

        # Check typecode before cleaning
        typecode = pyc_flight.attrs.get('aircraft_type', 'B738')
        if not typecode or typecode == "UNKNOWN" or pd.isna(typecode):
            warn_msg = f"Flight {flight_id} has missing or unknown typecode. Skipping EKF cleaning."
            logging.warning(warn_msg)
            continue

        # Convert to traffic.core.Flight (this handles renaming and scaling to aviation units)
        flight = pycontrails_to_traffic(pyc_flight)
        
        # Drop rows with NA values in the essential columns required by EKF
        flight_data = flight.data.dropna(subset=['timestamp', 'latitude', 'longitude', 'track', 'groundspeed', 'vertical_rate', 'altitude', 'onground'])
        if len(flight_data) < 10:
            continue
            
        # Re-wrap in Traffic Flight object
        flight = Flight(flight_data)
        
        f = flight.airborne()
        if f is None or len(f) < 10:
            continue
            
        try:
            waypoints_before = len(f.data)
            f_data = f.data.copy()
            
            # Generate a 1-minute time grid between start and end of flight
            start_time = f_data['timestamp'].min()
            end_time = f_data['timestamp'].max()
            grid_times = pd.date_range(start=start_time.floor('min'), end=end_time.ceil('min'), freq='1min', tz='UTC')
            
            # Create a grid DataFrame with timestamps
            df_grid = pd.DataFrame({'timestamp': grid_times})
            
            # Merge raw and grid timestamps, ensuring uniqueness
            df_merged = pd.concat([f_data, df_grid]).drop_duplicates(subset=['timestamp']).sort_values(by='timestamp').reset_index(drop=True)
            
            # Identify which rows are grid placeholders (i.e. newly inserted, where latitude is NaN)
            grid_mask = df_merged['latitude'].isna()
            
            # Check gap sizes for coordinates and apply hybrid interpolation (geodesic vs linear)
            raw_indices = df_merged.index[~grid_mask].tolist()
            geod = Geod(ellps="WGS84")
            
            for idx in range(len(raw_indices) - 1):
                i_start = raw_indices[idx]
                i_end = raw_indices[idx + 1]
                
                if i_end - i_start <= 1:
                    continue # No grid points in between
                    
                lon1, lat1 = df_merged.loc[i_start, 'longitude'], df_merged.loc[i_start, 'latitude']
                lon2, lat2 = df_merged.loc[i_end, 'longitude'], df_merged.loc[i_end, 'latitude']
                
                # Geodetic distance
                _, _, distance = geod.inv(lon1, lat1, lon2, lat2)
                
                grid_indices_in_between = list(range(i_start + 1, i_end))
                
                if distance > GEODESIC_DISTANCE_THRESHOLD_M:
                    # Geodesic interpolation
                    npts = len(grid_indices_in_between)
                    path = geod.npts(lon1, lat1, lon2, lat2, npts)
                    for step_idx, grid_idx in enumerate(grid_indices_in_between):
                        df_merged.loc[grid_idx, 'longitude'] = path[step_idx][0]
                        df_merged.loc[grid_idx, 'latitude'] = path[step_idx][1]
            
            # Apply standard linear interpolation for all scalar columns and remaining coordinates
            cols_to_interpolate = ['latitude', 'longitude', 'altitude', 'groundspeed', 'track', 'vertical_rate']
            df_merged = df_merged.set_index('timestamp', drop=False)
            df_merged[cols_to_interpolate] = df_merged[cols_to_interpolate].interpolate(method='time')
            df_merged[cols_to_interpolate] = df_merged[cols_to_interpolate].ffill().bfill()
            df_merged = df_merged.reset_index(drop=True)
            
            # Project to local Cartesian space using pyproj
            mean_lat = df_merged['latitude'].mean()
            mean_lon = df_merged['longitude'].mean()
            
            proj4_str = f"+proj=laea +lat_0={mean_lat} +lon_0={mean_lon} +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
            proj_crs = CRS.from_proj4(proj4_str)
            geo_crs = CRS.from_epsg(4326)
            
            to_xy = Transformer.from_crs(geo_crs, proj_crs, always_xy=True)
            to_lonlat = Transformer.from_crs(proj_crs, geo_crs, always_xy=True)
            
            x_vals, y_vals = to_xy.transform(df_merged['longitude'].values, df_merged['latitude'].values)
            df_merged['x'] = x_vals
            df_merged['y'] = y_vals
            
            # EKF expects the DataFrame to have standard RangeIndex so that postprocessed Series align correctly.
            # Avoid setting timestamp as index here.
            # df_merged = df_merged.set_index('timestamp', drop=False)
            # df_merged.index.name = 'time_index'
            
            # Apply EKF
            df_smoothed = ekf.apply(df_merged)
            
            # Reset index to RangeIndex (restoring standard traffic behavior)
            df_smoothed = df_smoothed.reset_index(drop=True)
            
            # Reproject x and y back to Lat/Lon
            lon_vals, lat_vals = to_lonlat.transform(df_smoothed['x'].values, df_smoothed['y'].values)
            df_smoothed['latitude'] = lat_vals
            df_smoothed['longitude'] = lon_vals
            
            # Slice/sample the DataFrame at the 1-minute grid times
            df_resampled = df_smoothed[df_smoothed['timestamp'].isin(grid_times)].copy()
            
            # Re-inject metadata
            for col in ['icao24', 'callsign', 'typecode', 'estdepartureairport', 'estarrivalairport', 'firstseen', 'lastseen', 'flight_id']:
                if col in flight.data.columns:
                    df_resampled[col] = flight.data[col].iloc[0]
            
            # Ensure onground is explicitly False for all resampled airborne waypoints
            df_resampled['onground'] = False
            
            # Convert to PyContrails using the unified adapter (which reverts units to SI)
            pc_flight = traffic_to_pycontrails(df_resampled.reset_index(drop=True), typecode=typecode)
            if pc_flight:
                pc_flights.append(pc_flight)
                logging.info(f"Flight {flight.callsign}: EKF complete - {waypoints_before} -> {len(df_resampled)} waypoints")
        
        except Exception as e:
            warn_msg = f"Failed to process flight {flight.callsign}: {e}"
            logging.warning(warn_msg)

    # Write cleaning statistics and logs to standard loggers
    n_total = len(flights_dict)
    logging.info(
        f"\n==================================================\n"
        f"CLEANING RUN SUMMARY - {pd.Timestamp.now()}\n"
        f"Source raw file: {input_file}\n"
        f"Total flights: {n_total}\n"
        f"Success (Cleaned): {len(pc_flights)}\n"
        f"Failure/Skipped: {n_total - len(pc_flights)}\n"
        f"=================================================="
    )

    # 5. Save the adapted pycontrails objects
    if pc_flights:
        out_path = out_dir_path / Path(input_file).name.replace('_raw.parquet', '_clean_si.parquet')
        write_flights_to_parquet(pc_flights, out_path)
        
        # Update Clean EKF Registry Cache Index
        clean_registry_file = GLOBAL_CLEAN_REGISTRY
        rel_clean_path = out_path.resolve().relative_to(BASE_DIR).as_posix()
        new_entries = [{"flight_id": fid, "file_path": rel_clean_path} for fid in [f.attrs['flight_id'] for f in pc_flights]]
        update_global_registry(clean_registry_file, new_entries)
    else:
        logging.warning("No flights successfully processed by EKF")

if __name__ == "__main__":
    import argparse
    from pathlib import Path
    
    # Configure logging for better observability during batch processing
    setup_file_logger(log_filename="processing.log")
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", required=True, help="Path to raw Parquet file or directory containing raw Parquet files")
    parser.add_argument("--out-dir", default=None, help="Output directory. Defaults to input file's sibling 'clean' directory or parent directory.")
    args = parser.parse_args()
    
    input_path = Path(args.input_file)
    if not input_path.exists():
        logging.error(f"Input path does not exist: {input_path}")
        exit(1)
        
    if input_path.is_dir():
        # Directory mode: find all raw files
        raw_files = list(input_path.glob("*_raw.parquet"))
        if not raw_files:
            logging.warning(f"No *_raw.parquet files found in directory: {input_path}")
            exit(0)
            
        logging.info(f"Found {len(raw_files)} raw files in directory: {input_path}")
        for raw_file in raw_files:
            # Resolve out_dir for this file
            file_out_dir = args.out_dir
            if file_out_dir is None:
                if raw_file.parent.name == "raw":
                    file_out_dir = str(raw_file.parent.parent / "clean")
                else:
                    file_out_dir = str(raw_file.parent)
                    
            # Check if output file already exists
            expected_out_path = Path(file_out_dir) / raw_file.name.replace('_raw.parquet', '_clean_si.parquet')
            if expected_out_path.exists():
                logging.info(f"Clean file already exists: {expected_out_path}. Skipping.")
                continue
                
            try:
                clean_trajectories(str(raw_file), file_out_dir)
            except Exception as e:
                logging.error(f"Failed to process {raw_file.name}: {e}")
    else:
        # Single file mode
        out_dir = args.out_dir
        if out_dir is None:
            if input_path.parent.name == "raw":
                out_dir = str(input_path.parent.parent / "clean")
            else:
                out_dir = str(input_path.parent)
                
        # Check if output file already exists
        expected_out_path = Path(out_dir) / input_path.name.replace('_raw.parquet', '_clean_si.parquet')
        if expected_out_path.exists():
            logging.info(f"Clean file already exists: {expected_out_path}. Skipping.")
        else:
            clean_trajectories(args.input_file, out_dir)