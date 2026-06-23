# API Trajectory Fetching Module

This module queries raw flight trajectory coordinates (state vectors) from the OpenSky Trino database or loads them from a local cache, consolidating them into dynamically generated dataset directories.

It operates as **Loop 1** of the Flight Physics Pipeline.

---

## 1. Module Structure

```text
src/fetching/
├── README.md                  # This documentation file
├── opensky_fetcher.py         # Downloader logic with local cache pre-checks
└── fetcher_orchestrator.py     # Coordinates batch corridor fetches
```

---

## 2. Function Analysis Solution Tree (FAST)

```text
Module Objectives
 └── Query flight coordinates from OpenSky and save to isolated run directories
      │
      ├── Sub-objective 1: Query coordinates for a single flight list with cache checking and filtering
      │    └── Solution: fetch_trajectories() in opensky_fetcher.py
      │         ├── Inputs:
      │         │    ├── input_list_path (str): Path to sliced route Parquet
      │         │    ├── out_dir (str): Directory to save trajectories and manifest
      │         │    ├── sample_size (int): Number of flights to randomly sample
      │         │    ├── seed (int): Random seed for sampling reproducibility
      │         │    ├── start_date (str): Optional ISO start date window
      │         │    ├── end_date (str): Optional ISO end date window
      │         │    └── typecode (str): Optional aircraft typecode (case-insensitive)
      │         └── Outputs: Consolidated raw Parquet, Manifest JSON, and updated global index
      │
      ├── Sub-objective 2: Apply modular column-matching and time bounds to flight lists in-memory
      │    └── Solution: filter_flight_list() in opensky_fetcher.py
      │         ├── Inputs: df (pd.DataFrame), start_date, end_date, **kwargs
      │         └── Outputs: Filtered pd.DataFrame
      │
      ├── Sub-objective 3: Prevent Trino server overloads and retry query failures
      │    └── Solution: fetch_with_backoff() in opensky_fetcher.py
      │         ├── Inputs: trino_client, query, max_retries
      │         └── Outputs: DataFrame of waypoints or None on permanent failure
      │
      └── Sub-objective 4: Batch coordinate acquisition across multiple route corridors
           ├── Solution: extract_target_routes() in fetcher_orchestrator.py
           │    ├── Inputs: summary_path, lower, upper, specific_ranks, fetch_format, min_distance
           │    ├── Outputs: DataFrame with columns '[rank, dep, arr, no_of_flights]'
           │    └── Role: Resolves ranked corridors (supporting oneway/roundtrip routes) and filters by distance
           │
           ├── Solution: compute_fetch_targets() in fetcher_orchestrator.py
           │    ├── Inputs: routes_df, input_dir, strategy, value, start_date, end_date, typecode
           │    ├── Outputs: execution_plan (list of dicts containing sample sizes)
           │    └── Role: Maps routes, applies filters in-memory, and computes quotas on matching subset size
           │
           └── Solution: execute_batch_fetch() in fetcher_orchestrator.py
                ├── Inputs: execution_plan, out_dir, seed, start_date, end_date, typecode
                └── Role: Sequentially triggers opensky_fetcher for each corridor in the plan
```

---

## 3. Data Workflow

> [!NOTE]
> **Mermaid Render Support**: The workflow diagram below uses Mermaid syntax. If you are viewing this markdown file in VS Code and it does not render visually, you will need to install a Mermaid preview extension, such as **Markdown Preview Mermaid Support** (by Matt Bierner) or view it in an environment that supports it natively (like GitHub or Obsidian).

```mermaid
graph TD
    A[data/flight_lists/DEP-ARR.parquet] -->|Target Flight Corridor| B[fetcher_orchestrator.py]
    B -->|Calls| C[opensky_fetcher.py]
    D[data/flight_registry/registries/global_trajectory_registry.parquet] -->|Pre-fetch Cache check| C
    C -->|Cache Hit: Load Waypoints| E[Load from existing raw Parquet in raw/]
    C -->|Cache Miss: Query Trino| F[(Trino Database)]
    F -->|Raw ADSB Data| C
    C -->|Update Cache Registry| D
    C -->|Save Manifest & Log| G[data/trajectories/ranks_..._param_hash/DEP-ARR_cohort_hash_manifest.json & extraction.log]
    C -->|Save Raw Trajectories| H[data/trajectories/ranks_..._param_hash/raw/DEP-ARR_cohort_hash_raw.parquet]
```

