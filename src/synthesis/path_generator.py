"""
Module 2.3: Synthesized Trajectory Generator
Aggregates a cohort of raw trajectories for a given route rank
into a single idealized 'synthesized' route, mapped onto a uniform temporal grid
starting at a fixed baseline date (2025-01-01 00:00:00 UTC).
Uses open-source aviation libraries (traffic, openap, pycontrails) for kinematics,
spatial clustering, phase labeling, and temporal resampling.
"""

import argparse
import logging
from pathlib import Path
import pandas as pd
import numpy as np
from collections import Counter
import pyproj

from pycontrails import Flight
from traffic.core import Traffic, Flight as TrafficFlight
from openap.phase import FlightPhase

from src.common.config import BASE_DIR, REGISTRIES_DIR, SYNTHESIZED_FLIGHT_PATHS_DIR
from src.common.utils import load_route_summary, split_route_string
from src.common.adapters import (
    parquet_to_pycontrails,
    pycontrails_to_traffic,
    pycontrails_to_parquet
)

# Configure logging to match pipeline standards
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger(__name__)


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


def find_toc_tod(flight: TrafficFlight, labels: list) -> tuple:
    """
    Identifies the indices corresponding to Top of Climb (TOC) and Top of Descent (TOD)
    using the phase labels from OpenAP's FlightPhase and altitude heuristics.
    """
    df = flight.data
    altitudes = df['altitude'].values  # altitude in feet
    max_alt = np.max(altitudes)
    
    # Locate cruise phase indices (where altitude is high and label is cruise/level)
    cruise_indices = [idx for idx, lbl in enumerate(labels) if lbl in ('CR', 'LVL') and altitudes[idx] > 15000]
    
    if cruise_indices:
        toc_idx = cruise_indices[0]
        tod_idx = cruise_indices[-1]
    else:
        # Fallback heuristic: find where altitude is above 90% of max altitude
        cruise_thresh = max_alt * 0.90
        above_thresh = np.where(altitudes >= cruise_thresh)[0]
        if len(above_thresh) > 0:
            toc_idx = above_thresh[0]
            tod_idx = above_thresh[-1]
        else:
            # Absolute fallback
            toc_idx = int(len(df) * 0.2)
            tod_idx = int(len(df) * 0.8)
            
    # Keep within safe physical limits of flight segments
    toc_idx = max(2, min(toc_idx, int(len(df) * 0.4)))
    tod_idx = max(int(len(df) * 0.6), min(tod_idx, len(df) - 3))
    
    return toc_idx, tod_idx


