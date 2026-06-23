# Architecture Blueprint: Flight Physics Pipeline

This document defines the architecture, data schemas, module objectives, and workflow connections within the Flight Physics Pipeline. It is written to match the exact current state of the codebase.

---

## 1. Pipeline Overview

The Flight Physics Pipeline is a modular, data-driven framework written in Python designed to ingest, process, cluster, and simulate aircraft trajectories. It transitions raw ADSB waypoints into physical contrail simulations by executing a sequence of decoupled loops:

1.  **Ingestion & Filtering (Loops 1 & 2)**: Corridor slicing and sampling ADSB data.
2.  **Smoothing & Resampling (Loop 2b)**: Extended Kalman Filtering (EKF) and grid resampling.
3.  **Synthesis (Loop 2c)**: DTW trajectory clustering and route centroid synthesis.
4.  **Weather Acquisition (Loop 3a)**: ERA5 NetCDF reanalysis downloads.
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
│   └── era5/                # Copernicus CDS pressure/surface levels NetCDFs
│   └── logs/                # Weather acquisition logs
├── master_flight_paths/     # Flight route boundary and definition files
├── simulation_profiles/     # Configuration profiles for physics runs
├── synthesized_paths/       # DTW-synthesized baseline route centroid Parquet files
└── results/                 # Output simulated Parquet files (PSFlight + CoCiP outcomes)
```

### Run Folder Naming Template
Run directories under `data/trajectories/` and `data/results/` are generated dynamically based on CLI configurations:
`ranks_[lower]to[upper]_strat_[strategy]_val_[val]_seed_[seed]_format_[format]_mindist_[dist]_[hash]`
*   *Hash Suffix*: A deterministic 6-character MD5 checksum of the parameter prefix to guarantee unique cohort namespaces.

---

## 3. Global Conventions & Standards

### 3.1 Datetime Timezone Standard
*   **naive UTC**: All loaded datetimes are coerced to timezone-naive UTC representation (`datetime64[ns]`) upon loading to prevent timezone comparison mismatch warnings:
    ```python
    df['time'] = pd.to_datetime(df['time']).dt.tz_localize(None)
    ```

### 3.2 File Suffix Conventions
Standard suffixes indicate the processing state of trajectory datasets:

| File Suffix | Description | Format |
|---|---|---|
| `*_raw.parquet` | Raw waypoints containing coordinates, noise, and gaps. | Parquet |
| `*_clean_si.parquet` | Resampled and EKF-smoothed coordinates. | Parquet |
| `*_synthesized_c[ID].parquet` | Temporal-gridded DTW trajectory route centroids. | Parquet |
| `*_simulated.parquet` | Trajectories containing PSFlight and CoCiP simulation results. | Parquet |

### 3.3 Physical Units Standards
The pipeline converts raw aviation inputs into SI units during EKF smoothing and simulation phases:

| Parameter | Aviation Units (Raw) | SI Units (Internal/Sim) | Conversion Math |
|---|---|---|---|
| **Altitude** | Feet (ft) | Meters (m) | $1 \text{ ft} = 0.3048 \text{ m}$ |
| **Speed** | Knots (kt) | Meters per second (m/s) | $1 \text{ kt} \approx 0.5144 \text{ m/s}$ (Exact: $1852/3600$) |
| **Distance** | Kilometers (km) | Meters (m) | $1 \text{ km} = 1000 \text{ m}$ |
| **ROCD** | Feet per minute (ft/min) | Meters per second (m/s) | $1 \text{ m/s} \approx 196.8504 \text{ ft/min}$ |

---

## 4. Module Specifications

### 4.1 Common Module (`src/common/`)
*   **Objectives**: Centralizes pipeline paths, configurations, DataFrame-to-PyContrails serialization adapters, global registry manifest rebuilding, and route distance calculations.
*   **Key Files**:
    *   [config.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/config.py): Path definitions and ERA5 constants.
    *   [adapters.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/adapters.py): Converts DataFrames to `pycontrails.Flight` structures and handles timezone-naive UTC parsing.
    *   [build_global_manifest.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/build_global_manifest.py): Scans run directories to reconstruct global registries under `data/flight_registry/registries/`.
    *   [enrich_route_summary.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/enrich_route_summary.py): Vectorized Haversine geodesic distance enrichment.
    *   [migrate_directories.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/migrate_directories.py): Moves flat cohort directories into `raw/` and `clean/` subdirectories.
    *   [utils.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/utils.py): Logger utilities and dynamic folder name hash generation.

### 4.2 Fetching Module (`src/fetching/`)
*   **Objectives**: Queries raw waypoints for sliced flight lists from OpenSky database partitions.
*   **Key Files**:
    *   [opensky_fetcher.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/fetching/opensky_fetcher.py): Remote Trino client queries and local cache checks.
    *   [fetcher_orchestrator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/fetching/fetcher_orchestrator.py): Argument parsers, seed validations, and batch downloads.

### 4.3 Filtering Module (`src/filtering/`)
*   **Objectives**: Slices master schedules based on corridor origin/destination airport, temporal bounds, and route summary ranks.
*   **Key Files**:
    *   [population_filter.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/filtering/population_filter.py): Filtering algorithms, timezone naivety coercions.
    *   [filter_orchestrator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/filtering/filter_orchestrator.py): Mutually exclusive rank-slicing pipelines with typo resiliency checks.

### 4.4 Trajectory Processing Module (`src/processing/`)
*   **Objectives**: Prepares flight coordinates by applying an Extended Kalman Filter (EKF) in a local Lambert projection plane and interpolating points to a uniform 1-minute temporal grid.
*   **Key Files**:
    *   [kalman_filter.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/processing/kalman_filter.py): Integrates the `traffic` EKF library (properly utilizing `DatetimeIndex` before EKF and reverting to `RangeIndex` post-EKF to resolve index mismatches), ensures metadata propagation, and logs runs to `extraction.log`.

### 4.5 Path Synthesis Module (`src/synthesis/`)
*   **Objectives**: Compiles DTW spatial trajectory centroids across flight cohorts to produce idealized baseline route trajectories.
*   **Key Files**:
    *   [path_generator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/synthesis/path_generator.py): DTW clustering, spatial resampling, and FL250 cruise altitude synthesis validations.
    *   [synthesis_orchestrator.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/synthesis/synthesis_orchestrator.py): Cohort synthesis manager and skip checks.

### 4.6 Weather Acquisition Module (`src/weather/`)
*   **Objectives**: Bulk fetches ERA5 reanalysis datasets via Copernicus CDS API.
*   **Key Files**:
    *   [era5_manager.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/weather/era5_manager.py): Background download threads, self-healing corruption handlers, and file logger integration.

### 4.7 Physics Simulation Module (`src/physics/`)
*   **Objectives**: Simulates fuel usage, emissions, contrail generation, and radiative forcing using `PSFlight` and `CoCiP` models.
*   **Key Files**:
    *   [simulation.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/physics/simulation.py): PSFlight + CoCiP executor. Raises a `ValueError` for unsupported aircraft types, which is caught by the runner to log skipped flights.
    *   [clone_simulation.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/physics/clone_simulation.py): Re-simulates flight cohorts using synthesized route paths under temporal offsets.