1. **Local Trajectory Cache Check**: For each flight schedule, the fetcher checks `data/flight_registry/registries/global_trajectory_registry.parquet` for an existing `flight_id`.
   - **Cache Hit**: Waypoints are read locally from the existing raw file path, avoiding database queries and API costs.
   - **Cache Miss**: A Trino query is executed with exponential backoff to retrieve coordinates from the remote OpenSky database.
2. **In-Memory Filtering**: Flights are filtered in-memory using the provided start/end dates and aircraft typecodes.
3. **Dynamic Cohort Isolation**: Saves data into uniquely named folders like `data/trajectories/<corridors>_strat_..._seed_..._[hash]/` containing an `extraction.log`, a run `[base_name]_[cohort_hash]_manifest.json`, and raw parquet files (`[base_name]_[cohort_hash]_raw.parquet`) written to a `raw/` sub-folder.
4. **Registry Updates**: Freshly fetched trajectory records are registered in `data/flight_registry/registries/global_trajectory_registry.parquet` for future cache hits.

---

## 4. CLI Usage Guide

### Bash
```bash
# 1. Fetch trajectories for a single corridor directly
python -m src.fetching.opensky_fetcher \
    --input-list data/flight_lists/LEPA-LEBL.parquet \
    --out-dir data/trajectories/manual_test \
    --start-date "2025-01-01T11:00:00" \
    --end-date "2025-01-01T13:00:00" \
    --typecode "A320" \
    --sample-size 5

# 2. Orchestrate batch downloading for specific ranked routes
python -m src.fetching.fetcher_orchestrator \
    --ranks "1,76" \
    --strategy fixed \
    --value 5 \
    --seed 42 \
    --start-date "2025-01-02" \
    --end-date "2025-01-05" \
    --typecode "A320"

# 3. Batch fetch with rank range and percentage quota
python -m src.fetching.fetcher_orchestrator \
    --lower-rank 1 \
    --upper-rank 50 \
    --format oneway \
    --strategy percent \
    --value 10.0 \
    --seed 99 \
    --start-date "2025-01-01" \
    --end-date "2025-01-10" \
    --min-distance 500.0
```

### PowerShell
```powershell
# 1. Fetch trajectories for a single corridor directly
python -m src.fetching.opensky_fetcher `
    --input-list data/flight_lists/LEPA-LEBL.parquet `
    --out-dir data/trajectories/manual_test `
    --start-date "2025-01-01T11:00:00" `
    --end-date "2025-01-01T13:00:00" `
    --typecode "A320" `
    --sample-size 5

# 2. Orchestrate batch downloading for specific ranked routes
python -m src.fetching.fetcher_orchestrator `
    --ranks "1,76" `
    --strategy fixed `
    --value 5 `
    --seed 42 `
    --start-date "2025-01-02" `
    --end-date "2025-01-05" `
    --typecode "A320"

# 3. Batch fetch with rank range and percentage quota
python -m src.fetching.fetcher_orchestrator `
    --lower-rank 1 `
    --upper-rank 50 `
    --format oneway `
    --strategy percent `
    --value 10.0 `
    --seed 99 `
    --start-date "2025-01-01" `
    --end-date "2025-01-10" `
    --min-distance 500.0
```

**Parameters (`opensky_fetcher.py`)**:
- `--input-list`: Path to the sliced corridor list Parquet file.
- `--out-dir`: Sliced list directory for output.
- `--sample-size`: Number of flights to randomly sample.
- `--seed`: Seed value for random state reproducibility (default: `42`).
- `--start-date` / `--end-date`: Temporal departure windows (ISO format).
- `--typecode`: Aircraft model designator (e.g. `A320`).

**Parameters (`fetcher_orchestrator.py`)**:
- `--route-summary`: Custom path to RouteSummary pickle file (default: `data/flight_registry/master_flights_RouteSummary.pkl`).
- `--input-dir`: Sliced list input folder (default: `data/flight_lists/`).
- `--format`: Directionality (`oneway` / `roundtrip`).
- `--ranks`: Comma-separated ranks to extract.
- `--lower-rank` & `--upper-rank`: Corridor bounds of ranks to extract.
- `--strategy`: Quota strategy (`fixed` / `percent` / `all`).
- `--value`: Numeric size value mapping to the chosen strategy, accepting float values (e.g. `50.0`).
- `--seed`: Seed value for random state reproducibility (default: `42`, allowed values: `0` to `4294967295`).
- `--start-date` / `--end-date`: Temporal departure windows (ISO format).
- `--typecode`: Aircraft model designator (e.g. `A320`).
- `--min-distance`: Minimum route distance in kilometers (default: `800.0` km).

