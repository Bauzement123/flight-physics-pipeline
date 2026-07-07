"""
build_audit_candidate_pool.py — Component 1 of Phase Quality Filter Campaign.

Extracts up to 400 candidate flights per target route from master_flights.parquet
and GLOBAL_TRAJECTORY_REGISTRY, computes duration and cohort mappings (10 cohorts
of 40 flights each), and atomically writes:
  - AUDIT_CANDIDATE_POOL_REGISTRY
  - AUDIT_COHORT_MAP_REGISTRY
"""

import logging
import sys
from pathlib import Path
import pandas as pd

from src.common.config import (
    MASTER_FLIGHTS_FILE,
    GLOBAL_TRAJECTORY_REGISTRY,
    AUDIT_CANDIDATE_POOL_REGISTRY,
    AUDIT_COHORT_MAP_REGISTRY,
    PHASE_QUALITY_REGISTRIES_DIR,
)
from src.common.utils import setup_file_logger
from src.core.fetching.helpers import build_flight_id, write_parquet_atomic

logger = logging.getLogger(__name__)

TARGET_ROUTES = [
    "EDDF-LIRF",
    "EGLL-BIKF",
    "ESSA-EHAM",
    "ESSA-LEMD",
    "LFRS-LFMN",
    "LGSA-LGAV",
]
FLIGHTS_PER_ROUTE = 400
FLIGHTS_PER_COHORT = 40


def build_candidate_pool() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Builds the audit candidate pool and cohort map DataFrames across all target routes.
    """
    if not MASTER_FLIGHTS_FILE.exists():
        logger.critical(f"Master flights file not found: {MASTER_FLIGHTS_FILE}")
        sys.exit(1)
    if not GLOBAL_TRAJECTORY_REGISTRY.exists():
        logger.critical(f"Global trajectory registry not found: {GLOBAL_TRAJECTORY_REGISTRY}")
        sys.exit(1)

    logger.info(f"Loading global trajectory registry from {GLOBAL_TRAJECTORY_REGISTRY.name}...")
    df_reg = pd.read_parquet(GLOBAL_TRAJECTORY_REGISTRY)
    reg_map = dict(zip(df_reg["flight_id"], df_reg["file_path"]))
    logger.info(f"Loaded {len(reg_map)} registered trajectory file paths.")

    logger.info(f"Loading master flights from {MASTER_FLIGHTS_FILE.name}...")
    df_master = pd.read_parquet(MASTER_FLIGHTS_FILE)
    logger.info(f"Loaded {len(df_master)} master flights records.")

    all_candidates = []
    all_cohort_maps = []

    for route in TARGET_ROUTES:
        dep, arr = route.split("-")
        logger.info(f"Processing candidate pool for route {route} ({dep} -> {arr})...")

        df_route = df_master[
            (df_master["estdepartureairport"] == dep) &
            (df_master["estarrivalairport"] == arr)
        ].copy()

        if df_route.empty:
            logger.warning(f"No master flights found for route {route}!")
            continue

        # Build flight_ids and match against global trajectory registry
        fids = []
        file_paths = []
        valid_indices = []

        for idx, row in df_route.iterrows():
            fid = build_flight_id(row)
            if fid and fid in reg_map:
                fids.append(fid)
                file_paths.append(reg_map[fid])
                valid_indices.append(idx)

        df_matched = df_route.loc[valid_indices].copy()
        df_matched["route_id"] = route
        df_matched["flight_id"] = fids
        df_matched["file_path"] = file_paths

        # Calculate duration in seconds
        df_matched["firstseen"] = pd.to_datetime(df_matched["firstseen"])
        df_matched["lastseen"] = pd.to_datetime(df_matched["lastseen"])
        df_matched["duration_s"] = (
            df_matched["lastseen"] - df_matched["firstseen"]
        ).dt.total_seconds()

        # Deterministic sort for reproducibility
        df_matched = df_matched.sort_values(by=["firstseen", "flight_id"]).reset_index(drop=True)

        if len(df_matched) > FLIGHTS_PER_ROUTE:
            df_matched = df_matched.head(FLIGHTS_PER_ROUTE).copy()
        elif len(df_matched) < FLIGHTS_PER_ROUTE:
            logger.warning(
                f"Route {route} only has {len(df_matched)} registered flights "
                f"(target: {FLIGHTS_PER_ROUTE})."
            )

        # Assign cohort_idx (1 to 10)
        df_matched["cohort_idx"] = [
            (i // FLIGHTS_PER_COHORT) + 1 for i in range(len(df_matched))
        ]

        logger.info(
            f"Route {route}: Selected {len(df_matched)} candidate flights across "
            f"{df_matched['cohort_idx'].max()} cohorts."
        )

        all_candidates.append(df_matched)
        df_map = df_matched[["route_id", "cohort_idx", "flight_id"]].copy()
        all_cohort_maps.append(df_map)

    if not all_candidates:
        logger.critical("No valid candidate flights found across any target route!")
        sys.exit(1)

    df_pool = pd.concat(all_candidates, ignore_index=True)
    df_cohorts = pd.concat(all_cohort_maps, ignore_index=True)

    return df_pool, df_cohorts


def main() -> None:
    setup_file_logger("calibration.log")
    logger.info("Starting Script 1: build_audit_candidate_pool...")

    df_pool, df_cohorts = build_candidate_pool()

    PHASE_QUALITY_REGISTRIES_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"Writing candidate pool ({len(df_pool)} rows) to {AUDIT_CANDIDATE_POOL_REGISTRY}...")
    write_parquet_atomic(df_pool, AUDIT_CANDIDATE_POOL_REGISTRY)

    logger.info(f"Writing cohort map ({len(df_cohorts)} rows) to {AUDIT_COHORT_MAP_REGISTRY}...")
    write_parquet_atomic(df_cohorts, AUDIT_COHORT_MAP_REGISTRY)

    logger.info("Successfully built audit candidate pool and cohort map!")


if __name__ == "__main__":
    main()
