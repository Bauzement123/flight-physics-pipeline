"""
Developer Utility: Trajectory Manager CLI

Manages raw and clean trajectory datasets for flight corridor cohorts.

Sub-commands:
    pack      Append loose single-flight Parquets into a cohort batch archive.
              Only flights NOT already present in the archive are appended.
              pack is a BACKUP operation — pack raw before deleting raw data.
    unpack    Extract single-flight Parquets from a cohort batch archive.
              Only flights with NO existing single file on disk are extracted.
              Automatically rebuilds the registry for the restored type.
              unpack is a RESTORE operation — recovers lost single-flight files.
    relabel   Re-apply OpenAP fuzzy-logic flight phase labels (raw files only).
              Converts SI units to nautical in-memory for OpenAP, writes SI back.

For registry rebuilds use the authoritative tool directly:
    python -m src.common.build_global_manifest --only raw
    python -m src.common.build_global_manifest --only clean
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.common.config import (
    TRAJECTORIES_DIR, BASE_DIR, init_runtime,
    RAW_TRAJECTORY_SUFFIX, RAW_CONCAT_SUFFIX, RAW_TRAJECTORY_DIRNAME,
    CLEAN_TRAJECTORY_SUFFIX, CLEAN_CONCAT_SUFFIX, CLEAN_TRAJECTORY_DIRNAME,
)
from src.common.utils import setup_file_logger
from src.common.adapters import df_si_to_df_nautic
from src.common.build_global_manifest import rebuild_raw_registry, rebuild_clean_registry
from src.core.fetching.helpers import build_expected_raw_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cohort discovery
# ---------------------------------------------------------------------------

def discover_cohort_directories(target_cohort: Optional[str] = None) -> List[Path]:
    """Discovers trajectory cohort directories under TRAJECTORIES_DIR."""
    if not TRAJECTORIES_DIR.exists():
        logger.error(f"Trajectories directory does not exist: {TRAJECTORIES_DIR}")
        return []

    if target_cohort:
        cohort_dir = TRAJECTORIES_DIR / target_cohort
        if cohort_dir.exists() and cohort_dir.is_dir():
            return [cohort_dir]
        matches = [d for d in TRAJECTORIES_DIR.rglob(target_cohort) if d.is_dir()]
        if matches:
            return matches
        logger.error(f"Cohort directory '{target_cohort}' not found under {TRAJECTORIES_DIR}")
        return []

    reserved = {"flight_lists", "flight_registry", "weather", "master_flight_paths", "original_raw"}
    return sorted(
        [d for d in TRAJECTORIES_DIR.iterdir() if d.is_dir() and d.name not in reserved],
        key=lambda p: p.name,
    )


# ---------------------------------------------------------------------------
# Core: virtual in-memory registry reconciliation
# ---------------------------------------------------------------------------

def find_all_fids(
    cohort_dir: Path,
    type_: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Scans concat archive and single-flight files for a cohort and trajectory type,
    building a virtual in-memory registry that reconciles both sources.

    Scan order is intentional:
      - Concat entries are appended FIRST  (→ lower deduplication priority)
      - Single-file entries appended SECOND (→ higher deduplication priority)

    Args:
        cohort_dir: Path to the cohort directory (e.g. data/trajectories/rank_143_EDDF-LIRF)
        type_: "raw" or "clean"

    Returns:
        pack_needed:   DataFrame (flight_id, path, source='single') of flights present
                       as single files but NOT yet in any concat archive.
                       → These need to be packed.
        unpack_needed: DataFrame (flight_id, path, source='concat') of flights that exist
                       ONLY in a concat archive with no corresponding single file on disk.
                       → These need to be unpacked.
    """
    if type_ == "raw":
        single_suffix = RAW_TRAJECTORY_SUFFIX    # "_raw.parquet"
        concat_suffix = RAW_CONCAT_SUFFIX        # "_all_raw.parquet"
        concat_dir = cohort_dir                  # raw concat lives at cohort root
        single_dir = cohort_dir / RAW_TRAJECTORY_DIRNAME
    else:
        single_suffix = CLEAN_TRAJECTORY_SUFFIX  # "_clean_si.parquet"
        concat_suffix = CLEAN_CONCAT_SUFFIX      # "_all_clean.parquet"
        concat_dir = cohort_dir                  # clean concat lives at cohort root
        single_dir = cohort_dir / CLEAN_TRAJECTORY_DIRNAME

    rows: list[dict] = []

    # 1. Concat entries FIRST → lower priority for keep='first', higher for keep='last'
    for concat_file in sorted(concat_dir.glob(f"*{concat_suffix}")):
        try:
            df_ids = pd.read_parquet(concat_file, columns=["flight_id"])
            for fid in df_ids["flight_id"].dropna().unique():
                rows.append({"flight_id": str(fid), "path": concat_file, "source": "concat"})
            logger.info(
                f"  [{type_}] Scanned concat {concat_file.name}: "
                f"{df_ids['flight_id'].dropna().nunique()} flight_ids"
            )
        except Exception as e:
            logger.error(f"Failed to read flight_ids from {concat_file.name}: {e}")

    # 2. Single-file entries SECOND → higher priority for keep='first'
    concat_suffixes = (concat_suffix,)
    single_count = 0
    if single_dir.exists():
        for f in sorted(single_dir.glob(f"*{single_suffix}")):
            if f.name.endswith(concat_suffixes):
                continue
            try:
                df_ids = pd.read_parquet(f, columns=["flight_id"])
                if df_ids.empty or df_ids["flight_id"].dropna().empty:
                    continue
                fid = str(df_ids["flight_id"].dropna().iloc[0])
                rows.append({"flight_id": fid, "path": f, "source": "single"})
                single_count += 1
            except Exception as e:
                logger.error(f"Failed to read flight_id from {f.name}: {e}")
    logger.info(f"  [{type_}] Scanned {single_count} single-flight files in {single_dir.name if single_dir.exists() else 'N/A'}")

    if not rows:
        empty = pd.DataFrame(columns=["flight_id", "path", "source"])
        return empty, empty

    df_all = pd.DataFrame(rows)

    # keep='first': concat rows came first → survive deduplication for already-packed flights
    # source='single' rows that survive = flights NOT in any concat → pack_needed
    packed_index = df_all.drop_duplicates(subset="flight_id", keep="first")
    pack_needed = packed_index[packed_index["source"] == "single"].copy()

    # keep='last': single rows came last → survive deduplication for flights in both
    # source='concat' rows that survive = flights with NO single file on disk → unpack_needed
    unpack_index = df_all.drop_duplicates(subset="flight_id", keep="last")
    unpack_needed = unpack_index[unpack_index["source"] == "concat"].copy()

    logger.info(
        f"  [{type_}] Reconciliation: {len(pack_needed)} flights need packing, "
        f"{len(unpack_needed)} flights need unpacking."
    )
    return pack_needed, unpack_needed


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------