### Maximal CLI Examples

Here are comprehensive examples showing all available parameters in action:

#### Example 1: Direct Single Corridor Fetch with Full Options (Bash)
```bash
python -m src.fetching.opensky_fetcher \
    --input-list "data/flight_lists/LEPA-LEBL.parquet" \
    --out-dir "data/trajectories/lepa_blb_detailed_analysis" \
    --sample-size 50 \
    --seed 123 \
    --start-date "2025-01-01T06:00:00" \
    --end-date "2025-01-31T22:00:00" \
    --typecode "A320"
```

#### Example 2: Batch Fetch Specific High-Value Routes (PowerShell)
```powershell
python -m src.fetching.fetcher_orchestrator `
    --ranks "1,3,5,10" `
    --format oneway `
    --strategy fixed `
    --value 100 `
    --seed 42 `
    --start-date "2025-01-01" `
    --end-date "2025-01-15" `
    --typecode "A380" `
    --min-distance 800.0
```

#### Example 3: Large-Scale Batch with Percentage-Based Quota (Bash)
```bash
python -m src.fetching.fetcher_orchestrator \
    --lower-rank 1 \
    --upper-rank 100 \
    --format oneway \
    --strategy percent \
    --value 5.0 \
    --seed 99 \
    --start-date "2024-12-01" \
    --end-date "2025-01-31" \
    --min-distance 600.0
```

#### Example 4: Complete Dataset with All Available Flights (Production)
```bash
python -m src.fetching.fetcher_orchestrator \
    --lower-rank 1 \
    --upper-rank 926 \
    --format roundtrip \
    --strategy all \
    --seed 42 \
    --start-date "2025-01-01" \
    --end-date "2025-12-31" \
    --min-distance 400.0
```

#### Example 5: Limited Test Fetch (Quick Validation)
```bash
python -m src.fetching.opensky_fetcher \
    --input-list "data/flight_lists/KJFK-EGLL.parquet" \
    --out-dir "data/trajectories/test_kjfk_egll" \
    --sample-size 10 \
    --seed 42 \
    --start-date "2025-01-02" \
    --end-date "2025-01-02"
```

### Key Configuration Notes

**Strategy Modes** (`fetcher_orchestrator.py`):
- `--strategy fixed`: Fetches exactly `N` flights per route (e.g., `--value 50` fetches 50 flights per corridor). Best for controlled sampling.
- `--strategy percent`: Fetches a percentage of available flights per route (e.g., `--value 10.0` fetches 10% of each route). Best for proportional representation.
- `--strategy all`: Fetches **all** available flights for all routes. Highest data volume, longest runtime, highest API cost.

**Route Format**:
- `--format oneway`: One-directional routes (A→B only). Lower data volume.
- `--format roundtrip`: Bidirectional routes (A→B and B→A). Doubles coverage per corridor pair.

**Sampling & Reproducibility**:
- `--seed`: Controls random sampling order. Use same seed for reproducible runs (default: `42`).
- `--sample-size` (opensky_fetcher): Direct flight count override for single-corridor fetches.

**Cost & Performance**:
- `--min-distance`: Skip short routes to focus on long-haul (e.g., `800.0` km). Reduces API queries and data volume.
- `--typecode`: Filter to specific aircraft (e.g., `A320`, `B787`). Narrows scope and speeds up queries.
- Larger date ranges (`--start-date` to `--end-date`) increase data volume and API cost linearly.

**Caching Behavior**:
- All fetches are automatically cached in `registries/global_trajectory_registry.parquet`.
- Re-running the same fetch parameters will use cached data for previously fetched flights, avoiding redundant API queries.
- Cache is persistent across runs and modules.


---

## 5. Prerequisites & Dependencies

### Python Libraries
* `pandas` & `pyarrow` (for data manipulation and Parquet parsing)
* `pyopensky` (OpenSky Network Trino query client API)

### Credentials
* Active Trino connection credentials for OpenSky Network.

For naming standards and coordinate reference systems, refer to the centralized **[conventions.md](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/conventions.md)** standards.
