"""
Test R2-1: 3-Way Endpoint Distance Comparison Extraction Script

Builds a single data foundation parquet table containing 6 horizontal distance metrics
(Departure + Arrival) across the 3 data sources (OpenSky fd4 ground-truth, raw quality, clean quality)
for every flight in the clean quality registry.
"""

import sys
import time
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc
import pandas as pd

from src.common.config import (
    GLOBAL_CLEAN_QUALITY_REGISTRY,
    GLOBAL_RAW_QUALITY_REGISTRY,
    MASTER_FLIGHTS_FILE,
    REPORTS_DIR,
)
from src.core.fetching.helpers import parse_flight_id_components


def main():
    t0 = time.time()
    print("=" * 70)
    print("BUILDING R2-1 THREE-WAY DISTANCE COMPARISON TABLE (6 METRICS)")
    print("=" * 70)

    # 1. Load clean quality registry
    print(f"\n[Step 1/5] Loading clean quality registry from:\n  {GLOBAL_CLEAN_QUALITY_REGISTRY}")
    if not GLOBAL_CLEAN_QUALITY_REGISTRY.exists():
        raise FileNotFoundError(f"Clean quality registry missing: {GLOBAL_CLEAN_QUALITY_REGISTRY}")

    cq = pd.read_parquet(
        GLOBAL_CLEAN_QUALITY_REGISTRY,
        columns=["flight_id", "metric_dep_horiz_dist_m", "metric_arr_horiz_dist_m"],
    )
    cq = cq.rename(
        columns={
            "metric_dep_horiz_dist_m": "clean_dep_horiz_m",
            "metric_arr_horiz_dist_m": "clean_arr_horiz_m",
        }
    )
    print(f"  -> Loaded {len(cq):,} flights from clean quality registry.")

    # 2. Load raw quality registry
    print(f"\n[Step 2/5] Loading raw quality registry from:\n  {GLOBAL_RAW_QUALITY_REGISTRY}")
    if not GLOBAL_RAW_QUALITY_REGISTRY.exists():
        raise FileNotFoundError(f"Raw quality registry missing: {GLOBAL_RAW_QUALITY_REGISTRY}")

    rq = pd.read_parquet(
        GLOBAL_RAW_QUALITY_REGISTRY,
        columns=["flight_id", "metric_dep_horiz_dist_m", "metric_arr_horiz_dist_m"],
    )
    rq = rq.rename(
        columns={
            "metric_dep_horiz_dist_m": "raw_dep_horiz_m",
            "metric_arr_horiz_dist_m": "raw_arr_horiz_m",
        }
    )
    # Filter raw quality to target clean flight_ids
    rq = rq[rq["flight_id"].isin(set(cq["flight_id"]))]
    print(f"  -> Loaded raw quality metrics for {len(rq):,} matching flights.")

    # 3. Parse flight_id components for master_flights join
    print("\n[Step 3/5] Parsing flight_id components for master_flights join...")
    parsed_list = [parse_flight_id_components(fid) for fid in cq["flight_id"]]
    parsed_df = pd.DataFrame(parsed_list)

    cq["_icao24"] = parsed_df["icao24"]
    cq["_callsign"] = parsed_df["callsign"]
    cq["_dep"] = parsed_df["estdepartureairport"]
    cq["_arr"] = parsed_df["estarrivalairport"]
    cq["_fs_minute"] = parsed_df["firstseen_minute_str"]

    # 4. Read & prepare master_flights lookup
    print(f"\n[Step 4/5] Reading master_flights ground-truth from:\n  {MASTER_FLIGHTS_FILE}")
    if not MASTER_FLIGHTS_FILE.exists():
        raise FileNotFoundError(f"Master flights file missing: {MASTER_FLIGHTS_FILE}")

    mf_table = pq.read_table(
        MASTER_FLIGHTS_FILE,
        columns=[
            "icao24",
            "callsign",
            "estdepartureairport",
            "estarrivalairport",
            "firstseen",
            "estdepartureairporthorizdistance",
            "estarrivalairporthorizdistance",
        ],
    )

    cs_raw = mf_table["callsign"]
    dep_raw = mf_table["estdepartureairport"]
    arr_raw = mf_table["estarrivalairport"]

    cs_key = pc.coalesce(cs_raw, pa.scalar("UNK"))
    dep_key = pc.coalesce(dep_raw, pa.scalar("UNK"))
    arr_key = pc.coalesce(arr_raw, pa.scalar("UNK"))

    fs_s = pc.cast(mf_table["firstseen"], pa.timestamp("s")).cast(pa.int64())
    fs_minute = pc.divide(fs_s, 60).cast(pa.uint32())

    mf_df = pd.DataFrame({
        "_icao24": mf_table["icao24"].to_numpy().astype(str),
        "_callsign": cs_key.to_numpy().astype(str),
        "_dep": dep_key.to_numpy().astype(str),
        "_arr": arr_key.to_numpy().astype(str),
        "fs_min_int": fs_minute.to_numpy(),
        "fd4_dep_horiz_m": mf_table["estdepartureairporthorizdistance"].to_numpy(),
        "fd4_arr_horiz_m": mf_table["estarrivalairporthorizdistance"].to_numpy(),
    })

    # Clean whitespace & empty strings to match parse_flight_id_components
    mf_df["_callsign"] = mf_df["_callsign"].str.strip().replace("", "UNK")
    mf_df["_dep"] = mf_df["_dep"].str.strip().replace("", "UNK")
    mf_df["_arr"] = mf_df["_arr"].str.strip().replace("", "UNK")

    # Format fs_min_int to datetime string YYYYMMDD_HHMM
    # Convert minutes since epoch to pd.to_datetime
    dt_series = pd.to_datetime(mf_df["fs_min_int"] * 60, unit="s", utc=True)
    mf_df["_fs_minute"] = dt_series.dt.strftime("%Y%m%d_%H%M")
    mf_df = mf_df.drop(columns=["fs_min_int"])

    # 5. Join all 3 sources
    print("\n[Step 5/5] Performing 3-way join...")
    # Join master_flights onto clean quality
    merged = pd.merge(
        cq,
        mf_df,
        on=["_icao24", "_callsign", "_dep", "_arr", "_fs_minute"],
        how="left",
    )

    # Join raw quality onto clean quality by flight_id
    merged = pd.merge(
        merged,
        rq,
        on="flight_id",
        how="left",
    )

    # Select final 7 columns
    final_cols = [
        "flight_id",
        "fd4_dep_horiz_m",
        "fd4_arr_horiz_m",
        "raw_dep_horiz_m",
        "raw_arr_horiz_m",
        "clean_dep_horiz_m",
        "clean_arr_horiz_m",
    ]
    out_df = merged[final_cols].copy()

    # Save output parquet
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / "r2_distance_table.parquet"
    out_df.to_parquet(out_path, index=False)

    print("\n" + "=" * 70)
    print("EXTRACTION SUMMARY")
    print("=" * 70)
    print(f"Output saved to: {out_path}")
    print(f"Table shape: {out_df.shape[0]:,} rows x {out_df.shape[1]} columns")
    print(f"Time taken:  {time.time() - t0:.2f}s")
    print("\nColumn Non-Null Counts & Match Rates:")
    for col in final_cols:
        non_nulls = out_df[col].notna().sum()
        pct = (non_nulls / len(out_df)) * 100
        print(f"  - {col:<20}: {non_nulls:>7,} / {len(out_df):,} ({pct:.1f}%)")

    print("\nFirst 5 rows:")
    print(out_df.head(5).to_string())
    print("=" * 70)


if __name__ == "__main__":
    main()