def _concat_path(cohort_dir: Path, type_: str) -> Path:
    """Returns the canonical batch archive path for a cohort and type."""
    if type_ == "raw":
        return cohort_dir / f"{cohort_dir.name}{RAW_CONCAT_SUFFIX}"
    return cohort_dir / f"{cohort_dir.name}{CLEAN_CONCAT_SUFFIX}"


def _pack_type(cohort_dir: Path, type_: str, delete_originals: bool) -> None:
    """Packs a single cohort for one type (raw or clean)."""
    pack_needed, _ = find_all_fids(cohort_dir, type_)

    if pack_needed.empty:
        logger.info(f"  [{type_}] All flights already packed — nothing to do.")
        return

    batch_path = _concat_path(cohort_dir, type_)
    logger.info(f"  [{type_}] {len(pack_needed)} flights to pack into {batch_path.name}")

    dfs: list[pd.DataFrame] = []

    # Load existing batch first (to append, not overwrite)
    if batch_path.exists():
        try:
            dfs.append(pd.read_parquet(batch_path))
            logger.info(f"  [{type_}] Loaded existing batch ({len(dfs[0]):,} rows)")
        except Exception as e:
            logger.warning(f"  [{type_}] Could not read existing batch: {e} — will overwrite")

    # Read only the genuinely new files
    for _, row in pack_needed.iterrows():
        try:
            dfs.append(pd.read_parquet(row["path"]))
        except Exception as e:
            logger.error(f"  Failed to read {Path(row['path']).name}: {e}")

    if not dfs:
        logger.warning(f"  [{type_}] No data to write after loading files.")
        return

    df_combined = pd.concat(dfs, ignore_index=True)
    df_combined.to_parquet(batch_path, index=False)
    logger.info(f"  [{type_}] Wrote batch archive: {len(df_combined):,} rows → {batch_path}")

    if delete_originals:
        deleted = 0
        for _, row in pack_needed.iterrows():
            try:
                Path(row["path"]).unlink()
                deleted += 1
            except OSError as e:
                logger.error(f"  Could not delete {Path(row['path']).name}: {e}")
        logger.info(f"  [{type_}] Deleted {deleted} packed single-flight files.")


