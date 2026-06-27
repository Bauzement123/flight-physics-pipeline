"""
Module 3c: Batch Clone Simulation Engine
Loads a synthesized trajectory, clones it, shifts its departure times in-memory
to match individual flights' schedules, and simulates them under Cocip/PSFlight.
"""

import argparse
import logging
from pathlib import Path
import sys
import os
import pandas as pd
import numpy as np

from pycontrails import Flight, DiskCacheStore
from pycontrails.datalib.ecmwf import ERA5
from pycontrails.models.ps_model import PSFlight
from pycontrails.models.cocip import Cocip
from pycontrails.models.humidity_scaling import ConstantHumidityScaling

# Add project root to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from src.common.config import (
    BASE_DIR, WEATHER_DIR, RESULTS_DIR, MASTER_FLIGHTS_FILE,
    GLOBAL_SYNTHESIZED_REGISTRY, GLOBAL_SYNTH_SIM_REGISTRY,
    ERA5_PRESSURE_LEVEL_VARIABLES, ERA5_SURFACE_VARIABLES,
    ERA5_REQUIRED_PRESSURE_LEVELS, ERA5_GRID
)
from src.common.utils import load_route_summary, split_route_string, update_global_registry, setup_file_logger
from src.common.adapters import read_flights_from_parquet, write_flights_to_parquet

logger = logging.getLogger(__name__)

def simulate_cloned_flight(
    fl_cloned: Flight, 
    cache_dir: str, 
    max_age_hours: int,
    met_dataset=None,
    rad_dataset=None
) -> Flight:
    """Simulates a single shifted flight using Cocip and PSFlight."""
    # Resolve times
    fl_times = pd.to_datetime(fl_cloned['time'])
    min_time = fl_times.min()
    max_time = fl_times.max()
    
    # Calculate weather window if not pre-provided
    if met_dataset is None or rad_dataset is None:
        weather_start = (min_time - pd.Timedelta(hours=1)).floor('h')
        if hasattr(weather_start, 'tz') and weather_start.tz is not None:
            weather_start = weather_start.tz_convert('UTC').tz_localize(None)
            
        weather_end = (max_time + pd.Timedelta(hours=max_age_hours + 1)).ceil('h')
        if hasattr(weather_end, 'tz') and weather_end.tz is not None:
            weather_end = weather_end.tz_convert('UTC').tz_localize(None)
        
        start_date = weather_start.strftime('%Y-%m-%dT%H:%M:%S')
        end_date = weather_end.strftime('%Y-%m-%dT%H:%M:%S')
        
        disk_cache = DiskCacheStore(cache_dir=cache_dir)
        
        era5_pl = ERA5(
            time=(start_date, end_date),
            variables=ERA5_PRESSURE_LEVEL_VARIABLES,
            pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
            grid=ERA5_GRID,
            cachestore=disk_cache
        )
        
        era5_sl = ERA5(
            time=(start_date, end_date),
            variables=ERA5_SURFACE_VARIABLES,
            pressure_levels=-1,
            grid=ERA5_GRID,
            cachestore=disk_cache
        )
        
        met = era5_pl.open_metdataset()
        rad = era5_sl.open_metdataset()
    else:
        met = met_dataset
        rad = rad_dataset
        
    # Initialize Models
    ps_model = PSFlight(
        met=met,
        params={
            "fill_low_altitude_with_isa_temperature": True,
            "fill_low_altitude_with_zero_wind": False,
            "correct_fuel_flow": False,
            "n_iter": 5,
        },
    )
    
    cocip_params = {
        "process_emissions": True,
        "verbose_outputs": False,
        "humidity_scaling": ConstantHumidityScaling(rhi_adj=0.97),
        "max_age": pd.Timedelta(hours=max_age_hours),
        "dt_integration": np.timedelta64(30, "m"),
        "dz_m": 200.0,
        "effective_vertical_resolution": 2000.0,
        "filter_sac": True,
        "filter_initially_persistent": True,
        "min_altitude_m": 6000.0,
        "max_altitude_m": 13000.0,
        "max_seg_length_m": 40000.0,
    }
    cocip_model = Cocip(met=met, rad=rad, params=cocip_params, aircraft_performance=ps_model)
    
    # Resolve and validate aircraft type
    typecode = fl_cloned.attrs.get('aircraft_type')
    if not typecode or pd.isna(typecode):
        raise ValueError(f"Unsupported aircraft: {typecode} (Missing/NaN)")
        
    ps_supported_types = list(ps_model.aircraft_engine_params.keys())
    if typecode not in ps_supported_types:
        raise ValueError(f"Unsupported aircraft: {typecode}")
        
    fl_evaluated = ps_model.eval(fl_cloned)
    fl_out = cocip_model.eval(source=fl_evaluated)
    return fl_out

