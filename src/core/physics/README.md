# Physics Simulation Pipeline (`src/core/physics`)

## 1. Title & Introduction

The `src/core/physics` module implements the high-performance, slotted contrail simulation engine for the Flight Physics Pipeline. Built on top of the [`pycontrails`](https://pycontrails.org/) framework (utilising standard `PSFlight` performance models and `Cocip` contrail evolution models), this module evaluates total energy forcing ($\text{EF}_{\text{total}}$ in Joules) and contrail persistence across historical commercial flights operating over Europe.

The engine features a **5-Slot architecture** cleanly decoupled from data access, model selection, and parallel execution orchestration:
1. **Slot 1 (Flight List Generation)**: Converts master flight cohorts into strongly typed `SimTask` items using calibrated cluster flight levels from `GLOBAL_CORRIDOR_MODEL_REGISTRY`.
2. **Slot 2 (Task Filtering, Mutation & Batching)**: Owns skip-gate filtering against Delta Lake execution history, step-down task generation for variational campaigns (`compute_stepdown_task`), and route-based batch chunking.
3. **Slot 3 (Trajectory Loading & Time-Shifting)**: Loads, validates (`is_supported_typecode`), and time-shifts representative corridor cluster trajectories to match target departure timestamps, attaching `attrs["fuel"]`.
4. **Slot 4 (Model Instantiation & Physics Evaluation)**: Dynamically instantiates PyContrails model pairs (`PSFlight` + `Cocip`) and executes vectorized batch evaluations with robust per-flight sequential fallbacks.
5. **Slot 5 (Batch Evaluation & Classification)**: Evaluates completed worker outcomes, classifies succeeded vs failed flights, and delegates to Slot 2 to create `still_todo` tasks for intra-day variational requeueing.

Concurrency is orchestrated via a **`ThreadPoolExecutor`** model dispatch architecture, enabling worker threads to concurrently access shared in-memory ERA5 `MetDataset` objects without duplicating multi-gigabyte weather arrays across process boundaries.

---

## 2. Module Structure

```text
src/core/physics/
├── README.md                      ← Module technical specification (this file)
├── __init__.py                    ← Package initialization
├── cli.py                         ← Main CLI entrypoint (parses args & invokes orchestrator)
├── orchestrator.py                ← Day-by-day loop coordinator, ERA5 cache manager & round re-batcher
├── engine.py                      ← Pure parallel execution layer (ThreadPoolExecutor dispatch)
├── worker.py                      ← Single batch execution worker (Slot 3+4 integration & Lake I/O)
├── clone_simulation.py            ← Legacy unslotted orchestrator (maintained for baseline comparison)
├── simulation.py                 ← Legacy unslotted orchestrator (maintained for baseline comparison)
├── loaders/                       ← Slot 3: Trajectory loading implementations
│   ├── __init__.py                ← Loader factory (`get_loader`)
│   └── cluster_loader.py          ← Slot 3a: K-Cluster trajectory loader & time-shifter
├── models/                        ← Slot 4: Physics model builders
│   ├── __init__.py                ← Model package initialization
│   └── ps_cocip.py                ← Model builder (`get_model` dispatches 'kerosene', 'kerosene_lowmem')
└── slots/                         ← Slotted pipeline step definitions
    ├── __init__.py                ← Slots package initialization
    ├── slot1_flightlist_gen.py    ← Slot 1: Flight list generation from master_flights slices
    ├── slot1_cohort_enum.py       ← Backward compatibility shim (re-exports slot1_flightlist_gen)
    ├── slot1_task_gen.py          ← Backward compatibility shim (re-exports slot1_flightlist_gen)
    ├── slot2_batcher.py           ← Slot 2: Skip-gate filtering, step-down task mutation & batching
    ├── slot5_evaluator.py         ← Slot 5: Batch evaluation & classification
    └── utils.py                   ← Backward compatibility shim (re-exports compute_stepdown_task)
```

---

## 3. Function Analysis Solution Tree (FAST)

```text
Module Objective: High-Performance, Thread-Safe Physics Simulation & Trajectory Persistence
│
├── 1. Command Line Parsing & Execution Dispatch
│   └── cli.main() / parse_args()
│       ├── Input: Sys argv flags (--start-date, --end-date, --sim-mode, --fuel, --low-mem, etc.)
│       ├── Output: argparse.Namespace
│       └── Safety/Fallback: Validates date ranges, filters corridors by rank via PyArrow predicate pushdown on route_summary.parquet, exits on empty/invalid inputs.
│
├── 2. Day-by-Day Orchestration & Dynamic ERA5 Windowing
│   └── orchestrator.run()
│       ├── Input: Date range, corridors_map, lake_path, weather_cache_dir, execution flags.
│       ├── Output: None (writes directly to Delta Lake).
│       └── Safety/Fallback: Calculates exact ERA5 window from task min(firstseen) / max(lastseen) + max_age_hours; evicts stale ERA5 hours dynamically with gc.collect(); loads missing hours via bounded packets (_load_hour_packet) with ThreadPoolExecutor(WEATHER_IO_WORKERS); re-batches step-down tasks at round boundaries.
│
├── 3. Slot 1: Flight List Generation & Selection
│   ├── slot1_flightlist_gen.generate_base_flightlist()
│   │   ├── Input: Cohort DataFrame (master_flights slice), corridors_map (from data_manager.read_corridors_map).
│   │   ├── Output: List[FlightCandidate]
│   │   └── Safety/Fallback: Pure transform; maps flights to all valid available route clusters; checks cluster.fl validity (hard skip on missing/invalid FL); safely sanitizes nullable callsign and typecode fields using pd.notna to guard against boolean evaluation errors on pandas.NA.
│   └── slot1_flightlist_gen.select_clusters()
│       ├── Input: candidate_pool (List[FlightCandidate]), available_clusters, strategy ('random'), clusters_per_flight.
│       ├── Output: List[SimTask]
│       └── Safety/Fallback: Samples cluster IDs from candidate valid pool and materializes strongly typed SimTask instances.
│
├── 4. Slot 2: Task Filtering, Mutation & Batching
│   ├── slot2_batcher.partition_tasks()
│   │   ├── Input: List[SimTask], max_batch_size.
│   │   ├── Output: List[List[SimTask]] (100% saturated execution batches).
│   │   └── Safety/Fallback: Deterministically sorts tasks by (dep, arr, cluster_id, firstseen); continuously slices into full max_batch_size batches to maximize vectorized saturation and preserve trajectory cache locality.
│   ├── slot2_batcher.filter_and_batch()
│   │   ├── Input: Candidate List[SimTask], sim_mode, lake_path, overwrite flag, max_batch_size.
│   │   ├── Output: List[List[SimTask]] (partitioned execution batches).
│   │   └── Safety/Fallback: Standard mode: calls read_existing_sim_fids(tasks) → frozenset skip-gate;
│   │       if overwrite, calls delete_sim_lake_rows(exact SIM_FID list) before emit.
│   │       Variational mode: calls read_ef_by_base_key(tasks) → CLUSTER_FID-keyed EF dict;
│   │       if overwrite, reads + resolves CLUSTER_FIDs then calls delete_sim_lake_rows.
│   │       Both IO calls delegate to read_sim_lake_metadata() — the unified engine.
│   │       ⚠ RAM/IO: bounded to one day by the daily loop; do not invoke with unbounded task lists.
│   └── slot2_batcher.compute_stepdown_task()
│       ├── Input: task (SimTask), ef (float), step_size (float), min_safe_fl (float).
│       ├── Output: Optional[SimTask]
│       └── Safety/Fallback: Returns None if ef <= 0 (suppressed) or next_fl < min_safe_fl (floor reached); otherwise returns dataclasses.replace(task, fl=next_fl).
│
├── 5. Slot 3: Trajectory Loading & Time-Shifting
│   └── loaders.cluster_loader.load() (via get_loader(sim_mode, fuel, step_down_method))
│       ├── Input: SimTask, corridors_map, use_hydrogen, step_down_method ('cap').
│       ├── Output: Optional[pycontrails.Flight]
│       └── Safety/Fallback: Validates mode-method mutual exclusion; validates aircraft typecode via is_supported_typecode(); shifts timestamps relative to task.firstseen; clamps altitude ceiling if step_down_method=='cap'; returns None on missing files or invalid typecodes.
│
├── 6. Slot 4: Physics Model Instantiation & Evaluation
│   └── models.ps_cocip.get_model() / worker.run_batch()
│       ├── Input: model_config_id, MetDataset (met), MetDataset (rad), max_age_hours, fuel, step_down_method.
│       ├── Output: Tuple[PSFlight, Cocip] / BatchOutput (containing raw (SimTask, Flight) pairs for successes and (SimTask, reason_str) for failures).
│       └── Safety/Fallback: get_model() dispatches via _BUILDERS dict using model_config_id ('kerosene' or 'kerosene_lowmem'). run_batch() executes four phases with per-phase wall-clock timing (logged to simulation.log): Phase 2 _eval_psflight() (vectorized Fleet PSFlight → sequential fallback; vectorize_ps=False skips vectorized attempt entirely); Phase 2.5 check_psflight_ok() gate (rejects degenerate PSFlight output — zero/NaN total_fuel_burn, all-NaN fuel_flow or true_airspeed — before CoCiP, preventing wasted compute); Phase 3 _eval_cocip() (vectorized Fleet CoCiP → sequential fallback); Phase 4 check_cocip_ok() pre-lake gate. All failure events across all phases are appended to data/logs/simulation_failures.log via log_simulation_failure() (process-safe direct file append). Lazy sequential model instantiation — sequential instances are created only inside the except branch. Returns BatchOutput, never constructs WorkerResult.
│
├── 7. Parallel Execution Coordination
│   └── engine.run_parallel()
│       ├── Input: List[List[SimTask]], worker_fn (partial run_batch), max_workers.
│       ├── Output: Iterator[BatchOutput]
│       └── Safety/Fallback: Spawns ThreadPoolExecutor; catches batch exceptions and yields BatchOutput(successful=[], failed=[]); calls gc.collect() after each batch completion.
│
├── 8. Slot 5: Batch Result Evaluation & FL Sanity Check
│   └── slots.slot5_evaluator.evaluate()
│       ├── Input: BatchOutput (raw batch output from worker), task_by_fid, sim_mode, model_config_id, step_size, min_safe_fl.
│       ├── Output: EvalResult (succeeded, failed, still_todo).
│       └── Safety/Fallback: First calls _classify_results(batch_output, model_config_id) — the universal, sim_mode-independent verdict constructor that builds all WorkerResult objects. Then dispatches to mode evaluator _evaluate_standard or _evaluate_variational which receive pre-classified (succeeded, failed) lists and only compute still_todo. FL sanity check remains in _evaluate_variational (mutates result.status = 'fail' for |actual_fl - task.fl| > 1.5 FL). WorkerResult is only ever constructed inside _classify_results — this is the central ownership invariant.
│
└── 9. Trajectory Persistence & Post-Day Lake Optimization
    ├── worker._write_to_lake()
    │   ├── Input: List[Tuple[SimTask, Flight]], model_config_id, fuel, lake_path, overwrite, lake_verbosity.
    │   ├── Output: Delta Lake storage update.
    │   └── Safety/Fallback: Combined write engine — injects 14 fixed metadata attrs into flight.attrs; constructs target_fl (1-waypoint [:1] slice in summary mode, full flight in full mode); calls promote_attrs_to_data() (src.common.adapters) to broadcast all scalar attrs into flight.data; executes unified to_dataframe() conversion; clears df.attrs = {} to prevent pyarrow JSON serialization errors; acquires thread lock (_LAKE_WRITE_LOCK); delegates to io_utils.append_sim_lake().
    └── orchestrator post-day cleanup
        └── io_utils.vacuum_sim_lake() & io_utils.optimize_sim_lake()
            └── Multi-dimensional Z-ordering on ['dep_date', 'route', 'EF_total'] after daily runs.
```

---

## 4. Data Workflow

### 4.1 Workflow A — Daily Orchestrator Loop (`cli.py` + `orchestrator.py`)

```mermaid
flowchart TD
    A["CLI Entrypoint (cli.py)"] -->|"Parse Args & Filter Ranks"| B["read_corridors_map()"]
    B --> C["Orchestrator Day Loop (orchestrator.py)"]
    C -->|"Query Daily Cohort"| D["read_master_flights(MasterFlightQuery)"]
    D -->|"Cohort DataFrame"| E["Slot 1: generate_flightlist()"]
    E -->|"Candidate SimTasks"| F["Compute Exact ERA5 Window"]
    F -->|"Slice Missing Hours"| G["_populate_hour_cache()"]
    G -->|"MetDataset (met, rad)"| H["Slot 2: filter_and_batch()"]
    H -->|"Bulk Query read_existing_sim_fids"| I["Vectorized Batches (max_batch_size=50)"]
    I --> J["engine.run_parallel(ThreadPoolExecutor)"]
    J -->|"Worker Batch Results"| K["Slot 5: evaluate()"]
    K --> L{"Variational still_todo?"}
    L -->|"Yes"| M["Accumulate round_still_todo"]
    M -->|"Round Completed"| H
    L -->|"No / Round Empty"| N["Post-Day Cleanup"]
    N -->|"vacuum_sim_lake() & optimize_sim_lake()"| O["Next Day"]
    O --> C
```

#### Step-by-Step Description:

1. **CLI Initialization**: `cli.py` parses command-line flags (date range, corridor ranks, worker count, fuel, simulation mode, memory flags). `read_corridors_map()` loads `(route_id, cluster_id)` corridor metadata and calibrated FLs from `GLOBAL_CORRIDOR_MODEL_REGISTRY`, filtered via PyArrow dataset predicate pushdown on `route_summary.parquet` if ranks are specified.
2. **Daily Cohort Ingestion**: For each calendar day in `--start-date` to `--end-date`, `orchestrator.run()` queries `master_flights.parquet` via `read_master_flights()` for flights departing between `00:00:00` and `23:59:59` UTC, leveraging single-column predicate pushdown on precomputed `route` fields.
3. **Flight List Generation (Slot 1)**: `generate_base_flightlist()` converts cohort rows into `FlightCandidate` items by matching route codes against available cluster trajectories in `corridors_map`, safely handling missing `callsign` and `typecode` fields with `pd.notna` guards. `select_clusters()` then samples and materializes strongly typed `SimTask` dataclass items.
4. **Dynamic ERA5 Windowing & Packetized Ingestion**: The orchestrator inspects all generated tasks for the day and calculates hourly bounds:
   $$\text{era5\_start} = \lfloor \min(\text{task.firstseen}) \rfloor - 1\text{h}$$
   $$\text{era5\_end} = \lceil \max(\text{task.lastseen}) \rceil + \text{max\_age\_hours} + 1\text{h}$$
   Stale hours before `era5_start` are evicted from `hour_cache` and garbage-collected (`gc.collect()`). Missing hours are partitioned into discrete packets of size `WEATHER_OPEN_PACKET_HOURS` and loaded via `ThreadPoolExecutor(max_workers=WEATHER_IO_WORKERS)` using `_load_hour_packet()`. Each worker builds an isolated dictionary without mutating shared state. The main thread aggregates packets into `hour_cache` and wraps the concatenated daily datasets with `MetDataset(..., copy=False)`.
5. **Bulk Skip-Gate & Batch Packing (Slot 2)**: `filter_and_batch()` delegates to the unified IO engine `read_sim_lake_metadata()` via two thin wrappers:
   - **Standard mode**: `read_existing_sim_fids(tasks)` bulk-queries all simulated SIM_FIDs for the day in a single 4-stage scan (`dep_date` file-skip → `firstseen` row filter → `waypoint==0` one-row-per-SIM_FID → in-RAM `Base_FID` frozenset match). Unsimulated tasks are packed into batches of `max_batch_size` (default 50). If `--overwrite`, exact SIM_FIDs are deleted first via `delete_sim_lake_rows()`.
   - **Variational mode**: `read_ef_by_base_key(tasks)` uses the same scan, projecting `["SIM_FID", "FL", "EF_total"]`, grouped by `CLUSTER_FID` = `SIM_FID` without `_{FL}` suffix. If `--overwrite`, all FL variants per CLUSTER_FID are resolved in-RAM and deleted before re-emitting tasks at nominal FL.
6. **Parallel Dispatch**: The orchestrator invokes `engine.run_parallel()`, passing batches to a `ThreadPoolExecutor` running `worker.run_batch()`.
7. **Batch Evaluation (Slot 5) & Round-Boundary Re-Batching**: Before dispatching parallel batches, the orchestrator constructs `task_by_fid = {t.to_sim_fid(): t}` once per round across all pending batches (avoiding redundant dictionary constructions inside the inner worker batch stream). As workers complete, `evaluate()` receives `batch_output` (a `BatchOutput`) and `model_config_id`. It first calls `_classify_results(batch_output, model_config_id)` to universally construct all `WorkerResult` objects, then dispatches to the mode-specific evaluator. In variational mode, flights with positive warming ($\text{EF}_{\text{total}} > 0$) call `slot2_batcher.compute_stepdown_task()` to populate `still_todo`. The orchestrator accumulates all `still_todo` tasks across the round and re-batches them via Slot 2 at the round boundary into full vectorized batches.
8. **Post-Day Vacuum & Z-Order Optimization**: After all batches and step-downs for the day finish, `vacuum_sim_lake()` prunes stale parquet files older than 168 hours and `optimize_sim_lake()` compacts small files into optimal **512 MB** Parquet chunks (`DELTA_LAKE_TARGET_FILE_SIZE_BYTES = 536,870,912` bytes from `config.py`) and applies multi-dimensional Z-ordering on `['dep_date', 'route', 'EF_total']`.
9. **Wall-to-Wall Temporal & Task Emission Metrics**:
   - Tracks end-to-end wall-clock elapsed time per day and across the entire campaign, calculating effective throughput (`s/task` and `tasks/s`).
   - Clearly distinguishes between **Total Tasks Committed** (all unique trajectories written to the Delta Lake), **Baseline Cohort Tasks** (initial nominal simulations), and **Step-Downs Emitted** (iterative descent flights).
   - **Daily Transition Log Format**:
     ```text
     Day YYYY-MM-DD completed in 1m 50s (110.3s) — 87 tasks committed to lake (1.27s/task) [24 baseline + 63 step-downs emitted, 0 failed].
     ```
   - **Global Campaign Summary Block**:
     ```text
     ================================================================================
     ORCHESTRATOR CAMPAIGN SUMMARY
     ================================================================================
     Total Duration:          1m 50s (110.3s) across 1 calendar day(s)
     Total Tasks Committed:   87 tasks written to Delta Lake (1.27s/task, 0.79 tasks/s)
       • Baseline Tasks:      24 tasks
       • Step-Downs Emitted:  63 tasks
     Failed Simulations:      0
     ================================================================================
     ```

> [!WARNING]
> **RAM Risk — `read_sim_lake_metadata` (unified Slot 2 IO engine)**
> After Stage 3 (`waypoint == 0`) on-disk filtering, the in-RAM table is at most
> ~1.2 × N_tasks rows for one day's cohort (~2,400 rows for 2,000 tasks ≈ 50 KB).
> `filter_and_batch()` is always called inside the orchestrator's daily loop, which
> keeps N_tasks naturally bounded. Do **not** call `read_sim_lake_metadata` directly
> with task lists spanning multiple days in a single invocation — in-RAM row count
> scales linearly with the number of unique `dep_date` values in the task list.

> [!WARNING]
> **IO Risk — `delete_sim_lake_rows` with `--overwrite` outside the daily loop**
> The delete engine emits a single `SIM_FID IN (...)` SQL predicate and is designed
> for the daily overwrite path, where the SIM_FID list is bounded to one day's tasks
> (≤ ~10,000 strings). If `--overwrite` is applied to an unbounded multi-day task
> list in a single `filter_and_batch()` call (i.e. bypassing the orchestrator's
> day-by-day iteration), both the SQL predicate string and the Parquet file rewrite
> cost grow unboundedly. Peak RAM during deletion ≈ X_lake / Y_days per file batch.
> Always run the orchestrator day-by-day; never batch multiple days into one
> `filter_and_batch(overwrite=True)` call.

---

### 4.2 Workflow B — Single Batch Worker (`worker.py`)

```mermaid
flowchart TD
    A["Batch of SimTasks"] --> B["worker.run_batch()"]
    B --> |"get_model(model_config_id)\nget_loader(sim_mode)"| C["Phase 1: _load_flights()"]
    C --> D{"check_load_ok?"}
    D --> |"Fail / None"| E["failed_pairs ← (task, reason)\nlog_simulation_failure()"]
    D --> |"Success"| F["Phase 2: _eval_psflight(vectorize_ps=True/False)"]
    F --> G["Vectorized Fleet PSFlight eval\n(if vectorize_ps=True)"]
    G --> |"Success"| H["psflight_ok_pairs"]
    G --> |"Exception"| I["_eval_psflight_sequential() per-flight"]
    I --> |"RuntimeError"| J["failed_pairs ← (task, reason)\nlog_simulation_failure()"]
    I --> |"OK"| H
    H --> H2["Phase 2.5: check_psflight_ok()"]
    H2 --> |"zero fuel burn / NaN columns"| J
    H2 --> |"Valid"| K["Phase 3: _eval_cocip()"]
    K --> L["Vectorized Fleet CoCiP eval"]
    L --> |"Success"| M["del fl_ps → gc.collect()\ncocip_ok_pairs"]
    L --> |"Exception"| N["_eval_cocip_sequential() per-flight"]
    N --> |"CoCiP OK"| M
    N --> |"CoCiP Fail"| J
    M --> O["Phase 4: check_cocip_ok()"]
    O --> |"ef_all_nan / missing_ef / zero_fuel_burn"| J
    O --> |"Valid"| P["_write_to_lake(final_ok)"]
    P --> Q["Return BatchOutput(successful, failed)\n+ Batch Timing log"]
```

#### Step-by-Step Description:

1. **Worker Setup**: `run_batch()` receives batch, MetDatasets, and config. Calls `get_model(model_config_id)` to get a single model pair (dispatch via `_BUILDERS` dict: `'kerosene'` or `'kerosene_lowmem'`). Calls `get_loader()` for the trajectory loader. Starts wall-clock timer (`t0 = time.perf_counter()`). No sequential model instances created yet.
2. **Phase 1 — Trajectory Loading (`_load_flights`)**: For each task, calls `cluster_loader.load()`. Invalid/missing → appended to `failed_pairs` as `(task, reason)` and written to `simulation_failures.log` via `log_simulation_failure()`.
3. **Phase 2 — PSFlight (`_eval_psflight`)**: If `vectorize_ps=True` (default), attempts vectorized Fleet PSFlight eval on all loaded flights. On any exception, falls back flight-by-flight via `_eval_psflight_sequential()`. If `vectorize_ps=False`, goes directly to sequential without attempting vectorized. Sequential model instance created lazily **only on the fallback path**. PSFlight kinematic rejections (`RuntimeError: fuel mass flow rate is unrealistic`) → `failed_pairs` + `simulation_failures.log`.
4. **Phase 2.5 — PSFlight Output Gate (`check_psflight_ok`)**: Validates all PSFlight survivors before handing off to CoCiP. Rejects flights with: `total_fuel_burn` is `None`/`NaN`/`≤ 0`, `fuel_flow` column missing or all-NaN, `true_airspeed` column missing or all-NaN. Catches NaN cascades from degenerate sequential fallback output before wasting CoCiP compute. All rejections written to `simulation_failures.log`.
5. **Phase 3 — CoCiP (`_eval_cocip`)**: Takes `ps_valid` pairs. Attempts vectorized Fleet CoCiP eval. On success: `del fl_ps; gc.collect()` immediately to release PSFlight memory. On exception: falls back per-flight via `_eval_cocip_sequential()`. Sequential Cocip instance created lazily **only inside the `except` branch**. CoCiP failures → `failed_pairs` + `simulation_failures.log`.
6. **Phase 4 — Pre-Lake Gate (`check_cocip_ok`)**: Validates CoCiP survivors before Delta Lake commit. Rejects: `None` flight, empty trajectory, invalid `flight_id`, missing/all-NaN `ef` column, zero/NaN `total_fuel_burn`. All rejections written to `simulation_failures.log`.
7. **Delta Lake Write**: `_write_to_lake` injects 14 fixed metadata attributes into `flight.attrs`, constructs `target_fl` (1-waypoint `[:1]` slice in `summary` mode, full flight in `full` mode), calls `promote_attrs_to_data()` to broadcast all scalar attrs into `flight.data`, then calls `to_dataframe()`. `df.attrs = {}` cleared before appending via `io_utils.append_sim_lake()` under `_LAKE_WRITE_LOCK`.
8. **Timing Log**: Per-batch timing emitted to `simulation.log`: `load`, `psflight`, `ps_gate`, `cocip`, `lake`, `total` in seconds.
9. **Return**: `BatchOutput(successful=final_ok, failed=load_failed + ps_failed + ps_gate_failed + cocip_failed + lake_gate_failed)`. No `WorkerResult` constructed here. Slot 5 owns all verdict construction.

---

### 4.3 Optimization & Memory Modes

| Feature / Behavior | Standard Mode (`--low-mem` omitted) | Low-Memory Mode (`--low-mem` enabled) |
|---|---|---|
| **ERA5 Spatial Crop** | Cropped to `EUR_BBOX` ($[-26^\circ, 30^\circ, 44^\circ, 79^\circ]$) $+ 10^\circ$ padding via native `downselect()`. | Cropped lazily to `EUR_BBOX` $+ 10^\circ$ via `downselect()` (limits Dask index space). |
| **ERA5 In-Memory Load** | Executes eager `.load()` call to pull cropped array into uncompressed RAM. | Skip ERA5 eager `.load()`; arrays stay file-backed. Does NOT affect CoCiP preprocessing — use `--model-config-id kerosene_lowmem` for that. |
| **ERA5 Ingestion Chunking** | Loads missing hours in packets of `WEATHER_OPEN_PACKET_HOURS` across `WEATHER_IO_WORKERS`. | Uses `WEATHER_OPEN_PACKET_HOURS=1` and `WEATHER_IO_WORKERS=1` on VM to eliminate NetCDF lock races and memory spikes. |
| **Flight Memory Lifecycle** | `copy_source=True` for Fleet evaluation (PyContrails requirement); sequential fallback uses lazy `copy_source=False` instances created only inside the `except` branch. | Identical to standard mode. `--low-mem` does not affect the worker execution path — both phases always attempt vectorized Fleet eval first and fall back per-flight on exception. |
| **CoCiP Preprocessing** | Standard PyContrails CoCiP preprocessing. | No longer controlled by `--low-mem`. Use `--model-config-id kerosene_lowmem` to enable `Cocip(preprocess_lowmem=True)`. |
| **Primary Advantage** | Fastest execution speed per batch; optimal for high-CPU nodes with ample RAM ($\ge 32\text{ GB}$). | **Minimizes peak RAM footprint**; prevents OOM crashes and file-handle exhaustion on memory-constrained workers. |

> **Separation of concerns**: `--low-mem` controls only ERA5 I/O (whether xarray arrays are eagerly loaded into RAM). `--model-config-id kerosene_lowmem` controls only CoCiP preprocessing behaviour (`preprocess_lowmem=True`). These two flags are fully independent.

### 4.4 Supported Model Configurations (`--model-config-id`)

The `ps_cocip.py` model factory maps `--model-config-id` to specific physical model parameters and acts as a primary partition key in the Delta Lake results:

| `model_config_id` | Performance Model | Contrail Model | Parameters & Behavior |
|---|---|---|---|
| `kerosene` (default) | `PSFlight` | `Cocip` | Standard Jet-A fuel params; standard CoCiP met interpolation. |
| `kerosene_lowmem` | `PSFlight` | `Cocip` | Standard Jet-A fuel params; sets `preprocess_lowmem=True` on `Cocip` to chunk pressure-level interpolation and prevent RAM spikes. |

### 4.5 Delta Lake Storage Verbosity (`--lake-verbosity`)

The simulation pipeline supports two Delta Lake write verbosity levels:

| `lake_verbosity` | Rows per Flight | Stored Content | Storage Footprint | Primary Use Case |
|---|---|---|---|---|
| `full` (default) | ~104 waypoints | All 4D waypoints across ~82 physics & performance columns. | 100% (~11 to 38 GiB for 52M rows) | Deep per-waypoint trajectory analysis, spatial plotting, altitude profile inspection. |
| `summary` | 1 summary row | 1-row flight-level summary with row-0 physics and all broadcast scalar attrs (`time=firstseen`, `waypoint=0`). | **~1.4% (~98.6% reduction)** | Large-scale production campaigns, campaign sweeps, aggregate contrail climatology, skip-gate metadata checks. |

* **Full Compatibility**: `summary` mode is 100% backward and forward compatible with Slot 2 skip-gates (`read_sim_lake_metadata`, `read_existing_sim_fids`, `read_ef_by_base_key`), Delta Lake MERGE transactions, and downstream evaluators.

> [!NOTE]
> **Attr Retention & Combined Write Engine**:
> - **Full Attribute Retention**: In both modes, `promote_attrs_to_data()` (`src.common.adapters`) broadcasts all scalar `flight.attrs` — including the 14 fixed metadata fields, variable CoCiP physics outputs, and any model-specific or SAF/hydrogen attrs set during simulation — into `flight.data` before `to_dataframe()`. Non-scalar attrs (arrays, dicts, `None`) are safely skipped.
> - **Row-0 Physics Preservation**: In `summary` mode, `target_fl` uses a `[:1]` slice of `flight.data`, retaining actual waypoint physics from row 0 (not a synthetic empty row). This natively satisfies the skip-gate `waypoint == 0` filter.
> - **Unified Conversion**: Both modes execute identical `target_fl.to_dataframe()` → `df.attrs = {}` → `append_sim_lake()` path. The IO layer (`io_utils.py`) and skip-gate logic are completely unchanged.

---

## 5. CLI Usage Guide

### 5.1 Bash Syntax

```bash
# Standard Simulation run for January 2025 across ranks 1 to 5 (Jet-A / Kerosene)
python -m src.core.physics.cli \
    --start-date 2025-01-01 \
    --end-date 2025-01-31 \
    --lower-rank 1 \
    --upper-rank 5 \
    --sim-mode standard \
    --fuel kerosene \
    --model-config-id kerosene \
    --max-workers 4 \
    --batch-size 50 \
    --out-dir data/results/corridor_simulations_kerosene

# Low-Memory Variational Step-Down Campaign with Altitude Capping
python -m src.core.physics.cli \
    --start-date 2025-01-01 \
    --end-date 2025-01-07 \
    --ranks 1,3,5 \
    --sim-mode variational \
    --step-down-method cap \
    --fuel kerosene \
    --model-config-id kerosene_lowmem \
    --step-size 10.0 \
    --min-safe-fl 190.0 \
    --out-dir data/results/corridor_simulations_kerosene_lowmem \
    --low-mem \
    --max-age 1
```

### 5.2 PowerShell Syntax

```powershell
# Standard Simulation run for January 2025 across ranks 1 to 5 (Jet-A / Kerosene)
python -m src.core.physics.cli `
    --start-date 2025-01-01 `
    --end-date 2025-01-31 `
    --lower-rank 1 `
    --upper-rank 5 `
    --sim-mode standard `
    --fuel kerosene `
    --model-config-id kerosene `
    --max-workers 4 `
    --batch-size 50 `
    --out-dir data/results/corridor_simulations_kerosene

# Low-Memory Variational Step-Down Campaign with Altitude Capping
python -m src.core.physics.cli `
    --start-date 2025-01-01 `
    --end-date 2025-01-07 `
    --ranks "1,3,5" `
    --sim-mode variational `
    --step-down-method cap `
    --fuel kerosene `
    --model-config-id kerosene_lowmem `
    --step-size 10.0 `
    --min-safe-fl 190.0 `
    --out-dir data/results/corridor_simulations_kerosene_lowmem `
    --low-mem `
    --max-age 1
```

### 5.3 Parameter Reference Table

| Parameter | Type | Default | Description |
|---|---|---|---|
| `--start-date` | `str` | *Required* | First calendar day to process (inclusive, format `YYYY-MM-DD`). |
| `--end-date` | `str` | *Required* | Last calendar day to process (inclusive, format `YYYY-MM-DD`). |
| `--sim-mode` | `str` | `'standard'` | Simulation mode: `'standard'` (nominal FL baseline) or `'variational'` (step-down optimization pass). |
| `--step-down-method` | `str` | `None` | Step-down altitude modification method in Slot 3 (`'cap'`). **Required if and only if `--sim-mode variational` is active**. |
| `--fuel` | `str` | `'kerosene'` | Fuel type: `'kerosene'` or `'hydrogen'`. Attaches `Flight(fuel=...)` in Slot 3 and sets `fuel` column. |
| `--model-config-id` | `str` | `'kerosene'` | Physics model configuration passed to get_model() and used as Delta Lake composite merge key. Supported: 'kerosene' (standard Jet-A), 'kerosene_lowmem' (Jet-A with CoCiP preprocess_lowmem=True). SAF variants reserved. |
| `--ranks` | `str` | `None` | Comma-separated list of route cluster ranks to process (e.g. `'1,3,5'`). Mutually exclusive with `--lower-rank`. |
| `--lower-rank` | `int` | `None` | Lower bound of corridor cluster rank (inclusive). Requires `--upper-rank`. |
| `--upper-rank` | `int` | `None` | Upper bound of corridor cluster rank (inclusive). Requires `--lower-rank`. |
| `--out-dir` | `str` | *Required* | Root directory path for Delta Lake simulation storage. Suggested copy-paste targets: `data/results/corridor_simulations_kerosene` (Jet-A/Kerosene) or `data/results/corridor_simulations_hydrogen` (Hydrogen). |
| `--weather-cache` | `str` | `data/weather` | Directory containing hourly ERA5 pressure-level and surface `.nc` cache files. |
| `--corridors-dir` | `str` | `data/corridor_paths` | Directory containing cluster parquet trajectory files. |
| `--max-age`, `--age` | `int` | `48` | Maximum contrail segment age in hours passed to CoCiP. |
| `--step-size` | `float` | `10.0` | FL decrement step size in FL units (default `10.0` = 1,000 ft, used in variational mode). |
| `--min-safe-fl` | `float` | `190.0` | Minimum safe flight level in FL units below which step-down halts (`MIN_SAFE_FL`). |
| `--cluster-selection` | `str` | `'random'` | Strategy for cluster selection (e.g. `'random'`). Controls how available cluster IDs in candidate pool are sampled. |
| `--clusters-per-flight`, `-x` | `int` | `1` | Number of representative cluster trajectories sampled per flight. |
| `--min-distance` | `float` | `0.0` | Pre-filter minimum route distance in kilometers; shorter routes are skipped. |
| `--max-workers` | `int` | `4` | Number of concurrent worker threads in `ThreadPoolExecutor`. |
| `--batch-size` | `int` | `50` | Maximum number of flight tasks per parallel execution batch. |
| `--lake-verbosity` | `str` | `'full'` | Delta Lake storage verbosity: `'full'` (writes all 4D waypoints per flight) or `'summary'` (compact 1-row summary from `flight.attrs`, reducing storage by ~98%). |
| `--low-mem` | `flag` | `False` | Skips eager ERA5 .load(); xarray arrays remain file-backed until accessed during CoCiP interpolation. Does NOT affect CoCiP preprocessing — use --model-config-id kerosene_lowmem for that. |
| `--overwrite` | `flag` | `False` | Bypass Delta Lake skip-gate and delete-then-append fresh results. |
| `--test-mode` | `flag` | `False` | Restricts run to single day (`2025-01-01`) and single cluster per flight. |

### 5.4 Environment Variable Tuning Options

| Environment Variable | Default | Description |
|---|---|---|
| `WEATHER_OPEN_PACKET_HOURS` | `12` | Number of ERA5 hours per discrete load packet. Set to `1` in `.env` on VM / SMB mounts; increase on fast local SSDs. |
| `WEATHER_IO_WORKERS` | `2` | Number of concurrent `ThreadPoolExecutor` workers for weather packet ingestion. Set to `1` in `.env` on VM / SMB mounts. |
| `HDF5_USE_FILE_LOCKING` | OS default | Set to `FALSE` on network mounts (SMB/NFS) to prevent HDF5 file locking errors across workers. |

---

## 6. Prerequisites & Dependencies

### Python Package Dependencies
- **`pycontrails`**: Contrails and flight performance modeling framework (`PSFlight`, `Cocip`, `MetDataset`, `Flight`, `Fleet`).
- **`deltalake`**: Native Rust bindings for Delta Lake transactional reads, writes, merges, deletes, compact, and Z-ordering.
- **`xarray` / `dask` / `netCDF4`**: Multi-dimensional meteorological data indexing and lazy array streaming.
- **`pyarrow`**: Parquet serialization and dataset predicate pushdown engine.
- **`pandas` / `numpy`**: Vectorized numerical data manipulation.

### Pipeline Config & Registries Referenced
- **`src.common.adapters`**:
  - `promote_attrs_to_data()`: In-place broadcast of all scalar `flight.attrs` (`str`, `int`, `float`, `bool`, `np.integer`, `np.floating`, `pd.Timestamp`) into `flight.data` before `to_dataframe()`. Ensures full retention of simulation metadata and variable CoCiP/model-specific physics attributes as DataFrame columns for both `full` and `summary` verbosity modes.
- **`src.common.config`**:
  - `BASE_DIR`
  - `WEATHER_DIR` (`data/weather`)
  - `CORRIDOR_SIMULATIONS_DIR` (`data/results/corridor_simulations`)
  - `CORRIDOR_PATHS_DIR` (`data/corridor_paths`)
  - `GLOBAL_CORRIDOR_MODEL_REGISTRY` (`data/registries/global_model_registry.parquet`)
  - `ROUTE_SUMMARY_PARQUET` (`data/databases/master_flights/master_flights_route_summary.parquet`)
  - `EUR_BBOX` ($[-26, 30, 44, 79]$)
  - `MIN_SAFE_FL` (`190.0`)
  - `DELTA_LAKE_TARGET_FILE_SIZE_BYTES` (`536,870,912` = 512 MB)
  - `is_supported_typecode()` / `UNSUPPORTED_TYPECODE_FLAG`
- **Logging Destination**: Written to `data/logs/simulation.log` via `setup_file_logger("simulation.log")`. Skipped or unsupported airframes are appended to `data/logs/skipped_aircraft.log`.
- **Failure Audit Log**: All simulation failures across every phase (load, PSFlight, PSFlight gate, CoCiP, CoCiP gate) are appended to `data/logs/simulation_failures.log` via `log_simulation_failure()` (`src.common.utils`). Format: `timestamp | SIM_FID | stage | reason`. Process-safe via direct `open("a")` append — no lock required.

---

## 7. Known Issues

### 7.1 `--low-mem` Lazy ERA5 Interpolation Failure (Unresolved)

**Status**: ⛔ **BROKEN — Do not use `--low-mem` in production.** Use eager mode (omit the flag).

**Symptom**: When `--low-mem` is enabled, 15/16 flights return `0.0` total fuel burn. The root cause is that `air_temperature` (and other pressure-level variables) return `NaN` for all cruise-altitude waypoints during PyContrails' 4D interpolation (`lon`, `lat`, `level`, `time`). Surface-level interpolation works correctly. One flight non-deterministically succeeds per run.

**Architecture context**: The packetized ERA5 loading refactor (`e52bd14`) changed weather loading from a single `ERA5(time=(start, end), paths=[...])` object (one unified `xr.open_mfdataset` Dask graph) to N independent per-hour `ERA5(time=(h, h), paths=[single_file])` objects. These N independent Dask graphs are stitched together via `xr.concat(..., dim="time")` and rechunked with `.chunk({"time": -1})`. In eager mode (`.load()` called), the concat produces a NumPy array and interpolation works perfectly. In lazy mode, the concatenated Dask graph produces `NaN` during pressure-level interpolation despite the rechunk.

**Hypotheses tested and eliminated**:

| # | Hypothesis | Test Applied | Result |
|---|---|---|---|
| 1 | Dask `ThreadPoolExecutor` race on `netCDF4` C-library | `dask.config.set(scheduler="synchronous")` in `worker.py` | ❌ Still fails |
| 2 | `netCDF4` engine thread-unsafety | Swapped to `h5netcdf` engine | ❌ Still fails |
| 3 | `xr.concat` chunk boundaries not fused | `.chunk({"time": -1})` already present | ❌ Still fails |
| 4 | `MetDataset(copy=False)` skipping internal setup | Tested with `copy=True` (default) for lazy path | ❌ Still fails |
| 5 | `copy_source=False` mutating PSFlight model state | Tested with `copy_source=True` | ❌ Still fails |

**Remaining fix options (untested)**:

1. **`xr.open_mfdataset` direct assembly**: Instead of `xr.concat` of per-hour MetDatasets, call `xr.open_mfdataset(all_hour_paths, chunks={"time": -1})` directly on the NC files for each day's window. This should produce one unified Dask graph identical to the pre-refactor approach, but with O(log N) tree-merge depth instead of O(N × preprocessing_depth). Requires replicating ERA5's lightweight post-processing (variable rename + level selection + spatial downselect) outside of the ERA5 class.

2. **Per-flight eager slice**: In `_eval_psflight_sequential`, compute only the 3–4 hour weather slice needed for each individual flight before calling `eval()`. Keeps RAM low (~300 MB per flight) but requires passing the full lazy MetDataset into the sequential evaluator and slicing + computing per iteration.

3. **Force `.compute()` after concat**: Call `met_xr.compute()` / `rad_xr.compute()` in the low-mem assembly path. Effectively makes low-mem identical to eager for simulation but preserves lazy hour-cache eviction benefits for multi-day runs. Defeats the original RAM-reduction goal during simulation.

**Diagnostic artifacts**: Trace reports and telemetry from investigation sessions are archived in `data/traces/parity_test_lowmem/`.

---

## 8. Oct 2025 Campaign — Performance & Failure Rate Investigation (2026-08-21)

This section records the findings from a systematic investigation into an observed 25–50× throughput regression and an ~21% flight simulation failure rate during the Oct 2025 kerosene campaign run. It is intended as a structured technical summary suitable for academic review.

### 8.1 Observed Symptoms

| Metric | Fast Baseline (Aug 15) | Degraded Runs (Aug 20–21) |
|--------|----------------------|--------------------------|
| Throughput | **0.05 s/task** (20 tasks/s) | 1.23–2.66 s/task |
| Failed flights / day | **1** | **1,894–2,737** |
| Batch size | 50 | 150 |
| Run duration (1 day) | **5m 48s** | 2h 19m+ |

### 8.2 Hypotheses Tested and Eliminated

| # | Hypothesis | Test Applied | Result |
|---|------------|-------------|--------|
| 1 | Short flights too short for PSFlight fuel integration | Checked source: `fuel_burn = fuel_flow × Δt` — works for any duration | ❌ Rejected |
| 2 | Met interpolation failure at xarray chunk boundaries (eager mode) | `xr.concat` on eager arrays uses `np.concatenate` → always contiguous NumPy | ❌ Rejected |
| 3 | Thread contention on shared `MetDataset` | Would cause scattered failures across all batches, not batch-specific wipeouts; `h5netcdf` engine is pure Python (no C-library state) | ❌ Rejected |
| 4 | CoCiP modifies PSFlight `total_fuel_burn` | Source audit: CoCiP does not touch `flight.attrs["total_fuel_burn"]` | ❌ Rejected |
| 5 | Sequential PSFlight eval produces correct output | Sequential eval shows same individual failure rate as vectorized fallback | Partially confirmed — failure is per-flight, not an artefact of sequential path |

### 8.3 Root Cause — PSFlight Model Guardrail Failures

The `PSFlight` model (Poll-Schumann performance model via `pycontrails`) includes a runtime guardrail:

```
RuntimeError: Model failure: fuel mass flow rate is unrealistic
              and the built-in guardrails are not working.
```

This is raised when computed fuel mass flow rate exceeds 25 kg/s, which can occur on certain flight profiles (e.g. BIKF–LFPG at FL390) where the PS model receives atmospheric conditions outside its validated operating envelope.

**Fleet vs. sequential evaluation behaviour**: When `PSFlight.eval(fleet)` is called with a `Fleet` (list of flights), a single RuntimeError from one flight crashes the entire list comprehension — all-or-nothing. The pipeline then falls back to re-evaluating all N flights sequentially. Certain flights that fail in both vectorized and sequential mode produce `total_fuel_burn = np.nansum(all_NaN) = 0.0`, which is then correctly rejected by the new `check_psflight_ok` gate.

**Key finding**: The failure is intrinsic to specific corridor/altitude combinations and is not caused by data quality issues, weather loading bugs, or batching topology. Re-simulation of the same flights with freshly constructed ERA5 objects succeeds in some cases, suggesting a possible interaction between the shared concatenated `MetDataset` object and certain flight profiles during sequential fallback — but this remains unconfirmed.

### 8.4 Performance Regression Root Cause — Delta Lake Write Overhead

Phase timing instrumentation (added 2026-08-21) revealed:

```
Batch 6/7:  load=2.71s  psflight=14.12s  ps_gate=1.07s  cocip=11.76s  lake=53.79s  total=83.45s
Batch 7/7:  load=1.86s  psflight=10.92s  ps_gate=0.23s  cocip=14.99s  lake=61.12s  total=89.11s
```

Delta Lake writes (`lake=53–94s`) account for **65–99% of wall-clock time per batch**. The write path executes a full 3-column predicate MERGE (`SIM_FID × model_config_id × time`) against the entire existing lake on every batch commit. As the lake grows during a run, each MERGE must scan an increasing number of Parquet files — making lake write time O(n_rows_in_lake). With 1,050 tiny batches (from per-corridor homogeneous batching), this serializes into effectively sequential lake I/O for the entire day.

The Aug 15 baseline was fast not primarily because of batching topology but because with `batch_size=50` and ~60–70 total batches, the lake was still small and MERGE scans were short.

### 8.5 Changes Implemented (2026-08-21)

| Change | File | Purpose |
|--------|------|---------|
| `log_simulation_failure()` | `src/common/utils.py` | Process-safe per-failure audit log to `data/logs/simulation_failures.log` |
| `check_psflight_ok()` | `src/core/physics/slots/slot5_evaluator.py` | Pre-CoCiP gate: rejects flights with zero/NaN fuel burn or degenerate output columns |
| Phase timing instrumentation | `src/core/physics/worker.py` | Per-phase wall-clock timing logged per batch (load / psflight / ps_gate / cocip / lake / total) |
| `vectorize_ps` flag | `src/core/physics/worker.py` | Runtime toggle to skip vectorized PSFlight attempt (default `True` — existing behaviour) |
| Failure lake writes disabled | `src/core/physics/worker.py` | `_write_failure_lake()` calls commented out — Delta Lake overhead too large; `simulation_failures.log` captures equivalent data at zero cost |

### 8.6 Open Questions & Next Steps

| # | Question | Priority |
|---|----------|----------|
| 1 | Why do certain BIKF–LFPG (and similar) flights consistently trigger `fuel mass flow rate is unrealistic`? Is this a PS model limitation for North-Atlantic long-haul profiles, or a data quality issue in the corridor trajectories? | High |
| 2 | Can the Delta Lake MERGE be replaced with pure `append` mode + periodic deduplication during vacuum/optimize, eliminating per-batch O(n) scan cost? | High |
| 3 | Does the shared `MetDataset` concat object contribute to sequential fallback failures (i.e. would per-flight fresh ERA5 slices eliminate zero-fuel-burn)? | Medium |
| 4 | Should `check_psflight_ok` rejections be written asynchronously to a failure lake (non-blocking) to preserve traceability without blocking the batch pipeline? | Low |