def create_synthesized_trajectory(rank: int, output_parquet: str, time_grid_seconds: int = 60) -> str:
    out_path = Path(output_parquet)
    if out_path.exists():
        logger.info(f"Synthesized trajectory already exists at {out_path.name}. Skipping computation.")
        return str(out_path)

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
    
    # 2. Query registry to find raw flight files
    raw_registry_file = REGISTRIES_DIR / "global_trajectory_registry.parquet"
    if not raw_registry_file.exists():
        logger.error("Global raw trajectory registry file not found.")
        return None
        
    df_raw_reg = pd.read_parquet(raw_registry_file)
    route_pattern = f"_{dep}-{arr}_"
    matching_flights = df_raw_reg[df_raw_reg['flight_id'].str.contains(route_pattern, na=False)]
    
    if matching_flights.empty:
        logger.error(f"No raw flights found in registry matching route pattern '{route_pattern}'.")
        return None
        
    unique_file_paths = matching_flights['file_path'].unique()
    logger.info(f"Found {len(matching_flights)} matching flights across {len(unique_file_paths)} raw files.")
    
    # 3. Load flights sequentially and build the traffic cohort
    raw_flights = []
    typecodes = []
    
    for rel_path in unique_file_paths:
        abs_path = BASE_DIR / rel_path
        if not abs_path.exists():
            logger.warning(f"File listed in registry not found: {abs_path}")
            continue
            
        try:
            # Parse raw file to pycontrails flight objects dict
            flights_dict = parquet_to_pycontrails(str(abs_path))
            
            for flight_id, fl in flights_dict.items():
                if route_pattern in flight_id:
                    # Convert to traffic flight
                    trf_flight = pycontrails_to_traffic(fl)
                    
                    # Strip ground segments
                    airborne_flight = trf_flight.airborne()
                    if airborne_flight is not None and len(airborne_flight) >= 10:
                        raw_flights.append(airborne_flight)
                        typecodes.append(fl.attrs.get('aircraft_type', 'B738'))
        except Exception as e:
            logger.error(f"Failed to load flights from {abs_path}: {e}")
            
    if not raw_flights:
        logger.error("No valid airborne flights successfully loaded.")
        return None
        
    logger.info(f"Successfully loaded {len(raw_flights)} airborne trajectories. Identifying phases...")
    
    # 4. Phase classification and ROCD statistics calculation
    flight_metrics = []
    
    for idx, flight in enumerate(raw_flights):
        try:
            df = flight.data
            ts = (df['timestamp'] - df['timestamp'].iloc[0]).dt.total_seconds().values
            alt = df['altitude'].values  # in feet
            spd = df['groundspeed'].values  # in knots
            roc = df['vertical_rate'].values  # in feet/min
            
            # Apply OpenAP FlightPhase classifier
            fp = FlightPhase()
            fp.set_trajectory(ts, alt, spd, roc)
            labels = fp.phaselabel()
            
            toc_idx, tod_idx = find_toc_tod(flight, labels)
            
            # Calculate average ROCD values (feet / minute)
            climb_dur_min = (ts[toc_idx] - ts[0]) / 60.0
            climb_rocd = (alt[toc_idx] - alt[0]) / climb_dur_min if climb_dur_min > 0 else 0.0
            
            descent_dur_min = (ts[-1] - ts[tod_idx]) / 60.0
            descent_rocd = (alt[tod_idx] - alt[-1]) / descent_dur_min if descent_dur_min > 0 else 0.0
            
            flight_metrics.append({
                'flight': flight,
                'toc_idx': toc_idx,
                'tod_idx': tod_idx,
                'climb_rocd': abs(climb_rocd),
                'descent_rocd': abs(descent_rocd)
            })
        except Exception as e:
            logger.warning(f"Failed phase identification on flight {idx}: {e}")
            # Fallback metric entry using heuristical TOC/TOD
            df = flight.data
            toc_idx = int(len(df) * 0.2)
            tod_idx = int(len(df) * 0.8)
            flight_metrics.append({
                'flight': flight,
                'toc_idx': toc_idx,
                'tod_idx': tod_idx,
                'climb_rocd': 1800.0,
                'descent_rocd': 1200.0
            })
            
    # 5. Classify cohort into clean flights vs holding-pattern outliers
    # Standard baseline thresholds for direct climb/descent profiles
    min_descent_rate = 1200.0  # ft/min (1.2k)
    min_climb_rate = 1800.0    # ft/min (1.8k)
    
    clean_metrics = [m for m in flight_metrics if m['descent_rocd'] >= min_descent_rate and m['climb_rocd'] >= min_climb_rate]
    
    # Fallback: if less than 30% of the flights meet standard, take top 60% by descent rate
    if len(clean_metrics) < max(1, len(flight_metrics) * 0.3):
        threshold_count = max(1, int(len(flight_metrics) * 0.6))
        sorted_by_descent = sorted(flight_metrics, key=lambda x: x['descent_rocd'], reverse=True)
        clean_metrics = sorted_by_descent[:threshold_count]
        logger.info(f"ROCD clustering fallback: selected top {len(clean_metrics)} flights as clean baseline.")
        
    median_clean_climb_rocd = np.median([m['climb_rocd'] for m in clean_metrics])
    median_clean_descent_rocd = np.median([m['descent_rocd'] for m in clean_metrics])
    
    # Fallback to defaults if stats are non-physical
    if pd.isna(median_clean_climb_rocd) or median_clean_climb_rocd < 500:
        median_clean_climb_rocd = 1800.0
    if pd.isna(median_clean_descent_rocd) or median_clean_descent_rocd < 500:
        median_clean_descent_rocd = 1200.0
        
    logger.info(f"Baseline clean rates calculated: Climb={median_clean_climb_rocd:.1f} ft/min, Descent={median_clean_descent_rocd:.1f} ft/min")
    
    # 6. Apply Hybrid Normalization to holding-pattern flights (outliers)
    normalized_flights = []
    clean_flight_ids = {m['flight'].callsign for m in clean_metrics}
    
    for metric in flight_metrics:
        flight = metric['flight']
        
        # If flight belongs to the clean cohort, keep its original trajectory intact
        if flight.callsign in clean_flight_ids:
            normalized_flights.append(flight)
            continue
            
        logger.info(f"Normalizing holding-pattern flight {flight.callsign} (Climb ROCD={metric['climb_rocd']:.1f}, Descent ROCD={metric['descent_rocd']:.1f})...")
        
        df_new = flight.data.copy()
        times = (df_new['timestamp'] - df_new['timestamp'].iloc[0]).dt.total_seconds().values
        altitudes = df_new['altitude'].values.copy()
        latitudes = df_new['latitude'].values.copy()
        longitudes = df_new['longitude'].values.copy()
        
        toc_idx = metric['toc_idx']
        tod_idx = metric['tod_idx']
        
        # Correct climb segment if below threshold
        if metric['climb_rocd'] < min_climb_rate:
            alt_diff_climb = altitudes[toc_idx] - altitudes[0]
            new_climb_duration = (alt_diff_climb / median_clean_climb_rocd) * 60.0
            old_climb_duration = times[toc_idx] - times[0]
            time_shift_climb = new_climb_duration - old_climb_duration
            
            # Linearize spatial climb segment
            climb_len = toc_idx + 1
            latitudes[:climb_len] = np.linspace(latitudes[0], latitudes[toc_idx], climb_len)
            longitudes[:climb_len] = np.linspace(longitudes[0], longitudes[toc_idx], climb_len)
            altitudes[:climb_len] = np.linspace(altitudes[0], altitudes[toc_idx], climb_len)
            
            # Re-distribute elapsed time linearly and shift subsequent timeline
            times[:climb_len] = np.linspace(times[0], times[0] + new_climb_duration, climb_len)
            times[climb_len:] = times[climb_len:] + time_shift_climb
            
        # Correct descent segment if below threshold
        if metric['descent_rocd'] < min_descent_rate:
            alt_diff_descent = altitudes[tod_idx] - altitudes[-1]
            new_descent_duration = (alt_diff_descent / median_clean_descent_rocd) * 60.0
            
            # Linearize spatial descent segment
            descent_len = len(df_new) - tod_idx
            latitudes[tod_idx:] = np.linspace(latitudes[tod_idx], latitudes[-1], descent_len)
            longitudes[tod_idx:] = np.linspace(longitudes[tod_idx], longitudes[-1], descent_len)
            altitudes[tod_idx:] = np.linspace(altitudes[tod_idx], altitudes[-1], descent_len)
            
            # Re-distribute elapsed time linearly
            times[tod_idx:] = np.linspace(times[tod_idx], times[tod_idx] + new_descent_duration, descent_len)
            
        # Update DataFrame fields
        df_new['latitude'] = latitudes
        df_new['longitude'] = longitudes
        df_new['altitude'] = altitudes
        df_new['timestamp'] = df_new['timestamp'].iloc[0] + pd.to_timedelta(times, unit='s')
        
        normalized_flights.append(TrafficFlight(df_new))
        
    # 7. Spatial Standardization and Projection setup
    combined_df = pd.concat([f.data for f in normalized_flights], ignore_index=True)
    traffic_cohort = Traffic(combined_df)
    
    max_duration_seconds = max(flight.duration.total_seconds() for flight in traffic_cohort)
    min_sample_spacing_seconds = max(time_grid_seconds / 10.0, 1.0)
    nb_samples = int(max_duration_seconds / min_sample_spacing_seconds)
    
    logger.info(f"Oversampling spatial grid: nb_samples={nb_samples} (spacing={min_sample_spacing_seconds:.1f}s)")
    resampled_traffic = traffic_cohort.resample(nb_samples).eval()
    
    # Set up dynamic local LAEA projection centered on cohort mean
    all_dfs = pd.concat([f.data for f in resampled_traffic])
    mean_lat = all_dfs['latitude'].mean()
    mean_lon = all_dfs['longitude'].mean()
    
    proj4_str = f"+proj=laea +lat_0={mean_lat} +lon_0={mean_lon} +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
    projection = pyproj.Proj(proj4_str)
    
    # 8. Compute the spatial centroid
    logger.info("Computing spatial track centroid using DTW...")
    centroid_flight = resampled_traffic.centroid(nb_samples=nb_samples, projection=projection)
    
    # 9. Snap Centroid to PyContrails grid
    centroid_df = centroid_flight.data.copy()
    
    # Convert Units back to SI from standard aviation units
    rename_si = {
        'timestamp': 'time',
        'track': 'heading',
        'groundspeed': 'gs',
        'vertical_rate': 'rocd'
    }
    centroid_df = centroid_df.rename(columns=rename_si)
    
    centroid_df['altitude'] = centroid_df['altitude'] / 3.28084             # feet to meters
    if 'gs' in centroid_df.columns:
        centroid_df['gs'] = centroid_df['gs'] / 1.9438447                   # knots to m/s
    if 'rocd' in centroid_df.columns:
        centroid_df['rocd'] = centroid_df['rocd'] / 196.8504                # ft/min to m/s
        
    # Re-verify that altitudes reach contrail relevant levels (FL250 threshold)
    max_alt_m = centroid_df['altitude'].max()
    min_threshold_m = 25000.0 / 3.28084
    if max_alt_m < min_threshold_m:
        logger.warning(f"Synthesis aborted: Synthesized cruise altitude {max_alt_m*3.28084:.0f} ft is below FL250.")
        return None
        
    # Find most common typecode to use as representative type
    representative_typecode = Counter(typecodes).most_common(1)[0][0] if typecodes else "B738"
    
    # Build Flight object attributes
    attrs = {
        "flight_id": f"{dep}-{arr}_synthesized",
        "aircraft_type": representative_typecode,
        "icao24": "SYNTH",
        "callsign": "SYNTH"
    }
    
    pyc_centroid = Flight(data=centroid_df, crs="EPSG:4326", drop_duplicated_times=True, **attrs)
    
    # Uniform temporal interpolation
    logger.info(f"Resampling synthesized trajectory to uniform {time_grid_seconds}s grid...")
    synthesized_flight = pyc_centroid.resample_and_fill(freq=f"{time_grid_seconds}s")
    
    # 10. Timeline Normalization & Save
    df_final = synthesized_flight.to_dataframe()
    delta_time = df_final['time'] - df_final['time'].min()
    df_final['time'] = pd.Timestamp("2025-01-01 00:00:00", tz="UTC") + delta_time
    
    # Re-instantiate final flight
    final_flight = Flight(data=df_final, crs="EPSG:4326", **attrs)
    
    out_path = Path(output_parquet)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save parquet via adapter
    pycontrails_to_parquet(final_flight, out_path)
    
    # Register output file
    synthesized_registry_file = REGISTRIES_DIR / "global_synthesized_registry.parquet"
    rel_path_to_save = out_path.resolve().relative_to(BASE_DIR).as_posix()
    update_synthesized_registry(synthesized_registry_file, route=f"{dep}-{arr}", rank=rank, file_path=rel_path_to_save)
    
    logger.info(f"✓ Synthesized flight saved and registered: {out_path.name}")
    return str(out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a Synthesized Trajectory from raw OpenSky cohorts.")
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
