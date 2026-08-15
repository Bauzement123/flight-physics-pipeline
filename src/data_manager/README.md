# Data Manager Module (`src/data_manager`)

## 1. Title & Introduction

The `src/data_manager` module serves as the centralized data interface, schema contract authority, and storage engine for the Flight Physics Pipeline. It provides type-safe schema contracts, high-performance PyArrow dataset readers with predicate pushdown, ACID-compliant Delta Lake I/O operations, and distributed node synchronization across network file systems (Windows SMB / NFS).

Key capabilities include:
- **Formal Schema Contracts & Runtime Validation**: Centralized PyArrow metadata schemas (`SIM_LAKE_METADATA_SCHEMA`, `_POSTFILTER_SCHEMA`) with pre-write validation gates enforcing 100% contract compliance on incoming telemetry.
- **Predicate Pushdown Query Engine**: High-speed filtering of multi-gigabyte Parquet datasets (`master_flights.parquet`, `route_summary.parquet`, and corridor model registries) directly at the PyArrow C++ level before loading into Python memory.
- **Universal Pipeline Dataclasses**: Strongly typed contracts (`FlightCandidate`, `SimTask`, `WorkerResult`, `EvalResult`, `SimResultQuery`) passed across pipeline slot boundaries without raw string serializations.
- **ACID Trajectory Delta Lake Storage**: Transactional persistence of full per-waypoint flight trajectories with dynamic physics columns, composite merging on `(SIM_FID, model_config_id, time)`, and automated post-day vacuuming and multi-dimensional Z-ordering.
- **Distributed Network Synchronization (`sync_delta_lake.py`)**: High-speed directional synchronization (`upsert` / `downsert`) between compute nodes and shared network storage (e.g. `\\PC182...\...`) using PyArrow C++ chunked anti-joins and atomic Delta commits with zero risk of row duplication.

---

## 2. Module Structure

```text
src/data_manager/
├── README.md            ← Module technical specification (this file)
├── __init__.py          ← Package initialization
├── io_utils.py          ← PyArrow & Delta Lake dataset readers, writers, skip-gate, vacuum & optimize utilities
├── schemas.py           ← Dataclass query definitions, task contracts, and central table schema definitions
└── sync_delta_lake.py   ← Distributed Delta Lake synchronization & maintenance utility (SMB / NFS)
```

---

## 3. Function Analysis Solution Tree (FAST)

