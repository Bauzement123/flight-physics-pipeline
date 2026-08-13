"""
Plotting Top 20 Wiped-Out Macro Routes (Clean Trajectories)
Reads the stage7_config_exclusion_results.csv (full ICAO route pairs), aggregates
internally to macro country-prefix pairs (e.g. ED-EG), identifies the top 20
most-penalised macro regions, then loads and plots the actual clean trajectory
parquet files for all matching ICAO route folders.
"""

import argparse
import logging
import random
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs

from src.common.config import BASE_DIR
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger, split_route_string

logger = logging.getLogger(__name__)

_DEFAULT_CSV      = BASE_DIR / "data" / "calibration" / "postfilter_calibration" / "stage7" / "stage7_config_exclusion_results.csv"
_DEFAULT_OUT      = BASE_DIR / "data" / "analysis" / "plots" / "wipeout_trajectories"
_DEFAULT_TRAJ_DIR = BASE_DIR / "data" / "trajectories"
_MIN_FLIGHTS      = 10
MAX_TRAJECTORIES_PER_MACRO = 1000


def _to_macro(canonical_route: str) -> str:
    """Reduce a full ICAO route pair to its 2-letter country prefix pair.

    e.g. EDDF-EGLL → ED-EG  (sorted alphabetically)
    Returns 'UNK' if the route cannot be parsed.
    """
    dep, arr = split_route_string(canonical_route)
    if dep != "UNK" and arr != "UNK":
        return "-".join(sorted([dep[:2], arr[:2]]))
    return "UNK"


def _build_macro_df(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-ICAO-pair stats from stage7 CSV to macro country-prefix level."""
    df = df.copy()
    df["macro_route"] = df["canonical_route"].apply(_to_macro)
    df = df[df["macro_route"] != "UNK"]

    macro = df.groupby("macro_route").agg(
        total_flights=("total_flights", "sum"),
        passed_flights=("passed_flights", "sum"),
    ).reset_index()
    macro["survival_pct"] = (macro["passed_flights"] / macro["total_flights"]) * 100
    macro["exclusion_pct"] = 100.0 - macro["survival_pct"]

    macro = macro[macro["total_flights"] >= _MIN_FLIGHTS]
    return macro.sort_values("survival_pct")


def plot_wiped_out_trajectories(csv_path: Path, output_dir: Path, trajectories_dir: Path) -> None:
    if not csv_path.exists():
        logger.error(f"CSV not found at {csv_path}")
        return

    df = pd.read_csv(csv_path)
    macro_df = _build_macro_df(df)
    top_20 = macro_df.head(20)

    if top_20.empty:
        logger.info("No macro routes found after aggregation.")
        return

    logger.info("Initializing EuropeanMapCache...")
    cache = EuropeanMapCache()
    cache.initialize(resolution="10m")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-gather all route folders once
    if not trajectories_dir.exists():
        logger.error(f"Trajectories directory not found: {trajectories_dir}")
        return
    all_route_folders = [d for d in trajectories_dir.iterdir() if d.is_dir()]

    for _, row in top_20.iterrows():
        macro_route = row["macro_route"]
        total       = int(row["total_flights"])
        passed      = int(row["passed_flights"])
        survival    = row["survival_pct"]

        logger.info(f"Processing macro {macro_route} ({passed}/{total} survive, {survival:.1f}%)")

        # macro_route is always "XX-XX" — plain split, no split_route_string needed
        dep_prefix, arr_prefix = macro_route.split("-", 1)

        # Find all trajectory folders whose route starts with the matching prefixes.
        # Folder names are full ICAO pairs (e.g. EDDF-EGLL or rank_001_EDDF-EGLL),
        # so split_route_string is correct here.
        matching_folders = []
        for folder in all_route_folders:
            route_part = folder.name.split("_")[-1]
            r_dep, r_arr = split_route_string(route_part)
            if r_dep != "UNK" and r_arr != "UNK":
                if r_dep.startswith(dep_prefix) and r_arr.startswith(arr_prefix):
                    matching_folders.append(folder)

        if not matching_folders:
            logger.warning(f"No trajectory folders found for macro {macro_route}")
            continue

        # Collect all clean parquet files across matching folders
        clean_parquets = []
        for fld in matching_folders:
            clean_dir = fld / "clean"
            if clean_dir.exists():
                clean_parquets.extend(list(clean_dir.glob("*.parquet")))

        if not clean_parquets:
            logger.warning(f"No clean trajectories found for macro {macro_route}")
            continue

        if len(clean_parquets) > MAX_TRAJECTORIES_PER_MACRO:
            logger.info(f"Found {len(clean_parquets)} files, sampling down to {MAX_TRAJECTORIES_PER_MACRO}")
            clean_parquets = random.sample(clean_parquets, MAX_TRAJECTORIES_PER_MACRO)
        else:
            logger.info(f"Found {len(clean_parquets)} clean trajectories for macro {macro_route}")

        fig = plt.figure(figsize=(10, 8))
        ax  = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
        cache.add_features_to_axes(ax)

        plotted_count = 0
        for pq_path in clean_parquets:
            try:
                df_flight = pd.read_parquet(pq_path, columns=["longitude", "latitude"])
                if not df_flight.empty:
                    ax.plot(
                        df_flight["longitude"], df_flight["latitude"],
                        color="steelblue", linewidth=0.5, alpha=0.15,
                        transform=ccrs.PlateCarree(), zorder=3,
                    )
                    plotted_count += 1
            except Exception as e:
                logger.debug(f"Failed to load {pq_path}: {e}")

        if plotted_count > 0:
            import matplotlib.lines as mlines
            proxy = mlines.Line2D([], [], color="steelblue", linewidth=1, alpha=0.5,
                                  label=f"Clean flights (n={plotted_count})")
            ax.legend(handles=[proxy], loc="lower left", fontsize=9)
        else:
            ax.legend(loc="lower left", fontsize=9)

        ax.set_title(
            f"Macro Route: {macro_route} — Actual Flight Paths\n"
            f"Survived: {passed}/{total} flights ({survival:.1f}%)",
            fontsize=12, fontweight="bold",
        )

        out_path = output_dir / f"{macro_route}_trajectories.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.info(f"Saved {out_path}")

    logger.info("Finished plotting all macro trajectory plots.")


def main() -> None:
    setup_file_logger(log_filename="analysis.log")
    parser = argparse.ArgumentParser(description="Plot clean trajectories for top 20 wiped-out macro routes")
    parser.add_argument(
        "--csv-path", type=str, default=str(_DEFAULT_CSV),
        help="Path to stage7_config_exclusion_results.csv",
    )
    parser.add_argument(
        "--output-dir", type=str, default=str(_DEFAULT_OUT),
        help="Output directory for PNG plots",
    )
    parser.add_argument(
        "--trajectories-dir", type=str, default=str(_DEFAULT_TRAJ_DIR),
        help="Root directory containing per-route trajectory folders",
    )
    args = parser.parse_args()
    plot_wiped_out_trajectories(
        Path(args.csv_path),
        Path(args.output_dir),
        Path(args.trajectories_dir),
    )


if __name__ == "__main__":
    main()
