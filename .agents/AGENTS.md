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

---

## 7. Code Review and Quality Audit Standards

Before broad documentation rewrites or major architectural refactors, perform a module-wise code review and record observations in temporary planning/audit notes under `data/temp/plans/`.

For each module, systematically inspect:
1. **Separation of concerns**: Ensure CLI parsing, business logic, and I/O are cleanly decoupled.
2. **Code atomization & function length**: Keep entrypoint `main()` functions ≤ 80 lines and helpers ≤ 50 lines.
3. **Dead code & unused imports**: Remove unreferenced variables, constants, and imports.
4. **Config & path centralization**: Derive all paths from `BASE_DIR` in `config.py`.
5. **Logging consistency**: Ensure single `setup_file_logger()` invocation per entrypoint.
6. **Registry & cache consistency**: Verify atomic write patterns and uniform deduplication rules.
7. **CLI design consistency**: Expose standard flags (`--out-dir`, `--log-file`, `--max-workers`, `--resume`).
8. **Error handling & failure gates**: Log critical errors at `CRITICAL` level followed by clean `sys.exit(1)`.
9. **Concurrency & process safety**: Guard shared resources and document thread vs. process pools.
10. **Data schema contracts**: Centralize dataframe column whitelists and rename dictionaries.
11. **Type hints & IO contracts**: Annotate all public helper functions and entrypoint methods.
12. **Testability & pure-function boundaries**: Isolate data transformations from filesystem mutation.

---

## 8. Git, Workspace Hygiene & Scratch Policy

* **Artifact Isolation**: Never commit generated files, recovered chat histories, local scratch folders, or OS artifacts (`.DS_Store`, `Thumbs.db`, `desktop.ini`).
* **Broken Ref Hygiene**: If `git log --all` fails due to corrupted `.git/refs/**/desktop.ini` files, clean only those invalid ref files before executing historical git audits.
* **Directory Policy**:
  ```text
  tools/ or src/devtools/        Tracked reusable developer utilities
  scratch/                       Ignored local notes and experiments
  data/temp/                     Ignored runtime temp and generated planning artifacts
  src/scratchpad/                Deprecated or emptied after migration to real modules/tools
  ```

---

## 9. Stale Reference & Namespace Hygiene

Before finalizing documentation or refactor milestones, verify that no stale legacy paths or placeholder registry filenames remain. Run standard workspace greps:

```bash
grep -RIn "src/acquisition\|src/fetching\|src/filtering\|src/processing\|src/physics" src --include="README.md"
grep -RIn "data/flight_registry\|global_synthesized\|global_corridor_registry" src --include="README.md"
```

Canonical modern namespaces:
`src/core/acquisition/`, `src/core/fetching/`, `src/core/filtering/`, `src/core/processing/`, `src/core/corridor/`, `src/core/weather/`, `src/core/physics/`, `src/analysis/campaigns/`, `src/analysis/verification/`.

---

## 10. Tiered Agent Delegation & Cost-Optimization Architecture

To maximize engineering velocity while keeping token consumption economical, the repository operates under a **Tiered Agent Delegation Architecture**:

### 10.1 Roles & Responsibilities
* **Primary Architect & Orchestrator (Antigravity / Frontier Models)**: Responsible for system architecture, complex multi-file planning, high-precision code review, git commit verification, and quality gate enforcement. Avoids burning frontier tokens on raw high-volume drafting, repetitive boilerplate generation, or initial document extraction.
* **Drafting & Synthesis Engine (Local / Open-Source Models e.g., GPT OSS 120B via Continue or Claw Code)**: Responsible for heavy text synthesis, reading long audit logs, drafting initial markdown pattern catalogs, and performing broad routine scans.

### 10.2 Delegation Protocols & Structured Return Formats
1. **Batched Prompting**: When delegating synthesis tasks (e.g., cross-module pattern extraction) to local open-source models, split instructions into focused batches (e.g., 3–4 categories per prompt) referencing explicit file paths to avoid context drift and output truncation.
2. **Structured Predraft Return Formats**: When delegating analytical or comparison tasks (such as Step 4.2 cross-module comparisons or Step 4.3 rule drafting), explicitly instruct the local model to return outputs as **structured predrafts** (e.g., Markdown comparison tables, explicit bulleted option lists, or draft rule blocks tagged with `[DRAFT]`). This ensures the local engine performs the heavy reading and initial structuring, leaving only a rapid, high-precision final review and polish pass for the Primary Architect.
3. **Safe CLI Execution Boundaries & Clean JSON Extraction**: When invoking external local CLI agents (such as Claw Code) from automated scripts or pipelines where output must be saved to a file or parsed programmatically:
   * **Use `--output-format json`**: Avoid `--output-format text` when writing to file (`Out-File`), as it embeds ANSI terminal spinners (`Thinking...`) and tool execution logs into the output file. Instead, invoke with `--output-format json` and parse the `.message` property.
   * **PowerShell UTF-8 Encoding & Mojibake Prevention**: In PowerShell 5.1 on Windows, pipeline streams (`| Out-String` or pipe captures) decode external process output using `[Console]::OutputEncoding` (which defaults to Windows-1252). This corrupts multi-byte UTF-8 Unicode characters (such as non-breaking hyphens `‐`, en-dashes `–`, curly apostrophes `’`, and math symbols `≈`, `≤`) into garbled Mojibake strings (`ÔÇæ`, `Ôëñ`). To prevent character corruption, always force UTF-8 console encoding before executing pipeline captures:
     ```powershell
     [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
     $OutputEncoding = [System.Text.Encoding]::UTF8
     $json = Get-Content prompt.txt | claw --model gpt-oss-120b --permission-mode read-only --allowedTools read,glob --output-format json prompt --stdin | Out-String | ConvertFrom-Json
     [IO.File]::WriteAllText("output.md", $json.message, [System.Text.Encoding]::UTF8)
     ```
   * **Prompt Piping vs. CLI Arguments**: Always pass prompt instructions and paths via input files piped to `--stdin` rather than `@parameter` syntax or raw string command-line arguments to ensure robust path resolution and prevent escaping errors.
   * **Environment Variable Propagation**: When running in PowerShell sub-processes after setting API credentials via `setx`, ensure session variables are refreshed or explicitly loaded (`[Environment]::GetEnvironmentVariables(...)`) before invoking the CLI tool.