def handle_pack(args: argparse.Namespace) -> None:
    """Combines loose single-flight Parquets into cohort batch archives."""
    cohorts = discover_cohort_directories(args.cohort)
    if not cohorts:
        logger.warning("No target cohorts found for packing.")
        return

    types = _resolve_types(args.type)
    for cohort_dir in cohorts:
        logger.info(f"--- Packing cohort: {cohort_dir.name} ---")
        for t in types:
            _pack_type(cohort_dir, t, args.delete_originals)


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------

def _unpack_single_path(cohort_dir: Path, f_id: str, type_: str) -> Path:
    """Reconstructs the canonical single-flight file path from a flight_id."""
    if type_ == "raw":
        # flight_id format: {icao24}_{callsign}_{dep}-{arr}_{YYYYMMDD}_{HHMM}
        # split('_', 3) → [icao24, callsign, dep-arr, YYYYMMDD_HHMM]
        icao, callsign, _route, fs_str = f_id.split("_", 3)
        single_path, _ = build_expected_raw_path(
            route_dir=cohort_dir,
            icao24=icao,
            callsign=callsign,
            fs_str=fs_str,
        )
        return single_path
    else:
        # clean files follow the same icao_callsign_fs_str stem, different suffix
        icao, callsign, _route, fs_str = f_id.split("_", 3)
        clean_dir = cohort_dir / CLEAN_TRAJECTORY_DIRNAME
        clean_dir.mkdir(parents=True, exist_ok=True)
        # fs_str = "YYYYMMDD_HHMM" — same timestamp token as raw
        filename = f"{icao}_{callsign}_{fs_str}{CLEAN_TRAJECTORY_SUFFIX}"
        return clean_dir / filename


def _unpack_type(cohort_dir: Path, type_: str, target_fids: Optional[set], force: bool) -> None:
    """Unpacks a single cohort for one type (raw or clean)."""
    _, unpack_needed = find_all_fids(cohort_dir, type_)

    if unpack_needed.empty:
        logger.info(f"  [{type_}] No flights need unpacking.")
        return

    if target_fids:
        unpack_needed = unpack_needed[unpack_needed["flight_id"].isin(target_fids)]
        if unpack_needed.empty:
            logger.info(f"  [{type_}] None of the specified --fids need unpacking.")
            return

    logger.info(f"  [{type_}] {len(unpack_needed)} flights to unpack.")

    # Group by concat file for a single read per archive
    extracted_total = 0
    for concat_path, group in unpack_needed.groupby("path"):
        try:
            df_batch = pd.read_parquet(concat_path)
        except Exception as e:
            logger.error(f"  Failed to read concat {Path(str(concat_path)).name}: {e}")
            continue

        for _, row in group.iterrows():
            f_id = row["flight_id"]
            single_path = _unpack_single_path(cohort_dir, f_id, type_)

            if not force and single_path.exists():
                continue

            df_flight = df_batch[df_batch["flight_id"] == f_id].copy()
            if df_flight.empty:
                logger.warning(f"  flight_id {f_id} not found in {Path(str(concat_path)).name}")
                continue

            single_path.parent.mkdir(parents=True, exist_ok=True)
            df_flight.to_parquet(single_path, index=False)
            extracted_total += 1

    logger.info(f"  [{type_}] Extracted {extracted_total} single-flight files.")


def handle_unpack(args: argparse.Namespace) -> None:
    """Extracts loose single-flight Parquets from cohort batch archives."""
    cohorts = discover_cohort_directories(args.cohort)
    if not cohorts:
        logger.warning("No target cohorts found for unpacking.")
        return

    target_fids = set(args.fids.split(",")) if args.fids else None
    types = _resolve_types(args.type)

    for cohort_dir in cohorts:
        logger.info(f"--- Unpacking cohort: {cohort_dir.name} ---")
        for t in types:
            _unpack_type(cohort_dir, t, target_fids, args.force)

    # Rebuild only the registries that were touched
    if "raw" in types:
        logger.info("Rebuilding raw trajectory registry...")
        rebuild_raw_registry()
    if "clean" in types:
        logger.info("Rebuilding clean trajectory registry...")
        rebuild_clean_registry()


# ---------------------------------------------------------------------------
# relabel (raw only)
# ---------------------------------------------------------------------------

