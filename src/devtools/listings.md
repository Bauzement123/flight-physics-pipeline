# `src/devtools/` — Developer Utilities

This directory contains standalone developer tools, test scripts, and migration utilities. These are not part of the core pipeline and are not invoked during normal pipeline runs.

---

## Files

### `audit_registry_scratch.py`
`audit_registry_scratch.py` is a standalone registry verification utility that compares the global clean trajectory registry (`GLOBAL_CLEAN_REGISTRY`) against raw and clean trajectory parquet files located in `data/trajectories/`. You would run this script after trajectory filtering or dataset maintenance to verify that all on-disk parquet files are properly indexed in the registry. The script expects an existing clean trajectory registry parquet file alongside on-disk trajectory directories matching the `*_clean_si.parquet` pattern. It outputs total flight count comparisons, lists unindexed flight IDs per file, reports missing registered flights, and identifies stale registry paths pointing to legacy `/raw/` directories directly to standard output. The script runs top-level code directly without a main execution block or CLI parameters.

---

### `find_dependencies.py`
`find_dependencies.py` AST-scans all Python source files under `src/` to compile a comprehensive catalog of standard library and external package imports used across the codebase. You run this tool when auditing workspace dependencies, generating project requirements, or detecting missing Python packages in the environment. It parses all `.py` files in `src/` and checks package metadata from the active Python environment without requiring CLI input parameters. The script outputs formatted 2-section tables summarizing standard library modules and external package versions to standard output and logs results to `data/logs/devtools.log`. It is invoked via `python -m src.devtools.find_dependencies` and requires no command-line flags.

---

### `rename_legacy_rank_folders.py`
`rename_legacy_rank_folders.py` is a migration utility that renames or merges legacy trajectory directories structured as `rank_NNN_DEP-ARR` into clean `DEP-ARR` route directory names under `data/trajectories/`. Run this script when standardizing directory layouts or upgrading legacy trajectory dataset folder structures. It reads folder names, parquet trajectory files, JSON route manifests, and run manifests under `data/trajectories/` (or a custom path specified via `--trajectories-dir`). The script renames directories and files, patches internal JSON manifest references, and writes operational logs to `data/logs/manifest.log`. Invoked via `python -m src.devtools.rename_legacy_rank_folders`, it defaults to a safe dry-run preview and requires the `--commit` flag to execute filesystem mutations.

---

### `show_base.py`
`show_base.py` is a lightweight helper script that resolves and prints the absolute filesystem path of the repository root directory. You would run this script to quickly inspect or verify repository path resolution from inside sub-shell environments or automated devtool scripts. It inspects its own module filepath location (`__file__`) and expects no input parameters or configuration files. The script outputs a single absolute path string representing the top-level repository folder directly to standard output. It is invoked via `python -m src.devtools.show_base` and accepts no command-line options.

---

### `sync_local_to_gdrive.py`
`sync_local_to_gdrive.py` is a dataset synchronization utility that compares local working copy trajectory registries and data directories against the canonical Google Drive copy (`G:\Meine Ablage\UNI\SS26\PythonPipeline - Kopie`). Run this utility to transfer newly acquired or updated local raw/clean trajectory parquet files to Google Drive backup storage, or to merge write-ahead log (WAL) registry dumps. It compares local and remote parquet files under `data/trajectories/` and maintains a temporary registry write-ahead log file (`registry_dump.tmp.parquet`). The script outputs transfer progress summaries to stdout, copies updated parquet files to remote storage, and merges temporary dumps into `global_trajectory_registry.parquet` when executed with `--apply-dump`. Invoked via `python -m src.devtools.sync_local_to_gdrive`, it supports `--dry-run` to preview file operations without mutating remote files and `--local-dir` to override source workspace paths.

---

### `test_ekf_autopsy.py`
`test_ekf_autopsy.py` is a modular unit test suite that validates the mathematical calculations, file I/O route parsing, and orchestration pipeline of the EKF autopsy campaign (`src.analysis.campaigns.phase_quality`). Run this test script when modifying normalized innovation squared (NIS) calculations, residual autocorrelation, covariance condition metrics, or campaign execution logic. It generates synthetic innovation vectors, covariance matrices, and mock EKF diagnostic archives inside temporary project directories under `data/temp/`. The script outputs color-coded PASS/FAIL assertions and a summary count table to standard output, exiting with status code 1 if any assertion fails. Invoked via `python -m src.devtools.test_ekf_autopsy`, it runs self-contained diagnostic tests without requiring external command-line flags.

---

