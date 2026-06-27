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

# Centralized data directory
DATA_DIR = BASE_DIR / "data"

# Static registries and global data directories
FLIGHT_REGISTRY_DIR = DATA_DIR / "flight_registry"
REGISTRIES_DIR = FLIGHT_REGISTRY_DIR / "registries"
FLIGHT_LISTS_DIR = DATA_DIR / "flight_lists"
WEATHER_DIR = DATA_DIR / "weather"
MASTER_FLIGHT_PATHS_DIR = DATA_DIR / "master_flight_paths"

# Centralized Registry and Summary Files
GLOBAL_TRAJECTORY_REGISTRY = REGISTRIES_DIR / "global_trajectory_registry.parquet"
GLOBAL_CLEAN_REGISTRY = REGISTRIES_DIR / "global_clean_registry.parquet"
GLOBAL_SIMULATION_REGISTRY = REGISTRIES_DIR / "global_simulation_registry.parquet"
GLOBAL_SYNTHESIZED_REGISTRY = REGISTRIES_DIR / "global_synthesized_registry.parquet"
GLOBAL_SYNTH_SIM_REGISTRY = REGISTRIES_DIR / "global_synthesized_simulation_registry.parquet"
GLOBAL_FLIGHT_CLUSTER_MAP = REGISTRIES_DIR / "global_flight_cluster_map.parquet"

ROUTE_SUMMARY_PKL = FLIGHT_REGISTRY_DIR / "master_flights_route_summary.pkl"
ROUTE_SUMMARY_CSV = FLIGHT_REGISTRY_DIR / "master_flights_route_summary.csv"
MASTER_FLIGHTS_FILE = FLIGHT_REGISTRY_DIR / "master_flights.parquet"

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
TRAJECTORIES_DIR = DATA_DIR / "trajectories"
SIMULATION_PROFILES_DIR = DATA_DIR / "simulation_profiles"
SYNTHESIZED_FLIGHT_PATHS_DIR = DATA_DIR / "synthesized_paths"
RESULTS_DIR = DATA_DIR / "results"

# Physical unit conversion factors
M_TO_FT = 3.280839895
MPS_TO_KT = 1.9438444924
MPS_TO_FPM = 196.8503937

def get_dataset_dir(dataset_name: str) -> Path:
    """
    Returns the unified folder path for a given dataset.
    Ensures the folder exists.
    """
    path = TRAJECTORIES_DIR / dataset_name
    path.mkdir(parents=True, exist_ok=True)
    return path

# Unify temporary directory handling and environment variables redirection
TEMP_DIR = DATA_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
for env_var in ['TEMP', 'TMP', 'TMPDIR']:
    os.environ[env_var] = str(TEMP_DIR)

