"""
Module 3c: Batch Clone Simulation Engine
Loads a synthesized trajectory, clones it, shifts its departure times in-memory
to match individual flights' schedules, and simulates them under Cocip/PSFlight.
Refactored to use vectorized batching, parallel threading, and low-memory options.
"""

import argparse
import logging
from pathlib import Path
import sys
import os
import pandas as pd
import numpy as np
import time

from concurrent.futures import ThreadPoolExecutor
from pycontrails import Flight, DiskCacheStore, MetDataset
from pycontrails.datalib.ecmwf import ERA5

# Add project root to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from src.common.config import (
    BASE_DIR, WEATHER_DIR, RESULTS_DIR, MASTER_FLIGHTS_FILE,
    GLOBAL_CORRIDOR_SIM_REGISTRY,
    ERA5_PRESSURE_LEVEL_VARIABLES, ERA5_SURFACE_VARIABLES,
    ERA5_REQUIRED_PRESSURE_LEVELS, ERA5_GRID, WEATHER_BOUNDS_BBOX
)
from src.common.utils import load_route_summary, split_route_string, update_global_registry, setup_file_logger
from src.common.adapters import read_flights_from_parquet, write_flights_to_parquet
from src.physics.engine import crop_met_dataset, simulate_flights_parallel, create_simulation_models

logger = logging.getLogger(__name__)

def prepare_cloned_flight(
    flight_row: pd.Series,
    base_flight: Flight,
    flight_id: str
) -> Flight:
    """Clones the base flight path, shifts to match schedule, and maps metadata."""
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
    
    # Drop static metadata columns from the dataframe to avoid duplicate warnings and conflicts
    metadata_cols = [
        'flight_id', 'icao24', 'callsign', 'typecode', 'firstseen', 'lastseen',
        'estdepartureairport', 'estarrivalairport', 'route_class', 'cluster_id'
    ]
    df_cloned = df_cloned.drop(columns=[col for col in metadata_cols if col in df_cloned.columns], errors='ignore')
    
    fl_cloned = Flight(data=df_cloned, drop_duplicated_times=True, crs="EPSG:4326", **attrs)
    return fl_cloned

def simulate_cloned_flight(
    fl_cloned: Flight, 
    cache_dir: str, 
    max_age_hours: int,
    met_dataset=None,
    rad_dataset=None
) -> Flight:
    """
    Simulates a single shifted flight using Cocip and PSFlight.
    Maintained for backward compatibility.
    """
    # Resolve times
    fl_times = pd.to_datetime(fl_cloned['time'])
    min_time = fl_times.min()
    max_time = fl_times.max()
    
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
        
    ps_model, cocip_model = create_simulation_models(met, rad, max_age_hours, low_mem=False)
    
    typecode = fl_cloned.attrs.get('aircraft_type')
    if not typecode or pd.isna(typecode):
        raise ValueError(f"Unsupported aircraft: {typecode} (Missing/NaN)")
        
    ps_supported_types = list(ps_model.aircraft_engine_params.keys())
    if typecode not in ps_supported_types:
        raise ValueError(f"Unsupported aircraft: {typecode}")
        
    fl_evaluated = ps_model.eval(fl_cloned)
    fl_out = cocip_model.eval(source=fl_evaluated)
    return fl_out

def simulate_single_flight(
    flight_row: pd.Series,
    base_flight: Flight,
    flight_id: str,
    weather_cache_dir: str,
    max_age_hours: int = 48,
    met_dataset=None,
    rad_dataset=None
) -> Flight:
    """Clones base flight, shifts times, and simulates. Maintained for backward compatibility."""
    fl_cloned = prepare_cloned_flight(flight_row, base_flight, flight_id)
    return simulate_cloned_flight(
        fl_cloned=fl_cloned,
        cache_dir=weather_cache_dir,
        max_age_hours=max_age_hours,
        met_dataset=met_dataset,
        rad_dataset=rad_dataset
    )