def filter_cohort_flights(
    master_flights_path,
    route_summary_path,
    start_date: str = None,
    end_date: str = None,
    ranks: list = None,
    out_dir: str = None,
    synthesized_registry_file: str = None,
    overwrite: bool = False,
    min_distance: float = 800.0
) -> pd.DataFrame:
    """
    Creates the cohort flight list in memory by filtering the master registry.
    Applies exclusions:
    1. Filter date range and rank corridor in memory.
    2. Throw out airport to airport loops.
    3. Retain routes present in synthesized registry and having a valid base path file.
    4. Throw out already simulated flights if overwrite is False.
    """
    from src.filtering.population_filter import filter_population_in_memory
    
    # 1. In-memory filtering + loop dropping
    logger.info("Applying in-memory temporal and rank filters...")
    df_filtered = filter_population_in_memory(
        master_flights=master_flights_path,
        route_summary=route_summary_path,
        start_date=start_date,
        end_date=end_date,
        ranks=ranks,
        drop_airport_loops=True,
        min_distance=min_distance
    )
    
    if df_filtered.empty:
        logger.warning("No flights matched filtering criteria.")
        return pd.DataFrame()

    # 2. Synthesized registry check
    logger.info("Filtering against synthesized registry manifest...")
    if not Path(synthesized_registry_file).exists():
        logger.error(f"Synthesized registry file not found: {synthesized_registry_file}")
        return pd.DataFrame()
        
    df_synth_reg = pd.read_parquet(synthesized_registry_file)
    valid_routes = {}
    for _, row in df_synth_reg.iterrows():
        route = row['route']
        rel_path = row['file_path']
        abs_path = BASE_DIR / rel_path
        if abs_path.exists():
            valid_routes[route] = abs_path
            
    # Keep only flights whose route is registered and base path file exists on disk
    df_filtered['route_key'] = df_filtered['estdepartureairport'] + '-' + df_filtered['estarrivalairport']
    initial_len = len(df_filtered)
    df_filtered = df_filtered[df_filtered['route_key'].isin(valid_routes.keys())].copy()
    dropped_synth = initial_len - len(df_filtered)
    if dropped_synth > 0:
        logger.info(f"Dropped {dropped_synth} flights missing registered synthesized base paths.")
        
    if df_filtered.empty:
        return df_filtered

    # 3. Already simulated checklist (manifest + file check)
    if not overwrite:
        logger.info("Filtering out already simulated flights...")
        cloned_registry_file = GLOBAL_SYNTH_SIM_REGISTRY
        simulated_ids = set()
        if cloned_registry_file.exists():
            try:
                df_sim_reg = pd.read_parquet(cloned_registry_file)
                simulated_ids = set(df_sim_reg['flight_id'].unique())
            except Exception as e:
                logger.warning(f"Could not load cloned registry file: {e}")
                
        # We can construct the flight IDs and check file existence
        keep_mask = []
        out_dir_path = Path(out_dir)
        for _, row in df_filtered.iterrows():
            dep = row['estdepartureairport']
            arr = row['estarrivalairport']
            icao24 = row['icao24']
            callsign = row.get('callsign', 'UNK')
            firstseen_dt = pd.to_datetime(row['firstseen'])
            
            if firstseen_dt.tz is None:
                firstseen_dt = firstseen_dt.tz_localize('UTC')
            else:
                firstseen_dt = firstseen_dt.tz_convert('UTC')
                
            fs_str = firstseen_dt.strftime('%Y%m%d_%H%M')
            flight_id = f"{icao24}_{callsign}_{dep}-{arr}_{fs_str}"
            
            # Target output file path
            out_file = out_dir_path / f"{dep}-{arr}_cloned_simulated" / f"{flight_id}_simulated.parquet"
            
            if flight_id in simulated_ids and out_file.exists():
                keep_mask.append(False)
            elif out_file.exists():
                keep_mask.append(False)
            else:
                keep_mask.append(True)
                
        initial_len = len(df_filtered)
        df_filtered = df_filtered[keep_mask].copy()
        dropped_sim = initial_len - len(df_filtered)
        if dropped_sim > 0:
            logger.info(f"Skipped {dropped_sim} already-simulated flights.")
            
    # Sort the cohort to optimize disk reads for the base synthesized flight paths
    df_filtered = df_filtered.sort_values(by=['estdepartureairport', 'estarrivalairport', 'firstseen']).copy()
    
    return df_filtered