```text
Module Objective: Type-Safe Data Contracts, High-Performance Readers, Delta Lake Storage & Distributed Node Sync
│
├── 1. Master Flights Cohort Retrieval & Counting
│   ├── io_utils.read_master_flights()
│   │   ├── Input: Optional[MasterFlightQuery], Optional[List[str]] (columns), Optional[ds.Dataset] (dataset).
│   │   ├── Output: pandas.DataFrame
│   │   └── Safety/Fallback: Raises FileNotFoundError if MASTER_FLIGHTS_FILE is missing; applies PyArrow dataset predicate pushdown for dates, typecodes, icao24s, callsigns, and airports.
│   └── io_utils.count_master_flights()
│       ├── Input: Optional[MasterFlightQuery], Optional[ds.Dataset] (dataset).
│       ├── Output: int (row count)
│       └── Safety/Fallback: Uses PyArrow dataset scanner count_rows() pushdown without loading table data into RAM.
│
├── 2. Route Summary & Corridor Registry Retrieval
│   ├── io_utils.read_route_summary()
│   │   ├── Input: Optional[RouteSummaryQuery], Optional[List[str]] (columns).
│   │   ├── Output: pandas.DataFrame
│   │   └── Safety/Fallback: Applies PyArrow pushdown on route keys, popularity ranks, and departure/arrival airports.
│   ├── io_utils.read_corridors_map()
│   │   ├── Input: ranks, registry_path, corridors_dir.
│   │   ├── Output: Dict[Tuple[str, int], CorridorCluster]
│   │   └── Safety/Fallback: Loads GLOBAL_CORRIDOR_MODEL_REGISTRY, delegates rank filtering to read_route_summary with PyArrow dataset pushdown and column projection, returns calibrated flight levels and absolute paths.
│   └── io_utils.read_flight_filepaths()
│       ├── Input: Optional[List[str] | set[str]] (flight_ids), Optional[Path] (registry_path), Optional[List[str]] (columns).
│       ├── Output: pandas.DataFrame
│       └── Safety/Fallback: Scans trajectory or clean registries using PyArrow dataset predicate pushdown (isin filter) for instant O(1) FID-to-filepath resolution.
│
├── 3. Trajectory Delta Lake Query Engine
│   ├── io_utils.read_sim_lake()
│   │   ├── Input: lake_path (str | Path), SimResultQuery.
│   │   ├── Output: pandas.DataFrame
│   │   └── Safety/Fallback: Opens DeltaTable; converts to PyArrow dataset; applies predicate pushdown on sim_fids, routes, EF_total thresholds, FL bounds, model_config_id, and fuel.
│   ├── io_utils.read_existing_sim_fids()
│   │   ├── Input: lake_path, routes, dep_date.
│   │   ├── Output: frozenset[str]
│   │   └── Safety/Fallback: Scans only the SIM_FID column with route and dep_date predicate pushdown for high-speed O(1) skip-gate evaluation.
│   └── io_utils.read_ef_by_base_key()
│       ├── Input: lake_path, base_keys, model_config_id, routes, dep_dates.
│       ├── Output: Dict[str, List[Tuple[float, float]]]
│       └── Safety/Fallback: Retrieves prior (FL, EF_total) pairs scoped by routes and dates for variational step-down planning.
│
├── 4. Runtime Schema Validation & Persistence
│   ├── io_utils.validate_sim_trajectory_df()
│   │   ├── Input: df (pandas.DataFrame).
│   │   ├── Output: None (raises ValueError on contract failure).
│   │   └── Safety/Fallback: Checks for all 14 SIM_LAKE_FIXED_COLUMNS; asserts non-null primary keys (SIM_FID, route, fuel, dep_date, FL).
│   └── io_utils.append_sim_lake()
│       ├── Input: lake_path (str | Path), df (pandas.DataFrame), overwrite (bool).
│       ├── Output: None
│       └── Safety/Fallback: Enforces schema validation; converts duration columns to seconds; casts string metadata; performs atomic MERGE on (SIM_FID, model_config_id, time) or clean DELETE-then-append on overwrite/schema evolution.
│
├── 5. Table Maintenance & Z-Order Optimization
│   ├── io_utils.vacuum_sim_lake()
│   │   ├── Input: lake_path (Path), retention_hours (int, default 168).
│   │   ├── Output: None
│   │   └── Safety/Fallback: Executes dt.vacuum(retention_hours); catches errors with warning log.
│   └── io_utils.optimize_sim_lake()
│       ├── Input: lake_path (Path), z_order_cols (List[str]).
│       ├── Output: None
│       └── Safety/Fallback: Executes dt.optimize.compact() followed by multi-dimensional Z-ordering on ['dep_date', 'route', 'EF_total'].
│
└── 6. Distributed Network Lake Synchronization & Maintenance
    ├── sync_delta_lake.extract_missing_arrow_table()
    │   ├── Input: src_lake_path (Path), dest_lake_path (Path), key_col (str), batch_size (int).
    │   ├── Output: Tuple[Optional[pa.Table], int, bool]
    │   └── Safety/Fallback: Extracts dest SIM_FIDs into contiguous Arrow buffer; streams source batches applying pc.is_in anti-join in C++; returns net-new Table, count, and bootstrap flag.
    ├── sync_delta_lake.run_sync()
    │   ├── Input: local_dir (Path), smb_dir (Path), direction (str), key_col (str), batch_size (int), dry_run (bool).
    │   ├── Output: int (synchronized row count)
    │   └── Safety/Fallback: Directionally maps paths; verifies corruption gates; writes via atomic append with zero existing file modification.
    └── sync_delta_lake.run_maintain()
        ├── Input: lake_dir (Path), target_size_mb (int), z_order_cols (List[str]), vacuum_hours (int), skip_vacuum (bool), force (bool).
        ├── Output: None
        └── Safety/Fallback: Bin-packs append fragments into 512 MiB chunks; applies Z-ordering; requires dry-run preview and confirmation before vacuum purge.
```

