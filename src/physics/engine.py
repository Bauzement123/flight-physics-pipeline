"""
Core Physics Engine Module (PSFlight + Cocip)
Provides reusable, modular, and thread-safe functions for weather dataset downselection,
model instantiation, vectorized batch simulation, and parallel orchestration.
"""

import logging
import numpy as np
import pandas as pd
from pycontrails import Flight, MetDataset, Fleet
from pycontrails.models.ps_model import PSFlight
from pycontrails.models.cocip import Cocip
from pycontrails.models.humidity_scaling import ConstantHumidityScaling

logger = logging.getLogger(__name__)

def crop_met_dataset(
    met: MetDataset, 
    bbox: list[float], 
    pad: float = 2.0
) -> MetDataset:
    """
    Spatially crops an xarray-backed MetDataset to a bounding box [West, South, East, North]
    with coordinate padding. Handles descending ERA5 latitude coords.
    """
    ds = met.data
    west, south, east, north = bbox
    
    # Resolve coordinate dimension names
    lat_name = 'latitude' if 'latitude' in ds.coords else 'lat'
    lon_name = 'longitude' if 'longitude' in ds.coords else 'lon'
    
    orig_lats = ds[lat_name].values
    orig_lons = ds[lon_name].values
    logger.info(
        f"Original weather grid bounds: "
        f"lat=[{orig_lats.min():.2f}, {orig_lats.max():.2f}] (shape={orig_lats.shape[0]}), "
        f"lon=[{orig_lons.min():.2f}, {orig_lons.max():.2f}] (shape={orig_lons.shape[0]})"
    )
    
    # Apply padding
    west = max(-180.0, west - pad)
    east = min(180.0, east + pad)
    south = max(-90.0, south - pad)
    north = min(90.0, north + pad)
    
    # Determine if latitude is descending (standard ERA5)
    lat_coords = ds[lat_name].values
    is_lat_descending = len(lat_coords) > 1 and lat_coords[0] > lat_coords[-1]
    
    if is_lat_descending:
        lat_slice = slice(north, south)
    else:
        lat_slice = slice(south, north)
        
    lon_slice = slice(west, east)
    
    logger.info(f"Slicing MetDataset coordinates: lat={lat_slice}, lon={lon_slice}")
    ds_cropped = ds.sel({lat_name: lat_slice, lon_name: lon_slice})
    
    cropped_lats = ds_cropped[lat_name].values
    cropped_lons = ds_cropped[lon_name].values
    logger.info(
        f"Cropped weather grid bounds: "
        f"lat=[{cropped_lats.min():.2f}, {cropped_lats.max():.2f}] (shape={cropped_lats.shape[0]}), "
        f"lon=[{cropped_lons.min():.2f}, {cropped_lons.max():.2f}] (shape={cropped_lons.shape[0]})"
    )
    return MetDataset(ds_cropped)

def create_simulation_models(
    met: MetDataset,
    rad: MetDataset,
    max_age_hours: int,
    low_mem: bool = False
) -> tuple[PSFlight, Cocip]:
    """
    Instantiates and returns the PSFlight and Cocip model instances.
    """
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
    
    if low_mem:
        cocip_params["preprocess_lowmem"] = True
        
    cocip_model = Cocip(
        met=met, 
        rad=rad, 
        params=cocip_params, 
        aircraft_performance=ps_model
    )
    
    return ps_model, cocip_model

