"""sync_delta_lake.py — Distributed Delta Lake Synchronization & Maintenance Utility
===================================================================================

Synchronizes physics simulation Delta Lakes between local compute nodes and a
shared network file share (e.g. Windows SMB at \\\\PC182.ilr.rwth-aachen.de\\...
or mapped network drive Z:\\).

Key Guarantees:
  1. Directional Sync: Supports both 'upsert' (Local -> SMB) and 'downsert' (SMB -> Local).
  2. Memory Efficiency: Anti-join deduplication is performed purely in PyArrow/C++
     buffers (Zero CPython frozenset allocation).
  3. Safe Appends: Net-new rows are appended with an atomic _delta_log JSON commit.
     Existing 512 MiB Parquet files on the network share are NEVER re-written or moved.
  4. Decoupled Maintenance: Compaction and Z-ordering are decoupled into a dedicated
     'maintain' command run after all compute node runs complete.

Usage:
------
# 1. Push new local simulations to SMB share (Upsert):
python -m src.data_manager.sync_delta_lake sync \\
    --direction upsert \\
    --local-dir data/databases/simulation_lake \\
    --smb-dir "\\\\PC182.ilr.rwth-aachen.de\\studiert_ilr\\Kirste\\PA_ZeroCloud\\PythonPipeline\\data\\databases\\simulation_lake"

# 2. Pull new simulations from SMB share to local node (Downsert):
python -m src.data_manager.sync_delta_lake sync \\
    --direction downsert \\
    --local-dir data/databases/simulation_lake \\
    --smb-dir "Z:\\PythonPipeline\\data\\databases\\simulation_lake"

# 3. Consolidate and Z-order SMB lake after all batch runs finish (Maintain):
python -m src.data_manager.sync_delta_lake maintain \\
    --lake-dir "Z:\\PythonPipeline\\data\\databases\\simulation_lake"
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

import pyarrow as pa
import pyarrow.compute as pc
from deltalake import DeltaTable, write_deltalake

from src.common.config import DELTA_LAKE_TARGET_FILE_SIZE_BYTES
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core Synchronization Engine (PyArrow Streamed Anti-Join)
# ---------------------------------------------------------------------------

def extract_missing_arrow_table(
    src_lake_path: Path,
    dest_lake_path: Path,
    key_col: str = "SIM_FID",
    batch_size: int = 200_000,
) -> tuple[pa.Table | None, int, bool]:
    """Scan the source Delta Lake in Arrow batches and extract only rows not in dest.

    Uses native PyArrow C++ compute operations (pc.is_in) without materializing
    large Python string collections in CPython memory.

    Parameters
    ----------
    src_lake_path : Path
        Path to source Delta Lake directory.
    dest_lake_path : Path
        Path to destination Delta Lake directory.
    key_col : str, default "SIM_FID"
        Primary key column name used for anti-join.
    batch_size : int, default 200,000
        Number of rows to stream per Arrow RecordBatch.

    Returns
    -------
    tuple[pa.Table | None, int, bool]
        (accumulated_table_or_None, total_net_new_row_count, is_bootstrap)
    """
    dest_str = str(dest_lake_path)
    src_str = str(src_lake_path)

    # 1. Verify source exists
    if not src_lake_path.exists():
        raise FileNotFoundError(f"Source Delta Lake does not exist: {src_lake_path}")

    src_dt = DeltaTable(src_str)
    src_ds = src_dt.to_pyarrow_dataset()

    # 2. Check if destination exists. If not, everything is net-new (bootstrap).
    if not dest_lake_path.exists() or not (dest_lake_path / "_delta_log").exists():
        logger.info("Destination lake %s does not exist. Bootstrapping full source table...", dest_lake_path)
        full_table = src_ds.to_table()
        return full_table, full_table.num_rows, True

    try:
        dest_dt = DeltaTable(dest_str)
    except Exception as exc:
        logger.error(
            "Destination path exists at %s with _delta_log, but DeltaTable failed to load: %s. "
            "Refusing to sync to prevent data corruption or duplicate rows.",
            dest_lake_path,
            exc,
        )
        raise RuntimeError(f"Corrupted destination DeltaTable at {dest_lake_path}: {exc}") from exc

    # 3. Verify key column exists in both tables
    src_schema_names = set(src_dt.schema().to_arrow().names)
    dest_schema_names = set(dest_dt.schema().to_arrow().names)

    if key_col not in src_schema_names:
        raise ValueError(f"Key column '{key_col}' not found in source schema: {src_schema_names}")
    if key_col not in dest_schema_names:
        raise ValueError(f"Key column '{key_col}' not found in destination schema: {dest_schema_names}")

    # 4. Extract destination key column into a flat Arrow Array
    t0 = time.perf_counter()
    logger.info("Reading destination key column '%s' from %s...", key_col, dest_lake_path)
    dest_keys_table = dest_dt.to_pyarrow_dataset().to_table(columns=[key_col])
    dest_key_chunked = dest_keys_table.column(key_col)
    
    # Combine chunks into a single contiguous array for pc.unique and pc.is_in
    dest_key_combined = dest_key_chunked.combine_chunks()
    dest_unique_keys_array = pc.unique(dest_key_combined)
    t_dest = time.perf_counter() - t0
    logger.info(
        "Loaded %d unique destination key(s) in %.2fs (Arrow contiguous array buffer).",
        len(dest_unique_keys_array),
        t_dest,
    )

    # 5. Stream source batches and filter using Arrow native compute
    logger.info("Streaming source lake %s in batches of %d rows...", src_lake_path, batch_size)
    new_batches: list[pa.RecordBatch] = []
    total_scanned = 0
    total_new = 0

    t_scan_start = time.perf_counter()
    for batch in src_ds.to_batches(batch_size=batch_size):
        total_scanned += batch.num_rows
        batch_keys = batch.column(key_col)
        # Compute boolean mask against contiguous value_set array
        is_in_dest = pc.is_in(batch_keys, value_set=dest_unique_keys_array)
        mask = pc.invert(is_in_dest)
        
        filtered_batch = batch.filter(mask)
        n_filtered = filtered_batch.num_rows
        if n_filtered > 0:
            new_batches.append(filtered_batch)
            total_new += n_filtered

    t_scan = time.perf_counter() - t_scan_start
    logger.info(
        "Scan complete in %.2fs: examined %d row(s), found %d net-new row(s).",
        t_scan,
        total_scanned,
        total_new,
    )

    if not new_batches:
        return None, 0, False

    return pa.Table.from_batches(new_batches), total_new, False


def run_sync(
    local_dir: Path,
    smb_dir: Path,
    direction: str,
    key_col: str = "SIM_FID",
    batch_size: int = 200_000,
    dry_run: bool = False,
) -> int:
    """Execute directional Delta Lake synchronization.

    Parameters
    ----------
    local_dir : Path
        Local data lake directory.
    smb_dir : Path
        Remote / SMB network share lake directory.
    direction : {"upsert", "downsert"}
        Sync direction.
    key_col : str, default "SIM_FID"
        Primary key column.
    batch_size : int, default 200,000
        Streaming batch chunk size.
    dry_run : bool, default False
        If True, only scans and logs without writing.

    Returns
    -------
    int
        Number of net-new rows synchronized (or identified during dry-run).
    """
    if direction == "upsert":
        src_path = local_dir
        dest_path = smb_dir
        logger.info("=== SYNC START: UPSERT (Local -> SMB) ===")
    elif direction == "downsert":
        src_path = smb_dir
        dest_path = local_dir
        logger.info("=== SYNC START: DOWNSERT (SMB -> Local) ===")
    else:
        raise ValueError(f"Invalid direction '{direction}'. Must be 'upsert' or 'downsert'.")

    logger.info("Source:      %s", src_path)
    logger.info("Destination: %s", dest_path)
    logger.info("Key Column:  %s", key_col)
    logger.info("Dry Run:     %s", dry_run)

    new_table, total_new, is_bootstrap = extract_missing_arrow_table(
        src_lake_path=src_path,
        dest_lake_path=dest_path,
        key_col=key_col,
        batch_size=batch_size,
    )

    if total_new == 0 or new_table is None:
        logger.info("[OK] Destination is already 100%% in sync with Source. 0 bytes transferred.")
        return 0

    if dry_run:
        action = "bootstrap" if is_bootstrap else "append"
        logger.info(
            "[DRY-RUN] Would %s %d net-new row(s) (approx %.2f MB uncompressed) to %s.",
            action,
            total_new,
            new_table.nbytes / (1024 * 1024),
            dest_path,
        )
        return total_new

    # Write net-new data to destination Delta Lake
    t_write_start = time.perf_counter()
    dest_path.mkdir(parents=True, exist_ok=True)
    dest_str = str(dest_path)

    mode = "overwrite" if is_bootstrap else "append"

    logger.info(
        "Committing %d row(s) to %s (mode='%s', schema_mode='merge')...",
        total_new,
        dest_path,
        mode,
    )

    write_deltalake(
        dest_str,
        new_table,
        mode=mode,
        schema_mode="merge",
    )
    t_write = time.perf_counter() - t_write_start
    logger.info(
        "[OK] Successfully synchronized %d row(s) to %s in %.2fs.",
        total_new,
        dest_path,
        t_write,
    )
    return total_new


# ---------------------------------------------------------------------------
# Maintenance Engine (Compaction, Z-Ordering, Vacuum)
# ---------------------------------------------------------------------------

def run_maintain(
    lake_dir: Path,
    target_size_mb: int = 512,
    z_order_cols: Optional[List[str]] = None,
    vacuum_hours: int = 168,
    skip_vacuum: bool = False,
    force: bool = False,
) -> None:
    """Consolidate batch append fragments into optimal 512 MiB chunks with Z-ordering.

    Parameters
    ----------
    lake_dir : Path
        Path to the target Delta Lake directory.
    target_size_mb : int, default 512
        Target compacted Parquet file size in MiB.
    z_order_cols : list[str], optional
        Columns to Z-order on. Default is ``["dep_date", "route"]``.
    vacuum_hours : int, default 168
        Retention period in hours before unreferenced files are pruned.
    skip_vacuum : bool, default False
        If True, skips the VACUUM step.
    force : bool, default False
        If True, bypasses interactive confirmation for VACUUM.
    """
    if z_order_cols is None:
        z_order_cols = ["dep_date", "route"]

    target_size_bytes = target_size_mb * 1024 * 1024
    lake_str = str(lake_dir)

    if not lake_dir.exists() or not (lake_dir / "_delta_log").exists():
        logger.error("Delta Lake does not exist at %s. Cannot run maintenance.", lake_dir)
        sys.exit(1)

    logger.info("=== MAINTENANCE START ===")
    logger.info("Target Lake:      %s", lake_dir)
    logger.info("Target File Size: %d MiB (%d bytes)", target_size_mb, target_size_bytes)
    logger.info("Z-Order Columns:  %s", z_order_cols)

    dt = DeltaTable(lake_str)

    # 1. Compact small fragments into target size
    t0 = time.perf_counter()
    logger.info("Step 1/3: Compacting small append fragments...")
    dt.optimize.compact(target_size=target_size_bytes)
    logger.info("[OK] Compaction completed in %.2fs.", time.perf_counter() - t0)

    # 2. Apply multi-dimensional Z-ordering
    # Filter z_order_cols to those actually present in schema
    table_cols = set(dt.schema().to_arrow().names)
    active_z_cols = [col for col in z_order_cols if col in table_cols]

    if active_z_cols:
        t1 = time.perf_counter()
        logger.info("Step 2/3: Applying Z-ordering on %s...", active_z_cols)
        dt.optimize.z_order(active_z_cols, target_size=target_size_bytes)
        logger.info("[OK] Z-ordering completed in %.2fs.", time.perf_counter() - t1)
    else:
        logger.warning(
            "Step 2/3: Skipping Z-ordering — none of %s found in table columns: %s",
            z_order_cols,
            table_cols,
        )

    # 3. Vacuum (Orphan file purge)
    if skip_vacuum:
        logger.info("Step 3/3: Vacuum skipped (--no-vacuum flag).")
        logger.info("=== MAINTENANCE COMPLETE ===")
        return

    logger.info("Step 3/3: Evaluating orphaned files for VACUUM (retention=%d hours)...", vacuum_hours)
    try:
        dry_run_deleted = dt.vacuum(
            retention_hours=vacuum_hours,
            dry_run=True,
            enforce_retention_duration=False,
        )
        logger.info("Identified %d orphaned file(s) eligible for purge.", len(dry_run_deleted))

        if not dry_run_deleted:
            logger.info("No stale files to vacuum.")
            logger.info("=== MAINTENANCE COMPLETE ===")
            return

        should_purge = force
        if not force:
            logger.info("Dry-run preview found %d files to remove.", len(dry_run_deleted))
            confirm = input(f"Execute vacuum purge of {len(dry_run_deleted)} file(s)? [y/N]: ").strip().lower()
            should_purge = confirm in ("y", "yes")

        if should_purge:
            deleted = dt.vacuum(
                retention_hours=vacuum_hours,
                dry_run=False,
                enforce_retention_duration=False,
            )
            logger.info("[OK] Vacuum removed %d stale file(s).", len(deleted))
        else:
            logger.info("Vacuum purge aborted by user. Stale files preserved.")

    except Exception as exc:
        logger.warning("Vacuum operation encountered an error (%s) — continuing.", exc)

    logger.info("=== MAINTENANCE COMPLETE ===")


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m src.data_manager.sync_delta_lake",
        description="Directional Delta Lake Synchronization & Maintenance Utility for compute nodes via SMB.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Sub-command to execute")

    # --- Subcommand: sync ---
    sync_parser = subparsers.add_parser("sync", help="Synchronize net-new rows between local node and network share")
    sync_parser.add_argument(
        "--direction",
        type=str,
        choices=["upsert", "downsert"],
        required=True,
        help="Sync direction: 'upsert' (Local -> SMB) or 'downsert' (SMB -> Local)",
    )
    sync_parser.add_argument(
        "--local-dir",
        type=Path,
        required=True,
        help="Path to local Delta Lake directory (e.g. data/databases/simulation_lake)",
    )
    sync_parser.add_argument(
        "--smb-dir",
        type=Path,
        required=True,
        help="Path to network share Delta Lake directory (UNC path \\\\server\\... or mapped drive Z:\\...)",
    )
    sync_parser.add_argument(
        "--key-col",
        type=str,
        default="SIM_FID",
        help="Primary key column used for anti-join deduplication (default: SIM_FID)",
    )
    sync_parser.add_argument(
        "--batch-size",
        type=int,
        default=200_000,
        help="Number of rows per streaming Arrow batch during scan (default: 200,000)",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report net-new row count without writing any data",
    )
    sync_parser.add_argument(
        "--log-file",
        type=str,
        default="simulation.log",
        help="Name of log file in data/logs/ (default: simulation.log)",
    )

    # --- Subcommand: maintain ---
    maint_parser = subparsers.add_parser(
        "maintain",
        help="Consolidate append fragments into 512 MiB chunks, apply Z-ordering, and vacuum",
    )
    maint_parser.add_argument(
        "--lake-dir",
        "--smb-dir",
        dest="lake_dir",
        type=Path,
        required=True,
        help="Path to Delta Lake to compact and maintain (e.g. SMB network share path or local path)",
    )
    maint_parser.add_argument(
        "--target-size-mb",
        type=int,
        default=int(DELTA_LAKE_TARGET_FILE_SIZE_BYTES / (1024 * 1024)),
        help=f"Target file size in MiB for compaction (default: {int(DELTA_LAKE_TARGET_FILE_SIZE_BYTES / (1024 * 1024))} MiB)",
    )
    maint_parser.add_argument(
        "--z-order-cols",
        type=str,
        default="dep_date,route",
        help="Comma-separated columns to Z-order on (default: 'dep_date,route')",
    )
    maint_parser.add_argument(
        "--vacuum-hours",
        type=int,
        default=168,
        help="Retention period in hours for vacuum (default: 168 = 7 days)",
    )
    maint_parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip the vacuum step entirely",
    )
    maint_parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass interactive confirmation prompt for vacuum purge",
    )
    maint_parser.add_argument(
        "--log-file",
        type=str,
        default="simulation.log",
        help="Name of log file in data/logs/ (default: simulation.log)",
    )

    return parser


def main() -> None:
    """CLI Entrypoint for sync_delta_lake."""
    parser = build_parser()
    args = parser.parse_args()

    # Centralized logging setup
    setup_file_logger(log_filename=getattr(args, "log_file", "simulation.log"))

    if args.command == "sync":
        run_sync(
            local_dir=args.local_dir,
            smb_dir=args.smb_dir,
            direction=args.direction,
            key_col=args.key_col,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    elif args.command == "maintain":
        z_cols = [c.strip() for c in args.z_order_cols.split(",") if c.strip()]
        run_maintain(
            lake_dir=args.lake_dir,
            target_size_mb=args.target_size_mb,
            z_order_cols=z_cols,
            vacuum_hours=args.vacuum_hours,
            skip_vacuum=args.no_vacuum,
            force=args.force,
        )


if __name__ == "__main__":
    main()
