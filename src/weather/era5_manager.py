"""
Module 3: ERA5 Weather Manager
Standalone utility to bulk-fetch ERA5 NetCDF data for PSFlight.
Fetches comprehensive 3D meteorological parameters and 2D radiation data
across high-resolution pressure levels.

Paradigm: Functional
"""

import argparse
import logging
from pathlib import Path
from pycontrails import DiskCacheStore
from pycontrails.datalib.ecmwf import ERA5

# Configure logging format with timestamps, levels, and messaging context
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 3D Atmospheric variable selection critical for CoCiP & PSFlight calculations (isobaric levels):
# - air_temperature: Determines speed of sound, true airspeed (TAS), and thermodynamic properties.
# - specific_humidity: Critical for contrail formation/persistence calculations (Schmidt-Appleman criterion).
# - eastward_wind / northward_wind: Used for wind correction vector calculations (Groundspeed vs. Airspeed).
# - lagrangian_tendency_of_air_pressure: Vertical velocity parameter used in atmospheric stability estimation.
# - specific_cloud_ice_water_content: Controls background ice concentrations for natural cirrus interaction.
PRESSURE_LEVEL_VARIABLES = [
    "air_temperature", 
    "specific_humidity", 
    "eastward_wind", 
    "northward_wind", 
    "lagrangian_tendency_of_air_pressure", 
    "specific_cloud_ice_water_content"
]

# 2D Surface/Single-level parameters critical for radiation calculations:
# - top_net_solar_radiation: 2D shortwave radiation parameter essential for daytime radiative forcing.
# - top_net_thermal_radiation: 2D longwave radiation parameter essential for greenhouse effect forcing.
SURFACE_VARIABLES = [
    "top_net_solar_radiation",
    "top_net_thermal_radiation"
]

# Required vertical pressure levels (hPa) covering the lower troposphere (900 hPa / ~3,000 ft)
# all the way up to the lower stratosphere (150 hPa / ~45,000 ft) to capture climbs, cruise, and descents.
REQUIRED_PRESSURE_LEVELS = [
    900, 850, 800, 750, 700, 650, 600, 550, 500, 
    450, 400, 350, 300, 250, 225, 200, 150
]

# Default generously padded Europe domain to track contrails over at least 24 hours.
# Spans from the deep Atlantic (-65°W) to the Middle East (50°E), 
# and from the High Arctic (80°N) down to North Africa/Canary Islands/Southern Israel (25°N).
# Format: [min_lon, min_lat, max_lon, max_lat]
DEFAULT_EUROPE_BBOX = [-65.0, 25.0, 50.0, 80.0]

