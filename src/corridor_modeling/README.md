# Corridor Modeling & Trajectory Synthesis Module

This module handles physical corridor modeling and representative trajectory synthesis for the Flight Physics Pipeline. It is responsible for identifying typical flight paths along specific route corridors using spatial and kinematic modeling.

The module operates in three distinct stages:
1. **Stage 2: Stability Sampling**: Run per-route trajectory cohorts through Principal Component Analysis (PCA) to calculate sequential batch variance ($\Delta$CV) and dynamically expand the flight dataset size until trajectory stability is reached.
2. **Stage 3: Cohort Clustering**: Segment converged route cohorts into spatial clusters using K-Means with Silhouette scoring, classify the route type, and identify typical "medoid" trajectory templates.
3. **Trajectory Synthesis**: Resample, EKF-smooth, and snap the chosen medoid trajectories onto a uniform temporal grid starting at a fixed baseline datetime (2025-01-01 00:00:00 UTC).

The module supports two processing strategies:
* **Strategy A (Batch-Registry Mode)**: Decoupled orchestration where stability sampling (Stage 2) and clustering (Stage 3) are run sequentially as separate commands using local registries.
* **Strategy B (Streaming-Pipeline Mode)**: A unified, asynchronous 3-pool streaming system that dynamically fetches trajectories via Trino, checks stability, resamples if necessary, clusters, and writes registries in a single command.

It operates as **Loop 2b** and **Loop 2c** of the Flight Physics Pipeline.

---

## 1. Module Structure

```text
src/corridor_modeling/
├── README.md                  # This documentation file
├── path_generator.py          # Core engine that performs coordinate projection, DTW medoid search, and SI grids
├── corridor_orchestrator.py   # CLI entrypoint for batch trajectory synthesis (Option A)
├── pca_compressor.py          # Core mathematical routines (Z-score scaling, PCA fitting, delta-CV computation)
├── stability_orchestrator.py  # CLI driver for Stage 2 Stability Sampling
├── stability_worker.py        # Picklable per-route worker logic for stability checks (runs in ProcessPoolExecutor)
├── clustering_orchestrator.py # CLI driver for Stage 3 Clustering
├── clustering_worker.py      # Picklable per-route worker logic for K-Means and medoid selection
└── streaming_pipeline.py      # Unified, 3-pool asynchronous fetch-compute-write streaming pipeline (Option B)
```

---

## 2. Function Analysis Solution Tree (FAST)

```text
Module Objectives
 └── Establish representative 4D medoid trajectory templates for flight corridors (Loop 2b & 2c)
      │
      ├── Sub-objective 1: Cohort Stability Sampling & Expansion (Stage 2)
      │    └── Solution: stability_orchestrator.py + stability_worker.py
      │         ├── Inputs: ranks, max_workers, trajectory registry, PCA/standard parameters (D_PCA, N_STANDARD)
      │         └── Outputs: delta-CV values, updates global_stability_registry.parquet
      │
      ├── Sub-objective 2: Trajectory Clustering & Medoid Selection (Stage 3)
      │    └── Solution: clustering_orchestrator.py + clustering_worker.py
      │         ├── Inputs: converged routes list, trajectory registry, maximum cluster size (CLUSTERING_MAX_K)
      │         └── Outputs: Medoid parquet files saved to corridor_paths, updates global_model_registry.parquet
      │
      ├── Sub-objective 3: Consolidated Batch Trajectory Synthesis (Option A)
      │    └── Solution: corridor_orchestrator.py + path_generator.py
      │         ├── Inputs: route rank, target output directory, temporal grid resolution (grid-seconds)
      │         └── Outputs: Synthesized reference parquets, updates global_model_registry.parquet
      │
      ├── Sub-objective 4: Unified Streaming Pipeline Execution (Option B)
      │    └── Solution: streaming_pipeline.py
      │         ├── Inputs: target ranks, fetch and compute worker counts, date range, min route distance
      │         └── Outputs: Fetched parquets, stability calculations, medoid parquets, updates registries
      │         └── Concurrency: Orchestrates 3 pools (Trino fetch, compute worker processes, main-thread writer)
      │
      ├── Sub-objective 5: Kinematic Normalization & Outlier Rectification
      │    └── Solution: classify_and_normalize_cohort() in pca_compressor.py
      │         ├── Inputs: raw trajectory coordinates, minimum climb/descent ROCD thresholds
      │         └── Outputs: Outlier-smoothed airborne trajectories projected to local coordinate systems
      │
      └── Sub-objective 6: Vectorization & PCA Dimensionality Reduction
           └── Solution: vectorize_cohort() + fit_pca() in pca_compressor.py
                ├── Inputs: standardized trajectory coordinates, PCA target components
                └── Outputs: Z-scored vectors, projection matrices, and reduced PCA dimensions
```