def simulate_flight_batch(
    flights: list[Flight],
    met: MetDataset,
    rad: MetDataset,
    max_age_hours: int,
    low_mem: bool = False
) -> tuple[list[Flight], list[tuple[str, str]]]:
    """
    Evaluates a batch of flights using local thread-safe models pointing to the shared weather datasets.
    Filters out unsupported aircraft and logs warnings. 
    Implements a robust fallback to sequential simulation if the vectorized evaluation fails.
    
    Returns:
        tuple: (list of simulated Flights, list of skipped flight_id & typecode tuples)
    """
    if not flights:
        return [], []
        
    ps_model, cocip_model = create_simulation_models(met, rad, max_age_hours, low_mem=low_mem)
    ps_supported_types = list(ps_model.aircraft_engine_params.keys())
    
    valid_flights = []
    skipped_flights = []
    
    for fl in flights:
        flight_id = fl.attrs.get('flight_id', 'UNK')
        typecode = fl.attrs.get('aircraft_type', 'B738')
        if not typecode or pd.isna(typecode) or typecode == "UNKNOWN":
            logger.warning(f"Skipping flight {flight_id}: Missing or unknown aircraft typecode")
            skipped_flights.append((flight_id, typecode))
            continue
            
        if typecode not in ps_supported_types:
            logger.warning(f"Skipping flight {flight_id}: Aircraft type {typecode} not supported by PSFlight")
            skipped_flights.append((flight_id, typecode))
            continue
            
        valid_flights.append(fl)
        
    if not valid_flights:
        return [], skipped_flights
        
    try:
        # Vectorized batch evaluation
        logger.info(f"Running vectorized evaluation for batch of {len(valid_flights)} flights...")
        fl_evaluated = ps_model.eval(valid_flights)
        fl_out = cocip_model.eval(source=fl_evaluated)
        
        if isinstance(fl_out, Fleet):
            fl_out = fl_out.to_flight_list()
        elif isinstance(fl_out, Flight):
            fl_out = [fl_out]
        return list(fl_out), skipped_flights
        
    except Exception as e:
        logger.warning(f"Vectorized batch evaluation failed: {e}. Falling back to sequential execution...")
        
        # Exception-safe sequential fallback loop for this batch
        simulated_flights = []
        for fl in valid_flights:
            flight_id = fl.attrs.get('flight_id', 'UNK')
            try:
                fl_eval = ps_model.eval(fl)
                fl_sim = cocip_model.eval(source=fl_eval)
                simulated_flights.append(fl_sim)
            except Exception as inner_err:
                logger.error(f"Failed to simulate flight {flight_id} sequentially: {inner_err}")
                skipped_flights.append((flight_id, fl.attrs.get('aircraft_type', 'UNKNOWN')))
                
        return simulated_flights, skipped_flights

def simulate_flights_parallel(
    flights: list[Flight],
    met: MetDataset,
    rad: MetDataset,
    max_age_hours: int,
    batch_size: int = 50,
    max_workers: int = 4,
    low_mem: bool = False
) -> tuple[list[Flight], list[tuple[str, str]]]:
    """
    Partitions flights into batches and orchestrates simulation.
    If low_mem is True or max_workers <= 1, executes sequentially.
    Otherwise, executes concurrently in a ThreadPoolExecutor.
    """
    if not flights:
        return [], []
        
    batches = [flights[i:i + batch_size] for i in range(0, len(flights), batch_size)]
    logger.info(f"Chunked {len(flights)} flights into {len(batches)} batches (size={batch_size}).")
    
    all_simulated = []
    all_skipped = []
    
    if low_mem or max_workers <= 1:
        logger.info("Executing batches sequentially (low-memory or single-thread mode)...")
        for batch in batches:
            sim, skip = simulate_flight_batch(batch, met, rad, max_age_hours, low_mem=low_mem)
            all_simulated.extend(sim)
            all_skipped.extend(skip)
    else:
        logger.info(f"Executing batches concurrently using ThreadPoolExecutor (max_workers={max_workers})...")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    simulate_flight_batch, 
                    batch, 
                    met, 
                    rad, 
                    max_age_hours, 
                    low_mem=low_mem
                ): batch for batch in batches
            }
            
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    sim, skip = future.result()
                    all_simulated.extend(sim)
                    all_skipped.extend(skip)
                except Exception as e:
                    logger.error(f"Task raised exception for batch containing {len(batch)} flights: {e}")
                    for fl in batch:
                        all_skipped.append((fl.attrs.get('flight_id', 'UNK'), fl.attrs.get('aircraft_type', 'UNKNOWN')))
                        
    return all_simulated, all_skipped
