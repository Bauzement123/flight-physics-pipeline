"""
Stage 7: Config Exclusion Verification
Analyzes the combined PC and VM registries against the active config limits
to ensure Western European corridors are not excessively penalized.
"""
import pandas as pd
import sys
from pathlib import Path
import logging

# Fix python path for src imports
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.append(str(BASE_DIR))

from src.common.config import (
    DEFAULT_POSTFILTER_THRESHOLDS,
    METRIC_COL_MAX_COORD_HORIZ_VEL,
    METRIC_COL_MAX_HORIZ_VEL,
    METRIC_COL_MAX_VERT_VEL,
    METRIC_COL_ARR_HORIZ_DIST,
    METRIC_COL_DEP_HORIZ_DIST,
    METRIC_COL_MAX_ACCEL,
    METRIC_COL_DEP_VERT_DIST,
    METRIC_COL_ARR_VERT_DIST
)
from src.common.utils import setup_file_logger, split_route_string


def main():
    setup_file_logger("calibration.log")
    logging.info("Starting Stage 7 Config Exclusion Verification")

    data_dir = BASE_DIR / "data" / "calibration" / "postfilter_calibration"
    pc_file = data_dir / "global_clean_quality_registry_PC.parquet"
    if not pc_file.exists():
        pc_file = data_dir / "data" / "sources" / "global_clean_quality_registry_PC.parquet"
    vm_file = data_dir / "global_clean_quality_registry_VM.parquet"
    if not vm_file.exists():
        vm_file = data_dir / "data" / "sources" / "global_clean_quality_registry_VM.parquet"

    # 1. Load Data
    logging.info(f"Loading {pc_file.name}...")
    df_pc = pd.read_parquet(pc_file)
    logging.info(f"Loading {vm_file.name}...")
    df_vm = pd.read_parquet(vm_file)

    df = pd.concat([df_pc, df_vm], ignore_index=True)
    logging.info(f"Combined total flights: {len(df)}")

    # Extract route from the flight_id (e.g. EDDF-LIRF_20240101_... -> EDDF-LIRF)
    df["route"] = df["flight_id"].apply(lambda fid: str(fid).split("_")[0])

    # Ensure canonical route exists
    def _to_canonical(r):
        dep, arr = split_route_string(r)
        return "-".join(sorted([dep, arr])) if dep != 'UNK' and arr != 'UNK' else r

    df["canonical_route"] = df["route"].apply(_to_canonical)

    # 2. Fetch Active Config Limits
    max_coord_kt = DEFAULT_POSTFILTER_THRESHOLDS["max_coord_horiz_velocity_kt"]
    max_horiz_kt = DEFAULT_POSTFILTER_THRESHOLDS["max_horiz_velocity_kt"]
    max_vert_fpm = DEFAULT_POSTFILTER_THRESHOLDS["max_vert_velocity_fpm"]
    max_accel = DEFAULT_POSTFILTER_THRESHOLDS["max_acceleration_mps2"]
    
    max_arr_dist = DEFAULT_POSTFILTER_THRESHOLDS["max_arr_horiz_dist"]
    max_dep_dist = DEFAULT_POSTFILTER_THRESHOLDS["max_dep_horiz_dist"]
    max_arr_vdist = DEFAULT_POSTFILTER_THRESHOLDS["max_arr_vert_dist"]
    max_dep_vdist = DEFAULT_POSTFILTER_THRESHOLDS["max_dep_vert_dist"]

    logging.info(f"Active Config Applied:")
    logging.info(f"  - Max Coord Horiz Vel: {max_coord_kt} kt")
    logging.info(f"  - Max Arr Horiz Dist:  {max_arr_dist} m")

    # 3. Apply Current Config Masks
    # Check if they pass all current limits
    pass_coord = (df[METRIC_COL_MAX_COORD_HORIZ_VEL].isna()) | (df[METRIC_COL_MAX_COORD_HORIZ_VEL] <= max_coord_kt)
    pass_horiz = (df[METRIC_COL_MAX_HORIZ_VEL].isna()) | (df[METRIC_COL_MAX_HORIZ_VEL] <= max_horiz_kt)
    pass_vert  = (df[METRIC_COL_MAX_VERT_VEL].isna()) | (df[METRIC_COL_MAX_VERT_VEL] <= max_vert_fpm)
    pass_accel = (df[METRIC_COL_MAX_ACCEL].isna()) | (df[METRIC_COL_MAX_ACCEL] <= max_accel)
    
    pass_arr   = (df[METRIC_COL_ARR_HORIZ_DIST].isna()) | (df[METRIC_COL_ARR_HORIZ_DIST] <= max_arr_dist)
    pass_dep   = (df[METRIC_COL_DEP_HORIZ_DIST].isna()) | (df[METRIC_COL_DEP_HORIZ_DIST] <= max_dep_dist)
    pass_varr  = (df[METRIC_COL_ARR_VERT_DIST].isna()) | (df[METRIC_COL_ARR_VERT_DIST] <= max_arr_vdist)
    pass_vdep  = (df[METRIC_COL_DEP_VERT_DIST].isna()) | (df[METRIC_COL_DEP_VERT_DIST] <= max_dep_vdist)

    df["passes_current_config"] = (
        pass_coord & pass_horiz & pass_vert & pass_accel & 
        pass_arr & pass_dep & pass_varr & pass_vdep
    )

    # 4. Compute Exclusion per Canonical Route
    stats = df.groupby("canonical_route").agg(
        total_flights=("flight_id", "count"),
        passed_flights=("passes_current_config", "sum")
    ).reset_index()

    stats["survival_pct"] = (stats["passed_flights"] / stats["total_flights"]) * 100
    stats["exclusion_pct"] = 100.0 - stats["survival_pct"]

    # Filter out routes with tiny sample sizes for a fair comparison
    stats = stats[stats["total_flights"] >= 10]
    stats_sorted = stats.sort_values("exclusion_pct", ascending=False)

    print("\n" + "="*80)
    print("STAGE 7: MOST PENALIZED REGIONS UNDER CURRENT CONFIG (>= 10 flights)")
    print("="*80)
    print(stats_sorted.head(20).to_string(index=False))

    print("\n" + "="*80)
    print("STAGE 7: WESTERN EUROPEAN SURVIVAL CHECK (Switzerland / Belgium)")
    print("="*80)
    we_routes = stats_sorted[
        stats_sorted["canonical_route"].str.contains("LS") | 
        stats_sorted["canonical_route"].str.contains("EB")
    ]
    print(we_routes.to_string(index=False))

    # Global survival
    global_survival = (df["passes_current_config"].sum() / len(df)) * 100
    print("\n" + "="*80)
    print(f"GLOBAL SURVIVAL RATE: {global_survival:.2f}%")
    print("="*80)
    
    # Save output
    out_dir = data_dir / "stage7"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "stage7_config_exclusion_results.csv"
    stats_sorted.to_csv(out_file, index=False)
    logging.info(f"Saved complete results to {out_file}")

if __name__ == "__main__":
    main()