---

## 3. Data Workflow

> [!NOTE]
> **Visual Rendering Warning**: Flowcharts are generated using Mermaid. If your markdown viewer does not natively support Mermaid rendering, please refer to the step-by-step text description provided directly below each diagram.

### 3.1. Strategy A: Standard Registry-Based Modeling (Stage 2 + Stage 3)

```mermaid
graph TD
    A[data/flight_registry/registries/global_trajectory_registry.parquet] -->|1. Ingest raw file paths| B(stability_orchestrator.py)
    B -->|2. Dispatch process tasks| C[stability_worker.py: process_route]
    C -->|3. Load trajectory parquets| D[Cohort Dataframes]
    D -->|4. Clean holding patterns| E[Normalized Tracks]
    E -->|5. Vectorize & fit PCA| F[Trajectory PCA Space]
    F -->|6. Calculate delta-CV| G{delta-CV < threshold?}
    G -->|No| H[Expand sample size N & requery]
    H --> D
    G -->|Yes| I[Mark route as converged]
    I -->|9. Flush results| J[global_stability_registry.parquet]
    
    J -->|10. Read converged list| K(clustering_orchestrator.py)
    A -->|11. Ingest raw file paths| K
    K -->|12. Dispatch process tasks| L[clustering_worker.py: cluster_route]
    L -->|13. Load converged cohort| M[Converged Cohort Dataframes]
    M -->|14. Clean & fit PCA| N[Converged PCA Space]
    N -->|15. K-Means Silhouette check| O[Optimal clusters K & labels]
    O -->|16. Medoid selection| P[Identify originally-clean medoid paths]
    P -->|17. Save corridor templates| Q[data/corridor_paths/<route>_corridor_c{id}.parquet]
    Q -->|18. Batch update registry| R[global_model_registry.parquet]
```

#### Step-by-Step Description: Strategy A (Decoupled Batch-Registry Mode)
1. **Raw Trajectory Identification**: The `stability_orchestrator.py` CLI loads the `global_trajectory_registry.parquet` mapping raw flight IDs to local trajectory parquets.
2. **Task Dispatching**: The orchestrator resolves ranks to route IDs (`DEP-ARR`) and dispatches individual route jobs to parallel subprocesses running `stability_worker.py:process_route`.
3. **Data Ingestion**: Each worker process loads up to `N_STANDARD` raw flights for its assigned route.
4. **Outlier Filtering & Normalization**: The worker filters trajectories to airborne phases and runs `classify_and_normalize_cohort` (ROCD-based classification) to replace holding-pattern circles/loops with direct 3D lines scaled to median clean durations.
5. **PCA Dimensionality Reduction**: Standardized spatial coordinates are vectorized, Z-score scaled, and fit with a fresh per-route PCA to compress 3D tracks.
6. **Stability Checking**: The worker divides the cohort in half and computes a sequential batch variance ($\Delta$CV) across the PCA coefficients.
7. **Sample Size Expansion**: If $\Delta$CV $\ge$ threshold, the worker expands the target flight sample size (using `STABILITY_RESAMPLE_MULTIPLIER`), queries additional flight IDs, and re-runs the PCA stability evaluation.
8. **Stability Registry Write**: When $\Delta$CV converges below the threshold (or max resample rounds is hit), the route is marked as converged, and the orchestrator flushes the results to `global_stability_registry.parquet`.
9. **Clustering Cohort Selection**: The `clustering_orchestrator.py` reads the stability registry to identify converged routes.
10. **Clustering Task Dispatching**: The orchestrator dispatches routes to worker processes running `clustering_worker.py:cluster_route`.
11. **K-Means Cluster Search**: The worker fits PCA on the converged cohort and runs K-Means clustering for $k \in [2, \text{CLUSTERING\_MAX\_K}]$. The optimal cluster count is determined via Silhouette scoring.
12. **Medoid Identification**: For each cluster, the algorithm selects the closest *originally-clean* flight path (no holding patterns) in PCA space as the representative medoid.
13. **Corridor Templating**: The chosen medoids are converted to PyContrails Flight formats, snapped to a uniform temporal grid (e.g. 60s) starting at the `2025-01-01` baseline, and written as parquet files to `data/corridor_paths/`.
14. **Model Registry Write**: The orchestrator flushes batch results, registering the new corridor files, route classes, and cluster sizes to `global_model_registry.parquet`.