---

## 4. Data Workflow

### 4.1 Workflow A — Trajectory Lake Simulation I/O & Pipeline Maintenance (`io_utils.py`)

```mermaid
flowchart TD
    subgraph Inputs ["Input Master Datasets & Registries"]
        A1["master_flights.parquet"]
        A2["route_summary.parquet"]
        A3["global_corridor_model_registry.parquet"]
    end

    subgraph DataManager_Read ["DataManager Readers (io_utils.py)"]
        B1["read_master_flights(MasterFlightQuery)"]
        B2["read_corridors_map(ranks)"]
        B3["read_existing_sim_fids(routes, dep_date)"]
        B4["read_ef_by_base_key(base_keys)"]
    end

    subgraph Orchestrator ["Physics Orchestrator & Slots"]
        C1["Slot 1: generate_flightlist()"]
        C2["Slot 2: filter_and_batch()"]
        C3["Engine & Worker Dispatch (Slots 3 & 4)"]
        C4["Slot 5: evaluate()"]
    end

    subgraph DataManager_Write ["DataManager Writers & Maintenance (io_utils.py)"]
        D1["validate_sim_trajectory_df(df)"]
        D2["append_sim_lake(df, overwrite)"]
        D3["vacuum_sim_lake() & optimize_sim_lake()"]
    end

    subgraph Storage ["Delta Lake Storage"]
        E1[("data/results/corridor_simulations
(Trajectory Delta Lake)")]
    end

    A1 --> B1
    A2 & A3 --> B2
    B1 -->|Cohort DataFrame| C1
    B2 -->|Corridors Map| C1
    C1 -->|List SimTask| C2
    E1 <-->|Pre-Batch Bulk Skip-Gate| B3
    E1 <-->|Variational FL/EF Query| B4
    B3 & B4 --> C2
    C2 -->|Partitioned Batches| C3
    C3 -->|Full Trajectory DataFrame| D1
    D1 -->|Validated DataFrame| D2
    D2 -->|ACID Merge / Overwrite| E1
    C3 -->|Worker Results| C4
    C4 -->|Round-Boundary Re-Batch| C2
    C3 -->|Post-Day Maintenance| D3
    D3 -->|Compact, Vacuum & Z-Order| E1
```

**Step-by-step Description:**
1. **Cohort & Metadata Ingestion**: The orchestrator calls `read_corridors_map()` to load calibrated cluster metadata from `GLOBAL_CORRIDOR_MODEL_REGISTRY`, and initializes a `MasterFlightQuery` for the day's departure date range. `read_master_flights()` applies PyArrow filter pushdown to stream matching rows from `master_flights.parquet` into memory.
2. **Task Generation (Slot 1)**: Cohort rows are converted into `SimTask` dataclass objects. Each task lazily generates its canonical `SIM_FID` identifier via `task.to_sim_fid()`.
3. **Pre-Batch Skip-Gate Verification (Slot 2)**: Before creating worker batches, Slot 2 invokes `read_existing_sim_fids()` to bulk-query all simulated `SIM_FID`s for the day's routes. Unsimulated tasks are chunked into full batches of `max_batch_size` (e.g. 50), maximizing CPU vectorization without empty slots.
4. **Variational Optimization (Slot 2 & Slot 5)**: In variational mode, Slot 2 calls `read_ef_by_base_key()` to retrieve prior simulated FLs and $\text{EF}_{\text{total}}$ values. Succeeded flights with positive warming ($\text{EF}_{\text{total}} > 0$) generate step-down tasks via `compute_stepdown_task()` until contrail suppression or minimum safe altitude is reached.
5. **Runtime Schema Validation & Persistence**: Upon batch simulation, worker threads construct a full per-waypoint DataFrame and call `append_sim_lake()`. `validate_sim_trajectory_df()` enforces that all 14 mandatory metadata columns are present and non-null. Under `_LAKE_WRITE_LOCK`, `append_sim_lake()` merges rows on `(SIM_FID, model_config_id, time)` (or executes targeted delete-then-append if `overwrite=True` or new physics columns evolve).
6. **Post-Day Vacuum & Multi-Dimensional Z-Ordering**: At the end of each daily orchestration loop, `vacuum_sim_lake()` prunes stale parquet files older than 168 hours, and `optimize_sim_lake()` compacts small files and applies multi-dimensional Z-ordering on `['dep_date', 'route', 'EF_total']`, guaranteeing high-speed downstream analytics.

