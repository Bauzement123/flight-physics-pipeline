"""
Flight Physics Pipeline Orchestrator
Executes the fully decoupled functional modules in sequence.
Includes automated advection padding (spatial/temporal) for the Cocip model.
"""

import argparse
import subprocess
import time
import os
import glob
import pandas as pd
from pathlib import Path
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='\n%(asctime)s - ORCHESTRATOR - %(message)s')

def get_latest_file(directory: str, extension: str = "*.parquet") -> str:
    """Utility to find the most recently generated file in a directory."""
    files = glob.glob(os.path.join(directory, extension))
    if not files:
        raise FileNotFoundError(f"No {extension} files found in {directory}")
    return max(files, key=os.path.getctime)

def calculate_dynamic_bbox(clean_parquet_path: str, padding: float = 15.0) -> str:
    """Reads the cleaned trajectory file to compute a spatially padded bounding box."""
    logging.info("Calculating dynamic spatial bounding box from trajectories...")
    df = pd.read_parquet(clean_parquet_path)
    
    # Extract min/max and add heavy padding (degrees) for contrail advection drift
    min_lon = df['longitude'].min() - padding
    max_lon = df['longitude'].max() + padding
    min_lat = df['latitude'].min() - padding
    max_lat = df['latitude'].max() + padding
    
    # Safety Check: Ensure padding doesn't push coordinates off the planet
    min_lon = max(-180.0, min_lon)
    max_lon = min(180.0, max_lon)
    min_lat = max(-90.0, min_lat)
    max_lat = min(90.0, max_lat)
    
    bbox_str = f"{min_lon},{min_lat},{max_lon},{max_lat}"
    logging.info(f"Dynamic Bounding Box Generated (with {padding}° padding): {bbox_str}")
    return bbox_str

def main():
    parser = argparse.ArgumentParser(description="End-to-End Flight Physics Pipeline")
    parser.add_argument("--csv", default="data/flight_registry/master_flights.parquet", help="Path to master registry")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--typecode", help="Aircraft Typecode (e.g., B738)")
    parser.add_argument("--origin", help="Origin ICAO")
    parser.add_argument("--dest", help="Destination ICAO")
    parser.add_argument("--sample-size", type=str, help="Optional limit for Trino queries")
    parser.add_argument("--start-from-raw", help="Optional: Fast-track pipeline starting from an existing _raw.parquet file")
    parser.add_argument("--min-distance", type=float, default=800.0, help="Minimum route distance in kilometers to process")
    
    args = parser.parse_args()
    
    t0 = time.time()

    # ---------------------------------------------------------
    # ADVECTION PADDING CALCULATION (Temporal)
    # ---------------------------------------------------------
    # Cocip requires future weather (up to 48h) to advect contrails after the flight lands.
    try:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d")
        weather_end_date = (end_dt + timedelta(days=2)).strftime("%Y-%m-%d")
        logging.info(f"Advection Padding: Weather bounds extended from {args.end_date} to {weather_end_date} (48h padding)")
    except ValueError:
        weather_end_date = args.end_date
        logging.warning("Could not parse end-date format. Using unpadded end-date for weather.")

    # ---------------------------------------------------------
    # LOOP 1: ACQUISITION (Skip if fast-tracking)
    # ---------------------------------------------------------
    if not args.start_from_raw:
        logging.info("=== STARTING LOOP 1a: Master Filter ===")
        cmd_filter = [
            "python", "src/filtering/population_filter.py",
            "--csv", args.csv,
            "--start-date", args.start_date,
            "--end-date", args.end_date
        ]
        if args.min_distance is not None: cmd_filter.extend(["--min-distance", str(args.min_distance)])
        if args.typecode: cmd_filter.extend(["--typecode", args.typecode])
        if args.origin: cmd_filter.extend(["--origin", args.origin])
        if args.dest: cmd_filter.extend(["--dest", args.dest])
        subprocess.run(cmd_filter, check=True)
        
        target_list = get_latest_file("data/flight_lists/", "*.parquet")
        
        logging.info("=== STARTING LOOP 1b: Trino Fetcher ===")
        cmd_fetch = [
            "python", "src/fetching/opensky_fetcher.py",
            "--input-list", target_list,
            "--out-dir", "data/trajectories/run_all_temp"
        ]
        if args.sample_size: cmd_fetch.extend(["--sample-size", args.sample_size])
        subprocess.run(cmd_fetch, check=True)
        
        raw_file = get_latest_file("data/trajectories/run_all_temp/raw/", "*_raw.parquet")
    else:
        logging.info(f"=== FAST-TRACK: Skipping Loop 1. Starting from {args.start_from_raw} ===")
        raw_file = args.start_from_raw
        if not os.path.exists(raw_file):
            raise FileNotFoundError(f"Provided raw file does not exist: {raw_file}")

    # ---------------------------------------------------------
    # LOOP 2: PROCESSING (EKF SMOOTHING)
    # ---------------------------------------------------------
    logging.info("=== STARTING LOOP 2: EKF Smoothing & Resampling ===")
    cmd_kalman = [
        "python", "src/processing/kalman_filter.py",
        "--input-file", raw_file
    ]
    subprocess.run(cmd_kalman, check=True)
    clean_file = get_latest_file("data/trajectories/run_all_temp/clean/", "*_clean_si.parquet")

    # ---------------------------------------------------------
    # LOOP 3a: WEATHER INTEGRATION (DYNAMIC BOUNDS)
    # ---------------------------------------------------------
    logging.info("=== STARTING LOOP 3a: Weather Cache Updates ===")
    # Spatial padding increased to 30.0 degrees to cover 48h contrail advection drift
    bbox_str = calculate_dynamic_bbox(clean_file, padding=30.0)
    
    cmd_weather = [
        "python", "src/weather/era5_manager.py",
        "--start", args.start_date,
        "--end", weather_end_date,  # Note: Padded date
        f"--bbox={bbox_str}",        # Note: Using '=' prevents argparse negative number parsing errors
        "--out-dir", "data/weather/"
    ]
    subprocess.run(cmd_weather, check=True)

    # ---------------------------------------------------------
    # LOOP 3b: PHYSICS SIMULATION (PSFLIGHT & COCIP)
    # ---------------------------------------------------------
    logging.info("=== STARTING LOOP 3b: Physics Simulation ===")
    # simulation.py automatically calculates flight bounds and 48h advection weather window dynamically
    cmd_sim = [
        "python", "src/physics/simulation.py",
        "--input-file", clean_file,
        "--out-dir", "data/results/run_all_temp/",
        "--weather-cache", "data/weather/"
    ]
    subprocess.run(cmd_sim, check=True)

    final_file = get_latest_file("data/results/run_all_temp/", "*_simulated.parquet")

    # ---------------------------------------------------------
    # WRAP UP
    # ---------------------------------------------------------
    t1 = time.time()
    total_time = (t1 - t0) / 60
    logging.info(f"=== PIPELINE COMPLETE ===")
    logging.info(f"Final output saved to: {final_file}")
    logging.info(f"Total execution time: {total_time:.2f} minutes")

if __name__ == "__main__":
    main()