---

### 3.2. Strategy B: Unified Streaming Pipeline (streaming_pipeline.py)

```mermaid
graph TD
    A[Ranks Range / Specific List] --> B(streaming_pipeline.py)
    B -->|1. Submit fetching tasks| C[Pool 1: ThreadPoolExecutor I/O]
    C -->|2. Query Trino / Cache| D[data/trajectories/.../*_raw.parquet]
    D -->|3. Update raw registry| E[global_trajectory_registry.parquet]
    
    B -->|4. Submit compute tasks| F[Pool 2: ProcessPoolExecutor CPU]
    E -->|5. Load raw file paths| F
    F -->|6. Clean holding patterns| G[Normalized Tracks]
    G -->|7. Fit PCA & check stability| H{delta-CV < threshold?}
    H -->|No| I[Re-queue for fetching larger sample]
    I --> C
    H -->|Yes| J[Run K-Means & select medoid paths]
    J -->|8. Save corridor templates| K[data/corridor_paths/<route>_corridor_c{id}.parquet]
    
    B -->|9. Main-thread serial writes| L[Pool 3: Registry Flush]
    K -->|10. Register corridors| L
    L -->|11. Flush database files| M[global_model_registry.parquet]
```

#### Step-by-Step Description: Strategy B (Unified Streaming Pipeline)
1. **Initialization**: The `streaming_pipeline.py` CLI loads target ranks and checks the model registry (`global_model_registry.parquet`) to filter out already-completed routes.
2. **Asynchronous Fetching (Pool 1)**: For each remaining route, a job is submitted to a thread pool (`ThreadPoolExecutor`). The threads query the OpenSky Trino database (or local cache) for flight coordinates, write them to disk, and update the `global_trajectory_registry.parquet`.
3. **Asynchronous Compute (Pool 2)**: Once raw files are ready, compute jobs are submitted to a process pool (`ProcessPoolExecutor`). The workers:
   * Load raw coordinate parquets.
   * Apply EKF-smoothing, airborne filtering, and ROCD holding-pattern normalization.
   * Vectorize coordinates, fit PCA, and compute the $\Delta$CV stability metric.
4. **Resampling Feedback Loop**: If stability has not converged and the route has not hit `max_resample_rounds`, the job state is updated with an expanded target flight count. The job is re-submitted back to the fetching pool (Pool 1) to retrieve additional flights, repeating the fetch-compute cycle.
5. **Route Clustering & Medoid Selection**: Once the cohort converges (or max rounds is reached), the worker runs K-Means clustering, classifies the route, identifies the medoid tracks in PCA space, and saves the resampled corridor parquet files to disk.
6. **Main Thread Writer (Pool 3)**: Completed jobs are pushed to a results queue. The main thread processes the queue sequentially, flushing metadata entries to `global_model_registry.parquet` without any write concurrency.

---

### 3.3. Optimization & Concurrency Modes