---

### 4.2 Workflow B — Distributed Delta Lake Synchronization (`sync_delta_lake.py sync`)

```mermaid
flowchart TD
    Start(["CLI: sync_delta_lake sync"]) --> Resolve["Resolve Direction:\n• upsert (Local -> SMB)\n• downsert (SMB -> Local)"]
    
    Resolve --> CheckDest{"Destination Lake\n_delta_log exists?"}
    
    CheckDest -- No (Bootstrap) --> LoadSource["Stream Source Table via PyArrow"]
    LoadSource --> WriteBootstrap["write_deltalake(mode='overwrite')"]
    WriteBootstrap --> DoneBootstrap(["Sync Complete: Bootstrapped Lake"])
    
    CheckDest -- Yes --> ExtractKeys["Extract Dest SIM_FID Column\n(combine_chunks -> pc.unique -> contiguous Array)"]
    ExtractKeys --> StreamBatches["Stream Source in Batches\n(chunk_size = 200,000 rows)"]
    StreamBatches --> ComputeMask["pc.invert(pc.is_in(batch['SIM_FID'], dest_keys))"]
    ComputeMask --> FilterRows{"Net-new rows\nin batch?"}
    FilterRows -- Yes --> Accumulate["Accumulate Net-New Batches"]
    FilterRows -- No --> NextBatch{"More Batches?"}
    Accumulate --> NextBatch
    NextBatch -- Yes --> StreamBatches
    
    NextBatch -- No --> CheckNewTotal{"Total net-new\nrows > 0?"}
    CheckNewTotal -- No --> AlreadySynced(["[OK] Lake 100% in sync (0 bytes transferred)"])
    CheckNewTotal -- Yes --> CheckDryRun{"--dry-run\nflag set?"}
    CheckDryRun -- Yes --> LogDryRun(["[DRY-RUN] Report N rows, M MB"])
    CheckDryRun -- No --> CommitAppend["write_deltalake(mode='append', schema_mode='merge')\n• Write new Parquet chunks\n• Single atomic _delta_log JSON commit\n• Existing 512MiB files UNTOUCHED"]
    CommitAppend --> DoneSync(["[OK] Successfully synchronized N rows"])
```

**Step-by-step Description:**
1. **Direction Mapping**: Evaluates `--direction`: in `upsert` mode, `local-dir` is source and `smb-dir` is destination; in `downsert` mode, paths are reversed.
2. **Corruption & Bootstrap Gate**: Checks if destination contains `_delta_log/`. If missing, bootstraps via `mode="overwrite"`. If present but unparseable, raises an explicit `RuntimeError` to prevent silent overwrites.
3. **Contiguous Key Projection**: Reads only the destination's `SIM_FID` column using PyArrow dataset pushdown, combines chunks, and extracts unique keys into a contiguous C++ Arrow array buffer.
4. **Streamed Anti-Join**: Streams source Delta Lake records in constant-memory batches (default 200k rows) and executes `pc.is_in()` to extract strictly net-new records without allocating CPython string objects.
5. **Atomic Append Transaction**: If net-new rows exist and `--dry-run` is false, writes newly identified rows via `write_deltalake(mode="append")`. Destination 512 MiB files remain untouched, and a single JSON commit updates the transaction log.

