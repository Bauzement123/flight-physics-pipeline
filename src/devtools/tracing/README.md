# Tracing, Telemetry, and Auditing Tools

This directory contains high-fidelity dynamic tracing, sub-second telemetry logging, static AST analysis, and performance correlation tools for the Flight Physics Pipeline.

All runtime trace logs, telemetry streams, and star-schema correlation tables are written to `data/traces/`.

---

## 1. Dynamic Tracing & Telemetry Architecture

The performance profiling subsystem couples continuous sub-second system resource monitoring with Python C-level dynamic tracing (`sys.settrace`), emitting structured star-schema execution tensors.

```
┌──────────────────────────────────────┐    ┌────────────────────────────────────────────────┐
│   telemetry_monitor.py (psutil)      │    │   trace_probe.py  +  runtime_tracer.py         │
│   Spawned by harness.py as Popen     │    │   Spawned by harness.py as Popen               │
│                                      │    │                                                │
│   Polls all python PIDs every ~200ms │    │   runpy.run_module("src.core.physics.cli")     │
│   Writes telemetry_<sid>.csv         │    │   sys.settrace active → runtime_trace_<sid>.log│
└──────────────────┬───────────────────┘    └───────────────────────┬────────────────────────┘
                   │                                                 │
                   └───────────────────┬─────────────────────────────┘
                                       │  harness.py joins both processes on exit
                                       │
                        ┌──────────────▼──────────────┐
                        │      trace_correlator.py     │
                        │                              │
                        │  1. Parse trace log via      │
                        │     stack state machine      │
                        │  2. pycontrails depth-1      │
                        │     boundary filter          │
                        │  3. Parallelism flag pass    │
                        │  4. Telemetry interpolator   │
                        │  5. Emit star-schema tables  │
                        └──────────────┬───────────────┘
                                       │
               ┌───────────────────────┴─────────────────────────┐
               ▼                                                 ▼
  ┌────────────────────────┐                      ┌──────────────────────────────────┐
  │   call_sites.parquet   │                      │   invocations.parquet            │
  │   (static dimension)   │◄─── site_id FK ─────►│   (dynamic fact table / tensor)  │
  │   site_id              │                      │   one row per invocation instance│
  │   func                 │                      └──────────────────────────────────┘
  │   module               │
  │   parent_func          │
  │   parent_module        │
  └────────────────────────┘
               │
               └──────────────────────┐
                                      │
                        ┌─────────────▼───────────────┐
                        │      trace_analyzer.py       │
                        │                              │
                        │  --report: single-run stats  │
                        │  --compare: two-run diff     │
                        │                              │
                        │  · Memory drift / leak       │
                        │  · Thrashing (page faults)   │
                        │  · GC sawtooth detection     │
                        │  · Invocation distributions  │
                        │  · Scaling / diminishing ret.│
                        └──────────────────────────────┘
```

---

## 2. Tools Reference

### 2.1 `harness.py` (End-to-End Orchestrator)
The primary entrypoint for benchmark campaigns. Concurrently starts the `telemetry_monitor.py` daemon, launches `trace_probe.py` with your chosen target pipeline module, and automatically triggers post-run correlation and performance reporting.

```powershell
# Standard Mode benchmark
python -m src.devtools.tracing.harness `
  --module src.core.physics.cli `
  --args "--start-date 2025-01-01 --end-date 2025-01-01 --lower-rank 1 --upper-rank 3 --max-workers 4 --batch-size 50 --out-dir data/results/corridor_simulations_kerosene" `
  --out-dir data/traces/run_standard

# Low-Memory Mode benchmark (with direct comparative diff against standard run)
python -m src.devtools.tracing.harness `
  --module src.core.physics.cli `
  --args "--start-date 2025-01-01 --end-date 2025-01-01 --lower-rank 1 --upper-rank 3 --max-workers 2 --batch-size 10 --low-mem --out-dir data/results/corridor_simulations_kerosene" `
  --out-dir data/traces/run_lowmem `
  --compare data/traces/run_standard
