"""Centralized Flight Pipeline Configurations"""
import os
from pathlib import Path

# Project root directory (resolved dynamically based on config.py location)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Fallback to Google Drive G: path if running in the C: drive agent sandbox
if BASE_DIR.drive.upper() == 'C:':
    G_DRIVE_DIR = Path("G:/Meine Ablage/UNI/SS26/PythonPipeline - Kopie")
    if G_DRIVE_DIR.exists():
        BASE_DIR = G_DRIVE_DIR

# Static registries and global data directories
FLIGHT_REGISTRY_DIR = BASE_DIR / "data" / "flight_registry"
REGISTRIES_DIR = FLIGHT_REGISTRY_DIR / "registries"
FLIGHT_LISTS_DIR = BASE_DIR / "data" / "flight_lists"
WEATHER_DIR = BASE_DIR / "data" / "weather"
MASTER_FLIGHT_PATHS_DIR = BASE_DIR / "data" / "master_flight_paths"

# ERA5 Weather Parameters
ERA5_PRESSURE_LEVEL_VARIABLES = [
    "air_temperature", 
    "specific_humidity", 
    "eastward_wind", 
    "northward_wind", 
    "lagrangian_tendency_of_air_pressure", 
    "specific_cloud_ice_water_content"
]

ERA5_SURFACE_VARIABLES = [
    "top_net_solar_radiation",
    "top_net_thermal_radiation"
]

ERA5_REQUIRED_PRESSURE_LEVELS = [
    900, 850, 800, 750, 700, 650, 600, 550, 500, 
    450, 400, 350, 300, 250, 225, 200, 150
]

ERA5_GRID = 0.5

# Dynamic datasets and outcomes
TRAJECTORIES_DIR = BASE_DIR / "data" / "trajectories"
SIMULATION_PROFILES_DIR = BASE_DIR / "data" / "simulation_profiles"
SYNTHESIZED_FLIGHT_PATHS_DIR = BASE_DIR / "data" / "synthesized_paths"
RESULTS_DIR = BASE_DIR / "data" / "results"

def get_dataset_dir(dataset_name: str) -> Path:
    """
    Returns the unified folder path for a given dataset.
    Ensures the folder exists.
    """
    path = TRAJECTORIES_DIR / dataset_name
    path.mkdir(parents=True, exist_ok=True)
    return path
