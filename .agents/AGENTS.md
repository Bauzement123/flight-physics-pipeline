# Agent Guidelines and Project Rules

This document defines workspace-scoped guidelines and rules for agents working on the Flight Physics Pipeline codebase.

---

## 1. Module README Standards & Structure

Every module in the codebase must have a detailed `README.md` that serves as a highly precise, technical source of truth. The document must maintain a high level of detail and adhere to the following structure:

1. **Title & Introduction**: Summarize the system module's purpose.
2. **Module Structure**: A text-based tree diagram of the directory and its files.
3. **Function Analysis Solution Tree (FAST)**: A hierarchy mapping high-level module objectives to their code implementations, highlighting input/output contracts and safety/fallback behaviors.
4. **Data Workflow**:
   * **One Mermaid flowchart per distinct workflow**. If a module exposes multiple independent execution paths (e.g., a batch orchestrator and a single-item processor), each path gets its own titled sub-section with its own Mermaid diagram and step-by-step description. Do not combine unrelated workflows into a single diagram.
   * Every Mermaid flowchart must show: files read, registries/caches updated, core operations performed, and output files saved.
   * **Mandatory Step-by-Step Description**: Directly below every Mermaid flowchart, provide a detailed, numbered, text-based walkthrough of the workflow. This serves as a fallback for non-rendering environments and must detail every logical transition and branching decision visible in the diagram.
   * Include sub-sections detailing performance profiles (e.g., **Optimization & Memory Modes** such as Standard vs. Low-Memory) and metric/progress logging formats.
5. **CLI Usage Guide**:
   * Provide separate copy-pasteable syntax blocks for **Bash** and **PowerShell**.
   * Include detailed **Parameter Reference** tables listing each command-line option, its type, its default value, and its description.
6. **Prerequisites & Dependencies**: List library dependencies and specific registry files referenced by the module, linking to the global `conventions.md` file.

---

## 2. Code-README Analysis & Verification Workflow

When examining a module, updating its documentation, or reviewing changes, agents must follow this systematic verification workflow:

1. **Trace Configuration Constants**:
   * Compare all file path constructions and registries in the module with [config.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/config.py).
   * Ensure the README references correct, standardized registry files (e.g., `global_corridor_simulation_registry.parquet` rather than outdated placeholder names like `global_synthesized_simulation_registry.parquet`).
2. **Scan for Dead/Unused Code**:
   * Inspect module scripts for unused imported constants (e.g., `GLOBAL_MODEL_REGISTRY`) and variables defined but never referenced (e.g., local registry file definitions).
   * Remove unused variables and imports to keep scripts clean and maintainable.
3. **Audit CLI Argument Defaults**:
   * Inspect the `argparse` setups in `simulation.py`, `clone_simulation.py`, and other entrypoints.
   * Verify that the defaults, requirements, and allowed options in the code match the README's parameter tables exactly (e.g., matching standard directories like `data/results/corridor_simulations`).
4. **Inspect Weather, Coordinate, and timezone logic**:
   * Check logic for time zones (aware vs. naive UTC conversions) and geographic coordinate projections (WGS84 lat/lon bounding boxes vs. local cartesian coordinate projections) to ensure descriptions match the code implementation and `conventions.md`.
5. **Verify Vectorized vs. Sequential Fallbacks**:
   * Verify batch size partitioning, thread-safety, and exception handling logic in parallel executors. Ensure documented warnings, sequential fallback loops, and error logs (e.g., `skipped_aircraft.log`) match the runtime execution flow.

---

## 3. Centralized Configuration Standards

All pipeline-wide settings must live in `src/common/config.py`. No module may define its own local copies of values that belong in config. Use the following classification to decide where a value lives:

### 3.1 What Belongs in `config.py`

| Category | Examples | Rule |
|---|---|---|
| **File & directory paths** | `MASTER_FLIGHTS_FILE`, `AIRCRAFT_DB_DIR`, `REGISTRIES_DIR` | All paths are derived from `BASE_DIR`. No module constructs its own absolute path strings. |
| **Registry file paths** | `GLOBAL_TRAJECTORY_REGISTRY`, `GLOBAL_CORRIDOR_SIM_REGISTRY` | One canonical constant per registry file. Never hardcode the filename string inline. |
| **Physical unit conversions** | `M_TO_FT`, `MPS_TO_KT`, `MPS_TO_FPM` | Defined once; all modules import from config. Never repeat the numeric literal. |
| **Algorithm tuning constants** | `D_PCA`, `N_STANDARD`, `CLUSTERING_MAX_K`, `SILHOUETTE_THRESHOLD` | Any numeric threshold or hyperparameter that a calibration step has determined. |
| **Geographic bounds** | `WEATHER_BOUNDS_BBOX`, `EUR_LAT_MIN/MAX`, `EUR_LON_MIN/MAX` | All bounding box definitions. Must include a comment citing the geographic edges they represent. |
| **Typecode family lists** | `ALL_TARGET_FAMILIES`, `A320_NEO_FAMILY`, `B737_MAX_FAMILY` | Aircraft family definitions used across acquisition, filtering, and simulation. |
| **Default pipeline parameters** | `DEFAULT_AIRPORT_PREFIXES`, `CORRIDOR_TIME_GRID_SECONDS` | Any default that is referenced in more than one module. |

