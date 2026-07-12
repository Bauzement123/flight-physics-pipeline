# Analytical Campaigns & Calibration Suite (`src/analysis/campaigns/`)

This package houses the analytical campaigns, data quality audits, hyperparameter optimization suites, and schema enrichment pipelines for the Flight Physics Pipeline. It is structured into domain-specific subpackages to ensure clean separation of concerns, maintainable namespaces, and rigorous documentation compliance.

---

## 1. Package Architecture & Index

```text
src/analysis/campaigns/
├── __init__.py                # Master package initialization
├── README.md                  # This architectural index and overview
├── common/                    # Shared plotting infrastructure & CLI wrappers
│   ├── README.md              # Technical guide for basemap caching & styling tokens
│   ├── plot_helpers.py
│   └── plot_cli.py
├── variational/               # Hyperparameter sweeps & PCA calibration suite
│   ├── README.md              # FAST tree & workflows for Phase A & Ground Truth sweeps
│   ├── phase_a_d_pca.py
│   ├── gt_stability_sweep.py
│   ├── variational_orchestrator.py
│   └── variational_plots.py
├── phase_quality/             # Flight phase quality & metadata filtering campaign
│   ├── README.md              # FAST tree & workflows for candidate extraction, audit reporting & EKF autopsy
│   ├── build_audit_candidate_pool.py
│   ├── phase_quality_filters.py
│   ├── run_phase_quality_campaign.py
│   ├── phase_quality_plots.py
│   ├── analyze_ekf_diagnostics.py  # Script 3: EKF diagnostic autopsy CLI
│   ├── diagnostics.py              # Pure-math phases (NIS, residuals, condition)
│   ├── io.py                       # Filesystem & PDF reporting for autopsy
│   └── orchestration.py            # ProcessPoolExecutor driver for autopsy workers
└── schema_enrichment/         # Phase schema sequence auditing & cost calibration
    ├── README.md              # FAST tree & workflows for schema sequence auditing
    └── phase_schema_orchestrator.py
```

---

## 2. Sub-Package Summary & Navigation

Each subpackage maintains its own dedicated `README.md` adhering strictly to **Rule 1 of `AGENTS.md`** (featuring Function Analysis Solution Trees, Mermaid data workflows with text walkthroughs, copy-pasteable Bash/PowerShell syntax blocks, and complete parameter reference tables).

### 2.1 Shared Plotting Infrastructure (`common/`)
- **Documentation**: [src/analysis/campaigns/common/README.md](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/analysis/campaigns/common/README.md)
- **Purpose**: Provides shared geospatial plotting utilities (`EuropeanMapCache`), standardized styling tokens, and command-line wrappers used across all campaigns.
- **Key Modules**: `plot_helpers.py`, `plot_cli.py`.

### 2.2 Variational Calibration Suite (`variational/`)
- **Documentation**: [src/analysis/campaigns/variational/README.md](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/analysis/campaigns/variational/README.md)
- **Purpose**: Calibrates spatial compression and clustering hyperparameters ($D_{PCA}$, $N_0$, $\tau$, and $K_{max}$) by benchmarking against Oracle Ground Truth medoid trajectories.
- **Key Modules**: `phase_a_d_pca.py`, `gt_stability_sweep.py`, `variational_orchestrator.py`, `variational_plots.py`.

### 2.3 Phase Quality Campaign Suite (`phase_quality/`)
- **Documentation**: [src/analysis/campaigns/phase_quality/README.md](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/analysis/campaigns/phase_quality/README.md)
- **Purpose**: Extracts representative candidate pools across European corridors, evaluates them against metadata pre-filters and trajectory post-filters (including coordinate-derived 3-D velocity filtering via `filter_max_coordinate_velocity()`), compiles multi-page visual audit PDF reports, and runs offline deep EKF tensor autopsies via the `analyze_ekf_diagnostics.py` CLI.
- **Key Modules**: `build_audit_candidate_pool.py`, `phase_quality_filters.py`, `run_phase_quality_campaign.py`, `phase_quality_plots.py`, `analyze_ekf_diagnostics.py`, `diagnostics.py`, `io.py`, `orchestration.py`.

### 2.4 Schema Enrichment Suite (`schema_enrichment/`)
- **Documentation**: [src/analysis/campaigns/schema_enrichment/README.md](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/analysis/campaigns/schema_enrichment/README.md)
- **Purpose**: Calibrates database query cost multipliers and acceptance rates required to obtain structurally valid trajectories whose flight phase labels follow canonical aeronautical sequence rules (`ONGROUND -> CLIMB -> CRUISE -> DESCENT -> ONGROUND`).
- **Key Modules**: `phase_schema_orchestrator.py`.

---

## 3. General Standards & Hygiene

1. **Centralized Configuration**: All campaign scripts import file paths, unit conversions, and physical constants exclusively from `src.common.config`. Hardcoding paths or numeric constants inline is strictly forbidden.
2. **Unified Logging**: Entrypoints must initialize logging via `setup_file_logger()` from `src.common.utils`.
3. **Multiprocessing & GUI Safety**: All plotting modules enforce non-interactive matplotlib backends (`matplotlib.use("Agg")`) at import time to prevent thread contention during multi-worker execution.
4. **Conventions & Registries**: For global pipeline naming rules, directory structures, and parquet schema definitions, refer to [conventions.md](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/conventions.md).
