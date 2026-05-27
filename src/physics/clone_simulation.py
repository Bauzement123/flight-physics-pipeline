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
from src.common.config import BASE_DIR, FLIGHT_REGISTRY_DIR
from src.common.utils import load_route_summary, split_route_string, update_global_registry
from src.common.adapters import read_flights_from_parquet, write_flights_to_parquet

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger(__name__)

# Constants matching simulation standards
PRESSURE_LEVEL_VARIABLES = [
    "air_temperature", "specific_humidity", "eastward_wind", 
    "northward_wind", "lagrangian_tendency_of_air_pressure", "specific_cloud_ice_water_content"
]
SURFACE_VARIABLES = ["top_net_solar_radiation", "top_net_thermal_radiation"]
REQUIRED_PRESSURE_LEVELS = [
    900, 850, 800, 750, 700, 650, 600, 550, 500, 
    450, 400, 350, 300, 250, 225, 200, 150
]

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
    
    # Resolve and validate aircraft type fallback
    typecode = fl_cloned.attrs.get('aircraft_type', 'B738')
    ps_supported_types = list(ps_model.aircraft_engine_params.keys())
    if typecode not in ps_supported_types:
        fallback = "B738"
        if typecode.startswith("A32") or typecode.startswith("A31") or typecode.startswith("A2"):
            fallback = "A320"
        elif typecode.startswith("B73") or typecode.startswith("B3"):
            fallback = "B738"
        fl_cloned.attrs['aircraft_type'] = fallback
        
    fl_evaluated = ps_model.eval(fl_cloned)
    fl_out = cocip_model.eval(source=fl_evaluated)
    return fl_out