---

### 4.3 Workflow C — Distributed Delta Lake Maintenance & Z-Ordering (`sync_delta_lake.py maintain`)

```mermaid
flowchart TD
    StartMaint(["CLI: sync_delta_lake maintain"]) --> OpenTable["Open Target DeltaTable"]
    OpenTable --> Compact["Step 1: dt.optimize.compact(target_size=512MB)\nConsolidate small append fragments"]
    Compact --> ZOrder["Step 2: dt.optimize.z_order(['dep_date', 'route'])\nMulti-dimensional spatial-temporal clustering"]
    ZOrder --> CheckSkipVacuum{"--no-vacuum\nflag passed?"}
    CheckSkipVacuum -- Yes --> CompleteMaint(["[OK] Maintenance Complete (Compacted & Z-Ordered)"])
    CheckSkipVacuum -- No --> VacuumDry["Step 3: dt.vacuum(dry_run=True)\nIdentify unreferenced files older than retention"]
    VacuumDry --> ConfirmPrompt{"--force passed OR\ninteractive user confirms?"}
    ConfirmPrompt -- No --> AbortVacuum(["Maintenance Complete (Orphan files preserved)"])
    ConfirmPrompt -- Yes --> VacuumExec["dt.vacuum(dry_run=False)\nPurge stale pre-compaction files"]
    VacuumExec --> DoneFullMaint(["[OK] Maintenance Complete (Compacted, Z-Ordered, Vacuumed)"])
```

**Step-by-step Description:**
1. **Lake Verification**: Opens the target Delta Table on the specified network or local path.
2. **Compaction**: Calls `dt.optimize.compact(target_size=512MB)` to bin-pack small append Parquet files into full 512 MiB chunks (`DELTA_LAKE_TARGET_FILE_SIZE_BYTES`).
3. **Z-Ordering**: Applies multi-dimensional Z-ordering on active schema dimensions (`['dep_date', 'route']`), grouping related corridor simulation records for maximum downstream file-skipping.
4. **Vacuum Guard**: If `--no-vacuum` is omitted, conducts a dry-run preview of unreferenced files older than the retention threshold (default 168 hours). Requires `--force` or interactive `[y/N]` confirmation before executing physical file deletion.

---

## 5. CLI Usage Guide (`sync_delta_lake.py`)

### 5.1 Syntax Blocks

#### Bash
```bash
# Push new local simulations to SMB share (Upsert)
python -m src.data_manager.sync_delta_lake sync \
    --direction upsert \
    --local-dir data/databases/simulation_lake \
    --smb-dir "\\PC182.ilr.rwth-aachen.de\studiert_ilr\Kirste\PA_ZeroCloud\PythonPipeline\data\databases\simulation_lake"

# Pull remote simulations from SMB share to local compute node (Downsert)
python -m src.data_manager.sync_delta_lake sync \
    --direction downsert \
    --local-dir data/databases/simulation_lake \
    --smb-dir "Z:\PythonPipeline\data\databases\simulation_lake"

# Preview sync delta without writing data
python -m src.data_manager.sync_delta_lake sync \
    --direction upsert \
    --local-dir data/databases/simulation_lake \
    --smb-dir "Z:\PythonPipeline\data\databases\simulation_lake" \
    --dry-run

# Run maintenance (compaction and Z-ordering) on shared network lake
python -m src.data_manager.sync_delta_lake maintain \
    --lake-dir "Z:\PythonPipeline\data\databases\simulation_lake" \
    --target-size-mb 512 \
    --z-order-cols "dep_date,route"
```

