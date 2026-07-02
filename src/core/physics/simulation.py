"""
Module 3b: Physics Simulation Engine (PSFlight + Cocip)
Consumes cleaned trajectories and cached weather data to run aircraft performance
and contrail modeling.
Refactored to use vectorized batching, parallel threading, and low-memory options.
"""

import argparse
import logging
import pandas as pd
import numpy as np
from pathlib import Path
import sys
import os

from pycontrails import DiskCacheStore
from pycontrails.datalib.ecmwf import ERA5

# Ensure we can import the adapters and configurations
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from src.common.adapters import read_flights_from_parquet, write_flights_to_parquet
from src.common.config import (
    GLOBAL_SIMULATION_REGISTRY, 
    BASE_DIR, 
    ERA5_PRESSURE_LEVEL_VARIABLES,
    ERA5_SURFACE_VARIABLES,
    ERA5_REQUIRED_PRESSURE_LEVELS,
    ERA5_GRID,
    WEATHER_BOUNDS_BBOX
)
from src.common.utils import setup_file_logger
from src.physics.engine import crop_met_dataset, simulate_flights_parallel

logger = logging.getLogger(__name__)

# Constants (Must match era5_manager.py for cache hits)
PRESSURE_LEVEL_VARIABLES = ERA5_PRESSURE_LEVEL_VARIABLES
SURFACE_VARIABLES = ERA5_SURFACE_VARIABLES
REQUIRED_PRESSURE_LEVELS = ERA5_REQUIRED_PRESSURE_LEVELS