```

### 2.2 `telemetry_monitor.py` (System Resource Monitor)
A lightweight `psutil` sampling daemon that runs in a dedicated process, polling all running Python PIDs (main + worker pool) at configurable sub-second intervals (default 200 ms).
- **Captured Metrics**: `Time_ms`, `RAM_MB` (aggregate RSS), `USS_MB` (unique set size), `Commit_MB` (private commit / pagefile allocation), `CPU_Pct_Python`, `CPU_Pct_System`, `CPU_Per_Core` (per-core JSON array), `Page_Faults_Delta`, `Handle_Count`, `Thread_Count`, `Worker_Process_Count`, `Read_MBs`, `Write_MBs`, `Read_IOPS`, `Write_IOPS`, `Sys_Avail_RAM_MB`.
- Rates and fault deltas are pre-computed internally per sampling tick to minimize downstream compute.

```powershell
python -m src.devtools.tracing.telemetry_monitor --interval-ms 100 --csv-file data/traces/telemetry_sample.csv
```

### 2.3 `trace_probe.py` (Generic Tracing Injection Shim)
Uses `runpy.run_module` to attach `runtime_tracer.start_tracing()` / `stop_tracing()` to any pipeline entrypoint without modifying repository source files.

```powershell
python -m src.devtools.tracing.trace_probe --trace-log data/traces/my_run.log src.core.physics.cli --start-date 2025-01-01 ...
```

### 2.4 `trace_correlator.py` (Star-Schema Correlator)
Merges raw trace logs and telemetry CSV streams into structured star-schema tables (`call_sites.parquet` / `invocations.parquet`).
- Reconstructs per-PID call stacks and unwinds frames on exceptions.
- Implements **pycontrails depth-1 boundary filtering**: captures the entry into pycontrails (e.g. `Cocip.eval()`, `PSFlight.eval()`, `downselect()`) while suppressing millions of internal leaf math calls.
- Flags concurrent multi-process intervals with `parallelism_flag=True`.
- Performs $O(\log N)$ interval slicing via `numpy.searchsorted`.

```powershell
python -m src.devtools.tracing.trace_correlator `
  --trace-log data/traces/runtime_trace_20260819_150000.log `
  --telemetry-csv data/traces/telemetry_20260819_150000.csv `
  --out-dir data/traces/run_standard `
  --format both
```

### 2.5 `trace_analyzer.py` (Performance & Drift Analyzer)
Consumes star-schema tables to generate executive markdown reports, detect memory leaks, and compute comparative diffs between execution modes.

```powershell
# Single-run report
python -m src.devtools.tracing.trace_analyzer --data-dir data/traces/run_standard --report data/traces/report_standard.md

# Comparative diff
python -m src.devtools.tracing.trace_analyzer `
  --data-dir data/traces/run_lowmem `
  --compare data/traces/run_standard `
  --report data/traces/report_standard_vs_lowmem.md
```

---

## 3. Star-Schema Data Model

### 3.1 Static Dimension Table (`call_sites.parquet` / `.csv`)
Identifies structural call sites in the codebase.
- `site_id`: Deterministic 8-character hex hash of `(func, module, parent_func, parent_module)`.
- `func`: Target function or method name.
- `module`: Repository-normalized source file path.
- `parent_func`: Calling function name.
- `parent_module`: Calling module path.

### 3.2 Dynamic Fact Table (`invocations.parquet` / `.csv`)
Contains individual execution instances enriched with interval telemetry.
- `site_id`: Foreign key to `call_sites`.
- `inv_idx`: 0-indexed invocation sequence number for this specific `site_id`.
- `pid`: Process/thread identifier (`MAIN`, `T-<ThreadName>`, `PID=<pid>`, `PID=<pid>:T-<ThreadName>`).
- `depth`: Call stack depth.
- `start_ts` / `end_ts`: Wall-clock timestamps (`HH:MM:SS.mmm`).
- `duration_ms` / `self_time_ms`: Execution duration and exclusive self-time.
- `pycontrails_boundary`: Boolean flag indicating entry into pycontrails.
- `parallelism_flag`: Boolean flag indicating overlapping sibling worker execution.
- `telemetry_interpolated`: Boolean flag indicating sub-200ms duration (nearest-neighbor sampled).
- `ram_start_mb`, `ram_peak_mb`, `ram_end_mb`, `ram_delta_mb`: Working set memory metrics.
- `uss_start_mb`, `uss_peak_mb`: Unique non-shared process memory.
- `commit_start_mb`, `commit_peak_mb`, `commit_delta_mb`: OS Private Commit charge.
- `swap_pressure`: Ratio $\frac{\text{Commit}_{\text{peak}} - \text{RAM}_{\text{peak}}}{\text{RAM}_{\text{peak}}}$ (indicator of pagefile offload fraction).
- `page_faults_delta`: Total hard page faults incurred during span.
- `peak_read_mbs`, `peak_write_mbs`, `cum_read_mb`, `cum_write_mb`: Disk I/O metrics.
- `cpu_pct_python_mean`, `cpu_pct_python_peak`, `cpu_pct_system_mean`: CPU utilization metrics.
- `cpu_per_core_peak`: JSON array of peak per-core CPU percentages.
- `handle_count_start`, `handle_count_end`, `handle_count_delta`: OS file/object handles.
- `thread_count_mean`, `worker_process_count_mean`: Concurrency scale.

---

## 4. Static & Auditing Devtools

### 4.1 `static_trace_generator.py` (Static AST Analyzer)
Parses Python source ASTs to construct static Call Traces and Disk I/O Ledgers without running the code. Recognizes multiprocessing dispatch boundaries (`pool.submit`, `pool.map`).

```powershell
python -m src.devtools.tracing.static_trace_generator --file src/core/physics/clone_simulation.py --hide-ext
```

### 4.2 `trace_registry_dataflow.py` (Dataflow Mapper)
Scans the entire codebase for parquet registry touches (`read_parquet`, `to_parquet`, `append_sim_lake`) and traces call chains up to module orchestrators.

### 4.3 `audit_common_imports.py` (Import Standardizer)
Scans for `src.common` imports to verify clean configuration standard compliance and wildcard prevention.
