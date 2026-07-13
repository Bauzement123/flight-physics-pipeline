"""Centralized Flight Pipeline Configurations"""
import os
from pathlib import Path
from typing import Any
from dataclasses import dataclass

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
MASTER_FLIGHTS_DB_DIR = DATA_DIR / "databases" / "master_flights"
AIRCRAFT_DB_DIR = DATA_DIR / "databases" / "aircraft_db"
REGISTRIES_DIR = DATA_DIR / "registries"
FLIGHT_LISTS_DIR = DATA_DIR / "flight_lists"
WEATHER_DIR = DATA_DIR / "weather"
MASTER_FLIGHT_PATHS_DIR = DATA_DIR / "master_flight_paths"
REPORTS_DIR = DATA_DIR / "analysis" / "reports"

# Centralized Registry and Summary Files
GLOBAL_TRAJECTORY_REGISTRY = REGISTRIES_DIR / "global_trajectory_registry.parquet"
GLOBAL_CLEAN_REGISTRY = REGISTRIES_DIR / "global_clean_registry.parquet"
GLOBAL_SIMULATION_REGISTRY = REGISTRIES_DIR / "global_simulation_registry.parquet"
GLOBAL_MODEL_REGISTRY = REGISTRIES_DIR / "global_model_registry.parquet"
GLOBAL_CORRIDOR_SIM_REGISTRY = REGISTRIES_DIR / "global_corridor_simulation_registry.parquet"
GLOBAL_STABILITY_REGISTRY = REGISTRIES_DIR / "global_stability_registry.parquet"
GLOBAL_FLIGHT_CLUSTER_MAP = REGISTRIES_DIR / "global_flight_cluster_map.parquet"
GLOBAL_EKF_DIAG_REGISTRY = REGISTRIES_DIR / "global_ekf_diag_registry.parquet"
CALIBRATION_FLIGHT_CLUSTER_MAP = DATA_DIR / "calibration" / "calibration_flight_cluster_map.parquet"
CALIBRATION_PLOT_REGISTRY = REGISTRIES_DIR / "calibration_plot_registry.parquet"
CALIBRATION_PLOTS_DIR = DATA_DIR / "calibration" / "plots"
ORACLE_COHORT_CACHE_DIR = DATA_DIR / "calibration" / "cache" / "oracle_cohorts"
# Note: Medoid flight_id is also stored per-cluster directly in GLOBAL_MODEL_REGISTRY (medoid_historical_flight_id column).

# --- Phase Quality Filter Campaign Paths ---
PHASE_QUALITY_DIR = DATA_DIR / "calibration" / "phase_quality"
PHASE_QUALITY_REGISTRIES_DIR = PHASE_QUALITY_DIR / "registries"
PHASE_QUALITY_RUNS_DIR = PHASE_QUALITY_DIR / "runs"
AUDIT_CANDIDATE_POOL_REGISTRY = PHASE_QUALITY_REGISTRIES_DIR / "audit_candidate_pool.parquet"
AUDIT_COHORT_MAP_REGISTRY = PHASE_QUALITY_REGISTRIES_DIR / "audit_cohort_map.parquet"

# --- Aircraft Database Paths ---
DEFAULT_AIRCRAFT_DB_PATH = AIRCRAFT_DB_DIR / "aircraft-database-complete-2025-08.csv"
DEFAULT_OPENAIRFRAMES_PATH = AIRCRAFT_DB_DIR / "openairframes_adsb_2024-01-01_2026-02-23.csv.gz"
AIRCRAFT_DB_DOWNLOAD_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
AIRPORT_REGISTRIES_DIR = REGISTRIES_DIR
AIRPORTS_CACHE_PATH = REGISTRIES_DIR / "airport_coordinates.json"

# --- Default Pipeline Parameters ---
DEFAULT_AIRPORT_PREFIXES = ["B", "E", "L"]

# Fetching module defaults (§3.3.1)
MIN_DISTANCE_KM: float = 0          # Default minimum corridor distance filter
DEFAULT_SAMPLE_SIZE: int = 50           # Default fixed sample size per corridor

# Pipeline Concurrency and Threading Defaults
PROCESSING_DEFAULT_MAX_WORKERS: int = 4
PROCESSING_NUMERIC_THREADS_PER_WORKER: int = 1
PROCESSING_KALMAN_THREADS_PER_WORKER: int = 2
WEATHER_IO_WORKERS: int = 2
CORRIDOR_IO_THREADS: int = 4
CORRIDOR_CLUSTERING_THREADS_PER_WORKER: int = 2

