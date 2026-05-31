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
