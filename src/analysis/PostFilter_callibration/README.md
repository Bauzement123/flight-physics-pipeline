# Post-Filter Calibration (Stage 1-6)

## 1. Title & Introduction
The `PostFilter_calibration` module is an analytical testing suite designed to validate the physical accuracy, consistency, and downstream survival rates of the trajectory filtering pipeline (specifically the Kalman filter). It sweeps geographical, spatial, and bounding thresholds against both the clean pipeline registries and the raw OpenSky `master_flights.parquet` database to ensure our pipeline rules accurately represent true flight physics without inadvertently destroying valid routes due to sample variance, terrain masking, or Electronic Warfare (EW) spoofing.

## 2. Module Structure
```text
src/analysis/PostFilter_callibration/
├── README.md                           ← This document
├── stage1_directional_impact.py        ← Computes directional fail percentiles
├── stage2_subspace_validation.py       ← Cross-maps directional fail rates
├── stage3_undirected_correlation.py    ← Computes undirected covariance matrices
├── stage4_sensitivity_sweep.py         ← Sweeps config thresholds on clean cohort
├── stage5_master_sensitivity.py        ← Sweeps config thresholds on absolute raw database
└── stage6_prefilter_verification.py    ← Validates fetcher pre-filter estimation accuracy
```

## 3. Function Analysis Solution Tree (FAST)
* **Validate Pipeline Physical Reality**
  * **Identify Geographic Spoofing (EW)**: Execute Stage 1-3 to mathematically isolate undirected horizontal coordinate spoofing (e.g. Baltics/East Med) vs actual physics violations.
  * **Verify Terminal Thresholds**: Execute Stage 4 and Stage 5 to sweep the 15km/1000m radar limits across both the clean cohort and the massive raw database, proving the strict `config.py` limits are perfectly safe and broadly applicable without deleting entire nations like Switzerland.
  * **Audit Pre-filter Accuracy**: Execute Stage 6 to calculate Median Absolute Error (MedAE) between OpenSky's raw bulk estimates and the Kalman filter's physical reconstruction, ensuring the fetcher's pre-filters do not inadvertently reject valid flights.

## 4. Data Workflow

### 4.1 Calibration Sweep Workflow (`stage1` to `stage6`)

```mermaid
flowchart TD
    A[GLOBAL_CLEAN_QUALITY_REGISTRY] --> S1[Stage 1: Directional Percentiles]
    A --> S2[Stage 2: Subspace Cross-Mapping]
    A --> S3[Stage 3: Undirected Covariance]
    A --> S4[Stage 4: Discrete Sensitivity Sweep]
    
    B[master_flights.parquet] --> S5[Stage 5: Master Raw Sensitivity Sweep]
    B --> S6[Stage 6: Pre-filter Accuracy Join]
    A --> S6
    
    S1 --> C(stage1_directional.csv)
    S2 --> D(stage2_subspace.csv)
    S3 --> E(stage3_undirected.parquet)
    S4 --> F(stage4_sensitivity_sweep.parquet)
    S5 --> G(stage5_master_sensitivity.parquet)
    S6 --> H(stage6_prefilter_verification.csv)
```

**Step-by-step:**
1. **Stage 1 (Directional Percentiles)**: Ingests `GLOBAL_CLEAN_QUALITY_REGISTRY` to compute percentile distributions of threshold failures directionally (e.g., A->B vs B->A), outputting `stage1_directional.csv`.
2. **Stage 2 (Subspace Cross-Mapping)**: Uses the registry to cross-map horizontal failure rates against vertical failure rates, proving failures are geographic rather than altitude-dependent. Saves to `stage2_subspace.csv`.
3. **Stage 3 (Undirected Covariance)**: Analyzes the registry to compute covariance matrices (A, B, AUB) for macro-routes, isolating extreme directional divergence caused by regional EW spoofing. Saves to `stage3_undirected.parquet`.
4. **Stage 4 (Discrete Sensitivity Sweep)**: Sweeps physical configuration bounds (e.g., 15km to 50km) over the clean cohort to identify how survival rates decay, outputting `stage4_sensitivity_sweep.parquet`.
5. **Stage 5 (Master Raw Sensitivity Sweep)**: Bypasses the clean registry and loads the massive 3.2 million flight `master_flights.parquet` universe. Sweeps the strict physical limits to mathematically prove that regional "wipeouts" seen in Stage 4 were due to small sample sizes, not pipeline flaws. Saves to `stage5_master_sensitivity.parquet`.
6. **Stage 6 (Pre-filter Accuracy Join)**: Extracts flight keys from the clean registry and joins them with `master_flights.parquet`. Calculates the Median Absolute Error (MedAE) between raw OpenSky estimates and downstream physical values to prove the fetcher pre-filters do not reject valid flights. Saves to `stage6_prefilter_verification.csv`.

## 5. CLI Usage Guide
Currently, these analytical scripts are executed as one-off procedural runs without CLI arguments.

**PowerShell**
```powershell
python -m src.analysis.PostFilter_callibration.stage1_directional_impact
python -m src.analysis.PostFilter_callibration.stage2_subspace_validation
python -m src.analysis.PostFilter_callibration.stage3_undirected_correlation
python -m src.analysis.PostFilter_callibration.stage4_sensitivity_sweep
python -m src.analysis.PostFilter_callibration.stage5_master_sensitivity
python -m src.analysis.PostFilter_callibration.stage6_prefilter_verification
```

## 6. Prerequisites & Dependencies
* **Files Read**:
  * `data/databases/master_flights/master_flights_route_summary.parquet`
  * `data/databases/master_flights/master_flights.parquet`
  * `GLOBAL_CLEAN_QUALITY_REGISTRY` (via `src.common.registry_utils.load_clean_cohort`)
* **Constants Referenced** (from `config.py`):
  * `DEFAULT_PREFILTER_THRESHOLDS`
  * `BASE_DIR`

## 7. Future Technical Debt (TODO)
* **Massive Evals Integration**: The hardcoded validation limits and procedural checks across Stages 1-6 should be integrated directly into a centralized pipeline "Evaluation Gate" in code. Rather than acting as standalone analysis scripts, they should operate as automated integration tests that run on a subset of the data during future pipeline updates.
* **Code Modularization**: Consolidate repetitive joining, sorting, and ICAO extraction logic (specifically in Stages 5 & 6) into shared utility helpers in `src/analysis/`.