# Trino retry / timeout parameters (§3.3.1)
BACKOFF_MAX_RETRIES: int = 10           # Max Trino retry attempts (exponential back-off)
BACKOFF_INITIAL_DELAY: float = 1.0      # Initial back-off delay in seconds
BACKOFF_FACTOR: float = 2.0             # Multiplicative factor applied after each retry
BACKOFF_MAX_DELAY: float = 60.0         # Hard cap on per-retry delay in seconds
TRINO_QUERY_TIMEOUT_SECS: int = 300     # Trino query execution timeout in seconds

# Fetching filename conventions (§3.3.1)
RAW_TRAJECTORY_SUFFIX: str = "_raw.parquet"
RAW_CONCAT_SUFFIX: str = "_all_raw.parquet"
FETCH_RUNS_DIRNAME: str = "runs"
RAW_TRAJECTORY_DIRNAME: str = "raw"

# Target aircraft typecode families
A320_NEO_FAMILY = ["A19N", "A20N", "A21N"]
A320_CEO_FAMILY = ["A318", "A319", "A320", "A321"]
B737_NG_FAMILY = ["B733", "B734", "B735", "B736", "B737", "B738", "B739"]
B737_MAX_FAMILY = ["B37M", "B38M", "B39M"]

ALL_TARGET_FAMILIES = A320_NEO_FAMILY + A320_CEO_FAMILY + B737_NG_FAMILY + B737_MAX_FAMILY

# Sentinel indicating an explicitly missing, NaN, or unassigned/unsupported aircraft typecode
UNSUPPORTED_TYPECODE_FLAG = "NaN_OR_UNSUPPORTED"


def is_supported_typecode(typecode: Any) -> bool:
    """
    Validates whether a given aircraft typecode belongs strictly to ALL_TARGET_FAMILIES.
    Returns False for None, NaN, empty strings, 'UNKNOWN', or any model outside the target families.
    """
    from typing import Any
    import pandas as pd
    if typecode is None or pd.isna(typecode):
        return False
    tc_str = str(typecode).strip().upper()
    if not tc_str or tc_str in ("NAN", "NONE", "UNKNOWN", "UNK", UNSUPPORTED_TYPECODE_FLAG):
        return False
    return tc_str in ALL_TARGET_FAMILIES


# Geographic filtering limits (Padded European Bounding Box)
# Recomputed from custom airport extent (LLRM, LTCF, ENAS, LPAZ/LPPD) with 5.0 degree padding
# Base bounds: Lat [30.0, 80.0], Lon [-26.0, 45.0]
EUR_LAT_MIN = 25.0
EUR_LAT_MAX = 85.0
EUR_LON_MIN = -31.0
EUR_LON_MAX = 50.0



# PCA Calibration Constants
# D_PCA and N_STANDARD are sentinel placeholders (-1). Run the Phase A/B
# calibration script (Step 5) on 3 oversampled routes to derive these values
# and update them here. No model file is saved -- PCA is fit fresh per-route.
D_PCA                 = 13     # Populated by Phase A: number of PCA components (95% variance)
N_STANDARD            = 65     # Populated by Phase A: per-route query budget = 5 × D_PCA
DELTA_CV_THRESHOLD    = 0.01   # Populated by Phase B: ΔCV convergence threshold
DELTA_CV_EPSILON      = 1e-8   # Guard for near-zero std in relative ΔCV formula

# Stability Sampling — Resampling Loop Controls
STABILITY_RESAMPLE_MULTIPLIER = 2   # On resample: query N_STANDARD × (multiplier^round) flights
STABILITY_MAX_RESAMPLE_ROUNDS = 3   # Hard cap: max resample rounds before forcing convergence

# Clustering Hyperparameter Tuning Constants
# Promoted from hardcoded literals in path_generator.py.
CLUSTERING_MAX_K          = 4      # Maximum k to evaluate in Silhouette loop
SILHOUETTE_THRESHOLD      = 0.35   # Minimum silhouette score to accept k > 1
CHAOS_VARIANCE_THRESHOLD  = 200.0  # Total coordinate variance above which k=1 is classified as Chaos
MIN_FLIGHTS_FOR_CLUSTERING = 3     # Minimum cohort size; below this k=1 is forced
CORRIDOR_TIME_GRID_SECONDS = 60    # Temporal resolution of saved corridor parquets

# Calibration Routes
# Edit this list to control which routes are evaluated by phase_a_d_pca.py,
# gt_stability_sweep.py, and variational_orchestrator.py.
CALIBRATION_ROUTES: list[str] = [
    "EDDF-LIRF",
    "EGLL-BIKF",
    "ESSA-LEMD",
    "ESSA-EHAM",
    "LFRS-LFMN",
    "LGSA-LGAV",
]