* **Process-based Concurrency**: CPU-heavy calculations (such as PCA fits, coordinate projections, and K-Means silhouette evaluations) run inside a `ProcessPoolExecutor` to bypass Python's Global Interpreter Lock (GIL).
* **Thread-based Concurrency**: I/O-heavy operations (Trino queries, cached parquet file loading, and disk writes) run inside a `ThreadPoolExecutor`, enabling parallel data ingestion.
* **Spawn Multiprocessing**: Windows lacks native `fork` support. To prevent imports side-effects, worker methods in `stability_worker.py` and `clustering_worker.py` are stateless, picklable top-level functions that operate in a `spawn` context.
* **Main-Thread Registry Flushes**: To prevent concurrent file lock contention and parquet write conflicts, database file updates are queued and flushed exclusively by the main thread.

---

## 4. CLI Usage Guide

### Bash

```bash
# ==========================================
# 1. Strategy A: Decoupled Batch-Registry Mode
# ==========================================

# Step 1: Run Stage 2 Stability Campaign on ranks 1-100 (4 workers)
python -m src.corridor_modeling.stability_orchestrator \
    --lower-rank 1 \
    --upper-rank 100 \
    --max-workers 4

# Step 2: Run Stage 3 Clustering Campaign on ranks 1-100
python -m src.corridor_modeling.clustering_orchestrator \
    --lower-rank 1 \
    --upper-rank 100 \
    --max-workers 4

# Step 3: Run Trajectory Synthesis directly on rank 76
python -m src.corridor_modeling.corridor_orchestrator \
    --ranks 76 \
    --grid-seconds 60 \
    --overwrite


# ==========================================
# 2. Strategy B: Unified Streaming Pipeline
# ==========================================

# Run unified fetch-compute-write pipeline for specific ranks
python -m src.corridor_modeling.streaming_pipeline \
    --ranks 1,5,12 \
    --fetch-threads 4 \
    --compute-workers 4

# Run pipeline over rank range with stability checks disabled (single-pass fetch)
python -m src.corridor_modeling.streaming_pipeline \
    --lower-rank 1 \
    --upper-rank 20 \
    --no-stability \
    --overwrite
```

### PowerShell

```powershell
# Run Stage 2 Stability Campaign on specific ranks
python -m src.corridor_modeling.stability_orchestrator `
    --ranks 1,76,177 `
    --max-workers 4 `
    --overwrite

# Run Stage 3 Clustering Campaign on specific ranks
python -m src.corridor_modeling.clustering_orchestrator `
    --ranks 1,76,177 `
    --max-workers 4 `
    --overwrite

# Run Unified Streaming Pipeline in dry-run mode (prints route stats without running)
python -m src.corridor_modeling.streaming_pipeline `
    --lower-rank 1 `
    --upper-rank 10 `
    --dry-run
```

---

### 4.1. Parameter References

#### Parameter Reference (`corridor_orchestrator.py`)

| CLI Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--ranks` | `str` | *None* | Comma-separated list of route ranks to process (e.g. `"1,76"`). Mutually exclusive with `--lower-rank`. |
| `--lower-rank` | `int` | *None* | Start of a corridor rank range to process. Requires `--upper-rank`. |
| `--upper-rank` | `int` | *None* | End of a corridor rank range to process. Requires `--lower-rank`. |
| `--out-dir` | `str` | `data/corridor_paths` | Output directory where the synthesized Parquet file is saved. |
| `--grid-seconds` | `int` | `60` | Resampling temporal grid resolution in seconds. |
| `--overwrite` | `flag` | *False* | Forces regeneration and replacement of any existing synthesized Parquet file. |

#### Parameter Reference (`stability_orchestrator.py`)

