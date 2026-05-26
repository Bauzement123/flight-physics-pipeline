# API Trajectory Fetching Module

This module queries raw flight trajectory coordinates (state vectors) from the OpenSky Trino database or loads them from a local cache, consolidating them into dynamically generated dataset directories.

## Module Structure

```
src/fetching/
├── README.md                  # This documentation file
├── opensky_fetcher.py         # Downloader logic with local cache pre-checks
└── fetcher_orchestrator.py     # Coordinates batch corridor fetches
```

---

## Function Analysis Solution Tree (FAST)

```
Module Objectives
 └── Query flight coordinates from OpenSky and save to isolated run directories
      ├── Sub-objective: Query coordinates for a single flight list with cache checking and filtering
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
      ├── Sub-objective: Apply modular column-matching and time bounds to flight lists in-memory
      │    └── Solution: filter_flight_list() in opensky_fetcher.py
      │         ├── Inputs: df (pd.DataFrame), start_date, end_date, **kwargs
      │         └── Outputs: Filtered pd.DataFrame
      │
      ├── Sub-objective: Prevent Trino server overloads and retry query failures
      │    └── Solution: fetch_with_backoff() in opensky_fetcher.py
      │         ├── Inputs: trino_client, query, max_retries
      │         └── Outputs: DataFrame of waypoints or None on permanent failure
      │
      └── Sub-objective: Batch coordinate acquisition across multiple route corridors
           ├── Solution: extract_target_routes() in fetcher_orchestrator.py
           │    ├── Inputs: summary_path, lower, upper, specific_ranks, fetch_format
           │    ├── Outputs: DataFrame with columns '[rank, dep, arr, no_of_flights]'
           │    └── Role: Resolves ranked corridors (supporting oneway/roundtrip routes)
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

## Data Workflow

> [!NOTE]
> **Mermaid Render Support**: The workflow diagram below uses Mermaid syntax. If you are viewing this markdown file in VS Code and it does not render visually, you will need to install a Mermaid preview extension, such as **Markdown Preview Mermaid Support** (by Matt Bierner) or view it in an environment that supports it natively (like GitHub or Obsidian).

```mermaid
graph TD
    A[data/flight_lists/DEP-ARR.parquet] -->|Target Flight Corridor| B[fetcher_orchestrator.py]
    B -->|Calls| C[opensky_fetcher.py]
    D[data/flight_registry/global_trajectory_registry.parquet] -->|Pre-fetch Cache check| C
    C -->|Cache Hit: Load Waypoints| E[Load from existing raw Parquet in raw/]
    C -->|Cache Miss: Query Trino| F[(Trino Database)]
    F -->|Raw ADSB Data| C
    C -->|Update Cache Registry| D
    C -->|Save Manifest & Log| G[data/trajectories/ranks_..._01_hash/manifest.json & extraction.log]
    C -->|Save Raw Trajectories| H[data/trajectories/ranks_..._01_hash/raw/DEP-ARR_raw.parquet]
```

1. **Local Trajectory Cache Check**: For each target flight, the fetcher checks `global_trajectory_registry.parquet` for a matching `flight_id`. 
   - **Cache Hit**: Waypoints are read locally from the existing raw file path (inside `raw/`), avoiding API calls.
   - **Cache Miss**: A Trino query is executed with exponential backoff.
2. **In-Memory Slicing & Filtering**: Target flights are loaded from the base sliced flight lists and filtered in-memory using the provided start/end dates and typecodes. Sliced flight lists on disk remain unmodified. Quotas are calculated dynamically on the count of matching flights.
3. **Dynamic Dataset Namespaces**: Folder directories are dynamically named based on prompt inputs (and include formatted start date, end date, and typecode filters if active), ensuring that runs are isolated and cross-validation cohorts do not stomp on each other:
   `data/trajectories/<corridors>_strat_..._start_<start>_end_<end>_type_<typecode>_<hash>/`
   - Manifest files and execution logs are saved at the root of this folder.
   - Raw trajectories are written to the `raw/` subdirectory.
4. **Registry Updates**: Freshly fetched flights are appended to the global index for future cache hits.

---

## CLI Guide

### 1. `opensky_fetcher.py` (Single Corridor Fetcher)
Fetches trajectories for a single corridor list directly.

```bash
# Fetches waypoints for A320 flights between 11:00 and 13:00 on Jan 1st, 2025
python -m src.fetching.opensky_fetcher --input-list data/flight_lists/LEPA-LEBL.parquet --out-dir data/trajectories/manual_test --start-date "2025-01-01T11:00:00" --end-date "2025-01-01T13:00:00" --typecode "A320" --sample-size 5
```

**Parameters**:
- `--input-list`: Sliced route list file.
- `--out-dir`: Directory where raw Parquet files and manifest JSON are saved.
- `--sample-size`: Number of flights to randomly sample.
- `--seed`: Seed value for random state reproducibility (default: `42`).
- `--start-date`: Start boundary of flight departure window (ISO format, e.g. `2025-01-01` or `2025-01-01T11:00:00`).
- `--end-date`: End boundary of flight departure window (ISO format).
- `--typecode`: Aircraft model code (case-insensitive, e.g. `A320`, `B738`, `A20N`).

---

### 2. `fetcher_orchestrator.py` (Batch Corridor Orchestrator)
Orchestrates downloading trajectories for ranks corridors, automatically resolving dynamic dataset folder names.

```bash
# Fetch 5 random A320 flights per corridor for rank 1 within a date range
python -m src.fetching.fetcher_orchestrator --ranks "1" --strategy fixed --value 5 --seed 42 --start-date "2025-01-01T11:00:00" --end-date "2025-01-01T13:00:00" --typecode "A320"
```

**Parameters**:
- `--route-summary`: Custom path to RouteSummary pickle file (default: `data/flight_registry/master_flights_RouteSummary.pkl`).
- `--input-dir`: Folder containing flight lists (default: `data/flight_lists/`).
- `--format`: Directionality (`oneway` / `roundtrip`). If `roundtrip`, resolves and includes inverse return routes automatically.
- `--ranks`: Comma-separated ranks to extract.
- `--lower-rank` & `--upper-rank`: Corridor bounds of ranks to extract.
- `--strategy`: Sampling quota strategy (`fixed` / `percent` / `all`).
- `--value`: Integer size value mapping to the chosen strategy (e.g. 50 flights for `fixed`).
- `--seed`: Seed value for random state reproducibility (default: `42`).
- `--start-date`: Start boundary of flight departure window (ISO format).
- `--end-date`: End boundary of flight departure window (ISO format).
- `--typecode`: Aircraft model code (case-insensitive, e.g. `A320`, `B738`, `A20N`).
  - Using a different seed (e.g., `101` instead of `42`) changes the specific random flights selected in the sample.
  - Using the same seed guarantees that the exact same flight sample is selected on repeated runs.