def load_weather_for_flights(
    flights_df: pd.DataFrame,
    weather_cache_dir: str,
    max_age_hours: int = 48
):
    """
    Loads ERA5 pressure level and surface level datasets for the temporal span
    of the flights, padded with 1 hour before start and max_age_hours + 1 hour after end.
    Attempts to load files offline using paths if they exist locally, otherwise falls back to online retrieval.
    """
    if flights_df.empty:
        return None, None
        
    times = pd.to_datetime(flights_df['firstseen'])
    min_time = times.min()
    if 'lastseen' in flights_df.columns:
        max_time = pd.to_datetime(flights_df['lastseen']).max()
    else:
        max_time = min_time + pd.Timedelta(hours=15)
        
    weather_start = (min_time - pd.Timedelta(hours=1)).floor('h')
    if hasattr(weather_start, 'tz') and weather_start.tz is not None:
        weather_start = weather_start.tz_convert('UTC').tz_localize(None)
        
    weather_end = (max_time + pd.Timedelta(hours=max_age_hours + 1)).ceil('h')
    if hasattr(weather_end, 'tz') and weather_end.tz is not None:
        weather_end = weather_end.tz_convert('UTC').tz_localize(None)
        
    start_str = weather_start.strftime('%Y-%m-%dT%H:%M:%S')
    end_str = weather_end.strftime('%Y-%m-%dT%H:%M:%S')
    
    # Check if we can load offline via paths (all files exist)
    cache_path = Path(weather_cache_dir)
    hours = pd.date_range(start=weather_start, end=weather_end, freq='h')
    
    pl_paths = []
    sl_paths = []
    all_pl_exist = True
    all_sl_exist = True
    
    for h in hours:
        pl_name = f"{h.strftime('%Y%m%d-%H')}-era5pl0.5reanalysis.nc"
        sl_name = f"{h.strftime('%Y%m%d-%H')}-era5sl0.5reanalysis.nc"
        
        pl_file = cache_path / pl_name
        sl_file = cache_path / sl_name
        
        if pl_file.exists():
            pl_paths.append(str(pl_file))
        else:
            all_pl_exist = False
            
        if sl_file.exists():
            sl_paths.append(str(sl_file))
        else:
            all_sl_exist = False
            
    # If all files exist, load offline using paths parameter
    if all_pl_exist and all_sl_exist and len(pl_paths) > 0 and len(sl_paths) > 0:
        logger.info(f"Offline Mode: All {len(hours)} hourly files found in local cache. Loading offline via paths...")
        try:
            era5_pl = ERA5(
                time=(start_str, end_str),
                paths=pl_paths,
                variables=ERA5_PRESSURE_LEVEL_VARIABLES,
                pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
                grid=ERA5_GRID
            )
            era5_sl = ERA5(
                time=(start_str, end_str),
                paths=sl_paths,
                variables=ERA5_SURFACE_VARIABLES,
                pressure_levels=-1,
                grid=ERA5_GRID
            )
            met = era5_pl.open_metdataset()
            rad = era5_sl.open_metdataset()
            logger.info("Successfully loaded weather datasets offline.")
            return met, rad
        except Exception as offline_err:
            logger.warning(f"Failed to load datasets offline: {offline_err}. Falling back to standard online retrieval...")
            
    # Fallback: Online query (will check cache first, then download missing)
    logger.info(f"Loading weather datasets via standard online queries for range: {start_str} to {end_str}...")
    disk_cache = DiskCacheStore(cache_dir=weather_cache_dir)
    
    era5_pl = ERA5(
        time=(start_str, end_str),
        variables=ERA5_PRESSURE_LEVEL_VARIABLES,
        pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
        grid=ERA5_GRID,
        cachestore=disk_cache
    )
    
    era5_sl = ERA5(
        time=(start_str, end_str),
        variables=ERA5_SURFACE_VARIABLES,
        pressure_levels=-1,
        grid=ERA5_GRID,
        cachestore=disk_cache
    )
    
    met = era5_pl.open_metdataset()
    rad = era5_sl.open_metdataset()
    return met, rad

