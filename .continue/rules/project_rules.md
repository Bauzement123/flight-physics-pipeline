---
description: Workspace-specific project rules for the Flight Physics Pipeline codebase
---

# Project Rules

This file contains non-README project rules for the Flight Physics Pipeline. README-specific generation and verification rules have been extracted to `.continue/rules/README_gen_rules.md`.

---

## 1. Centralized Configuration Standards

All pipeline-wide settings must live in `src/common/config.py`. No module may define local copies of values that belong in config.

### 1.1 What Belongs in `config.py`

| Category | Examples | Rule |
|---|---|---|
| **File & directory paths** | `MASTER_FLIGHTS_FILE`, `AIRCRAFT_DB_DIR`, `REGISTRIES_DIR` | All paths are derived from `BASE_DIR`. |
| **Registry file paths** | `GLOBAL_TRAJECTORY_REGISTRY`, `GLOBAL_CORRIDOR_SIM_REGISTRY` | One canonical constant per registry file. |
| **Physical unit conversions** | `M_TO_FT`, `MPS_TO_KT`, `MPS_TO_FPM` | Define once; import everywhere else. |
| **Algorithm tuning constants** | `D_PCA`, `N_STANDARD`, `CLUSTERING_MAX_K`, `SILHOUETTE_THRESHOLD` | Centralize calibrated numeric thresholds. |
| **Geographic bounds** | `WEATHER_BOUNDS_BBOX`, `EUR_LAT_MIN/MAX`, `EUR_LON_MIN/MAX` | Centralize all bounding boxes with comments. |
| **Typecode family lists** | `ALL_TARGET_FAMILIES`, `A320_NEO_FAMILY`, `B737_MAX_FAMILY` | Use shared family definitions across modules. |
| **Default pipeline parameters** | `DEFAULT_AIRPORT_PREFIXES`, `CORRIDOR_TIME_GRID_SECONDS` | Centralize defaults referenced by multiple modules. |

### 1.2 What Does Not Belong in `config.py`

| Category | Rule |
|---|---|
| **Runtime arguments** | Values supplied via CLI stay in `argparse`; config may hold defaults only. |
| **Local loop variables** | Keep intermediate per-run state local. |
| **Module-internal rename maps** | Keep transform-specific dictionaries in the owning module. |
| **Logging formatters / handler setup** | Use `src.common.utils.setup_file_logger()`. |

### 1.3 Config Hygiene Rules

- No unused imports of config constants.
- No local shadowing of config constant names.
- Paths are `pathlib.Path` objects, not bare strings.
- Do not hardcode registry filenames inline when a config constant exists.

---

## 2. Unified Logging Standards

All entrypoint logging is centralized through `src.common.utils.setup_file_logger()`.

### 2.1 Logger Setup

- Every module entrypoint invoked via `python -m src...` calls `setup_file_logger()` as the first action in its `if __name__ == "__main__":` block.
- `logging.basicConfig(...)` is forbidden in committed source code.
- Library-level code uses `logging.getLogger(__name__)` only and does not configure handlers.

### 2.2 Log File Locations

All log files are written to `data/logs/` via `LOGS_DIR`.

```text
data/logs/
├── fetching.log
├── filtering.log
├── processing.log
├── acquisition.log
├── corridor.log
├── simulation.log
├── weather.log
├── calibration.log
└── skipped_aircraft.log
```

`skipped_aircraft.log` is the single append-only global record for unsupported/skipped airframes.

### 2.3 Log Level Policy

| Level | Usage |
|---|---|
| `INFO` | Normal milestones and progress. |
| `WARNING` | Recoverable anomalies and fallback paths. |
| `ERROR` | Non-fatal unit-of-work failures. |
| `CRITICAL` | Run-aborting failures followed by `sys.exit(1)` or re-raise. |

`DEBUG` must be guarded by a `--verbose` flag if committed.

---

## 3. Code Review and Quality Audit Standards

Before broad documentation rewrites, perform module-wise code review and record observations in the temporary audit notes.

For each module, inspect:

1. separation of concerns,
2. code atomization and function length,
3. dead code and unused imports,
4. config/path centralization,
5. logging consistency,
6. registry/cache consistency,
7. CLI design consistency,
8. error handling and failure gates,
9. concurrency and thread/process safety,
10. data schema contracts,
11. type hints and IO contracts,
12. testability and pure-function boundaries.

If the review uncovers behavior that should change, discuss or plan the code change before locking the README as source-of-truth documentation.

---

## 4. Git and Workspace Hygiene

- Do not commit generated files, recovered chat histories, local scratch folders, cache folders, or OS artifacts.
- Broken Git refs caused by files such as `.git/refs/**/desktop.ini` should be cleaned in a dedicated hygiene step before relying on `git log --all`.
- Prefer temporary planning and audit files under `data/temp/plans/` unless the user explicitly wants them committed.

---

## 5. Scratch and Developer Utility Policy

Long-term target:

```text
tools/ or src/devtools/        tracked reusable developer utilities
scratch/                       ignored local notes and experiments
data/temp/                     ignored runtime temp and generated artifacts
src/scratchpad/                deprecated or emptied after migration
```

Do not move or delete scratchpad utilities without an explicit classification pass and user approval.

---

## 6. Tiered Agent Delegation & Claw Code Protocols

For detailed instructions and exact execution scripts when delegating heavy analysis, codebase scanning, and predrafting to local models via Claw Code (`gpt-oss-120b`), see [claw_delegation_rules.md](file:///G:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/.continue/rules/claw_delegation_rules.md).