def filter_cohort_flights(
    master_df: pd.DataFrame,
    df_summary: pd.DataFrame,
    start_date: str = None,
    end_date: str = None,
    ranks: list = None,
    out_dir: str = None,
    overwrite: bool = False,
    min_distance: float = 800.0,
    valid_routes_set: set = None
) -> pd.DataFrame:
    """
    Filters the master registry by ranks, distance, availability, date range, and loops.
    """
    # 1. Filter RouteSummary by requested ranks (original ranks)
    df_ranks = df_summary[df_summary['rank'].isin(ranks)].copy() if ranks else df_summary.copy()
    
    # 2. Filter by distance
    if min_distance is not None:
        df_ranks = df_ranks[df_ranks['distance_m'] >= min_distance * 1000.0]
        
    # 3. Filter by availability
    if valid_routes_set is not None:
        df_ranks = df_ranks[df_ranks['route_key'].isin(valid_routes_set)]
        
    fully_filtered_ranks = set(df_ranks['route_key'].unique())
    
    # 4. Filter master flights by date range
    df_filtered = master_df.copy()
    
    # Convert datetime columns to timezone-naive UTC
    for col in ['firstseen', 'lastseen']:
        if col in df_filtered.columns:
            if not pd.api.types.is_datetime64_any_dtype(df_filtered[col]):
                df_filtered[col] = pd.to_datetime(df_filtered[col], utc=True)
            if df_filtered[col].dt.tz is not None:
                df_filtered[col] = df_filtered[col].dt.tz_convert('UTC').dt.tz_localize(None)
                
    if start_date:
        start_dt = pd.to_datetime(start_date, utc=True).tz_localize(None)
        df_filtered = df_filtered[df_filtered['firstseen'] >= start_dt]
        
    if end_date:
        end_dt_str = f"{end_date} 23:59:59" if len(end_date) <= 10 else end_date
        end_dt = pd.to_datetime(end_dt_str, utc=True).tz_localize(None)
        df_filtered = df_filtered[df_filtered['firstseen'] <= end_dt]
        
    if df_filtered.empty:
        return df_filtered
        
    # 5. Drop airport loops
    dep_col = 'estdepartureairport' if 'estdepartureairport' in df_filtered.columns else 'estdepatureairport'
    arr_col = 'estarrivalairport'
    if dep_col in df_filtered.columns and arr_col in df_filtered.columns:
        df_filtered = df_filtered[df_filtered[dep_col] != df_filtered[arr_col]]
        
    # 6. Filter against the fully filtered rank list
    df_filtered['route_key'] = df_filtered[dep_col] + '-' + df_filtered[arr_col]
    df_filtered = df_filtered[df_filtered['route_key'].isin(fully_filtered_ranks)].copy()
    
    if df_filtered.empty:
        return df_filtered

    if not overwrite:
        logger.info("Filtering out already simulated flights...")
        cloned_registry_file = GLOBAL_CORRIDOR_SIM_REGISTRY
        simulated_ids = set()
        if cloned_registry_file.exists():
            try:
                df_sim_reg = pd.read_parquet(cloned_registry_file)
                simulated_ids = set(df_sim_reg['flight_id'].unique())
            except Exception as e:
                logger.warning(f"Could not load cloned registry file: {e}")
                
        keep_mask = []
        out_dir_path = Path(out_dir)
        for _, row in df_filtered.iterrows():
            dep = row[dep_col]
            arr = row[arr_col]
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
            
    df_filtered = df_filtered.sort_values(by=['estdepartureairport', 'estarrivalairport', 'firstseen']).copy()
    return df_filtered

def _open_crop_and_load(era5_obj: ERA5, bbox: list[float], low_mem: bool) -> MetDataset:
    """Helper to open, crop, and conditionally load a single ERA5 dataset in a thread-safe manner."""
    met_ds = era5_obj.open_metdataset()
    met_ds_cropped = crop_met_dataset(met_ds, bbox)
    if not low_mem:
        met_ds_cropped.data.load()
    return met_ds_cropped