def simulate_single_flight(
    flight_row: pd.Series,
    base_flight: Flight,
    flight_id: str,
    weather_cache_dir: str,
    max_age_hours: int = 48,
    met_dataset=None,
    rad_dataset=None
) -> Flight:
    """Clones the base flight path, shifts to match schedule, recovers metadata and simulates."""
    df_cloned = base_flight.to_dataframe().copy()
    
    if df_cloned['time'].dt.tz is None:
        df_cloned['time'] = df_cloned['time'].dt.tz_localize('UTC')
    else:
        df_cloned['time'] = df_cloned['time'].dt.tz_convert('UTC')
        
    synth_start = df_cloned['time'].iloc[0]
    
    target_start = pd.to_datetime(flight_row['firstseen'])
    if target_start.tz is None:
        target_start = target_start.tz_localize('UTC')
    else:
        target_start = target_start.tz_convert('UTC')
        
    time_offset = target_start - synth_start
    df_cloned['time'] = df_cloned['time'] + time_offset
    
    dep = flight_row['estdepartureairport']
    arr = flight_row['estarrivalairport']
    icao24 = flight_row['icao24']
    callsign = flight_row.get('callsign', 'UNK')
    typecode = flight_row.get('typecode')
    if pd.isna(typecode) or not typecode:
        typecode = "UNKNOWN"
    fs_str = target_start.strftime('%Y%m%d_%H%M')
    
    attrs = {
        "flight_id": flight_id,
        "aircraft_type": typecode,
        "icao24": icao24,
        "callsign": callsign,
        "firstseen": flight_row['firstseen'],
        "lastseen": flight_row['lastseen'],
        "estdepartureairport": dep,
        "estarrivalairport": arr,
    }
    
    for k, v in base_flight.attrs.items():
        if k not in attrs:
            attrs[k] = v
    attrs.pop('crs', None)
    
    fl_cloned = Flight(data=df_cloned, drop_duplicated_times=True, crs="EPSG:4326", **attrs)
    
    fl_simulated = simulate_cloned_flight(
        fl_cloned=fl_cloned,
        cache_dir=weather_cache_dir,
        max_age_hours=max_age_hours,
        met_dataset=met_dataset,
        rad_dataset=rad_dataset
    )
    return fl_simulated

