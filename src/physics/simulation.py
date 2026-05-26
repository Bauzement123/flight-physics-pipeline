"""
Module 3b: Physics Simulation Engine (PSFlight + Cocip)
Consumes cleaned trajectories and cached weather data to run aircraft performance
and contrail modeling. 

Paradigm: Functional
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
from pycontrails.models.ps_model import PSFlight
from pycontrails.models.cocip import Cocip
from pycontrails.models.humidity_scaling import ConstantHumidityScaling

# Ensure we can import the adapter from the processing module
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from src.processing.traffic_adapter import extract_flights_from_parquet, dataframe_to_pycontrails

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants (Must match era5_manager.py for cache hits)
PRESSURE_LEVEL_VARIABLES = [
    "air_temperature", "specific_humidity", "eastward_wind", 
    "northward_wind", "lagrangian_tendency_of_air_pressure", "specific_cloud_ice_water_content"
]
SURFACE_VARIABLES = ["top_net_solar_radiation", "top_net_thermal_radiation"]
REQUIRED_PRESSURE_LEVELS = [
    900, 850, 800, 750, 700, 650, 600, 550, 500, 
    450, 400, 350, 300, 250, 225, 200, 150
]

def run_physics_pipeline(input_path: str, out_dir: str, cache_dir: str, max_age_hours: int = 48):
    logger.info(f"Loading cleaned trajectories: {Path(input_path).name}")
    
    # Load Flight Groupings first to inspect time bounds
    flights_dict = extract_flights_from_parquet(input_path)
    if not flights_dict:
        logger.error("No flights found in the input parquet file.")
        return
        
    # Dynamically compute weather temporal window based on flight bounds and max_age_hours
    min_time = None
    max_time = None
    for flight_id, group_df in flights_dict.items():
        for col in ['time', 'timestamp']:
            if col in group_df.columns:
                f_min = pd.to_datetime(group_df[col]).min()
                f_max = pd.to_datetime(group_df[col]).max()
                if min_time is None or f_min < min_time:
                    min_time = f_min
                if max_time is None or f_max > max_time:
                    max_time = f_max
                break
    
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
        grid=0.5,
        cachestore=disk_cache
    )
    
    era5_sl = ERA5(
        time=(start_date, end_date),
        variables=SURFACE_VARIABLES,
        pressure_levels=-1,
        grid=0.5,
        cachestore=disk_cache
    )
    
    # Open weather datasets from on-disk cache
    met = era5_pl.open_metdataset()
    rad = era5_sl.open_metdataset()
    
    # 2. Initialize Models
    # PSFlight calculates thrust, fuel flow, TAS, and emissions
    # Cocip calculates the contrail formation and radiative forcing
    logger.info("Initializing PSFlight and Cocip models with thesis parameters...")
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

    # 3. Load Flight Groupings (already loaded at start of function)
    simulated_dataframes = []
    
    logger.info(f"Found {len(flights_dict)} flights to simulate.")

    # Create output directory and setup simulation.log
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    sim_log_path = Path(out_dir) / "simulation.log"
    
    # 4. Simulation Loop
    success_count = 0
    failure_count = 0
    log_messages = []
    
    ps_supported_types = list(ps_model.aircraft_engine_params.keys())
    
    for flight_id, group_df in flights_dict.items():
        logger.info(f"Simulating {flight_id}...")
        
        # Get typecode dynamically from metadata or default to B738
        typecode = group_df['typecode'].iloc[0] if 'typecode' in group_df.columns else "B738"
        
        # Validate typecode and resolve fallback mappings
        if typecode not in ps_supported_types:
            fallback = "B738"
            if typecode.startswith("A32") or typecode.startswith("A31") or typecode.startswith("A2"):
                fallback = "A320"
            elif typecode.startswith("B73") or typecode.startswith("B3"):
                fallback = "B738"
            logger.warning(f"Aircraft type '{typecode}' is not supported by PSFlight. Falling back to '{fallback}'.")
            log_messages.append(f"[{flight_id}] Warning: aircraft type '{typecode}' not supported. Falling back to '{fallback}'.")
            typecode = fallback
            
        # Adapt to pycontrails Flight
        fl = dataframe_to_pycontrails(group_df, typecode)
        if fl is None:
            failure_count += 1
            log_messages.append(f"[{flight_id}] Failed: Adapter returned None.")
            continue
            
        try:
            # Evaluate aircraft performance and contrail modeling
            fl = ps_model.eval(fl)
            fl_out = cocip_model.eval(source=fl)
            
            # Convert back to DataFrame
            df_sim = fl_out.to_dataframe()
            
            # Reinject metadata attributes
            for attr in ['flight_id', 'icao24', 'callsign', 'typecode', 'estdepartureairport', 'estarrivalairport', 'firstseen', 'lastseen']:
                if attr in group_df.columns:
                    df_sim[attr] = group_df[attr].iloc[0]
                    
            simulated_dataframes.append(df_sim)
            success_count += 1
            log_messages.append(f"[{flight_id}] Success: Simulated successfully.")
        except Exception as e:
            failure_count += 1
            logger.error(f"Error simulating flight {flight_id}: {e}")
            log_messages.append(f"[{flight_id}] Failed: {str(e)}")

    # Write simulation statistics and logs to simulation.log
    with open(sim_log_path, "a") as log_file:
        log_file.write(f"\n==================================================\n")
        log_file.write(f"SIMULATION RUN SUMMARY - {pd.Timestamp.now()}\n")
        log_file.write(f"Source clean file: {input_path}\n")
        log_file.write(f"Total flights: {len(flights_dict)}\n")
        log_file.write(f"Success: {success_count}\n")
        log_file.write(f"Failure: {failure_count}\n")
        log_file.write(f"==================================================\n")
        for msg in log_messages:
            log_file.write(msg + "\n")
            
    logger.info(f"Simulation run complete. Status details written to {sim_log_path}")

    # 5. Save the simulated flight paths
    if simulated_dataframes:
        consolidated_df = pd.concat(simulated_dataframes, ignore_index=True)
        out_name = Path(input_path).name.replace('_clean_si.parquet', '_simulated.parquet')
        out_path = Path(out_dir) / out_name
        consolidated_df.to_parquet(out_path, index=False)
        logger.info(f"✓ Saved {len(consolidated_df):,} simulated waypoints to {out_path}")
        
        # Update simulation registry cache index
        from src.common.config import FLIGHT_REGISTRY_DIR, BASE_DIR
        from src.common.utils import update_global_registry
        
        sim_registry_file = FLIGHT_REGISTRY_DIR / "global_simulation_registry.parquet"
        rel_sim_path = out_path.resolve().relative_to(BASE_DIR).as_posix()
        new_entries = [{"flight_id": fid, "file_path": rel_sim_path} for fid in consolidated_df['flight_id'].unique()]
        update_global_registry(sim_registry_file, new_entries)
    else:
        logger.warning("No flights were successfully simulated.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run PSFlight and Cocip Physics Simulation")
    parser.add_argument("--input-file", required=True, help="Path to cleaned SI trajectory Parquet file or directory containing cleaned Parquet files")
    parser.add_argument("--out-dir", required=True, help="Output directory for simulation results")
    parser.add_argument("--weather-cache", required=True, help="Directory containing ERA5 NetCDF cache files")
    parser.add_argument("--max-age", "--age", type=int, default=48, dest="max_age", help="Maximum contrail simulation/advection age in hours (default: 48)")
    
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
                    max_age_hours=args.max_age
                )
            except Exception as e:
                logger.error(f"Failed to simulate {clean_file.name}: {e}")
    else:
        run_physics_pipeline(
            input_path=args.input_file,
            out_dir=args.out_dir,
            cache_dir=args.weather_cache,
            max_age_hours=args.max_age
        )