### 3.2 What Does NOT Belong in `config.py`

| Category | Rule |
|---|---|
| **Runtime arguments** | Values supplied via CLI (`argparse`) at execution time. Config holds defaults; argparse exposes them. |
| **Local loop variables** | Intermediate counters, temporary file handles, per-iteration state. |
| **Module-internal rename maps** | Column rename dictionaries (e.g., `OPENAIRFRAMES_RENAME_MAP`) stay in the module that owns the transform. |
| **Logging formatters / handler setup** | Logging configuration belongs in `src/common/utils.py` via `setup_file_logger()`. |

### 3.3 Config Hygiene Rules

* **No unused imports of config constants.** Before committing, verify every constant imported from `config.py` is actually referenced in the file. Remove unused imports.
* **No local shadowing.** A module must never define a variable with the same name as a `config.py` constant (e.g., `FLIGHT_REGISTRY_DIR = ...` locally).
* **Paths are always `pathlib.Path` objects**, never bare strings. Downstream code calls `.exists()`, `/` operator, etc. directly.

---

## 4. Unified Logging Standards

All logging in the pipeline is centralized through `src/common/utils.py`. No module may configure its own log handlers independently.

### 4.1 Logger Setup

* **Every module entrypoint** (scripts invoked via `python -m src...`) calls `setup_file_logger()` from `src.common.utils` as the first action in its `if __name__ == "__main__":` block.
* **`logging.basicConfig(...)` is forbidden** anywhere in the codebase. It must not appear in any module file. Use `setup_file_logger()` exclusively.
* Library-level code (functions, classes) uses `logging.getLogger(__name__)` without configuring handlers. Only entrypoints configure handlers.

### 4.2 Log File Locations

All log files are written to `data/logs/` (defined as `LOGS_DIR` in `config.py`). Subdirectory structure within `data/logs/`:

```text
data/logs/
├── fetching.log          ← fetcher_orchestrator.py runs
├── filtering.log         ← filter_orchestrator.py runs
├── processing.log        ← kalman_filter.py runs
├── acquisition.log       ← build_master_population.py, fleet_builder.py, master_merger.py
├── corridor.log          ← corridor_orchestrator.py, streaming_pipeline.py runs
├── simulation.log        ← simulation.py, clone_simulation.py runs
├── weather.log           ← era5_manager.py runs
├── calibration.log       ← variational_orchestrator.py, gt_stability_sweep.py runs
└── skipped_aircraft.log  ← appended by any module that skips an unsupported aircraft
```

* Log filenames are **fixed per module**, not dynamically generated, so log history accumulates and is appendable across runs.
* The exception is `skipped_aircraft.log` which is always appended (never overwritten) and is the single global record of skipped airframes across all pipeline stages.

### 4.3 Log Level Policy

| Level | Usage |
|---|---|
| `INFO` | Normal progress milestones: files loaded, rows processed, outputs saved. |
| `WARNING` | Recoverable anomalies: missing optional file, fallback path taken, empty result set for a corridor. |
| `ERROR` | Non-fatal failures that skip a unit of work (one corridor, one flight) but allow the pipeline to continue. |
| `CRITICAL` | Unrecoverable failures that abort the current run. Followed by `sys.exit(1)` or re-raise. |

* `DEBUG` may be used during development but must not appear in committed code without a `--verbose` flag guarding it.

---

## 5. Pre-Commit README Verification Checklist

Before committing any change that touches a Python source file in a module directory, the agent **must** verify the corresponding `README.md` for that module. Specifically:

1. **Workflow coverage**: Every distinct execution path (CLI entrypoint) in the changed files is represented by its own Mermaid diagram + step-by-step description in the README.
2. **CLI parameter parity**: Every `argparse` argument in the changed files appears in the README's Parameter Reference table with the correct type, default, and description.
3. **Config constant parity**: Every constant imported from `config.py` that is used in the changed files is mentioned by name in the README (in the Data Workflow, FAST tree, or Prerequisites section).
4. **Log file reference**: The README states which log file the module writes to (matching Section 4.2 of this document).
5. **No stale path references**: The README does not contain any directory paths or registry filenames that no longer exist in the current codebase (cross-check against `config.py`).

If the README is out of date in any of the above respects, it **must be updated in the same commit** as the code change. A code change without a corresponding README update is not considered complete.

---

## 6. Multi-Workflow Module Documentation Pattern

When a module directory contains scripts that implement more than one independent workflow (e.g., an orchestrator and a worker, or a batch runner and a single-item processor), the README must structure the Data Workflow section as follows:

```markdown
## 4. Data Workflow

### 4.1 Workflow A — <Short Name> (`<script_name>.py`)

```mermaid
...diagram for workflow A...
```

**Step-by-step:**
1. ...
2. ...

---

### 4.2 Workflow B — <Short Name> (`<script_name>.py`)

```mermaid
...diagram for workflow B...
```

**Step-by-step:**
1. ...
2. ...
```

Rules:
* Each workflow sub-section is numbered (4.1, 4.2, …) and titled with the script filename.
* Diagrams must be self-contained — a reader should be able to understand one workflow without reading the others.
* If two scripts share a significant portion of their workflow (e.g., both write to the same registry), this shared portion is described once in a "Shared Infrastructure" note at the top of Section 4, then referenced briefly in each sub-diagram.