def load_and_crop_weather(
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
    weather_cache_dir: str,
    bbox: list[float],
    low_mem: bool = False
) -> tuple[MetDataset, MetDataset]:
    """Loads PL and SL weather datasets for a time range, crops them, and loads them in parallel."""
    start_str = start_time.strftime('%Y-%m-%dT%H:%M:%S')
    end_str = end_time.strftime('%Y-%m-%dT%H:%M:%S')
    
    cache_path = Path(weather_cache_dir)
    hours = pd.date_range(start=start_time, end=end_time, freq='h')
    
    pl_paths = []
    sl_paths = []
    all_pl_exist = True
    all_sl_exist = True
    missing_pl = []
    missing_sl = []
    
    for h in hours:
        pl_name = f"{h.strftime('%Y%m%d-%H')}-era5pl0.5reanalysis.nc"
        sl_name = f"{h.strftime('%Y%m%d-%H')}-era5sl0.5reanalysis.nc"
        pl_file = cache_path / pl_name
        sl_file = cache_path / sl_name
        
        if pl_file.exists():
            pl_paths.append(str(pl_file))
        else:
            all_pl_exist = False
            missing_pl.append(pl_name)
        if sl_file.exists():
            sl_paths.append(str(sl_file))
        else:
            all_sl_exist = False
            missing_sl.append(sl_name)
            
    logger.info(f"Weather cache check from {start_str} to {end_str} ({len(hours)} hours):")
    logger.info(f"  -> PL (Pressure Levels): {len(pl_paths)}/{len(hours)} found.")
    if missing_pl:
        logger.warning(f"     Missing PL files: {missing_pl[:3]} ... (total {len(missing_pl)} missing)")
    logger.info(f"  -> SL (Surface Levels):  {len(sl_paths)}/{len(hours)} found.")
    if missing_sl:
        logger.warning(f"     Missing SL files: {missing_sl[:3]} ... (total {len(missing_sl)} missing)")
        
    if all_pl_exist and all_sl_exist and len(pl_paths) > 0 and len(sl_paths) > 0:
        logger.info(f"Offline Mode: Opening and cropping weather files in parallel...")
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
        except Exception as offline_err:
            logger.warning(f"Failed to initialize datasets offline: {offline_err}. Falling back to standard...")
            all_pl_exist = False
            
    if not all_pl_exist or not all_sl_exist:
        logger.info(f"Online Mode: Resolving weather files with cache store in parallel...")
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
        
    # Execute Open -> Crop -> Load concurrently in two threads
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_pl = executor.submit(_open_crop_and_load, era5_pl, bbox, low_mem)
        future_sl = executor.submit(_open_crop_and_load, era5_sl, bbox, low_mem)
        
        met = future_pl.result()
        rad = future_sl.result()
        
    return met, rad