| CLI Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--ranks` | `str` | *None* | Comma-separated list of route ranks to process. Mutually exclusive with `--lower-rank`. |
| `--lower-rank` | `int` | *None* | Start of a corridor rank range. Requires `--upper-rank`. |
| `--upper-rank` | `int` | *None* | End of a corridor rank range. Requires `--lower-rank`. |
| `--max-workers` | `int` | `4` | Number of parallel worker processes. |
| `--batch-write-size` | `int` | `500` | Number of completed routes between stability registry flushes. |
| `--overwrite` | `flag` | *False* | Re-process routes that already have a stability record. |

#### Parameter Reference (`clustering_orchestrator.py`)

| CLI Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--ranks` | `str` | *None* | Comma-separated list of route ranks to process. Mutually exclusive with `--lower-rank`. |
| `--lower-rank` | `int` | *None* | Start of a corridor rank range. Requires `--upper-rank`. |
| `--upper-rank` | `int` | *None* | End of a corridor rank range. Requires `--lower-rank`. |
| `--max-workers` | `int` | `4` | Number of parallel worker processes. |
| `--batch-write-size` | `int` | `200` | Number of completed routes between model registry flushes. |
| `--overwrite` | `flag` | *False* | Re-cluster routes already in the model registry. |

#### Parameter Reference (`streaming_pipeline.py`)

| CLI Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--ranks` | `str` | *None* | Comma-separated list of route ranks to process. Mutually exclusive with `--lower-rank`. |
| `--lower-rank` | `int` | *None* | Start of a corridor rank range. Requires `--upper-rank`. |
| `--upper-rank` | `int` | *None* | End of a corridor rank range. Requires `--lower-rank`. |
| `--min-distance` | `float` | `0.0` | Minimum route distance in km (routes shorter than this are bypassed). |
| `--start-date` | `str` | *None* | Flight departure window start ISO (e.g. YYYY-MM-DD). |
| `--end-date` | `str` | *None* | Flight departure window end ISO. |
| `--typecode` | `str` | *None* | Aircraft type filter (e.g. A320, B738). |
| `--seed` | `int` | `42` | Base random sampling seed (incremented per requery round). |
| `--format` | `str` | `oneway` | Route directionality (`oneway` or `roundtrip`). |
| `--fetch-threads` | `int` | `4` | Thread count for asynchronous Trino fetching (Pool 1). |
| `--compute-workers` | `int` | `4` | Process count for stability checking and clustering (Pool 2). |
| `--d-pca` | `int` | *None* | PCA target dimensionality (overrides config default `D_PCA`). |
| `--n-standard` | `int` | *None* | Initial fetch flight size (overrides config default `N_STANDARD`). |
| `--delta-cv-threshold` | `float` | *None* | delta-RSD convergence threshold (overrides config default `DELTA_CV_THRESHOLD`). |
| `--no-stability` | `flag` | *False* | Disables delta-RSD stability resampling (forces single-pass fetch). |
| `--max-resample-rounds`| `int` | `3` | Maximum resampling rounds before forcing clustering. |
| `--overwrite` | `flag` | *False* | Re-cluster routes already in the model registry. |
| `--batch-write-size` | `int` | `50` | Routes between model registry flushes (default 50). |
| `--out-dir` | `str` | *None* | Output directory for raw trajectory parquets. |
| `--dry-run` | `flag` | *False* | Prints route details and exits without executing. |

---

## 5. Prerequisites & Dependencies

### Python Libraries
* `pandas` & `pyarrow` (for Parquet table storage)
* `numpy` & `scipy` (for matrices, Z-scores, and coordinate interpolation)
* `pyproj` (for coordinate reference system transformations)
* `scikit-learn` (for K-Means clustering and Silhouette analysis)
* `traffic` (for spatial resampling, airborne subsetting, and DTW track centroids)
* `openap` (for fuzzy logic flight phase modeling and kinematic limits)
* `pycontrails` (for Flight structures and temporal interpolation engines)

### Input Files
* `data/flight_registry/master_flights.parquet` (for scheduling databases)
* `data/flight_registry/master_flights_route_summary.pkl` (for route rank translations)
* `data/flight_registry/registries/global_trajectory_registry.parquet` (stores raw coordinates mappings)
* `data/flight_registry/registries/global_stability_registry.parquet` (stores computed stability stats)
* `data/flight_registry/registries/global_model_registry.parquet` (stores final medoids registry)

For naming standards, unit conversions (aviation vs. SI), and coordinates, refer to the centralized **[conventions.md](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/conventions.md)** standards.