#### PowerShell
```powershell
# Push new local simulations to SMB share (Upsert)
python -m src.data_manager.sync_delta_lake sync `
    --direction upsert `
    --local-dir data/databases/simulation_lake `
    --smb-dir "\\PC182.ilr.rwth-aachen.de\studiert_ilr\Kirste\PA_ZeroCloud\PythonPipeline\data\databases\simulation_lake"

# Pull remote simulations from SMB share to local compute node (Downsert)
python -m src.data_manager.sync_delta_lake sync `
    --direction downsert `
    --local-dir data/databases/simulation_lake `
    --smb-dir "Z:\PythonPipeline\data\databases\simulation_lake"

# Preview sync delta without writing data
python -m src.data_manager.sync_delta_lake sync `
    --direction upsert `
    --local-dir data/databases/simulation_lake `
    --smb-dir "Z:\PythonPipeline\data\databases\simulation_lake" `
    --dry-run

# Run maintenance (compaction and Z-ordering) on shared network lake
python -m src.data_manager.sync_delta_lake maintain `
    --lake-dir "Z:\PythonPipeline\data\databases\simulation_lake" `
    --target-size-mb 512 `
    --z-order-cols "dep_date,route"
```

### 5.2 Parameter Reference

#### `sync` Subcommand
| Parameter | Type | Default | Description |
|---|---|---|---|
| `--direction` | `str` | *Required* | Sync direction: `upsert` (Local $\to$ SMB) or `downsert` (SMB $\to$ Local). |
| `--local-dir` | `Path` | *Required* | Path to local Delta Lake directory (e.g. `data/databases/simulation_lake`). |
| `--smb-dir` | `Path` | *Required* | Path to network share Delta Lake (UNC path `\\server\...` or drive letter `Z:\...`). |
| `--key-col` | `str` | `SIM_FID` | Primary key column name used for anti-join deduplication. |
| `--batch-size` | `int` | `200000` | Number of rows per streaming Arrow batch during scan. |
| `--dry-run` | `flag` | `False` | Scan and report net-new row counts without modifying destination data. |
| `--log-file` | `str` | `simulation.log` | Name of log file in `data/logs/` to record execution output. |

#### `maintain` Subcommand
| Parameter | Type | Default | Description |
|---|---|---|---|
| `--lake-dir` / `--smb-dir` | `Path` | *Required* | Path to target Delta Lake directory to compact and maintain. |
| `--target-size-mb` | `int` | `512` | Target file size in MiB for compacted Parquet files. |
| `--z-order-cols` | `str` | `dep_date,route` | Comma-separated list of column names for multi-dimensional Z-ordering. |
| `--vacuum-hours` | `int` | `168` | Retention threshold in hours before unreferenced files can be purged. |
| `--no-vacuum` | `flag` | `False` | Skip the vacuum stage entirely (perform compaction & Z-ordering only). |
| `--force` | `flag` | `False` | Bypass interactive confirmation prompt for vacuum purge. |
| `--log-file` | `str` | `simulation.log` | Name of log file in `data/logs/` to record execution output. |

---

## 6. Schema Reference

### 6.1 Pipeline Dataclasses (`schemas.py`)

#### `MasterFlightQuery`
Passed to `read_master_flights()` to construct PyArrow dataset filters.

| Field | Type | Default | Description |
|---|---|---|---|
| `dep_date_start` | `Optional[pd.Timestamp]` | `None` | Filter flights with `firstseen >= dep_date_start`. |
| `dep_date_end` | `Optional[pd.Timestamp]` | `None` | Filter flights with `firstseen <= dep_date_end`. |
| `routes` | `Optional[List[str]]` | `None` | List of route string keys (e.g. `['EGLL-LFPG']`). |
| `typecodes` | `Optional[List[str]]` | `None` | Filter by target ICAO aircraft typecodes (e.g. `['A320', 'B738']`). |
| `icao24s` | `Optional[List[str]]` | `None` | Filter by 24-bit ICAO transponder hex addresses. |
| `callsigns` | `Optional[List[str]]` | `None` | Filter by operational flight callsigns. |
| `dep_airports` | `Optional[List[str]]` | `None` | Filter by departure airport ICAO codes. |
| `arr_airports` | `Optional[List[str]]` | `None` | Filter by arrival airport ICAO codes. |

#### `RouteSummaryQuery`
Passed to `read_route_summary()`.

| Field | Type | Default | Description |
|---|---|---|---|
| `routes` | `Optional[List[str]]` | `None` | List of target route string keys. |
| `ranks` | `Optional[List[int]]` | `None` | List of route popularity rank integers (1-indexed). |
| `dep_airports` | `Optional[List[str]]` | `None` | List of departure airport codes. |
| `arr_airports` | `Optional[List[str]]` | `None` | List of arrival airport codes. |

#### `SimResultQuery`
Passed to `read_sim_lake()`.

| Field | Type | Default | Description |
|---|---|---|---|
| `sim_fids` | `Optional[List[str]]` | `None` | Filter by explicit list of `SIM_FID` strings. |
| `routes` | `Optional[List[str]]` | `None` | Filter by route key strings. |
| `ef_gt` | `Optional[float]` | `None` | Filter results where Energy Forcing $\text{EF}_{\text{total}} > \text{ef\_gt}$. |
| `fl_lte` | `Optional[float]` | `None` | Filter results where Flight Level $\text{FL} \le \text{fl\_lte}$. |
| `model_config_id` | `Optional[str]` | `None` | Filter by model config (e.g. `'kerosene'`). |
| `fuel` | `Optional[str]` | `None` | Filter by fuel type (e.g. `'kerosene'` or `'hydrogen'`). |

#### `SimTask`
Universal task struct passed across all slot boundaries.

| Field | Type | Description |
|---|---|---|
| `icao24` | `str` | 24-bit ICAO aircraft hex identifier. |
| `callsign` | `str` | Operational flight callsign. |
| `dep` | `str` | Departure airport ICAO code (`estdepartureairport`). |
| `arr` | `str` | Arrival airport ICAO code (`estarrivalairport`). |
| `firstseen` | `int` | Flight departure epoch timestamp (seconds UTC). |
| `lastseen` | `int` | Flight arrival epoch timestamp (seconds UTC). |
| `typecode` | `str` | ICAO aircraft type code designator. |
| `cluster_id` | `int` | Assigned trajectory cluster ID. |
| `fl` | `float` | Assigned flight level in feet. |

> **Canonical SIM_FID Method**: `task.to_sim_fid()` returns:  
> `{icao24}_{clean_cs}_{dep}-{arr}_{YYYYMMDD_HHMM}_{cluster_id}_{int(fl)}`

---

### 6.2 Trajectory Delta Lake Schema (`SIM_LAKE_METADATA_SCHEMA`)

Simulation results written to `data/results/corridor_simulations` persist **full per-waypoint trajectory records** containing all 68+ dynamic physics columns from PyContrails/CoCiP alongside **14 fixed metadata columns**:

| Column Name | PyArrow Type | Description | Key / Index Role |
|---|---|---|---|
| `SIM_FID` | `pa.string()` | Unique simulation identifier. | Composite Merge Key / Anti-Join Key |
| `model_config_id` | `pa.string()` | Model/physics configuration identifier (e.g. `'kerosene'`). | Composite Merge Key |
| `fuel` | `pa.string()` | Fuel type: `'kerosene'` or `'hydrogen'`. | Query Dimension |
| `route` | `pa.string()` | Route corridor key (e.g. `'LIRF-EGKK'`). | Z-Order Dimension |
| `icao24` | `pa.string()` | 24-bit ICAO aircraft transponder hex address. | Metadata |
| `callsign` | `pa.string()` | Sanitized operational flight callsign. | Metadata |
| `typecode` | `pa.string()` | ICAO aircraft type designator. | Metadata |
| `cluster_id` | `pa.int32()` | Trajectory cluster medoid index. | Metadata |
| `FL` | `pa.float64()` | Calibrated flight level in feet. | Parameter Column |
| `dep_date` | `pa.int32()` | Integer departure date (`YYYYMMDD`). | Z-Order Dimension |
| `firstseen` | `pa.timestamp("ns")` | First waypoint timestamp (tz-naive UTC). | Time Anchor |
| `lastseen` | `pa.timestamp("ns")` | Last waypoint timestamp (tz-naive UTC). | Time Anchor |
| `EF_total` | `pa.float64()` | Mission-integrated Energy Forcing in Joules ($J$). | Z-Order Dimension |
| `total_fuel_burn` | `pa.float64()` | Mission total fuel burn from PSFlight in kilograms ($kg$). | Metric Column |

---

## 7. Delta Lake Upsert & Optimization Contract

To maintain absolute data integrity across parallel execution threads and re-run campaigns, `append_sim_lake()` implements a strict Delta Lake transactional contract based on the composite key `(SIM_FID, model_config_id, time)`.

### Normal Mode (`overwrite=False`)
When `overwrite=False`, `append_sim_lake()` performs a Delta Table MERGE operation:

```sql
MERGE INTO target USING source
ON target.SIM_FID = source.SIM_FID 
AND target.model_config_id = source.model_config_id
AND target.time = source.time
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
```

- **Idempotency**: Running the pipeline multiple times over the same date range updates matching waypoints rather than duplicating rows.
- **Thread Safety**: Combined with `_LAKE_WRITE_LOCK` in `worker.py`, Delta Lake's underlying Rust transaction log ensures atomic commits across threads.

### Overwrite Mode (`overwrite=True`)
When `--overwrite` is specified on the CLI, `append_sim_lake()` executes a two-stage atomic transaction:
1. **Targeted Deletion**: Constructs an explicit SQL predicate deleting all rows whose `SIM_FID` is contained in the incoming batch:
   ```python
   dt.delete(f"SIM_FID IN ('{fid_1}', '{fid_2}', ...)")
   ```
2. **Append Write**: Writes the fresh batch rows directly to the table manifest using `mode="append", schema_mode="merge"`.

### End-of-Day Compaction & Multi-Dimensional Z-Ordering
Post-day maintenance calls `optimize_sim_lake()` which executes:
1. `dt.optimize.compact(target_size=DELTA_LAKE_TARGET_FILE_SIZE_BYTES)`: Bin-packs small per-batch parquet fragments into consolidated Parquet files targeting **512 MB** (`DELTA_LAKE_TARGET_FILE_SIZE_BYTES = 536,870,912` bytes from `config.py`).
2. `dt.optimize.z_order(["dep_date", "route", "EF_total"], target_size=DELTA_LAKE_TARGET_FILE_SIZE_BYTES)`: Co-locates spatially and temporally correlated trajectory records, accelerating downstream campaign analytics by over 10x.

---

## 8. Prerequisites & Dependencies

### Python Package Dependencies
- **`deltalake`**: Native Rust bindings for Delta Lake transactional reads, writes, merges, deletes, compact, and Z-ordering.
- **`pyarrow`**: In-memory Arrow tables, Parquet dataset reading, and predicate pushdown expressions.
- **`pandas`**: DataFrame structure and column manipulation.
- **`pathlib`**: Cross-platform path handling.

### Centralized Config References (`src.common.config`)
- `BASE_DIR`
- `MASTER_FLIGHTS_FILE` (`data/databases/master_flights/master_flights.parquet`)
- `ROUTE_SUMMARY_PARQUET` (`data/databases/master_flights/master_flights_route_summary.parquet`)
- `GLOBAL_CORRIDOR_MODEL_REGISTRY` (`data/registries/global_model_registry.parquet`)
- `DELTA_LAKE_TARGET_FILE_SIZE_BYTES` (`536,870,912` = 512 MB)

### Centralized Log Files (`data/logs/`)
- `simulation.log`: Main log stream for Delta Lake operations, orchestrator execution, and `sync_delta_lake.py` runs.
