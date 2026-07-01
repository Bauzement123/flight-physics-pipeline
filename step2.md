# Phase 2: Calibration Sweep Tracking, Oracle Registries & Cache Hit Logic

This document details the goals, requirements, and design concepts for the next phase of the calibration and corridor modeling pipeline.

---

## 1. Objectives

1. **Calibration-Specific Cluster Map (`data/calibration/calibration_flight_cluster_map.parquet`)**
   * Keep track of every single sweep run's flight-to-cluster assignments.
   * **Schema columns:**
     * `route_id`: str (e.g. `EDDF-LIRF`)
     * `N_0`: int (initial query size)
     * `tau`: float (stability threshold)
     * `K_max`: int (max cluster constraint)
     * `replicate`: int (bootstrap run ID)
     * `flight_id`: str (historical flight ID)
     * `cluster_id`: int (assigned cluster)
     * `is_medoid`: bool (was this flight selected as the representative template?)

2. **Oracle Integration into Production Registry**
   * Since the Oracle Ground Truth is the "best" representation of the route structure, the Oracle medoids and flight-to-cluster assignments should be written directly to the main production registries:
     * `global_flight_cluster_map.parquet` (with a designation like `route_id = "ORACLE_<route>"` or a dedicated source flag).
     * `global_model_registry.parquet` (registered as the gold-standard reference corridor).

3. **Cache Hit Logic for Sweepers**
   * Utilize `calibration_flight_cluster_map.parquet` and the existing summary sheets as a cache database.
   * If a route is run through the sweeper with a hyperparameter combination ($N_0, \tau, K_{max}$) that has already been successfully evaluated, bypass the PCA and clustering calculations. Instead, load the assignments directly from the cache.

4. **Visual Mapping Tooling**
   * Develop a visualization module to analyze hyperparameter sensitivity.
   * Show how flight memberships shift between clusters or dissolve into the "Chaos" class as we adjust $\tau$ and $N_0$.

---

## 2. Technical Dependencies & Sequence
* **Step 1 (Current Step):** Implement the production flight-to-cluster registry (under the main `GLOBAL_FLIGHT_CLUSTER_MAP` path) to capture normal run assignments.
* **Step 2 (Next Step):** Extend the calibrator to serialize its sweeps to the calibration-specific map and integrate cache checks.