def run_batch_clone_simulation(
    ranks: list,
    start_date: str = None,
    end_date: str = None,
    weather_cache: str = None,
    out_dir: str = None,
    max_age_hours: int = 48,
    overwrite: bool = False,
    test_mode: bool = False,
    day_by_day: bool = True,
    min_distance: float = 800.0,
    clusters_per_flight: int = 1
):
    # Setup paths
    master_flights_file = MASTER_FLIGHTS_FILE
    synthesized_registry_file = GLOBAL_SYNTHESIZED_REGISTRY
    cloned_registry_file = GLOBAL_SYNTH_SIM_REGISTRY
    
    # 1. Output Dir
    setup_file_logger(log_filename="clone_simulation.log")
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    
    # 2. Handle Test Mode
    if test_mode:
        logger.info("=== RUNNING IN TEST MODE ===")
        start_date = "2025-01-01"
        end_date = "2025-01-01"
        day_by_day = False  # No need for day-by-day loop in test mode
        
    # Check start and end dates
    if not start_date or not end_date:
        logger.error("Start date and end date are required (unless in test mode).")
        return
        
    # Get date range
    dates = pd.date_range(start=start_date, end=end_date, freq='D')
    logger.info(f"Targeting date range: {start_date} to {end_date} ({len(dates)} days)")
    
    # Load RouteSummary (only once globally)
    df_summary = load_route_summary(None)
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing.")
        return
        
    # Load master registry once globally (loading only the 7 columns we actually use)
    if not master_flights_file.exists():
        logger.error(f"Master flights file not found at: {master_flights_file}")
        return
    logger.info(f"Loading master flights database from {master_flights_file} (optimized columns)...")
    req_cols = ['firstseen', 'estdepartureairport', 'estarrivalairport', 'icao24', 'callsign', 'typecode', 'lastseen']
    master_df = pd.read_parquet(master_flights_file, columns=req_cols)
    logger.info(f"Loaded master flights registry with {len(master_df):,} entries.")
    
    new_registry_entries = []
    
    # (global_flight_cluster_map dependency removed)

    # Pre-resolve synthesized files dictionary to avoid reloading manifest file repeatedly
    valid_synthesized_files = {}
    if synthesized_registry_file.exists():
        df_synth_reg = pd.read_parquet(synthesized_registry_file)
        for col in ["cluster_id"]:
            if col not in df_synth_reg.columns:
                df_synth_reg[col] = 0
        for _, row in df_synth_reg.iterrows():
            route = row['route']
            rel_path = row['file_path']
            cluster_id = int(row['cluster_id'])
            abs_path = BASE_DIR / rel_path
            if abs_path.exists():
                valid_synthesized_files[(route, cluster_id)] = abs_path
                
    # 3. Outer Loop: Day-by-Day (or Single Batch if day_by_day=False)
    if not day_by_day:
        date_groups = [(start_date, end_date, "Full Batch")]
    else:
        date_groups = [(d.strftime('%Y-%m-%d'), d.strftime('%Y-%m-%d'), d.strftime('%Y-%m-%d')) for d in dates]
        
    for d_start, d_end, desc in date_groups:
        logger.info(f"\n--- Processing cohort for: {desc} ---")
        
        # A. Filter cohort flights for this date/dates
        cohort_df = filter_cohort_flights(
            master_flights_path=master_df,
            route_summary_path=df_summary,
            start_date=d_start,
            end_date=d_end,
            ranks=ranks,
            out_dir=str(out_dir_path),
            synthesized_registry_file=str(synthesized_registry_file),
            overwrite=overwrite,
            min_distance=min_distance
        )
        
        if cohort_df.empty:
            logger.info(f"No flights to simulate for {desc}.")
            continue
            
        if test_mode:
            logger.info("Test Mode: Slicing cohort to first 1 entries.")
            cohort_df = cohort_df.head(1)
            
        logger.info(f"Found {len(cohort_df)} flights to simulate for {desc}.")
        
        # B. Load Weather dataset globally once for this daily cohort
        met, rad = load_weather_for_flights(
            flights_df=cohort_df,
            weather_cache_dir=weather_cache,
            max_age_hours=max_age_hours
        )
        
        # C. Simulation Loop
        success_count = 0
        failure_count = 0
        skip_count = 0
        
        # Cache base synthesized Flight objects in memory (key: route_key)
        cached_base_flights = {}
        
        # Group and simulate
        for _, row in cohort_df.iterrows():
            dep = row['estdepartureairport']
            arr = row['estarrivalairport']
            route_key = f"{dep}-{arr}"
            
            icao24 = row['icao24']
            callsign = row.get('callsign', 'UNK')
            typecode = row.get('typecode')
            if pd.isna(typecode) or not typecode:
                typecode = "UNKNOWN"
            firstseen_dt = pd.to_datetime(row['firstseen'])
            if firstseen_dt.tz is None:
                firstseen_dt = firstseen_dt.tz_localize('UTC')
            else:
                firstseen_dt = firstseen_dt.tz_convert('UTC')
            fs_str = firstseen_dt.strftime('%Y%m%d_%H%M')
            
            corridor_out_dir = out_dir_path / f"{dep}-{arr}_cloned_simulated"
            corridor_out_dir.mkdir(parents=True, exist_ok=True)
            
            # Identify Available Clusters Per Route
            available_clusters = [cid for (r, cid) in valid_synthesized_files.keys() if r == route_key]
            if not available_clusters:
                logger.error(f"No synthesized base paths available for route {route_key}. Skipping flight.")
                continue
                
            # Randomized Track Sampling
            sample_size = min(clusters_per_flight, len(available_clusters))
            sampled_clusters = np.random.choice(available_clusters, size=sample_size, replace=False)
            
            for cluster_id in sampled_clusters:
                flight_id = f"{icao24}_{callsign}_{dep}-{arr}_{fs_str}_c{cluster_id}"
                out_file = corridor_out_dir / f"{flight_id}_simulated.parquet"
                
                # Double check existence for safety
                if not overwrite and out_file.exists():
                    logger.info(f"Skipping {flight_id}: output file exists.")
                    skip_count += 1
                    rel_out_path = out_file.resolve().relative_to(BASE_DIR).as_posix()
                    new_registry_entries.append({"flight_id": flight_id, "file_path": rel_out_path})
                    continue
                    
                # Get synthesized base flight
                base_flight = cached_base_flights.get((route_key, cluster_id))
                if base_flight is None:
                    synth_path = valid_synthesized_files.get((route_key, cluster_id))
                    if not synth_path:
                        logger.error(f"Synthesized base path missing for route {route_key} cluster {cluster_id}. Skipping.")
                        continue
                        
                    # Load Synthetic Flight
                    synth_flights = read_flights_from_parquet(str(synth_path))
                    if not synth_flights:
                        logger.error(f"Empty synthesized file for route {route_key} cluster {cluster_id}. Skipping.")
                        continue
                    base_flight = list(synth_flights.values())[0]
                    cached_base_flights[(route_key, cluster_id)] = base_flight
                    
                logger.info(f"Simulating flight {flight_id}...")
                
                try:
                    fl_simulated = simulate_single_flight(
                        flight_row=row,
                        base_flight=base_flight,
                        flight_id=flight_id,
                        weather_cache_dir=weather_cache,
                        max_age_hours=max_age_hours,
                        met_dataset=met,
                        rad_dataset=rad
                    )
                    
                    # Save single flight immediately to parquet
                    write_flights_to_parquet([fl_simulated], out_file)
                    
                    rel_out_path = out_file.resolve().relative_to(BASE_DIR).as_posix()
                    new_registry_entries.append({"flight_id": flight_id, "file_path": rel_out_path})
                    success_count += 1
                except Exception as e:
                    if "Unsupported aircraft" in str(e):
                        skip_count += 1
                        logger.warning(f"Skipping flight {flight_id}: Unsupported aircraft {typecode}")
                        with open(out_dir_path / "skipped_aircraft.log", "a") as f:
                            f.write(f"{flight_id},{typecode}\n")
                    else:
                        failure_count += 1
                        logger.error(f"Failed to simulate cloned flight {flight_id}: {e}")
                
        # D. Garbage Collect Weather and cache to release memory before next iteration
        if met is not None:
            if hasattr(met, 'data'):
                met.data.close()
            met = None
        if rad is not None:
            if hasattr(rad, 'data'):
                rad.data.close()
            rad = None
        import gc
        gc.collect()
        
        # Log stats
        summary = (
            f"\n==================================================\n"
            f"CLONED SIMULATION DAILY SUMMARY - {pd.Timestamp.now()}\n"
            f"Period/Date: {desc}\n"
            f"Total flights: {len(cohort_df)}\n"
            f"Success: {success_count}\n"
            f"Skipped: {skip_count}\n"
            f"Failure: {failure_count}\n"
            f"=================================================="
        )
        logger.info(summary)
        
    # 4. Update global cloned simulation registry manifest
    if new_registry_entries:
        update_global_registry(cloned_registry_file, new_registry_entries)
        logger.info(f"Successfully updated global cloned simulation registry at {cloned_registry_file.name}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
    parser = argparse.ArgumentParser(description="Batch Cloned Trajectory Simulation Engine")
    
    # Ranks specifications (Mutually Exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str, help="Comma-separated ranks list (e.g. '1,3')")
    
    group.add_argument("--lower-rank", type=int, help="Lower bound of corridor ranks")
    parser.add_argument("--upper-rank", type=int, help="Upper bound of corridor ranks")
    
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD) for flight scheduling")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD) for flight scheduling")
    
    parser.add_argument("--weather-cache", default=str(WEATHER_DIR), help="Directory containing ERA5 NetCDF cache files")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR / "cloned_simulations"), help="Output directory for simulation results")
    parser.add_argument("--max-age", "--age", type=int, default=48, dest="max_age", help="Maximum contrail simulation/advection age in hours (default: 48)")
    parser.add_argument("--overwrite", action="store_true", help="Forces re-simulation of already simulated flights")
    parser.add_argument("--test-mode", action="store_true", help="Limits simulation to 1 flight and defaults date to 2025-01-01")
    parser.add_argument("--no-day-by-day", action="store_false", dest="day_by_day", help="Disables day-by-day temporal weather windowing and runs as a single batch")
    parser.add_argument("--min-distance", type=float, default=800.0, help="Minimum route distance in kilometers to process")
    parser.add_argument("--clusters-per-flight", "-x", type=int, default=1, help="Number of randomized synthetic tracks to sample per flight schedule (default: 1)")
    
    args = parser.parse_args()
    
    # Validate ranks
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required if --lower-rank is specified.")
        
    # Validate dates (required if not in test mode)
    if not args.test_mode and (not args.start_date or not args.end_date):
        parser.error("--start-date and --end-date are required unless --test-mode is active.")
        
    specific_ranks_list = None
    if args.ranks:
        try:
            specific_ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")
            
    # Resolve ranks
    if specific_ranks_list:
        ranks_to_process = specific_ranks_list
    else:
        ranks_to_process = list(range(args.lower_rank, args.upper_rank + 1))
        
    run_batch_clone_simulation(
        ranks=ranks_to_process,
        start_date=args.start_date,
        end_date=args.end_date,
        weather_cache=args.weather_cache,
        out_dir=args.out_dir,
        max_age_hours=args.max_age,
        overwrite=args.overwrite,
        test_mode=args.test_mode,
        day_by_day=args.day_by_day,
        min_distance=args.min_distance,
        clusters_per_flight=args.clusters_per_flight
    )