4. **Strict Prohibition Against Creating/Executing Standalone `.ps1` Scripts**:
   * **Never create temporary or standalone `.ps1` script files** (e.g., `run_claw.ps1`, `audit.ps1`) to run external tools or Claw Code. Executing `.ps1` files frequently fails across Windows environments due to PowerShell Execution Policy blocks (`Restricted`/`RemoteSigned`), UTF-16LE/BOM encoding mismatches when generating the script file, and quote-stripping across sub-shell boundaries.
   * **Mandatory Execution Pattern**: Always execute commands directly inline using the terminal command tool (passing the exact PowerShell command block inline). Never wrap commands into `.ps1` script files on disk.
5. **Verification Loop**: All predrafts generated by local drafting engines must be reviewed, refined, and verified by the Primary Architect against the standards in this document before being integrated into permanent repository documentation or source code.

### 10.3 Standalone Rules & Copy-Pasteable Templates
For full execution templates, copy-pasteable PowerShell blocks, and standalone guidelines on delegating to local open-source models via Claw Code (`gpt-oss-120b`), consult [.agents/rules/claw_delegation_rules.md](file:///G:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/.agents/rules/claw_delegation_rules.md).

---

## 11. Strict Aircraft Typecode Verification & Anti-Default Policy

To preserve data integrity, aerodynamic validity, and simulation correctness across the entire pipeline, all modules must adhere to the **Strict Aircraft Typecode Verification & Anti-Default Policy**:

### 11.1 Absolute Prohibition of Default Typecode Injections (Anti-Default Rule)
* **Never inject, assign, or fall back to default or placeholder aircraft typecodes** (e.g., `typecode = typecode or 'B738'`, `typecode.fillna('UNKNOWN')`, `'unknown'`, `DEFAULT_B738`, or hardcoded family strings like `'A320'`) anywhere in the pipeline—whether during fetching (`Track A`), fleet building (`Track B`), ingestion (`adapters`), trajectory cleaning (`kalman_filter`), medoid clustering (`clustering_worker`), batch path synthesis (`path_generator`), or physical simulation (`engine` / `PSFlight`).
* If an incoming flight record, airframe row, parquet trajectory, or medoid has a missing (`None`, `np.nan`), empty, or unassigned `typecode`, **do not guess or fill it in**.

### 11.2 Mandatory Central Typecode Validation
* All modules that inspect, filter, or ingest aircraft typecodes must import and call `is_supported_typecode(typecode)` from `src.common.config`.
* `is_supported_typecode(typecode)` verifies that the typecode belongs strictly to `ALL_TARGET_FAMILIES` (`A320_NEO_FAMILY + A320_CEO_FAMILY + B737_NG_FAMILY + B737_MAX_FAMILY`). Any typecode outside these exact target families (`A19N`, `A20N`, `A21N`, `A318`, `A319`, `A320`, `A321`, `B733`, `B734`, `B735`, `B736`, `B737`, `B738`, `B739`, `B37M`, `B38M`, `B39M`) is considered unsupported and invalid.

### 11.3 Mandatory Error Flagging & Logging to `skipped_aircraft.log`
* When any pipeline stage encounters a record, flight, or medoid whose typecode is missing (`NaN`), `None`, empty, or unsupported (`not is_supported_typecode(typecode)`), the module **must** immediately drop/skip the record and log an error.
* All skipped aircraft error logging **must** use the centralized helper function `log_skipped_aircraft(flight_or_icao_id, typecode, reason)` imported from `src.common.utils`.
* The `reason` string passed to `log_skipped_aircraft()` **must** start with `ERROR_FLAG:` (for example: `"ERROR_FLAG: Missing, NaN, or non-target family aircraft typecode"` or `"ERROR_FLAG: Parquet trajectory has NaN or unsupported typecode"`).
* This ensures every skipped/dropped airframe or trajectory across every stage accumulates in `data/logs/skipped_aircraft.log` (`LOGS_DIR / "skipped_aircraft.log"`) as tab-separated audit entries (`ISO_UTC \t ID \t TYPECODE \t REASON`).

### 11.4 Verification & Regression Testing Gate
* Before committing changes to any core processing, ingestion, corridor, or simulation script, agents must verify that no fallback defaults exist (`grep -n "or 'B738'" src/...` must return nothing).
* Run the developer verification suite `python -m src.devtools.verify_typecode_validation` to confirm that all modules correctly reject `NaN`/unsupported typecodes without assigning defaults and verify that `skipped_aircraft.log` receives exact `ERROR_FLAG:` entries.