def run_batch_clone_simulation(
    ranks: list,
    start_date: str = None,
    end_date: str = None,
    weather_cache: str = WEATHER_DIR,
    out_dir: str = None,
    max_age_hours: int = 48,
    overwrite: bool = False,
    test_mode: bool = False,
    day_by_day: bool = True,
    min_distance: float = 800.0,
    clusters_per_flight: int = 1,
    low_mem: bool = False,
    batch_size: int = 50,
    max_workers: int = 4
):
    master_flights_file = MASTER_FLIGHTS_FILE
    cloned_registry_file = GLOBAL_CORRIDOR_SIM_REGISTRY
    
    setup_file_logger(log_filename="clone_simulation.log")
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    
    if test_mode:
        logger.info("=== RUNNING IN TEST MODE ===")
        start_date = "2025-12-03"
        end_date = "2025-12-04"
        day_by_day = False
        
    if not start_date or not end_date:
        logger.error("Start date and end date are required (unless in test mode).")
        return
        
    dates = pd.date_range(start=start_date, end=end_date, freq='D')
    logger.info(f"Targeting date range: {start_date} to {end_date} ({len(dates)} days)")
    
    df_summary = load_route_summary(None)
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing.")
        return
        
    # Saving relevant File Paths for synthesized clusters
    from src.common.registry_utils import load_synthesized_paths_map
    valid_synthesized_files = {k: p for k, p in load_synthesized_paths_map().items() if p.exists()}
                
    valid_routes_set = {route for (route, cluster_id) in valid_synthesized_files.keys()}
    
    def get_route_key(r_str):
        dep, arr = split_route_string(r_str)
        return f"{dep}-{arr}"
    df_summary['route_key'] = df_summary['route'].apply(get_route_key)
    
    # Pre-resolve target route keys for descriptive log output
    df_ranks = df_summary[df_summary['rank'].isin(ranks)].copy() if ranks else df_summary.copy()
    if min_distance is not None:
        df_ranks = df_ranks[df_ranks['distance_m'] >= min_distance * 1000.0]
    df_ranks = df_ranks[df_ranks['route_key'].isin(valid_routes_set)]
    
    logger.info(f"Resolved requested ranks {ranks} to target routes:")
    for _, row in df_ranks.sort_values(by='rank').iterrows():
        logger.info(f"  -> Rank {row['rank']}: {row['route']} ({row['distance_m']/1000.0:.1f} km)")
        
    if df_ranks.empty:
        logger.warning(f"No valid target routes matched the requested ranks {ranks} after applying distance and availability filters.")
        return
        
    if not master_flights_file.exists():
        logger.error(f"Master flights file not found at: {master_flights_file}")
        return
        
    logger.info(f"Loading master flights database from {master_flights_file} (optimized columns)...")
    req_cols = ['firstseen', 'estdepartureairport', 'estarrivalairport', 'icao24', 'callsign', 'typecode', 'lastseen']
    master_df = pd.read_parquet(master_flights_file, columns=req_cols)
    logger.info(f"Loaded master flights registry with {len(master_df):,} entries.")
    
    new_registry_entries = []
                
    if not day_by_day:
        date_groups = [(start_date, end_date, "Full Batch")]
    else:
        date_groups = [(d.strftime('%Y-%m-%d'), d.strftime('%Y-%m-%d'), d.strftime('%Y-%m-%d')) for d in dates]
        
    # Weather Rolling Cache State
    met_cache = {}
    rad_cache = {}
    
    global_scheduled = 0
    global_trajectories = 0
    global_success = 0
    global_skipped = 0
    global_failed = 0
    performance_metrics = []
    
    #
    for d_start, d_end, desc in date_groups:
        logger.info(f"\n--- Processing cohort for: {desc} ---")
        day_start_time = time.time()
        
        cohort_df = filter_cohort_flights(
            master_df=master_df,
            df_summary=df_summary,
            start_date=d_start,
            end_date=d_end,
            ranks=ranks,
            out_dir=str(out_dir_path),
            overwrite=overwrite,
            min_distance=min_distance,
            valid_routes_set=valid_routes_set
        )
        
        if cohort_df.empty:
            logger.info(f"No flights to simulate for {desc}.")
            continue
            
        if test_mode:
            logger.info("Test Mode: Slicing cohort to first 1 entries.")
            cohort_df = cohort_df.head(1)
            
        logger.info(f"Found {len(cohort_df)} flights to simulate for {desc}.")
        
        # Weather loading
        if not day_by_day:
            times = pd.to_datetime(cohort_df['firstseen'])
            min_time = times.min()
            if 'lastseen' in cohort_df.columns:
                max_time = pd.to_datetime(cohort_df['lastseen']).max()
            else:
                max_time = min_time + pd.Timedelta(hours=15)
                
            weather_start = (min_time - pd.Timedelta(hours=1)).floor('h')
            if hasattr(weather_start, 'tz') and weather_start.tz is not None:
                weather_start = weather_start.tz_convert('UTC').tz_localize(None)
                
            weather_end = (max_time + pd.Timedelta(hours=max_age_hours + 1)).ceil('h')
            if hasattr(weather_end, 'tz') and weather_end.tz is not None:
                weather_end = weather_end.tz_convert('UTC').tz_localize(None)

            met, rad = load_and_crop_weather(
                start_time=weather_start,
                end_time=weather_end,
                weather_cache_dir=weather_cache,
                bbox=WEATHER_BOUNDS_BBOX,
                low_mem=low_mem
            )
        else:
            # Day-by-Day Rolling Window (Day N, Day N+1, Day N+2)
            d_time = pd.to_datetime(d_start)
            needed_dates = [d_time, d_time + pd.Timedelta(days=1), d_time + pd.Timedelta(days=2)]
            
            # Evict expired dates from cache
            for cached_date in list(met_cache.keys()):
                if cached_date not in needed_dates:
                    logger.info(f"Evicting weather for {cached_date.strftime('%Y-%m-%d')} from rolling cache.")
                    if hasattr(met_cache[cached_date].data, 'close'):
                        met_cache[cached_date].data.close()
                    if hasattr(rad_cache[cached_date].data, 'close'):
                        rad_cache[cached_date].data.close()
                    met_cache.pop(cached_date)
                    rad_cache.pop(cached_date)
                    
            # Load new days into cache
            for nd in needed_dates:
                if nd not in met_cache:
                    logger.info(f"Cache Miss: Resolving weather for {nd.strftime('%Y-%m-%d')}...")
                    try:
                        met_day, rad_day = load_and_crop_weather(
                            start_time=nd,
                            end_time=nd + pd.Timedelta(hours=23),
                            weather_cache_dir=weather_cache,
                            bbox=WEATHER_BOUNDS_BBOX,
                            low_mem=low_mem
                        )
                        met_cache[nd] = met_day
                        rad_cache[nd] = rad_day
                    except Exception as e:
                        logger.error(f"Failed to load weather for date {nd.strftime('%Y-%m-%d')}: {e}")
                        
            active_dates = sorted(list(met_cache.keys()))
            if not active_dates:
                logger.error("No weather datasets available in cache. Skipping cohort day.")
                continue
                
            import xarray as xr
            met = MetDataset(xr.concat([met_cache[ad].data for ad in active_dates], dim='time'))
            rad = MetDataset(xr.concat([rad_cache[ad].data for ad in active_dates], dim='time'))
            
        daily_trajectories = 0
        daily_success = 0
        daily_failed = 0
        daily_skipped = 0
        
        flights_to_simulate = []
        flight_to_meta_map = {}
        cached_base_flights = {}
        
        # Prepare Cloned Flights in memory
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
            
            available_clusters = [cid for (r, cid) in valid_synthesized_files.keys() if r == route_key]
            if not available_clusters:
                logger.error(f"No synthesized base paths available for route {route_key}. Skipping flight.")
                continue
                
            sample_size = min(clusters_per_flight, len(available_clusters))
            sampled_clusters = np.random.choice(available_clusters, size=sample_size, replace=False)
            
            for cluster_id in sampled_clusters:
                flight_id = f"{icao24}_{callsign}_{dep}-{arr}_{fs_str}_c{cluster_id}"
                out_file = corridor_out_dir / f"{flight_id}_simulated.parquet"
                
                if not overwrite and out_file.exists():
                    logger.info(f"Skipping {flight_id}: output file exists.")
                    daily_skipped += 1
                    daily_trajectories += 1
                    rel_out_path = out_file.resolve().relative_to(BASE_DIR).as_posix()
                    new_registry_entries.append({"flight_id": flight_id, "file_path": rel_out_path})
                    continue
                    
                base_flight = cached_base_flights.get((route_key, cluster_id))
                if base_flight is None:
                    synth_path = valid_synthesized_files.get((route_key, cluster_id))
                    if not synth_path:
                        logger.error(f"Synthesized base path missing for route {route_key} cluster {cluster_id}. Skipping.")
                        continue
                        
                    synth_flights = read_flights_from_parquet(str(synth_path))
                    if not synth_flights:
                        logger.error(f"Empty synthesized file for route {route_key} cluster {cluster_id}. Skipping.")
                        continue
                    base_flight = list(synth_flights.values())[0]
                    cached_base_flights[(route_key, cluster_id)] = base_flight
                    
                try:
                    fl_cloned = prepare_cloned_flight(
                        flight_row=row,
                        base_flight=base_flight,
                        flight_id=flight_id
                    )
                    flights_to_simulate.append(fl_cloned)
                    flight_to_meta_map[flight_id] = (row, out_file, cluster_id, route_key)
                    daily_trajectories += 1
                except Exception as e:
                    logger.error(f"Failed to clone flight {flight_id}: {e}")
                    daily_failed += 1
                    daily_trajectories += 1
                    
        # Simulate Parallel Vectorized batches
        if flights_to_simulate:
            simulated_results, skipped_types = simulate_flights_parallel(
                flights=flights_to_simulate,
                met=met,
                rad=rad,
                max_age_hours=max_age_hours,
                batch_size=batch_size,
                max_workers=max_workers,
                low_mem=low_mem
            )
            
            # Serialize outputs
            for fl in simulated_results:
                fid = fl.attrs['flight_id']
                row, out_file, cluster_id, route_key = flight_to_meta_map[fid]
                try:
                    write_flights_to_parquet([fl], out_file)
                    rel_out_path = out_file.resolve().relative_to(BASE_DIR).as_posix()
                    new_registry_entries.append({"flight_id": fid, "file_path": rel_out_path})
                    daily_success += 1
                except Exception as e:
                    logger.error(f"Failed to serialize simulated flight {fid}: {e}")
                    daily_failed += 1
                    
            # Log skipped types
            for fid, typecode in skipped_types:
                daily_skipped += 1
                logger.warning(f"Skipping flight {fid}: Unsupported aircraft {typecode}")
                with open(out_dir_path / "skipped_aircraft.log", "a") as f:
                    f.write(f"{fid},{typecode}\n")
                    
        # D. Garbage Collect weather datasets
        met = None
        rad = None
        import gc
        gc.collect()
        
        # Accumulate to global counters
        global_scheduled += len(cohort_df)
        global_trajectories += daily_trajectories
        global_success += daily_success
        global_skipped += daily_skipped
        global_failed += daily_failed
        
        # Calculate daily elapsed time
        elapsed = time.time() - day_start_time
        avg_time_per_traj = elapsed / daily_trajectories if daily_trajectories > 0 else 0
        performance_metrics.append({
            "date": desc,
            "scheduled": len(cohort_df),
            "trajectories": daily_trajectories,
            "time_s": elapsed
        })
        
        summary = (
            f"\n==================================================\n"
            f"CLONED SIMULATION DAILY SUMMARY - {pd.Timestamp.now()}\n"
            f"Period/Date: {desc}\n"
            f"Cohort Scheduled Flights: {len(cohort_df)}\n"
            f"Total Trajectories: {daily_trajectories}\n"
            f"Success (Trajectories): {daily_success}\n"
            f"Skipped (Trajectories): {daily_skipped}\n"
            f"Failure (Trajectories): {daily_failed}\n"
            f"Time Elapsed: {elapsed:.2f} seconds ({avg_time_per_traj:.2f}s per trajectory)\n"
            f"=================================================="
        )
        logger.info(summary)
        
    # Clear the weather cache from memory
    for cached_date in list(met_cache.keys()):
        if hasattr(met_cache[cached_date].data, 'close'):
            met_cache[cached_date].data.close()
        if hasattr(rad_cache[cached_date].data, 'close'):
            rad_cache[cached_date].data.close()
    met_cache.clear()
    rad_cache.clear()
    
    # Final consolidated performance summary
    if performance_metrics:
        total_time = sum(m['time_s'] for m in performance_metrics)
        avg_time_per_day = total_time / len(performance_metrics)
        
        breakdown_str = ""
        for m in performance_metrics:
            breakdown_str += f"  - {m['date']}: {m['scheduled']} flights ({m['trajectories']} trajectories) in {m['time_s']:.2f}s\n"
            
        def format_duration(seconds):
            mins = int(seconds // 60)
            secs = seconds % 60
            if mins > 0:
                return f"{mins}m {secs:.1f}s"
            return f"{secs:.1f}s"
            
        final_summary = (
            f"\n==================================================\n"
            f"CLONED BATCH RUN PERFORMANCE SUMMARY\n"
            f"Total Simulation Days: {len(performance_metrics)}\n"
            f"Total Scheduled Flights: {global_scheduled}\n"
            f"Total Trajectories: {global_trajectories}\n"
            f"Overall Success: {global_success}\n"
            f"Overall Skipped: {global_skipped}\n"
            f"Overall Failure: {global_failed}\n"
            f"Total Execution Time: {format_duration(total_time)} (Avg: {format_duration(avg_time_per_day)} per day)\n"
            f"\nBreakdown:\n{breakdown_str}"
            f"=================================================="
        )
        logger.info(final_summary)
    
    if new_registry_entries:
        update_global_registry(cloned_registry_file, new_registry_entries)
        logger.info(f"Successfully updated global cloned simulation registry at {cloned_registry_file.name}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
    parser = argparse.ArgumentParser(description="Batch Cloned Trajectory Simulation Engine")
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str, help="Comma-separated ranks list (e.g. '1,3')")
    group.add_argument("--lower-rank", type=int, help="Lower bound of corridor ranks")
    parser.add_argument("--upper-rank", type=int, help="Upper bound of corridor ranks")
    
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD) for flight scheduling")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD) for flight scheduling")
    
    parser.add_argument("--weather-cache", default=str(WEATHER_DIR), help="Directory containing ERA5 NetCDF cache files")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR / "corridor_simulations"), help="Output directory for simulation results")
    parser.add_argument("--max-age", "--age", type=int, default=48, dest="max_age", help="Maximum contrail simulation/advection age in hours (default: 48)")
    parser.add_argument("--overwrite", action="store_true", help="Forces re-simulation of already simulated flights")
    parser.add_argument("--test-mode", action="store_true", help="Limits simulation to 1 flight and defaults date to 2025-01-01")
    parser.add_argument("--no-day-by-day", action="store_false", dest="day_by_day", help="Disables day-by-day temporal weather windowing and runs as a single batch")
    parser.add_argument("--min-distance", type=float, default=800.0, help="Minimum route distance in kilometers to process")
    parser.add_argument("--clusters-per-flight", "-x", type=int, default=1, help="Number of randomized synthetic tracks to sample per flight schedule (default: 1)")
    
    # New Optimization and RAM options
    parser.add_argument("--low-mem", action="store_true", help="Enforces low-RAM operations (lazy datasets, sequential workers)")
    parser.add_argument("--batch-size", type=int, default=50, help="Size of flight batches for vectorized execution (default: 50)")
    parser.add_argument("--max-workers", type=int, default=4, help="Number of concurrent worker threads (default: 4)")
    
    args = parser.parse_args()
    
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required if --lower-rank is specified.")
        
    if not args.test_mode and (not args.start_date or not args.end_date):
        parser.error("--start-date and --end-date are required unless --test-mode is active.")
        
    specific_ranks_list = None
    if args.ranks:
        try:
            specific_ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")
            
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
        clusters_per_flight=args.clusters_per_flight,
        low_mem=args.low_mem,
        batch_size=args.batch_size,
        max_workers=args.max_workers
    )
