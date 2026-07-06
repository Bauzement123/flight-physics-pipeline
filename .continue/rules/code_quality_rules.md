# Code Quality & Architecture Rules

This document defines the strict coding guidelines and architectural boundaries enforced across the Flight Physics Pipeline codebase. All developers and automated drafting engines must comply with these rules when writing or refactoring code.

---

## 1. Function Granularity & Size Limits

To maintain testability and ensure clean mapping to Function Analysis Solution Trees (FAST) and Mermaid workflows in module documentation:
- **Entrypoint Functions (`main`)**: Must not exceed **80 lines of code (LOC)**. Their sole responsibility is CLI argument parsing, top-level path resolution, orchestration, and logging initialization.
- **Helper & Worker Functions**: Must not exceed **50 lines of code (LOC)**. Each helper must perform a single logical operation (e.g., loading data, validating schema, applying algorithmic transforms, or writing output files).

---

## 2. Centralized Path Construction

- **No Scattered Path Logic**: Individual scripts must never re-implement project-relative path calculations (e.g., repeating `Path(BASE_DIR) / ... .resolve().relative_to(BASE_DIR)`).
- **Use Standardized Helpers**: Use centralized utilities such as `src.common.utils.get_relative_path(*parts)` or derive paths directly from canonical constants in `src.common.config`.

---

## 3. Pure Imports & No Filesystem Side-Effects

- **No I/O on Import**: Importing a Python module (such as `src.common.config` or any `src.core` script) must **never** mutate the filesystem, create directories, or write cache files.
- **Explicit Orchestration**: Required directories must be created explicitly at runtime by calling an infrastructure helper (e.g., `src.common.utils.ensure_dir_structure()`) inside the entrypoint orchestrator.

---

## 4. Idempotent Logging Setup

- **Single Initialization**: Only entrypoint scripts (`if __name__ == "__main__":`) may initialize logging handlers via `setup_file_logger()` from `src.common.utils`.
- **No `basicConfig`**: `logging.basicConfig(...)` is strictly forbidden anywhere in the repository.
- **Library Modules**: Functions and classes outside entrypoints must obtain loggers via `logging.getLogger(__name__)` without attaching handlers.
- **Handler Guarding**: Logging utilities must verify whether handlers are already attached before adding new `StreamHandler` or `FileHandler` instances to prevent duplicate log echoes.

---

## 5. Centralized Configuration & Constants

- **No Inline Numeric Magic Values**: Default algorithmic thresholds, unit conversion multipliers (`M_TO_FT`, `MPS_TO_KT`), retry counts (`ERA5_MAX_RETRIES`), and distance cutoffs (`MIN_DISTANCE_KM`) must be defined once in `src/common/config.py`.
- **Schema Whitelists**: Dataframe column whitelists and canonical rename maps shared across modules must live in centralized configuration or schema definition files.

---

## 6. Type Hints & API Contracts

- **Complete Annotations**: All public helper functions, class methods, and entrypoint signatures must include explicit Python type annotations (`-> None`, `-> pd.DataFrame`, `-> Path`, etc.).
- **Targeted Exception Handling**: Broad `except Exception:` catches must be avoided where possible. Catch specific exceptions (`FileNotFoundError`, `ValueError`, `KeyError`) or re-raise custom typed exceptions with clear logging context.
