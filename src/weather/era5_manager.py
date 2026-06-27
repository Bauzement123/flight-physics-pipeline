"""
Module 3: ERA5 Weather Manager
Standalone utility to bulk-fetch ERA5 NetCDF data for PSFlight.
Fetches comprehensive 3D meteorological parameters and 2D radiation data
across high-resolution pressure levels.

Paradigm: Functional
"""

import argparse
import logging
import sys
import os
import re
import time
from pathlib import Path
from pycontrails import DiskCacheStore
from pycontrails.datalib.ecmwf import ERA5

# Ensure we can import from the common module
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from src.common.config import (
    ERA5_PRESSURE_LEVEL_VARIABLES,
    ERA5_SURFACE_VARIABLES,
    ERA5_REQUIRED_PRESSURE_LEVELS,
    ERA5_GRID,
    WEATHER_DIR
)
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)

def init_cache_store(cache_dir: str) -> DiskCacheStore:
    """
    Ensures target cache directory exists and instantiates DiskCacheStore.
    """
    cache_path = Path(cache_dir)
    logger.debug(f"Ensuring target cache directory exists: {cache_path.resolve()}")
    cache_path.mkdir(parents=True, exist_ok=True)
    
    logger.debug(f"Initializing custom pycontrails DiskCacheStore at: {cache_path.resolve()}")
    return DiskCacheStore(cache_dir=str(cache_path))

def download_with_retry(era5_client: ERA5, max_retries: int = 3) -> None:
    """
    Safely executes the pycontrails ERA5 download call, wrapping it
    in a retry loop with linear backoff to mitigate transient CDS API dropouts.
    """
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Initiating download pipeline (attempt {attempt}/{max_retries})...")
            era5_client.download()
            logger.info("Download query completed successfully.")
            return
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"Failed download after {max_retries} attempts.")
                raise e
            wait_seconds = attempt * 10
            logger.warning(
                f"CDS API download attempt {attempt} failed: {e}. "
                f"Retrying in {wait_seconds} seconds..."
            )
            time.sleep(wait_seconds)

def retrieve_dataset(
    time_bounds: tuple[str, str],
    variables: list[str],
    pressure_levels: int | list[int],
    cache_store: DiskCacheStore
) -> None:
    """
    Instantiates Pycontrails ERA5 client, checks local cache for missing hours,
    and downloads missing data. Uses a reactive self-healing loop to automatically
    detect, delete, and retry if any corrupted NetCDF files are encountered.
    """
    is_surface = (pressure_levels == -1)
    if is_surface:
        dataset_desc = "2D surface-level"
    else:
        levels_list = [pressure_levels] if isinstance(pressure_levels, int) else pressure_levels
        dataset_desc = f"{len(levels_list)} pressure levels"
    
    logger.info(f"Initializing ERA5 client for retrieving {dataset_desc} globally.")
    era5_client = ERA5(
        time=time_bounds,
        variables=variables,
        pressure_levels=pressure_levels,
        grid=ERA5_GRID,
        cachestore=cache_store
    )

    # Reactive Self-Healing Loop: Detects corrupt files during cache check and deletes them.
    max_check_attempts = 5
    missing_times = []
    
    for attempt in range(1, max_check_attempts + 1):
        try:
            missing_times = era5_client.list_timesteps_not_cached()
            break
        except OSError as e:
            err_msg = str(e)
            # Match path inside quotes in: "Unable to open NETCDF file at 'path'"
            match = re.search(r"Unable to open NETCDF file at ['\"](.*?)['\"]", err_msg)
            
            if match and attempt < max_check_attempts:
                corrupt_path = Path(match.group(1))
                if corrupt_path.exists():
                    logger.warning(
                        f"⚠️ [Attempt {attempt}/{max_check_attempts}] Detected corrupted cache file: "
                        f"{corrupt_path.resolve()}. Deleting and retrying cache check..."
                    )
                    try:
                        corrupt_path.unlink()
                        continue  # Retry the loop to check cache again
                    except Exception as unlink_err:
                        logger.error(f"Failed to delete corrupted file {corrupt_path}: {unlink_err}")
                        raise e
            
            # If parsing fails or we run out of retries, raise the exception
            logger.error(f"Failed to resolve cache checking error: {e}")
            raise e

    if not missing_times:
        logger.info(f"✅ All requested {dataset_desc} data is already present in cache. Skipping download.")
    else:
        logger.info(
            f"Missing {len(missing_times)} hourly files for {dataset_desc} variables. "
            f"Triggering asynchronous CDS download pipeline..."
        )
        download_with_retry(era5_client)

def fetch_era5_data(start: str, end: str, cache_dir: str = None) -> None:
    """
    Fetches ERA5 reanalysis data for temporal bounds.
    Queries both pressure level and single-level variables sequentially.
    
    Args:
        start (str): Start date/time string (e.g., "2025-01-01T00:00:00" or "2025-01-01").
        end (str): End date/time string (e.g., "2025-01-02T23:59:59" or "2025-01-02").
        cache_dir (str): Path to store the cached NetCDF (.nc) files. Defaults to WEATHER_DIR from config.
    """
    if cache_dir is None:
        cache_dir = str(WEATHER_DIR)
    # Step 1: Get cache storage engine
    cache_store = init_cache_store(cache_dir)
    
    # Step 2: Fetch 3D pressure levels dataset (reactive self-healing is inside retrieve_dataset)
    retrieve_dataset(
        time_bounds=(start, end),
        variables=ERA5_PRESSURE_LEVEL_VARIABLES,
        pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
        cache_store=cache_store
    )
    
    # Step 3: Fetch 2D surface levels dataset
    retrieve_dataset(
        time_bounds=(start, end),
        variables=ERA5_SURFACE_VARIABLES,
        pressure_levels=-1,
        cache_store=cache_store
    )
    
    logger.info("All ERA5 weather datasets have been successfully processed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk fetch ERA5 atmospheric data for high-fidelity modeling")
    
    parser.add_argument("--start", required=True, help="Start date/time string (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end", required=True, help="End date/time string (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--out-dir", default=str(WEATHER_DIR), help="Path to directory for caching downloading NetCDFs")
    parser.add_argument("--debug", action="store_true", help="Enable verbose DEBUG logging level")
    
    args = parser.parse_args()
    
    # Configure root logging level and format only when invoked as a standalone script
    log_level = logging.DEBUG if args.debug else logging.INFO
    
    # Configure StreamHandler for console output with standardized format
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - [%(name)s] - [%(levelname)s] - %(message)s'))
    
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers = [console_handler]
    
    # Configure centralized FileHandler
    file_handler = setup_file_logger(log_filename="weather.log")
    file_handler.setLevel(log_level)
    
    if args.debug:
        logging.getLogger("pycontrails").setLevel(logging.DEBUG)
        logger.debug("Verbose debug logging enabled.")
    
    # Execute the purely functional fetch loop
    fetch_era5_data(
        start=args.start, 
        end=args.end,
        cache_dir=args.out_dir
    )