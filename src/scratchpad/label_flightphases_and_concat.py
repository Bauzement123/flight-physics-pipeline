"""
Batch Flight Phase Labeler & Parquet Re-concatenator.

Reads individual raw trajectory Parquet files, converts units to nautical/aviation standard,
applies OpenAP phase labeling (FlightPhase), and re-concatenates into clean batch Parquet files.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Pre-import OpenAP FlightPhase at module top-level so it is NEVER re-imported or re-loaded inside loops!
from openap.phase import FlightPhase

# Centralized project configuration and utilities (§3 & §4 in AGENTS.md)
from src.common.config import TRAJECTORIES_DIR, LOGS_DIR
from src.common.adapters import df_si_to_df_nautic
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)


def label_flight_phase(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies OpenAP fuzzy logic phase labeling (CLIMB, CRUISE, DESCENT, LEVEL, GROUND)
    to an individual flight trajectory DataFrame.
    
    Includes detailed diagnostic logging of time formats, conversion steps, and trajectory arrays.
    """
    if df.empty:
        df["flight_phase"] = None
        return df

    # 1. Identify time column and sort chronologically
    time_col = "time" if "time" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
    if time_col is None:
        logger.warning("No time or timestamp column found in DataFrame. Skipping phase labeling.")
        df["flight_phase"] = None
        return df

    df_sorted = df.sort_values(time_col).copy()
    logger.debug("  -> [After Loading/Sorting] col='%s', dtype=%s, min=%s, max=%s",
                 time_col, df_sorted[time_col].dtype, df_sorted[time_col].min(), df_sorted[time_col].max())

    # 2. Normalize to nautical/OpenAP units.
    # Raw OpenSky SI columns: baroaltitude, velocity, vertrate
    # Clean pycontrails SI columns: altitude, gs, rocd
    # Existing traffic/aviation columns: altitude, groundspeed, vertical_rate
    si_schema_markers = {"baroaltitude", "velocity", "vertrate", "gs", "rocd"}
    if any(col in df_sorted.columns for col in si_schema_markers):
        df_nautic = df_si_to_df_nautic(df_sorted)
    else:
        df_nautic = df_sorted.copy()
        if "time" in df_nautic.columns and "timestamp" not in df_nautic.columns:
            df_nautic = df_nautic.rename(columns={"time": "timestamp"})

    ts_col = "timestamp" if "timestamp" in df_nautic.columns else "time"
    logger.debug("  -> [After Nautic Conversion] col='%s', dtype=%s, min=%s, max=%s",
                 ts_col, df_nautic[ts_col].dtype, df_nautic[ts_col].min(), df_nautic[ts_col].max())

    # 3. Verify required nautical columns exist
    required_nautic = ["altitude", "groundspeed", "vertical_rate"]
    if any(col not in df_nautic.columns for col in required_nautic):
        logger.warning("Missing required nautical columns %s. Skipping phase labeling.", required_nautic)
        df_sorted["flight_phase"] = None
        return df_sorted.reset_index(drop=True)

    # 4. Extract elapsed time seconds and state vectors
    ts_series = df_nautic[ts_col]
    try:
        # Works for both NumPy datetime64 and PyArrow timestamp[ns][pyarrow] / timedelta
        ts_raw = (ts_series - ts_series.iloc[0]).dt.total_seconds()
    except AttributeError:
        # Fallback for raw numeric epoch timestamps
        ts_diff = (ts_series - ts_series.iloc[0]).astype(float)
        if len(ts_diff) > 0 and np.max(ts_diff) > 86400 * 10:
            if np.max(ts_diff) > 1e11:  # nanoseconds
                ts_raw = ts_diff / 1e9
            elif np.max(ts_diff) > 1e8: # microseconds
                ts_raw = ts_diff / 1e6
            else:                       # milliseconds
                ts_raw = ts_diff / 1e3
        else:
            ts_raw = ts_diff

    # Force strict conversion to pure NumPy float64 array (converting PyArrow ArrowExtensionArray if present)
    ts = np.asarray(ts_raw, dtype=float)

    logger.debug("  -> [After Extracting ts] dtype=%s, len=%d, min=%.4f, max=%.4f",
                 ts.dtype, len(ts), np.min(ts), np.max(ts))

    # Fill isolated NaNs and force pure NumPy float64 arrays for OpenAP resilience
    alt = np.asarray(pd.Series(df_nautic["altitude"]).ffill().bfill(), dtype=float)
    spd = np.asarray(pd.Series(df_nautic["groundspeed"]).ffill().bfill(), dtype=float)
    rocd = np.asarray(pd.Series(df_nautic["vertical_rate"]).ffill().bfill(), dtype=float)

    # 5. Execute OpenAP FlightPhase
    fp = FlightPhase()
    fp.set_trajectory(ts, alt, spd, rocd)
    logger.debug("  -> [After set_trajectory] fp.ts min=%.4f, max=%.4f, len=%d",
                 np.min(fp.ts), np.max(fp.ts), len(fp.ts))

    labels = fp.phaselabel()

    # 6. Assign directly to column array (avoiding .loc index alignment) and reset index
    df_sorted["flight_phase"] = labels
    df_sorted["phase"] = labels
    return df_sorted.reset_index(drop=True)


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """Writes a DataFrame to Parquet via temp file + os.replace()."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".tmp.{uuid.uuid4().hex}.parquet")
    try:
        df.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def label_file(raw_file_path: Path, force: bool = False) -> bool:
    """
    Reads an individual raw Parquet flight file, checks if phase labeling is needed,
    applies label_flight_phase(), and saves atomically back to the file.
    
    Returns True if labeled (or already labeled), False on error.
    """
    try:
        if not force:
            # Check schema first without loading data if possible
            schema = pq.read_schema(raw_file_path)
            if "flight_phase" in schema.names:
                logger.debug("File %s already has flight_phase. Skipping (use --force to overwrite).", raw_file_path.name)
                return True
        
        df = pd.read_parquet(raw_file_path)
        df_labeled = label_flight_phase(df)
        write_parquet_atomic(df_labeled, raw_file_path)
        logger.info("Successfully labeled and saved %s (%d rows)", raw_file_path.name, len(df_labeled))
        return True
    except Exception as e:
        logger.error("Failed to label file %s: %s", raw_file_path, e, exc_info=True)
        return False


def label_files_from_manifest(manifest_path: Path, force: bool = False) -> pd.DataFrame:
    """Relabels files listed in repacking_clean_move_manifest.tsv."""
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    df_manifest = pd.read_csv(manifest_path, sep="\t")
    if "source_path" not in df_manifest.columns:
        raise ValueError(f"Manifest must contain a source_path column: {manifest_path}")

    records = []
    for _, row in df_manifest.iterrows():
        source_path = Path(str(row["source_path"]))
        status = "OK"
        reason = ""
        n_rows = 0
        had_phase_before = False
        has_phase_after = False

        try:
            if not source_path.exists():
                raise FileNotFoundError(f"Missing source file: {source_path}")

            schema = pq.read_schema(source_path)
            had_phase_before = "flight_phase" in schema.names or "phase" in schema.names

            if had_phase_before and not force:
                status = "SKIPPED"
                reason = "already has phase labels"
            else:
                df = pd.read_parquet(source_path)
                n_rows = len(df)
                df_labeled = label_flight_phase(df)
                has_phase_after = "flight_phase" in df_labeled.columns and df_labeled["flight_phase"].notna().any()
                write_parquet_atomic(df_labeled, source_path)
                reason = "labeled"

        except Exception as exc:
            status = "ERROR"
            reason = str(exc)

        records.append({
            "source_path": str(source_path),
            "target_path": row.get("target_path", ""),
            "filename": row.get("filename", source_path.name),
            "route": row.get("route", ""),
            "status": status,
            "reason": reason,
            "n_rows": n_rows,
            "had_phase_before": had_phase_before,
            "has_phase_after": has_phase_after,
        })

    return pd.DataFrame(records)


def label_corridor_files(corridor_dir: Path, force: bool = False) -> bool:
    """
    Pass 1: Iterates over all raw flight files in a corridor directory (corridor_dir/raw/*_raw.parquet)
    and labels each file via label_file().
    """
    if not corridor_dir.is_dir():
        logger.error("Corridor path is not a directory: %s", corridor_dir)
        return False

    raw_dir = corridor_dir / "raw"
    if not raw_dir.exists() or not raw_dir.is_dir():
        logger.warning("No 'raw' subdirectory found in %s", corridor_dir.name)
        return False

    raw_files = sorted(raw_dir.glob("*.parquet"))
    if not raw_files:
        logger.warning("No parquet files found in %s", raw_dir)
        return False

    logger.info("Labeling %d raw flight files in corridor %s...", len(raw_files), corridor_dir.name)
    success_count = 0

    for file_path in raw_files:
        if label_file(file_path, force=force):
            success_count += 1

    logger.info("Labeled %d / %d files in %s.", success_count, len(raw_files), corridor_dir.name)
    return success_count > 0


def concat_corridor_files(corridor_dir: Path) -> bool:
    """
    Pass 2: Reads all labeled flight files from corridor_dir/raw/*.parquet and concatenates them
    into corridor_dir / f"{corridor_dir.name}_all_raw.parquet".
    """
    raw_dir = corridor_dir / "raw"
    raw_files = sorted(raw_dir.glob("*.parquet"))
    if not raw_files:
        return False

    logger.info("Concatenating %d labeled flight files for corridor %s...", len(raw_files), corridor_dir.name)
    dfs = []
    for file_path in raw_files:
        try:
            df_f = pd.read_parquet(file_path)
            if not df_f.empty:
                dfs.append(df_f)
        except Exception as e:
            logger.warning("Could not load file %s for concatenation: %s", file_path.name, e)

    if not dfs:
        logger.error("No valid labeled data loaded to concatenate for corridor %s", corridor_dir.name)
        return False

    df_all = pd.concat(dfs, ignore_index=True)
    master_path = corridor_dir / f"{corridor_dir.name}_all_raw.parquet"
    write_parquet_atomic(df_all, master_path)
    logger.info("Successfully created master file %s (%d total rows across %d flights)",
                master_path.name, len(df_all), len(dfs))
    return True


def main():
    """Main CLI entrypoint for batch flight phase labeling and re-concatenation."""
    setup_file_logger(LOGS_DIR, "processing.log")
    
    parser = argparse.ArgumentParser(description="Batch Flight Phase Labeler & Parquet Re-concatenator")
    parser.add_argument("--rank-pattern", type=str, default="rank_*",
                        help="Glob pattern for corridor directories in TRAJECTORIES_DIR (default: 'rank_*')")
    parser.add_argument("--force", action="store_true",
                        help="Force re-labeling even if flight_phase column already exists")
    parser.add_argument(
        "--manifest",
        type=str,
        default=None,
        help="Optional TSV move manifest. If provided, relabel only files listed in source_path.",
    )
    parser.add_argument(
        "--report",
        type=str,
        default="data/temp/plans/repacking_phase_relabel_report.tsv",
        help="Output TSV report for manifest-based relabeling.",
    )
    args = parser.parse_args()

    if args.manifest:
        logger.info("Running manifest-based repacking phase relabeling.")
        report_df = label_files_from_manifest(Path(args.manifest), force=args.force)
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_df.to_csv(report_path, sep="\t", index=False)

        ok_count = int((report_df["status"] == "OK").sum())
        skipped_count = int((report_df["status"] == "SKIPPED").sum())
        error_count = int((report_df["status"] == "ERROR").sum())

        logger.info(
            "Manifest relabeling complete. OK=%d | SKIPPED=%d | ERROR=%d | report=%s",
            ok_count,
            skipped_count,
            error_count,
            report_path,
        )

        if error_count > 0:
            sys.exit(1)
        return

    corridors = sorted(TRAJECTORIES_DIR.glob(args.rank_pattern))
    if not corridors:
        logger.error("No corridor directories found matching pattern '%s' in %s", args.rank_pattern, TRAJECTORIES_DIR)
        sys.exit(1)

    logger.info("==================================================")
    logger.info("STARTING BATCH FLIGHT PHASE LABELING & CONCATENATION")
    logger.info("Pattern: '%s' | Corridors Found: %d | Force: %s", args.rank_pattern, len(corridors), args.force)
    logger.info("==================================================")

    t_start = time.time()
    
    # PASS 1: Label individual flight files across all matching corridors
    logger.info("=== PASS 1: LABELING INDIVIDUAL FLIGHT FILES ===")
    completed_corridors = []
    for corridor_dir in corridors:
        if not corridor_dir.is_dir():
            continue
        logger.info("--- [Pass 1] Labeling Corridor: %s ---", corridor_dir.name)
        if label_corridor_files(corridor_dir, force=args.force):
            completed_corridors.append(corridor_dir)

    # PASS 2: Re-concatenate master files across all completed corridors
    logger.info("=== PASS 2: RE-CONCATENATING MASTER CORRIDOR FILES ===")
    success_count = 0
    for corridor_dir in completed_corridors:
        logger.info("--- [Pass 2] Concatenating Corridor: %s ---", corridor_dir.name)
        if concat_corridor_files(corridor_dir):
            success_count += 1

    total_time = time.time() - t_start
    logger.info("==================================================")
    logger.info("BATCH PROCESSING COMPLETED in %.2f seconds", total_time)
    logger.info("Successfully processed %d / %d corridors.", success_count, len(corridors))
    logger.info("==================================================")

    if success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