### `test_ekf_diagnostics.py`
`test_ekf_diagnostics.py` is an isolated test suite that verifies EKF diagnostic array loading, quality metric recomputation, manifest metric extraction, and EKF diagnostic registry rebuilding in `build_global_manifest.py`. Run this script when updating EKF diagnostic NPZ archive handling, quality score metrics, or global manifest registry generation routines. It constructs synthetic NPZ diagnostic archives and mock corridor path parquets inside isolated temporary test folders under `data/temp/`. The script prints step-by-step test execution progress alongside colorized PASS/FAIL indicators to standard output, raising system exit code 1 on failure. It is invoked via `python -m src.devtools.test_ekf_diagnostics` and executes all unit test functions automatically without requiring CLI flags.

---

### `test_phase_quality_plots.py`
`test_phase_quality_plots.py` is a test runner script for the phase quality plotting engine (`src.analysis.campaigns.phase_quality.phase_quality_plots`) that generates multi-page baseline visual audit PDF reports. Run this tool when testing or benchmarking PDF report rendering, matplotlib layout generation, or multi-process plot compilation across corridor flight cohorts. It reads candidate flight pool registries (`AUDIT_CANDIDATE_POOL_REGISTRY`), cohort map registries (`AUDIT_COHORT_MAP_REGISTRY`), and raw trajectory parquet files. The script outputs 10-page visual audit PDF reports per route under `data/results/phase_quality/runs/` (or a custom `--out-dir`) and writes logs to `data/logs/calibration.log`. Invoked via `python -m src.devtools.test_phase_quality_plots`, it supports `--route` (default EDDF-LIRF), `--all` (for all 6 target routes), `--workers` (process count), and `--format` (PNG or SVG rendering).

---

### `test_refactor.py`
`test_refactor.py` is a comprehensive regression test suite containing 13 test cases that validate safety and performance contracts across the fetching module (`src.core.fetching`). Run this script when refactoring fetching utilities, atomic parquet writers, flight phase labeling logic, or batch orchestrator resume behaviors. It programmatically constructs mock master flight dataframes, synthetic PyArrow timestamp arrays, and temporary parquet files inside `data/temp/`. The script outputs detailed PASS/FAIL test assertions and a final test tally to stdout, exiting with status code 1 if any regression assertion fails. It is invoked via `python -m src.devtools.test_refactor` and executes all unit tests synchronously without needing CLI options.

---

### `trajectory_manager.py`
`trajectory_manager.py` is a developer CLI tool for managing raw and clean trajectory datasets across corridor cohorts via `pack`, `unpack`, and `relabel` sub-commands. Run this tool to pack loose single-flight parquets into cohort batch archives, restore single-flight parquets from archives, or re-apply OpenAP flight phase labels to raw trajectories in SI units. It operates on single-flight parquet files and batch archives (`*_all_raw.parquet` / `*_all_clean.parquet`) located in `data/trajectories/`. The script updates trajectory archives and single-flight parquet files on disk, triggers manifest registry rebuilds upon unpacking, and logs operations to `data/logs/acquisition.log`. Invoked via `python -m src.devtools.trajectory_manager <subcommand>`, it accepts flags including `--cohort`, `--type` (`raw`, `clean`, `both`), `--delete-originals` (for `pack`), `--fids` / `--force` (for `unpack`), and `--force` (for `relabel`).

---

### `verify_postfilters.py`
`verify_postfilters.py` is a verification script that tests trajectory post-filtering rules (`apply_trajectory_postfilters`) and airport distance calculations (`recompute_airport_distances`) against trajectory data. Run this script when updating trajectory post-filtering thresholds, velocity limits, or acceleration anomaly detection logic in `phase_quality_filters.py`. It reads sample raw and clean trajectory parquet files on disk under `data/trajectories/rank_143_EDDF-LIRF/` and calculates distance metrics. The script outputs JSON-formatted trajectory filter metrics for baseline, velocity violation, and acceleration spike test runs to standard output, verifying that invalid trajectories are correctly rejected. It is invoked via `python -m src.devtools.verify_postfilters` and runs the verification suite directly without command-line parameters.

---

### `verify_typecode_validation.py`
`verify_typecode_validation.py` is a verification script that validates strict aircraft typecode validation rules (`is_supported_typecode`) and the pipeline Anti-Default Policy. Run this script after modifying target aircraft family definitions in `config.py` or typecode checking logic in `adapters`, `kalman_filter`, or `fetching`. It evaluates valid and invalid aircraft typecodes, tests DataFrame conversions, and asserts that missing typecodes are rejected rather than assigned default fallbacks. The script outputs assertion results to stdout and appends a verification entry to `data/logs/skipped_aircraft.log` via `log_skipped_aircraft()`. Invoked via `python -m src.devtools.verify_typecode_validation`, it executes all validation test functions automatically without requiring CLI flags.