# ROCD Classification Thresholds (ft/min)
# Promoted from hardcoded literals in path_generator.py.
ROCD_MIN_CLIMB_RATE   = 1800.0  # Minimum acceptable clean-flight climb rate
ROCD_MIN_DESCENT_RATE = 1200.0  # Minimum acceptable clean-flight descent rate

ROUTE_SUMMARY_PKL = MASTER_FLIGHTS_DB_DIR / "master_flights_route_summary.pkl"
ROUTE_SUMMARY_PARQUET = MASTER_FLIGHTS_DB_DIR / "master_flights_route_summary.parquet"
ROUTE_SUMMARY_CSV = MASTER_FLIGHTS_DB_DIR / "master_flights_route_summary.csv"
MASTER_FLIGHTS_FILE = MASTER_FLIGHTS_DB_DIR / "master_flights.parquet"

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
# Encompasses edges: Azores (LPAZ/LPPD), Svalbard (ENAS), Turkey (LTCF), Israel (LLRM)
# Built from base bounds Lat [30.0, 80.0], Lon [-26.0, 45.0]
# west, south, east, north = [-26.0, 30.0, 45.0, 80.0]
WEATHER_BOUNDS_BBOX = [-26.0, 30.0, 45.0, 80.0]

# Dynamic datasets and outcomes
TRAJECTORIES_DIR = DATA_DIR / "trajectories"
SIMULATION_PROFILES_DIR = DATA_DIR / "simulation_profiles"
CORRIDOR_PATHS_DIR = DATA_DIR / "corridor_paths"
RESULTS_DIR = DATA_DIR / "results"
CORRIDOR_SIMULATIONS_DIR = RESULTS_DIR / "corridor_simulations"

# --- Phase Quality Campaign Filtering Defaults ---
# Default metadata pre-filter thresholds (None = check is ignored/pass-through unless overridden via CLI)
DEFAULT_PREFILTER_THRESHOLDS = {
    "max_dep_horiz_dist": 15000,          # meters
    "max_dep_vert_dist": 1000,           # meters
    "max_arr_horiz_dist": 15000,          # meters
    "max_arr_vert_dist": 1000,           # meters
    "max_dep_candidates": None,          # count
    "max_arr_candidates": None,          # count
    "max_duration_pct_above_median": 20, # % above route median
    "min_duration_pct_below_median": 20, # % below route median
}

# Default post-filter thresholds
DEFAULT_POSTFILTER_THRESHOLDS = {
    "max_velocity_kt": 650.0,        # max 3D velocity (knots)
    "max_acceleration_mps2": 10,     # max 3D acceleration (m/s^2)
}

# Post-filter stage defaults
POSTFILTER_BATCH_SIZE_DEFAULT: int = 200

# Clean-registry column names written by the post-filter stage
POSTFILTER_COL_VELOCITY_PASS: str = "velocity_pass"
POSTFILTER_COL_VELOCITY_REASON: str = "velocity_reject_reason"
POSTFILTER_COL_COORD_VEL_PASS: str = "coordinate_velocity_pass"
POSTFILTER_COL_COORD_VEL_REASON: str = "coordinate_velocity_reject_reason"
POSTFILTER_COL_ACCEL_PASS: str = "acceleration_pass"
POSTFILTER_COL_ACCEL_REASON: str = "acceleration_reject_reason"
POSTFILTER_COL_DISTANCE_PASS: str = "distance_pass"
POSTFILTER_COL_DISTANCE_REASON: str = "distance_reject_reason"

# Flag to force a re-computation of airport distance metrics before filtering
RECOMPUTE_AIRPORT_DISTANCES = True

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

# Temporary directory (pure path constant — no side-effects on import)
TEMP_DIR = DATA_DIR / "temp"


def init_runtime() -> None:
    """Create required runtime directories and redirect temp env variables.

    Must be called explicitly by every module entrypoint (``if __name__ == '__main__'``
    block) before performing any filesystem I/O.  Must NOT be called at import time.
    """
    for directory in [
        DATA_DIR, MASTER_FLIGHTS_DB_DIR, AIRCRAFT_DB_DIR, REGISTRIES_DIR,
        LOGS_DIR, REPORTS_DIR, TEMP_DIR,
        DATA_DIR / "analysis" / "plots",
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    for env_var in ["TEMP", "TMP", "TMPDIR"]:
        os.environ[env_var] = str(TEMP_DIR)


@dataclass(frozen=True)
class PhaseControl:
    ENABLE_NIS          : bool = True
    ENABLE_RESIDUALS    : bool = True
    ENABLE_CONDITION    : bool = True
    ENABLE_REPORTING    : bool = True   # PDF/PNG generation
    ENABLE_TENSOR_SAVE  : bool = True   # Save .npz per route
    ENABLE_FLAT_TABLE   : bool = True   # Write flat parquet & CSV
