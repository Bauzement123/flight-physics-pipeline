"""
add_route_to_master_flights.py — Migration Devtool

Adds a precomputed 'route' column ('DEP-ARR') to master_flights.parquet to
enable instant, O(1) single-column PyArrow dataset predicate pushdown without
requiring full upstream acquisition re-runs across all nodes.

Usage:
    # Upgrade canonical master_flights in-place (atomic replace)
    python -m src.devtools.add_route_to_master_flights

    # Upgrade to custom output path
    python -m src.devtools.add_route_to_master_flights --input path/to/file.parquet --output path/to/upgraded.parquet

    # Preview migration without writing
    python -m src.devtools.add_route_to_master_flights --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from src.common.config import MASTER_FLIGHTS_FILE
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)


def add_route_column(df: pd.DataFrame) -> pd.DataFrame:
    """Materializes precomputed 'route' column as 'DEP-ARR'."""
    if "estdepartureairport" not in df.columns or "estarrivalairport" not in df.columns:
        raise ValueError("Input DataFrame missing required 'estdepartureairport' or 'estarrivalairport' columns.")

    dep_clean = df["estdepartureairport"].fillna("").astype(str).str.strip()
    arr_clean = df["estarrivalairport"].fillna("").astype(str).str.strip()

    df["route"] = dep_clean + "-" + arr_clean

    mask_incomplete = (
        df["estdepartureairport"].isna()
        | df["estarrivalairport"].isna()
        | (df["route"] == "-")
        | (df["estdepartureairport"].astype(str).str.strip() == "")
        | (df["estarrivalairport"].astype(str).str.strip() == "")
    )
    df.loc[mask_incomplete, "route"] = None
    return df


def migrate_master_flights(
    input_path: Path,
    output_path: Optional[Path] = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Reads master_flights.parquet, appends 'route' column, and writes out."""
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    t0 = time.perf_counter()
    logger.info("Loading master flights from %s...", input_path)
    df = pd.read_parquet(input_path)
    read_t = time.perf_counter() - t0
    logger.info("Loaded %d rows (%d columns) in %.2fs.", len(df), len(df.columns), read_t)

    t_add = time.perf_counter()
    df = add_route_column(df)
    logger.info(
        "Added 'route' column in %.3fs. Unique routes: %d, Null routes: %d.",
        time.perf_counter() - t_add,
        df["route"].nunique(dropna=True),
        df["route"].isna().sum(),
    )

    if dry_run:
        logger.info("[DRY-RUN] Preview complete. No files modified.")
        return df

    target_out = output_path or input_path
    target_out.parent.mkdir(parents=True, exist_ok=True)

    t_write0 = time.perf_counter()
    if output_path and output_path != input_path:
        logger.info("Writing upgraded dataset to %s...", target_out)
        df.to_parquet(target_out, index=False)
    else:
        # Atomic in-place write via temporary sibling file
        tmp_path = target_out.with_name(f"{target_out.stem}_tmp_{os.getpid()}{target_out.suffix}")
        logger.info("Writing temporary dataset to %s...", tmp_path)
        df.to_parquet(tmp_path, index=False)
        logger.info("Replacing %s atomically with upgraded dataset...", target_out)
        os.replace(tmp_path, target_out)

    write_t = time.perf_counter() - t_write0
    total_t = time.perf_counter() - t0
    logger.info(
        "Successfully migrated %s in %.2fs (write: %.2fs, total: %.2fs). Final size: %.2f MB.",
        target_out,
        total_t,
        write_t,
        total_t,
        target_out.stat().st_size / (1024 * 1024),
    )
    return df


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.devtools.add_route_to_master_flights",
        description="Append precomputed 'route' column to master_flights.parquet.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=MASTER_FLIGHTS_FILE,
        help="Path to input master_flights.parquet.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to output parquet. If omitted, overwrites --input atomically.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform column addition in memory without writing to disk.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    setup_file_logger(log_filename="acquisition.log")
    args = parse_args(argv)
    migrate_master_flights(
        input_path=args.input,
        output_path=args.output,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