def run_batch_clone_simulation(
    ranks: list, 
    weather_cache: str, 
    out_dir: str, 
    max_age_hours: int = 48,
    test_mode: bool = False
):
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing.")
        return
        
    synthesized_registry_file = FLIGHT_REGISTRY_DIR / "global_synthesized_registry.parquet"
    if not synthesized_registry_file.exists():
        logger.error(f"Global synthesized registry does not exist at {synthesized_registry_file}. Run path synthesis first.")
        return
        
    df_synth_reg = pd.read_parquet(synthesized_registry_file)
    
    # Output Dir
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    
    # Simulation Log
    sim_log_path = out_dir_path / "simulation.log"
    
    new_registry_entries = []
    
    for rank in ranks:
        route_row = df_summary[df_summary['rank'] == rank]
        if route_row.empty:
            logger.error(f"Rank {rank} not found in RouteSummary. Skipping.")
            continue
            
        route_str = route_row['route'].iloc[0]
        dep, arr = split_route_string(route_str)
        logger.info(f"Processing Rank {rank} ({dep} -> {arr})...")
        
        # 1. Locate Synthesized Path in registry
        synth_match = df_synth_reg[df_synth_reg['route'] == f"{dep}-{arr}"]
        if synth_match.empty:
            logger.error(f"No synthesized path registered for route {dep}-{arr}. Skipping.")
            continue
            
        synth_rel_path = synth_match['file_path'].iloc[0]
        synth_abs_path = BASE_DIR / synth_rel_path
        if not synth_abs_path.exists():
            logger.error(f"Registered synthesized path file not found at: {synth_abs_path}. Skipping.")
            continue
            
        # Load the base synthetic flight (only one flight is inside)
        synth_flights = read_flights_from_parquet(str(synth_abs_path))
        if not synth_flights:
            logger.error(f"Could not load flight from synthesized file: {synth_abs_path}. Skipping.")
            continue
        base_flight = list(synth_flights.values())[0]
        
        # 2. Load original flight list for schedule
        flight_list_file = BASE_DIR / "data" / "flight_lists" / f"{dep}-{arr}.parquet"
        if not flight_list_file.exists():
            logger.error(f"Flight list file not found at: {flight_list_file}. Skipping corridor.")
            continue
            
        df_flight_list = pd.read_parquet(flight_list_file)
        if df_flight_list.empty:
            logger.warning(f"Flight list {flight_list_file.name} is empty. Skipping.")
            continue
            
        # Filter first 3 flights if in test mode
        if test_mode:
            logger.info("Test Mode enabled: Slicing flight list to first 3 entries.")
            df_flight_list = df_flight_list.head(3)
            
        logger.info(f"Resolved {len(df_flight_list)} target flights to simulate.")
        
        # Create output subdirectory for corridor
        corridor_out_dir = out_dir_path / f"{dep}-{arr}_cloned_simulated"
        corridor_out_dir.mkdir(parents=True, exist_ok=True)
        
        # Prepare list of flight info and pre-resolve times
        flights_info = []
        for idx, (_, row) in enumerate(df_flight_list.iterrows()):
            icao24 = row['icao24']
            callsign = row.get('callsign', 'UNK')
            firstseen = row['firstseen']
            lastseen = row['lastseen']
            typecode = row.get('typecode', 'B738')
            
            firstseen_dt = pd.to_datetime(firstseen)
            lastseen_dt = pd.to_datetime(lastseen)
            
            # Normalize to timezone-aware UTC
            if firstseen_dt.tz is None:
                firstseen_dt = firstseen_dt.tz_localize('UTC')
            else:
                firstseen_dt = firstseen_dt.tz_convert('UTC')
                
            if lastseen_dt.tz is None:
                lastseen_dt = lastseen_dt.tz_localize('UTC')
            else:
                lastseen_dt = lastseen_dt.tz_convert('UTC')
            
            # Resolve time bounds
            if test_mode:
                # Override: Spaced 2 hours starting on Jan 1st 2025 (timezone aware UTC)
                target_start = pd.Timestamp("2025-01-01 00:00:00", tz="UTC") + pd.Timedelta(hours=idx * 2)
                target_end = target_start + (lastseen_dt - firstseen_dt)
            else:
                target_start = firstseen_dt
                target_end = lastseen_dt
                
            fs_str = target_start.strftime('%Y%m%d_%H%M')
            flight_id = f"{icao24}_{callsign}_{dep}-{arr}_{fs_str}"
            out_file = corridor_out_dir / f"{flight_id}_simulated.parquet"
            
            flights_info.append({
                'idx': idx,
                'flight_id': flight_id,
                'icao24': icao24,
                'callsign': callsign,
                'typecode': typecode,
                'target_start': target_start,
                'target_end': target_end,
                'out_file': out_file
            })
            
        # Determine if we can share a single MetDataset for the batch
        min_start = min(f['target_start'] for f in flights_info)
        max_end = max(f['target_end'] for f in flights_info)
        total_span_hours = (max_end - min_start).total_seconds() / 3600.0
        
        met = None
        rad = None
        
        # If total span is small (e.g. test mode on same day), load weather once
        if total_span_hours <= 72:
            logger.info(f"Total temporal span is small ({total_span_hours:.1f} hours). Loading weather once for the entire batch...")
            try:
                weather_start = (min_start - pd.Timedelta(hours=1)).floor('h')
                if hasattr(weather_start, 'tz') and weather_start.tz is not None:
                    weather_start = weather_start.tz_convert('UTC').tz_localize(None)
                    
                weather_end = (max_end + pd.Timedelta(hours=max_age_hours + 1)).ceil('h')
                if hasattr(weather_end, 'tz') and weather_end.tz is not None:
                    weather_end = weather_end.tz_convert('UTC').tz_localize(None)
                    
                start_str = weather_start.strftime('%Y-%m-%dT%H:%M:%S')
                end_str = weather_end.strftime('%Y-%m-%dT%H:%M:%S')
                
                disk_cache = DiskCacheStore(cache_dir=weather_cache)
                era5_pl = ERA5(time=(start_str, end_str), variables=PRESSURE_LEVEL_VARIABLES, pressure_levels=REQUIRED_PRESSURE_LEVELS, grid=0.5, cachestore=disk_cache)
                era5_sl = ERA5(time=(start_str, end_str), variables=SURFACE_VARIABLES, pressure_levels=-1, grid=0.5, cachestore=disk_cache)
                
                met = era5_pl.open_metdataset()
                rad = era5_sl.open_metdataset()
                logger.info("Successfully loaded weather datasets.")
            except Exception as e:
                logger.warning(f"Failed to load shared weather: {e}. Falling back to individual weather loading.")
                met = None
                rad = None
                
        # 3. Simulation Loop
        success_count = 0
        skip_count = 0
        failure_count = 0
        
        for info in flights_info:
            flight_id = info['flight_id']
            out_file = info['out_file']
            
            # Check existing file for resume capability
            if out_file.exists():
                logger.info(f"   -> File exists: {out_file.name}. Skipping simulation.")
                skip_count += 1
                # Add to registry list
                rel_out_path = out_file.resolve().relative_to(BASE_DIR).as_posix()
                new_registry_entries.append({"flight_id": flight_id, "file_path": rel_out_path})
                continue
                
            logger.info(f"Simulating flight {flight_id}...")
            
            try:
                # 4. Clone and shift base trajectory
                df_cloned = base_flight.to_dataframe().copy()
                
                # Ensure df_cloned['time'] is timezone-aware UTC
                if df_cloned['time'].dt.tz is None:
                    df_cloned['time'] = df_cloned['time'].dt.tz_localize('UTC')
                else:
                    df_cloned['time'] = df_cloned['time'].dt.tz_convert('UTC')
                    
                synth_start = df_cloned['time'].iloc[0]
                
                target_start_tz = info['target_start']
                if target_start_tz.tz is None:
                    target_start_tz = target_start_tz.tz_localize('UTC')
                else:
                    target_start_tz = target_start_tz.tz_convert('UTC')
                    
                synth_start_tz = synth_start
                if synth_start_tz.tz is None:
                    synth_start_tz = synth_start_tz.tz_localize('UTC')
                else:
                    synth_start_tz = synth_start_tz.tz_convert('UTC')
                    
                time_offset = target_start_tz - synth_start_tz
                df_cloned['time'] = df_cloned['time'] + time_offset
                
                attrs = {
                    "flight_id": flight_id,
                    "aircraft_type": info['typecode'],
                    "icao24": info['icao24'],
                    "callsign": info['callsign'],
                    "firstseen": info['target_start'],
                    "lastseen": info['target_end'],
                    "estdepartureairport": dep,
                    "estarrivalairport": arr,
                }
                
                # Copy other custom attributes
                for k, v in base_flight.attrs.items():
                    if k not in attrs:
                        attrs[k] = v
                        
                # Ensure we don't pass 'crs' twice if it's present in the base flight attrs
                attrs.pop('crs', None)
                
                fl_cloned = Flight(data=df_cloned, drop_duplicated_times=True, crs="EPSG:4326", **attrs)
                
                # 5. Run simulation
                fl_simulated = simulate_cloned_flight(
                    fl_cloned=fl_cloned,
                    cache_dir=weather_cache,
                    max_age_hours=max_age_hours,
                    met_dataset=met,
                    rad_dataset=rad
                )
                
                # 6. Save incrementally
                write_flights_to_parquet([fl_simulated], out_file)
                
                # Add registry entry
                rel_out_path = out_file.resolve().relative_to(BASE_DIR).as_posix()
                new_registry_entries.append({"flight_id": flight_id, "file_path": rel_out_path})
                success_count += 1
                
            except Exception as e:
                failure_count += 1
                logger.error(f"Failed to simulate cloned flight {flight_id}: {e}")
                
        # Write stats to simulation log
        with open(sim_log_path, "a") as log_file:
            log_file.write(f"\n==================================================\n")
            log_file.write(f"CLONED SIMULATION SUMMARY - {pd.Timestamp.now()}\n")
            log_file.write(f"Route: {dep}-{arr} | Rank: {rank}\n")
            log_file.write(f"Total flights processed: {len(flights_info)}\n")
            log_file.write(f"Success: {success_count}\n")
            log_file.write(f"Skipped (existing): {skip_count}\n")
            log_file.write(f"Failure: {failure_count}\n")
            log_file.write(f"==================================================\n")
            
        logger.info(f"Route {dep}-{arr} batch simulation complete. Stats appended to {sim_log_path.name}")
        
    # 7. Update Global Cloned Simulation Registry
    if new_registry_entries:
        cloned_registry_file = FLIGHT_REGISTRY_DIR / "global_cloned_simulation_registry.parquet"
        update_global_registry(cloned_registry_file, new_registry_entries)
        logger.info(f"Updated global cloned simulation registry manifest at {cloned_registry_file.name}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch Cloned Trajectory Simulation Engine")
    
    # Ranks specifications (Mutually Exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str, help="Comma-separated ranks list (e.g. '1,3')")
    group.add_argument("--lower-rank", type=int, help="Lower bound of corridor ranks")
    
    parser.add_argument("--upper-rank", type=int, help="Upper bound of corridor ranks")
    parser.add_argument("--weather-cache", default=str(BASE_DIR / "data" / "weather"), help="Directory containing ERA5 NetCDF cache files")
    parser.add_argument("--out-dir", default=str(BASE_DIR / "data" / "results" / "cloned_simulations"), help="Output directory for simulation results")
    parser.add_argument("--max-age", "--age", type=int, default=48, dest="max_age", help="Maximum contrail simulation/advection age in hours (default: 48)")
    parser.add_argument("--test-mode", action="store_true", help="Limits simulation to 3 flights and overrides times to Jan 1, 2025")
    
    args = parser.parse_args()
    
    # Validate ranks
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required if --lower-rank is specified.")
        
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
        weather_cache=args.weather_cache,
        out_dir=args.out_dir,
        max_age_hours=args.max_age,
        test_mode=args.test_mode
    )