def run_physics_pipeline(
    input_path: str, 
    out_dir: str, 
    cache_dir: str, 
    max_age_hours: int = 48,
    low_mem: bool = False,
    batch_size: int = 50,
    max_workers: int = 4
):
    setup_file_logger(log_filename="simulation.log")
    logger.info(f"Loading cleaned trajectories: {Path(input_path).name}")
    
    # Load Flight Groupings first to inspect time bounds
    flights_dict = read_flights_from_parquet(input_path)
    if not flights_dict:
        logger.error("No flights found in the input parquet file.")
        return
        
    flights_list = list(flights_dict.values())
    
    # Dynamically compute weather temporal window based on flight bounds and max_age_hours
    min_time = None
    max_time = None
    for fl in flights_list:
        f_min = pd.to_datetime(fl['time']).min()
        f_max = pd.to_datetime(fl['time']).max()
        if min_time is None or f_min < min_time:
            min_time = f_min
        if max_time is None or f_max > max_time:
            max_time = f_max
    
    if min_time is None or max_time is None:
        logger.error("Could not determine flight time bounds from coordinates.")
        return
        
    # Add 1 hour buffer to start, and max_age_hours plus 1 hour buffer to end
    weather_start = (min_time - pd.Timedelta(hours=1)).floor('h').tz_localize(None)
    weather_end = (max_time + pd.Timedelta(hours=max_age_hours + 1)).ceil('h').tz_localize(None)
    
    start_date = weather_start.strftime('%Y-%m-%dT%H:%M:%S')
    end_date = weather_end.strftime('%Y-%m-%dT%H:%M:%S')
        
    logger.info(f"Dynamically calculated weather window:")
    logger.info(f"  -> Flight bounds: {min_time} to {max_time}")
    logger.info(f"  -> Weather bounds: {start_date} to {end_date} (includes {max_age_hours}h advection padding)")
    
    # 1. Load Weather Sources (Points to Cache, does not trigger API downloads)
    cache_path = Path(cache_dir).resolve()
    disk_cache = DiskCacheStore(cache_dir=str(cache_path))
    
    logger.info(f"Initializing ERA5 interfaces via local cache at {cache_path}...")
    era5_pl = ERA5(
        time=(start_date, end_date),
        variables=PRESSURE_LEVEL_VARIABLES,
        pressure_levels=REQUIRED_PRESSURE_LEVELS,
        grid=ERA5_GRID,
        cachestore=disk_cache
    )
    
    era5_sl = ERA5(
        time=(start_date, end_date),
        variables=SURFACE_VARIABLES,
        pressure_levels=-1,
        grid=ERA5_GRID,
        cachestore=disk_cache
    )
    
    # Open weather datasets from on-disk cache
    met = era5_pl.open_metdataset()
    rad = era5_sl.open_metdataset()
    
    # Crop to WEATHER_BOUNDS_BBOX
    met = crop_met_dataset(met, WEATHER_BOUNDS_BBOX)
    rad = crop_met_dataset(rad, WEATHER_BOUNDS_BBOX)
    
    if not low_mem:
        logger.info("Loading weather datasets into RAM...")
        met.data.load()
        rad.data.load()
        
    # 2. Create output directory
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    
    # 3. Simulate Parallel Vectorized Batches
    simulated_flights, skipped_types = simulate_flights_parallel(
        flights=flights_list,
        met=met,
        rad=rad,
        max_age_hours=max_age_hours,
        batch_size=batch_size,
        max_workers=max_workers,
        low_mem=low_mem
    )
    
    success_count = len(simulated_flights)
    skip_count = len(skipped_types)
    failure_count = len(flights_list) - success_count - skip_count
    
    # Log skipped types to output directory
    for fid, typecode in skipped_types:
        logger.warning(f"Skipping flight {fid}: Unsupported aircraft {typecode}")
        with open(Path(out_dir) / "skipped_aircraft.log", "a") as f:
            f.write(f"{fid},{typecode}\n")

    summary = (
        f"\n==================================================\n"
        f"SIMULATION RUN SUMMARY - {pd.Timestamp.now()}\n"
        f"Source clean file: {input_path}\n"
        f"Total flights: {len(flights_list)}\n"
        f"Success: {success_count}\n"
        f"Skipped: {skip_count}\n"
        f"Failure: {failure_count}\n"
        f"=================================================="
    )
    logger.info(summary)

    # 4. Save the simulated flight paths
    if simulated_flights:
        out_name = Path(input_path).name.replace('_clean_si.parquet', '_simulated.parquet')
        out_path = Path(out_dir) / out_name
        write_flights_to_parquet(simulated_flights, out_path)
        
        # Update simulation registry cache index
        from src.common.utils import update_global_registry
        
        sim_registry_file = GLOBAL_SIMULATION_REGISTRY
        rel_sim_path = out_path.resolve().relative_to(BASE_DIR).as_posix()
        new_entries = [{"flight_id": fid, "file_path": rel_sim_path} for fid in [f.attrs['flight_id'] for f in simulated_flights]]
        update_global_registry(sim_registry_file, new_entries)
    else:
        logger.warning("No flights were successfully simulated.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser(description="Run PSFlight and Cocip Physics Simulation")
    parser.add_argument("--input-file", required=True, help="Path to cleaned SI trajectory Parquet file or directory containing cleaned Parquet files")
    parser.add_argument("--out-dir", required=True, help="Output directory for simulation results")
    parser.add_argument("--weather-cache", required=True, help="Directory containing ERA5 NetCDF cache files")
    parser.add_argument("--max-age", "--age", type=int, default=48, dest="max_age", help="Maximum contrail simulation/advection age in hours (default: 48)")
    
    # New Optimization and RAM options
    parser.add_argument("--low-mem", action="store_true", help="Enforces low-RAM operations (lazy datasets, sequential workers)")
    parser.add_argument("--batch-size", type=int, default=50, help="Size of flight batches for vectorized execution (default: 50)")
    parser.add_argument("--max-workers", type=int, default=4, help="Number of concurrent worker threads (default: 4)")
    
    args = parser.parse_args()
    
    input_path = Path(args.input_file)
    if not input_path.exists():
        logger.error(f"Input path does not exist: {input_path}")
        exit(1)
        
    if input_path.is_dir():
        clean_files = list(input_path.glob("*_clean_si.parquet"))
        if not clean_files:
            logger.warning(f"No *_clean_si.parquet files found in directory: {input_path}")
            exit(0)
            
        logger.info(f"Found {len(clean_files)} cleaned files in directory: {input_path}")
        for clean_file in clean_files:
            expected_out_name = clean_file.name.replace('_clean_si.parquet', '_simulated.parquet')
            expected_out_path = Path(args.out_dir) / expected_out_name
            if expected_out_path.exists():
                logger.info(f"Simulation output file already exists: {expected_out_path}. Skipping.")
                continue
                
            try:
                run_physics_pipeline(
                    input_path=str(clean_file),
                    out_dir=args.out_dir,
                    cache_dir=args.weather_cache,
                    max_age_hours=args.max_age,
                    low_mem=args.low_mem,
                    batch_size=args.batch_size,
                    max_workers=args.max_workers
                )
            except Exception as e:
                logger.error(f"Failed to process file {clean_file.name}: {e}")
    else:
        run_physics_pipeline(
            input_path=str(input_path),
            out_dir=args.out_dir,
            cache_dir=args.weather_cache,
            max_age_hours=args.max_age,
            low_mem=args.low_mem,
            batch_size=args.batch_size,
            max_workers=args.max_workers
        )