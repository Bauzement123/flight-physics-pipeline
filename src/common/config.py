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
LOGS_DIR = DATA_DIR / "logs"

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
GLOBAL_MODEL_REGISTRY = REGISTRIES_DIR / "global_model_registry.parquet"
GLOBAL_CORRIDOR_SIM_REGISTRY = REGISTRIES_DIR / "global_corridor_simulation_registry.parquet"
GLOBAL_STABILITY_REGISTRY = REGISTRIES_DIR / "global_stability_registry.parquet"
# Note: GLOBAL_FLIGHT_CLUSTER_MAP removed. Medoid flight_id is stored per-cluster
# directly in GLOBAL_MODEL_REGISTRY (medoid_historical_flight_id column).

# PCA Calibration Constants
# D_PCA and N_STANDARD are sentinel placeholders (-1). Run the Phase A/B
# calibration script (Step 5) on 3 oversampled routes to derive these values
# and update them here. No model file is saved -- PCA is fit fresh per-route.
D_PCA                 = -1     # Populated by Phase A: number of PCA components (95% variance)
N_STANDARD            = -1     # Populated by Phase A: per-route query budget = 5 × D_PCA
DELTA_CV_THRESHOLD    = 0.01   # Populated by Phase B: ΔCV convergence threshold
DELTA_CV_EPSILON      = 1e-8   # Guard for near-zero std in relative ΔCV formula

# Stability Sampling — Resampling Loop Controls
STABILITY_RESAMPLE_MULTIPLIER = 2   # On resample: query N_STANDARD × (multiplier^round) flights
STABILITY_MAX_RESAMPLE_ROUNDS = 3   # Hard cap: max resample rounds before forcing convergence

# Clustering Hyperparameter Tuning Constants
# Promoted from hardcoded literals in path_generator.py.
CLUSTERING_MAX_K          = 8      # Maximum k to evaluate in Silhouette loop
SILHOUETTE_THRESHOLD      = 0.35   # Minimum silhouette score to accept k > 1
CHAOS_VARIANCE_THRESHOLD  = 200.0  # Total coordinate variance above which k=1 is classified as Chaos
MIN_FLIGHTS_FOR_CLUSTERING = 3     # Minimum cohort size; below this k=1 is forced
CORRIDOR_TIME_GRID_SECONDS = 60    # Temporal resolution of saved corridor parquets

# ROCD Classification Thresholds (ft/min)
# Promoted from hardcoded literals in path_generator.py.
ROCD_MIN_CLIMB_RATE   = 1800.0  # Minimum acceptable clean-flight climb rate
ROCD_MIN_DESCENT_RATE = 1200.0  # Minimum acceptable clean-flight descent rate

ROUTE_SUMMARY_PKL = FLIGHT_REGISTRY_DIR / "master_flights_route_summary.pkl"
ROUTE_SUMMARY_PARQUET = FLIGHT_REGISTRY_DIR / "master_flights_route_summary.parquet"
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

# --- Geographic Filtering Constants (European Bounding Box) ---
# Encompasses edges: Greenland, Svalbard, Canary Islands, Turkey, Israel
# Built from base bounds Lat [27.0, 80.0], Lon [-55.0, 48.0] Padding aplied in code 
# west, south, east, north = [-55.0, 27.0, 48.0, 80.0]
WEATHER_BOUNDS_BBOX = [-55.0, 27.0, 48.0, 80.0]

# Dynamic datasets and outcomes
TRAJECTORIES_DIR = DATA_DIR / "trajectories"
SIMULATION_PROFILES_DIR = DATA_DIR / "simulation_profiles"
CORRIDOR_PATHS_DIR = DATA_DIR / "corridor_paths"
RESULTS_DIR = DATA_DIR / "results"
CORRIDOR_SIMULATIONS_DIR = RESULTS_DIR / "corridor_simulations"

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

