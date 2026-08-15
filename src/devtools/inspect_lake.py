"""
src/devtools/inspect_lake.py — Quick Delta Lake Inspector

Provides instant high-level summary metrics for any Delta Lake table:
- Unique SIM_FIDs
- Total Waypoint Rows
- Routes and Aircraft Types
- Flight Level range (min, max)
- Date range (dep_date)
- Delta Lake Version & File Count

Usage:
    python -m src.devtools.inspect_lake <lake_path>
    python -m src.devtools.inspect_lake data/results/test_smoothed_cap_lake
    python -m src.devtools.inspect_lake \\\\PC182.ilr.rwth-aachen.de\\studiert_ilr\\...
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
import pyarrow.compute as pc
from deltalake import DeltaTable

def inspect_lake(lake_path: str) -> None:
    path = Path(lake_path)
    if not path.exists() and not str(lake_path).startswith("\\\\"):
        print(f"Error: Path '{lake_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        dt = DeltaTable(str(lake_path))
    except Exception as e:
        print(f"Error opening Delta Lake at '{lake_path}': {e}", file=sys.stderr)
        sys.exit(1)

    version = dt.version()
    files = dt.file_uris()
    
    # Stream only lightweight metadata columns via PyArrow scanner (constant O(1) RAM)
    cols_to_read = ["SIM_FID", "route", "typecode", "FL", "dep_date", "EF_total"]
    available_cols = [f.name for f in dt.schema().fields]
    read_cols = [c for c in cols_to_read if c in available_cols]
    
    scanner = dt.to_pyarrow_dataset().scanner(columns=read_cols, batch_size=100_000)
    
    total_rows = 0
    unique_fids: set[str] = set()
    unique_routes: set[str] = set()
    unique_types: set[str] = set()
    min_date, max_date = None, None
    min_fl, max_fl = None, None
    ef_sum, ef_max = 0.0, 0.0
    ef_count = 0

    for batch in scanner.to_batches():
        total_rows += batch.num_rows

        if "SIM_FID" in batch.schema.names:
            unique_fids.update(pc.unique(batch.column("SIM_FID")).to_pylist())

        if "route" in batch.schema.names:
            unique_routes.update(pc.unique(batch.column("route")).to_pylist())

        if "typecode" in batch.schema.names:
            unique_types.update(pc.unique(batch.column("typecode")).to_pylist())

        if "dep_date" in batch.schema.names:
            b_min = pc.min(batch.column("dep_date")).as_py()
            b_max = pc.max(batch.column("dep_date")).as_py()
            min_date = b_min if min_date is None else min(min_date, b_min)
            max_date = b_max if max_date is None else max(max_date, b_max)

        if "FL" in batch.schema.names:
            b_min = pc.min(batch.column("FL")).as_py()
            b_max = pc.max(batch.column("FL")).as_py()
            if b_min is not None:
                min_fl = b_min if min_fl is None else min(min_fl, b_min)
            if b_max is not None:
                max_fl = b_max if max_fl is None else max(max_fl, b_max)

        if "EF_total" in batch.schema.names:
            b_sum = pc.sum(batch.column("EF_total")).as_py() or 0.0
            b_max = pc.max(batch.column("EF_total")).as_py() or 0.0
            ef_sum += b_sum
            ef_max = max(ef_max, b_max)
            ef_count += batch.num_rows

    print("=" * 65)
    print(f"DELTA LAKE INSPECTION: {path.name}")
    print("=" * 65)
    print(f"Path:            {lake_path}")
    print(f"Table Version:   {version}")
    print(f"Parquet Files:   {len(files):,}")
    print(f"Total Rows:      {total_rows:,}")
    print(f"Unique SIM_FIDs: {len(unique_fids):,}")

    if unique_routes:
        routes_str = ", ".join(sorted([str(r) for r in unique_routes if r is not None]))
        print(f"Routes ({len(unique_routes)}):     {routes_str if len(routes_str) < 60 else routes_str[:57] + '...'}")

    if unique_types:
        types_str = ", ".join(sorted([str(t) for t in unique_types if t is not None]))
        print(f"Typecodes ({len(unique_types)}):  {types_str}")

    if min_date is not None:
        print(f"Date Range:      {min_date} -> {max_date}")

    if min_fl is not None:
        print(f"FL Range:        FL {min_fl} -> FL {max_fl}")

    if ef_count > 0:
        mean_ef = ef_sum / ef_count
        print(f"Contrail EF:     Mean = {mean_ef:.2e} J | Max = {ef_max:.2e} J")

    print("=" * 65)


def main() -> None:
    parser = argparse.ArgumentParser(description="Quick Delta Lake Inspector")
    parser.add_argument("lake_path", help="Path to Delta Lake directory (local or UNC share)")
    args = parser.parse_args()
    inspect_lake(args.lake_path)


if __name__ == "__main__":
    main()