def fetch_era5_data(start: str, end: str, bbox: list = None, cache_dir: str = "data/03_weather_cache/") -> None:
    """
    Fetches ERA5 reanalysis data for temporal bounds.
    Queries both pressure level and single-level variables sequentially to accommodate
    pycontrails dataset structures.
    
    Args:
        start (str): Start date/time string (e.g., "2025-01-01T00:00:00" or "2025-01-01").
        end (str): End date/time string (e.g., "2025-01-02T23:59:59" or "2025-01-02").
        bbox (list, optional): Spatial bounding box [min_lon, min_lat, max_lon, max_lat]. 
                               Parsed and validated for downstream processing, though pycontrails 
                               caches globally by default.
        cache_dir (str): Relative or absolute path to store the cached NetCDF (.nc) files.
    """
    cache_path = Path(cache_dir)
    if bbox is None:
        bbox = DEFAULT_EUROPE_BBOX
    
    # Debug logging: Check output directory state
    logger.debug(f"Ensuring target cache directory exists: {cache_path.resolve()}")
    cache_path.mkdir(parents=True, exist_ok=True)
    
    # Instantiate DiskCacheStore with our target cache directory
    logger.debug(f"Initializing custom pycontrails DiskCacheStore at: {cache_path.resolve()}")
    disk_cache = DiskCacheStore(cache_dir=str(cache_path))
    
    # Input validation logging
    logger.debug(f"Input validation parameters:")
    logger.debug(f"  -> Bounding Box (lon/lat limits): {bbox} (Note: Pycontrails fetches globally)")
    logger.debug(f"  -> Temporal Window: {start} to {end}")
    logger.debug(f"  -> Pressure Levels Count: {len(REQUIRED_PRESSURE_LEVELS)}")
    logger.debug(f"  -> Pressure Level Variables: {PRESSURE_LEVEL_VARIABLES}")
    logger.debug(f"  -> Surface/Radiation Variables: {SURFACE_VARIABLES}")
    
    if len(bbox) != 4:
        logger.error(f"Malformed bounding box format. Expected 4 elements, got {len(bbox)}")
        raise ValueError("Bounding box must be of format: min_lon, min_lat, max_lon, max_lat")

    # Spatial bounds checks (Longitude: -180 to 180, Latitude: -90 to 90)
    logger.debug("Validating spatial bounding box coordinates...")
    if not (-180 <= bbox[0] <= 180) or not (-180 <= bbox[2] <= 180):
        logger.warning(f"Longitude parameters {bbox[0]} or {bbox[2]} are outside normal standard bounds [-180, 180]")
    if not (-90 <= bbox[1] <= 90) or not (-90 <= bbox[3] <= 90):
        logger.error(f"Latitude parameters {bbox[1]} or {bbox[3]} are out of physical bounds [-90, 90]")
        raise ValueError("Latitude coordinates must be between -90 and 90 degrees.")

    # Query Step 1: Download 3D Pressure Level Meteorological Data
    try:
        logger.info(f"Initializing ERA5 client for downloading {len(REQUIRED_PRESSURE_LEVELS)} pressure levels globally.")
        logger.debug("Instantiating pycontrails.datalib.ecmwf.ERA5 for pressure level variables...")
        era5_pl = ERA5(
            time=(start, end),
            variables=PRESSURE_LEVEL_VARIABLES,
            pressure_levels=REQUIRED_PRESSURE_LEVELS,
            grid=0.5,
            cachestore=disk_cache
        )
        
        logger.debug(f"Pressure level ERA5 properties: {era5_pl}")
        
        missing_pl_times = era5_pl.list_timesteps_not_cached()
        if not missing_pl_times:
            logger.info("✅ All requested 3D pressure-level data is already present in the shared cache. Skipping download.")
        else:
            logger.info(f"Missing {len(missing_pl_times)} hourly 3D pressure-level files. Triggering asynchronous download pipeline...")
            era5_pl.download()
            logger.info("Pressure-level download query complete.")

    except Exception as e:
        logger.error(f"An error occurred during pressure-level ERA5 retrieval: {str(e)}", exc_info=True)
        raise

    # Query Step 2: Download 2D Surface/Single-Level Radiation Data
    try:
        logger.info("Initializing ERA5 client for downloading single-level surface radiation variables globally.")
        logger.debug("Instantiating pycontrails.datalib.ecmwf.ERA5 for surface level variables...")
        # Note: By setting pressure_levels=-1, pycontrails targets the 'reanalysis-era5-single-levels' dataset.
        era5_sl = ERA5(
            time=(start, end),
            variables=SURFACE_VARIABLES,
            pressure_levels=-1,
            grid=0.5,
            cachestore=disk_cache
        )
        
        logger.debug(f"Single-level ERA5 properties: {era5_sl}")
        
        missing_sl_times = era5_sl.list_timesteps_not_cached()
        if not missing_sl_times:
            logger.info("✅ All requested 2D surface-level data is already present in the shared cache. Skipping download.")
        else:
            logger.info(f"Missing {len(missing_sl_times)} hourly 2D surface-level files. Triggering asynchronous download pipeline...")
            era5_sl.download()
            logger.info("Single-level surface radiation download query complete.")
        
        logger.info(f"All ERA5 download sequences complete. Local weather cache fully updated.")
        
    except Exception as e:
        logger.error(f"An error occurred during surface-level ERA5 retrieval: {str(e)}", exc_info=True)
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk fetch ERA5 atmospheric data for high-fidelity modeling")
    
    # Positional or keyword options mapping to functional arguments
    parser.add_argument("--start", required=True, help="Start date/time string (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end", required=True, help="End date/time string (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--bbox", required=False, help="Optional spatial box: min_lon, min_lat, max_lon, max_lat. Defaults to padded Europe domain.")
    parser.add_argument("--out-dir", default="data/03_weather_cache/", help="Path to directory for caching downloading NetCDFs")
    parser.add_argument("--debug", action="store_true", help="Enable verbose DEBUG logging level")
    
    args = parser.parse_args()
    
    # If debug flag is passed, elevate logging level across the script context
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("pycontrails").setLevel(logging.DEBUG)
        logger.debug("Verbose debug logging enabled.")
    
    if args.bbox:
        logger.debug(f"Parsing raw command line bbox input: '{args.bbox}'")
        try:
            bbox_list = [float(x.strip()) for x in args.bbox.split(",")]
        except ValueError as val_err:
            logger.error(f"Failed to parse bbox arguments: '{args.bbox}'. Ensure it consists of 4 numerical values separated by commas.")
            raise val_err
    else:
        bbox_list = DEFAULT_EUROPE_BBOX
        logger.info(f"No bbox provided. Validating with default padded Europe domain: {bbox_list}")
        
    # Execute the purely functional fetch loop
    fetch_era5_data(
        start=args.start, 
        end=args.end,
        bbox=bbox_list, 
        cache_dir=args.out_dir
    )