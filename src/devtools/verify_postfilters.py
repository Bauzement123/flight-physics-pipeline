# src/devtools/verify_postfilters.py
import json
import pandas as pd
from src.common import config
from src.analysis.campaigns.phase_quality.phase_quality_filters import (
    apply_trajectory_postfilters,
    recompute_airport_distances,
    get_airport_coords,
)

def run_verification():
    print("=== Starting Post-Filter Verification ===")
    
    # 1. Load actual trajectory data
    # Flight ID: 3c0acc_CFG4FN_20250831_1352
    raw_path = config.BASE_DIR / "data/trajectories/rank_143_EDDF-LIRF/raw/3c0acc_CFG4FN_20250831_1352_raw.parquet"
    clean_path = config.BASE_DIR / "data/trajectories/rank_143_EDDF-LIRF/clean/3c0acc_CFG4FN_20250831_1352_clean_si.parquet"
    
    if not raw_path.exists() or not clean_path.exists():
        print(f"Error: Real test parquets not found on disk at:\nRaw: {raw_path}\nClean: {clean_path}")
        return
        
    df_raw = pd.read_parquet(raw_path)
    df_clean = pd.read_parquet(clean_path)
    
    # Get airport coords for EDDF -> LIRF
    airport_coords = get_airport_coords("EDDF", "LIRF")
    
    # 2. Re-compute airport distances
    df_clean_augmented = recompute_airport_distances(df_clean, airport_coords)
    
    # 3. Default run - should PASS
    rejected, reason, metrics = apply_trajectory_postfilters(
        df_clean_augmented, df_raw, config.DEFAULT_POSTFILTER_THRESHOLDS
    )
    print(f"\n[BASELINE DEFAULT RUN]")
    print(f"Rejected: {rejected} (should be False)")
    print(f"Reason: {reason}")
    print(f"Metrics: {json.dumps(metrics, indent=2)}")
    
    if rejected:
        print("FAIL: Baseline run failed post-filtering!")
        return
        
    # 4. Force Max Velocity violation
    bad_clean_vel = df_clean_augmented.copy()
    gs_col = "gs" if "gs" in bad_clean_vel.columns else ("velocity" if "velocity" in bad_clean_vel.columns else "gs")
    bad_clean_vel[gs_col] = 1000.0 # Force speed to 1000 kt
    
    rejected_vel, reason_vel, metrics_vel = apply_trajectory_postfilters(
        bad_clean_vel, df_raw, config.DEFAULT_POSTFILTER_THRESHOLDS
    )
    print(f"\n[MAX VELOCITY EXCEEDED RUN]")
    print(f"Rejected: {rejected_vel} (should be True)")
    print(f"Reason: {reason_vel}")
    print(f"Metrics: {json.dumps(metrics_vel, indent=2)}")
    
    if not rejected_vel:
        print("FAIL: Velocity filter failed to reject flight exceeding speed limit!")
        return
        
    # 5. Force 3D Acceleration (velocity spikes) violation
    bad_clean_acc = df_clean_augmented.copy()
    gs_col = "gs" if "gs" in bad_clean_acc.columns else ("velocity" if "velocity" in bad_clean_acc.columns else "gs")
    
    # Ensure index exists and is accessible
    bad_clean_acc.loc[bad_clean_acc.index[5], gs_col] = bad_clean_acc.loc[bad_clean_acc.index[5], gs_col] + 50000.0
    
    custom_thresholds = config.DEFAULT_POSTFILTER_THRESHOLDS.copy()
    custom_thresholds["max_velocity_kt"] = 1000000.0 # prevent speed short-circuit
    rejected_acc, reason_acc, metrics_acc = apply_trajectory_postfilters(
        bad_clean_acc, df_raw, custom_thresholds
    )
    print(f"\n[ACCELERATION EXCEEDED RUN (Velocity Spike)]")
    print(f"Rejected: {rejected_acc} (should be True)")
    print(f"Reason: {reason_acc}")
    print(f"Metrics: {json.dumps(metrics_acc, indent=2)}")
    
    if not rejected_acc:
        print("FAIL: Acceleration filter failed to reject flight with velocity spike!")
        return
        
    print("\n=== Post-Filter Verification PASSED Successfully! ===")

if __name__ == "__main__":
    run_verification()
