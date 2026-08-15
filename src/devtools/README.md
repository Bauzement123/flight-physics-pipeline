# `src/devtools/` — Developer Utilities & Diagnostics

This directory contains standalone developer utilities, diagnostic tools, and test suites. These utilities support maintenance, dataset migrations, and performance diagnostics, and are decoupled from normal end-to-end pipeline execution.

---

## 1. Quick Tool Index

| Utility | Description | Primary CLI Syntax |
|---|---|---|
| **[`inspect_lake.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/inspect_lake.py)** | High-performance $\mathcal{O}(1)$ RAM Delta Lake metric & flight inspector | `python -m src.devtools.inspect_lake <lake_path>` |
| **[`test_sync_delta_lake.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/test_sync_delta_lake.py)** | Unit & integration test suite for Delta Lake sync & maintenance | `python -m src.devtools.test_sync_delta_lake` |
| **[`find_dependencies.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/find_dependencies.py)** | AST-scanner for standard library and 3rd-party package dependencies | `python -m src.devtools.find_dependencies` |
| **[`trajectory_manager.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/trajectory_manager.py)** | Trajectory batch archiver (`pack`, `unpack`, `relabel`) | `python -m src.devtools.trajectory_manager <subcommand>` |
| **[`verify_typecode_validation.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/verify_typecode_validation.py)** | Strict Anti-Default typecode validation test suite | `python -m src.devtools.verify_typecode_validation` |
| **[`verify_postfilters.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/verify_postfilters.py)** | Verification of trajectory post-filters & distance metrics | `python -m src.devtools.verify_postfilters` |
| **[`test_refactor.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/test_refactor.py)** | Regression test suite (13 test cases) for fetching & data adapters | `python -m src.devtools.test_refactor` |
| **[`test_ekf_autopsy.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/test_ekf_autopsy.py)** | Unit test suite for EKF Kalman filter diagnostic calculations | `python -m src.devtools.test_ekf_autopsy` |
| **[`test_phase_quality_plots.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/test_phase_quality_plots.py)** | Test runner for multi-page phase quality PDF visual reports | `python -m src.devtools.test_phase_quality_plots --all` |
| **[`rename_legacy_rank_folders.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/rename_legacy_rank_folders.py)** | Migration utility to standardize legacy `rank_NNN` folder names | `python -m src.devtools.rename_legacy_rank_folders --commit` |
| **[`sync_local_to_gdrive.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/sync_local_to_gdrive.py)** | Registry and parquet backup synchronization to Google Drive | `python -m src.devtools.sync_local_to_gdrive` |
| **[`show_base.py`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/show_base.py)** | Prints the resolved repository root path to stdout | `python -m src.devtools.show_base` |

---

## 2. Highlighted Tool Usage Guide

### 2.1 `inspect_lake.py` — Delta Lake Metric Inspector

Use this tool to instantly inspect any local or remote Delta Lake without loading multi-gigabyte trajectory waypoints into memory.

#### Features
- **Constant $\mathcal{O}(1)$ Memory Footprint**: Uses PyArrow columnar scanning and chunked record batches ($100,000$ rows) to guarantee $< 20\text{ MB}$ peak RAM on tables of any size (even 100M+ rows).
- **Fast Metric Extraction**: Summarizes unique `SIM_FID` count, total waypoint rows, table versions, parquet file count, route and aircraft type distributions, flight level ranges, date ranges, and contrail radiative energy forcing ($\text{EF}_{\text{total}}$).

#### Syntax Examples

```powershell
# Inspect local simulation lake
python -m src.devtools.inspect_lake "data/results/corridor_simulations_hydrogen"

# Inspect central SMB network lake over RWTH VPN
python -m src.devtools.inspect_lake "\\PC182.ilr.rwth-aachen.de\studiert_ilr\Kirste\PA_ZeroCloud\PythonPipeline\data\results\corridor_simulations_hydrogen"
```

---

### 2.2 `test_sync_delta_lake.py` — Sync Test Suite

Runs a self-contained 5-scenario integration test suite validating Delta Lake bootstrapping, in-sync no-ops, incremental overlap deduplication, downserts, and compaction.

```powershell
python -m src.devtools.test_sync_delta_lake
```

---

## 3. Detailed File Catalog

For comprehensive, in-depth descriptions of all scripts in this directory, refer to **[`listings.md`](file:///d:/PA_overnight%20cache/flight-physics-pipeline/src/devtools/listings.md)**.
