# Architecture Blueprint: Flight Physics Pipeline

This document defines the architecture, data schemas, module objectives, and workflow connections within the Flight Physics Pipeline. It aligns precisely with the current state of the codebase.

---

## 1. Pipeline Overview

The Flight Physics Pipeline is a modular, data-driven framework written in Python designed to ingest, process, cluster, and simulate aircraft trajectories. It transitions raw ADSB waypoints into physical contrail simulations by executing a sequence of decoupled loops:

1.  **Ingestion & Filtering (Loops 1 & 2)**: Corridor slicing and sampling ADSB data.
2.  **Smoothing & Resampling (Loop 2b)**: Extended Kalman Filtering (EKF) and grid resampling.
3.  **Synthesis (Loop 2c)**: DTW trajectory clustering and route centroid synthesis.
4.  **Weather Acquisition (Loop 3a)**: Copernicus CDS ERA5 NetCDF reanalysis downloads.
5.  **Physics Simulation (Loop 3b)**: PSFlight performance and CoCiP contrail modeling.

---

## 2. Directory Structure

All modules interact with a standardized dataset layer stored under the project root:

```text
data/
├── flight_registry/         # Master lists and summary index files
│   └── registries/          # Global Parquet-based tracking registries
├── flight_lists/            # Sliced flight list Parquet files (e.g. EGLL-KJFK.parquet)
├── trajectories/            # Trajectory coordinates partitioned by run directories
│   └── <run_name>/
│       ├── raw/             # Raw trajectory waypoints fetched from OpenSky
│       └── clean/           # Smoothed and resampled EKF trajectory outputs
├── weather/                 # Local cache of ERA5 NetCDF files
│   └── logs/                # Weather acquisition logs
├── master_flight_paths/     # Flight route boundary and definition files
├── simulation_profiles/     # Configuration profiles for physics runs
├── synthesized_paths/       # DTW-synthesized baseline route centroid Parquet files
└── results/                 # Output simulated Parquet files (PSFlight + CoCiP outcomes)
```

### 2.1 Source Code Directory Structure

The source files under `src/` are structured by pipeline module:

```text
src/
├── common/                  # Shared serialization, configurations, and registries manager
│   ├── adapters.py
│   ├── build_global_manifest.py
│   ├── config.py
│   ├── enrich_route_summary.py
│   ├── migrate_directories.py
│   ├── utils.py
│   └── README.md
├── fetching/                # OpenSky Trino database querying and caching
│   ├── fetcher_orchestrator.py
│   ├── opensky_fetcher.py
│   └── README.md
├── filtering/               # Corridor schedules slicing and filter workflows
│   ├── filter_orchestrator.py
│   ├── population_filter.py
│   └── README.md
├── processing/              # Coordinate EKF smoothing and 1-minute resampling
│   ├── kalman_filter.py
│   ├── TRAFFIC_LIBRARY_EKF_ANALYSIS.md
│   └── README.md
├── synthesis/               # Dynamic Time Warping (DTW) route synthesis
│   ├── path_generator.py
│   ├── synthesis_orchestrator.py
│   └── README.md
├── weather/                 # ERA5 NetCDF reanalysis downloads from CDS API
│   ├── era5_manager.py
│   └── README.md
├── physics/                 # PSFlight performance and CoCiP contrail simulations
│   ├── clone_simulation.py
│   ├── simulation.py
│   └── README.md
├── Architecture Blueprint.md # This architecture overview
└── conventions.md           # Project-wide programming and coding standards
```

### Run Folder Naming Template
Run directories under `data/trajectories/` and `data/results/` are generated dynamically based on CLI configurations:
`ranks_[lower]to[upper]_strat_[strategy]_val_[val]_seed_[seed]_format_[format]_mindist_[dist]_[hash]`
*   **Hash Suffix**: A deterministic 6-character MD5 checksum of the parameter prefix to guarantee unique cohort namespaces.

---

## 3. Global Conventions & Standards

### 3.1 Datetime Timezone Standard
*   **UTC Standard**: Datetime columns can be timezone-aware UTC (e.g., with `+00:00` offset or `'UTC'`) or timezone-naive UTC, consistently used within each module to ensure seamless comparisons. Standard fetched data from Trino/OpenSky outputs timezone-aware UTC. Enforce timezone-naive UTC for internal simulation engine processing if required by third-party packages (e.g., PyContrails).

### 3.2 File Suffix Conventions
Standard suffixes indicate the processing state of trajectory datasets:

| File Suffix | Description | Format |
|---|---|---|
| `*_raw.parquet` | Raw waypoints containing coordinates, noise, and gaps. | Parquet |
| `*_clean_si.parquet` | Resampled and EKF-smoothed coordinates. | Parquet |
| `*_synthesized_c[ID].parquet` | Temporal-gridded DTW trajectory route centroids. | Parquet |
| `*_simulated.parquet` | Trajectories containing PSFlight and CoCiP simulation results. | Parquet |

### 3.3 Physical Units Standards
The pipeline converts raw aviation inputs into SI units during EKF smoothing and simulation phases. Conversion factors are centralized in `src/common/config.py`:

| Parameter | Aviation Units (Raw) | SI Units (Internal/Sim) | Centralized Constant |
|---|---|---|---|
| **Altitude** | Feet (ft) | Meters (m) | `M_TO_FT = 3.280839895` |
| **Speed** | Knots (kt) | Meters per second (m/s) | `MPS_TO_KT = 1.9438444924` |
| **Distance** | Kilometers (km) | Meters (m) | $1 \text{ km} = 1000 \text{ m}$ |
| **ROCD** | Feet per minute (ft/min) | Meters per second (m/s) | `MPS_TO_FPM = 196.8503937` |
| **Coordinates** | Degrees (WGS84) | Meters (LAEA Projection) | Custom Lambert Azimuthal Equal Area |

---

## 4. Module Specifications

### 4.1 Common Module (`src/common/`)
*   **Objectives**: Centralizes pipeline paths, configurations, DataFrame-to-PyContrails serialization adapters, global registry manifest rebuilding, and route summary utilities.
*   **Key Files**:
    *   [config.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/config.py): Path definitions, ERA5 constants, and centralized unit conversion factors (`M_TO_FT`, `MPS_TO_KT`, `MPS_TO_FPM`).
    *   [adapters.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/adapters.py): Converts DataFrames to `pycontrails.Flight` structures and handles timezone UTC parsing. Converts metrics to aviation units for Traffic using centralized config constants.
    *   [build_global_manifest.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/build_global_manifest.py): Rebuilds global registries under `data/flight_registry/registries/` using imported directory paths and standard `keep='last'` deduplication.
    *   [enrich_route_summary.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/enrich_route_summary.py): Vectorized Haversine geodesic distance enrichment. Run without path-injection hacks.
    *   [utils.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/utils.py): Logger utilities and dynamic folder name hash generation. Prioritizes lowercase `master_flights_route_summary.pkl`.

### 4.2 Fetching Module (`src/fetching/`)
*   **Objectives**: Queries raw waypoints for sliced flight lists from OpenSky database partitions.
*   **Key Files**:
    *   [opensky_fetcher.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/fetching/opensky_fetcher.py): Remote Trino client queries (already returning SI units from database) and local cache checks.
    *   [fetcher_orchestrator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/fetching/fetcher_orchestrator.py): Argument parsers, seed validations, and batch downloads. Validates and blocks conflicting `--ranks` and `--upper-rank` usage.

### 4.3 Filtering Module (`src/filtering/`)
*   **Objectives**: Slices master schedules based on corridor airport pairs, temporal bounds, and route summary ranks.
*   **Key Files**:
    *   [population_filter.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/filtering/population_filter.py): Filtering algorithms and timezone UTC mapping. Includes aliases for CLI arguments (`--file` / `--master-file`). Setup `extraction.log` file logging.
    *   [filter_orchestrator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/filtering/filter_orchestrator.py): Batch corridor generator. Includes argument aliases and setups `extraction.log` file logging in output directory.

### 4.4 Trajectory Processing Module (`src/processing/`)
*   **Objectives**: Prepares flight coordinates by applying an Extended Kalman Filter (EKF) in a local Lambert projection plane and interpolating points to a uniform 1-minute temporal grid.
*   **Key Files**:
    *   [kalman_filter.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/processing/kalman_filter.py): Integrates the `traffic` EKF library. Utilizes centralized constants from config for scaling. Writes execution details to `extraction.log`.
    *   **EKF Index Exception**: The index setting code prior to calling EKF remains commented out to prevent time-serialization JSON crashes. This is a documented exception to standard pandas row index alignment conventions.

### 4.5 Path Synthesis Module (`src/synthesis/`)
*   **Objectives**: Compiles DTW spatial trajectory centroids across flight cohorts to produce idealized baseline route trajectories.
*   **Key Files**:
    *   [path_generator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/synthesis/path_generator.py): DTW clustering, spatial resampling, and FL250 cruise altitude synthesis validations. Gracefully resolves absolute output directories outside the workspace. Drops derivative kinematic columns (`gs`, `heading`, `rocd`, etc.) before instantiating PyContrails flight containers to force dynamic, kinematically consistent recalculation.
    *   [synthesis_orchestrator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/synthesis/synthesis_orchestrator.py): Cohort synthesis manager and skip checks.

### 4.6 Weather Acquisition Module (`src/weather/`)
*   **Objectives**: Bulk fetches ERA5 reanalysis datasets via Copernicus CDS API.
*   **Key Files**:
    *   [era5_manager.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/weather/era5_manager.py): Background download threads, self-healing corruption handlers, and `weather_acquisition.log` logging. Handles single integer pressure level inputs robustly.

### 4.7 Physics Simulation Module (`src/physics/`)
*   **Objectives**: Simulates fuel usage, emissions, contrail generation, and radiative forcing using `PSFlight` and `CoCiP` models.
*   **Key Files**:
    *   [simulation.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/physics/simulation.py): PSFlight + CoCiP executor. Skips unsupported aircraft types, appending details to `skipped_aircraft.log` under the output directory.
    *   [clone_simulation.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/physics/clone_simulation.py): Re-simulates flight cohorts using synthesized route paths under temporal offsets. Appends skipped flights to the centralized `skipped_aircraft.log` in the root output folder.
