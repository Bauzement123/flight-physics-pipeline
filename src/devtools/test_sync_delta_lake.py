"""test_sync_delta_lake.py — Validation test suite for sync_delta_lake utility
=============================================================================

Tests:
  1. Bootstrapping a non-existent destination lake.
  2. Incremental upsert with row overlap (only net-new SIM_FIDs appended).
  3. Incremental downsert (pulling delta from remote to local).
  4. Dry-run mode verification (zero disk modifications).
  5. Compaction and Z-ordering via maintain command.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

from src.data_manager.sync_delta_lake import run_sync, run_maintain


def make_dummy_sim_df(fids: list[str], route: str = "EBBR-EGLL", dep_date: int = 20240101) -> pd.DataFrame:
    """Generate dummy simulation waypoint dataframe matching schema."""
    rows = []
    for fid in fids:
        for t in [0.0, 10.0, 20.0]:
            rows.append({
                "SIM_FID": fid,
                "model_config_id": "BASE_B738",
                "route": route,
                "dep_date": dep_date,
                "time": t,
                "latitude": 50.0 + t * 0.01,
                "longitude": 4.0 + t * 0.01,
                "altitude_m": 10000.0,
                "FL": 330.0,
                "EF_total": 1200.0 + t * 5.0,
            })
    return pd.DataFrame(rows)


def main():
    temp_dir = Path(tempfile.mkdtemp(prefix="test_delta_sync_"))
    local_dir = temp_dir / "local_lake"
    smb_dir = temp_dir / "smb_lake"

    try:
        print("=== Test 1: Bootstrap Upsert (Local -> SMB) ===")
        df_local_init = make_dummy_sim_df(["FID_001", "FID_002", "FID_003"])
        write_deltalake(str(local_dir), df_local_init, mode="overwrite")
        
        synced = run_sync(
            local_dir=local_dir,
            smb_dir=smb_dir,
            direction="upsert",
            batch_size=1000,
        )
        assert synced == len(df_local_init), f"Expected {len(df_local_init)} rows, got {synced}"
        
        dest_dt = DeltaTable(str(smb_dir))
        dest_df = dest_dt.to_pandas()
        assert len(dest_df) == len(df_local_init), f"Mismatch in bootstrapped rows: {len(dest_df)}"
        print("[OK] Test 1 passed: Bootstrapped successfully.\n")

        print("=== Test 2: In-Sync Check (0 bytes transferred) ===")
        synced = run_sync(
            local_dir=local_dir,
            smb_dir=smb_dir,
            direction="upsert",
            batch_size=1000,
        )
        assert synced == 0, f"Expected 0 rows when already in sync, got {synced}"
        print("[OK] Test 2 passed: 0 rows transferred on identical tables.\n")

        print("=== Test 3: Incremental Upsert with Overlapping SIM_FIDs ===")
        # Local gets FID_002 (overlap), FID_003 (overlap), FID_004 (new), FID_005 (new)
        df_local_batch2 = make_dummy_sim_df(["FID_002", "FID_003", "FID_004", "FID_005"])
        write_deltalake(str(local_dir), df_local_batch2, mode="append")

        # Test Dry Run first
        dry_synced = run_sync(
            local_dir=local_dir,
            smb_dir=smb_dir,
            direction="upsert",
            dry_run=True,
            batch_size=1000,
        )
        expected_new_rows = 2 * 3  # FID_004 and FID_005, 3 waypoints each = 6 rows
        assert dry_synced == expected_new_rows, f"Expected {expected_new_rows} in dry-run, got {dry_synced}"
        
        # Verify SMB table unchanged during dry run
        dest_dt = DeltaTable(str(smb_dir))
        assert len(dest_dt.to_pandas()) == 9, "SMB table was modified during dry run!"

        # Execute actual Upsert
        synced = run_sync(
            local_dir=local_dir,
            smb_dir=smb_dir,
            direction="upsert",
            dry_run=False,
            batch_size=1000,
        )
        assert synced == expected_new_rows, f"Expected {expected_new_rows} synced, got {synced}"
        
        dest_df_after = DeltaTable(str(smb_dir)).to_pandas()
        unique_fids = set(dest_df_after["SIM_FID"])
        assert unique_fids == {"FID_001", "FID_002", "FID_003", "FID_004", "FID_005"}, f"Unexpected FIDs: {unique_fids}"
        assert len(dest_df_after) == 15, f"Expected 15 total rows, got {len(dest_df_after)}"
        print("[OK] Test 3 passed: Incremental overlap sync appended only net-new FIDs.\n")

        print("=== Test 4: Downsert Sync (SMB -> Local) ===")
        # Simulate another node adding FID_006 and FID_007 to SMB share
        df_remote_batch = make_dummy_sim_df(["FID_006", "FID_007"])
        write_deltalake(str(smb_dir), df_remote_batch, mode="append")

        # Pull down to local
        downserted = run_sync(
            local_dir=local_dir,
            smb_dir=smb_dir,
            direction="downsert",
            batch_size=1000,
        )
        assert downserted == 6, f"Expected 6 rows downserted, got {downserted}"
        
        local_df_final = DeltaTable(str(local_dir)).to_pandas()
        local_fids = set(local_df_final["SIM_FID"])
        assert "FID_006" in local_fids and "FID_007" in local_fids, "Downsert failed to pull new remote keys!"
        print("[OK] Test 4 passed: Downsert pulled remote additions cleanly.\n")

        print("=== Test 5: Maintenance (Compaction & Z-Ordering) ===")
        run_maintain(
            lake_dir=smb_dir,
            target_size_mb=1,
            z_order_cols=["dep_date", "route"],
            skip_vacuum=True,
        )
        dest_dt_maint = DeltaTable(str(smb_dir))
        assert len(dest_dt_maint.to_pandas()) == 21, "Compaction corrupted row count!"
        print("[OK] Test 5 passed: Maintain consolidated and Z-ordered successfully.\n")

        print("ALL TESTS PASSED SUCCESSFULLY!")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