def apply_flight_phase_labeling(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies OpenAP fuzzy logic FlightPhase labeling to a trajectory DataFrame.
    Converts SI state vectors to nautical units temporarily in memory for OpenAP,
    attaches flight_phase labels to the original SI DataFrame, and returns it in SI units.
    """
    if df.empty:
        df["flight_phase"] = None
        return df

    try:
        from openap.phase import FlightPhase
    except ImportError:
        logger.error("openap package is required for flight phase labeling.")
        return df

    time_col = "time" if "time" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
    if not time_col:
        logger.warning("No time column found for flight phase labeling.")
        df["flight_phase"] = None
        return df

    df_sorted = df.sort_values(time_col).copy()

    # Convert SI → nautical in memory only, for OpenAP input
    df_nautic = df_si_to_df_nautic(df_sorted)

    try:
        fp = FlightPhase()
        phases = fp.label(
            stat=df_nautic["groundspeed"].values,
            alt=df_nautic["altitude"].values,
            roc=df_nautic["vertical_rate"].values,
        )
        # Attach labels back to the SI DataFrame — on-disk file remains in SI units
        df_sorted["flight_phase"] = phases
    except Exception as e:
        logger.error(f"FlightPhase labeling failed: {e}")
        df_sorted["flight_phase"] = None

    return df_sorted


def handle_relabel(args: argparse.Namespace) -> None:
    """Re-applies OpenAP flight phase labels to raw single-flight files only (SI units preserved)."""
    cohorts = discover_cohort_directories(args.cohort)
    if not cohorts:
        logger.warning("No target cohorts found for relabeling.")
        return

    for cohort_dir in cohorts:
        logger.info(f"--- Relabeling cohort: {cohort_dir.name} ---")
        raw_dir = cohort_dir / RAW_TRAJECTORY_DIRNAME
        if not raw_dir.exists():
            continue

        # Exclude concat/batch archives
        parquet_files = [
            f for f in raw_dir.glob(f"*{RAW_TRAJECTORY_SUFFIX}")
            if not f.name.endswith(RAW_CONCAT_SUFFIX)
        ]
        logger.info(f"  [raw] {len(parquet_files)} single-flight files to inspect.")

        relabeled_count = 0
        for p_file in parquet_files:
            try:
                df = pd.read_parquet(p_file)
                if "flight_phase" in df.columns and not args.force:
                    continue
                df_labeled = apply_flight_phase_labeling(df)
                df_labeled.to_parquet(p_file, index=False)
                relabeled_count += 1
            except Exception as e:
                logger.error(f"  Failed to relabel {p_file.name}: {e}")

        logger.info(f"  [raw] Relabeled {relabeled_count} files.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_types(type_arg: str) -> list[str]:
    """Resolves --type argument to a list of type strings."""
    if type_arg == "both":
        return ["raw", "clean"]
    return [type_arg]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    init_runtime()
    setup_file_logger(log_filename="acquisition.log")

    parser = argparse.ArgumentParser(
        description="Trajectory Dataset Manager — pack/unpack/relabel cohort trajectories"
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-command to execute")

    # pack
    p_pack = subparsers.add_parser(
        "pack",
        help="Append loose single-flight Parquets into a cohort batch archive (backup)"
    )
    p_pack.add_argument("--cohort", help="Target cohort name (default: all cohorts)")
    p_pack.add_argument(
        "--type", choices=["raw", "clean", "both"], default="raw",
        help="Trajectory type to pack (default: raw)"
    )
    p_pack.add_argument(
        "--delete-originals", action="store_true",
        help="Delete single-flight files after successful packing"
    )

    # unpack
    p_unpack = subparsers.add_parser(
        "unpack",
        help="Extract single-flight Parquets from a cohort batch archive (restore)"
    )
    p_unpack.add_argument("--cohort", help="Target cohort name (default: all cohorts)")
    p_unpack.add_argument(
        "--type", choices=["raw", "clean", "both"], default="raw",
        help="Trajectory type to unpack (default: raw)"
    )
    p_unpack.add_argument("--fids", help="Comma-separated flight_ids to unpack (default: all)")
    p_unpack.add_argument("--force", action="store_true", help="Overwrite existing single-flight files")

    # relabel
    p_relabel = subparsers.add_parser(
        "relabel",
        help="Re-apply OpenAP flight phase labels to raw single-flight files"
    )
    p_relabel.add_argument("--cohort", help="Target cohort name (default: all cohorts)")
    p_relabel.add_argument(
        "--force", action="store_true",
        help="Force relabeling even if flight_phase column already exists"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    {
        "pack": handle_pack,
        "unpack": handle_unpack,
        "relabel": handle_relabel,
    }[args.command](args)


if __name__ == "__main__":
    main()
