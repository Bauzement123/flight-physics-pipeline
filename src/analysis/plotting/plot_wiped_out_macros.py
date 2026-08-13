"""
Plotting Top 20 Wiped-Out Macro Routes
Reads the stage7_config_exclusion_results.csv (full ICAO route pairs), aggregates
internally to macro country-prefix pairs (e.g. ED-EG), and visualises the top 20
most-penalised macro regions as airport scatter plots on a European map.
"""

import argparse
import logging
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

_DEFAULT_CSV = BASE_DIR / "data" / "calibration" / "postfilter_calibration" / "stage7" / "stage7_config_exclusion_results.csv"
_DEFAULT_OUT  = BASE_DIR / "data" / "analysis" / "plots" / "wipeouts"
_MIN_FLIGHTS  = 10


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


def plot_top_wiped_out_routes(csv_path: Path, output_dir: Path) -> None:
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
    df_airports = cache.airports_df.copy()
    df_airports = df_airports[df_airports["survived_bbox"] == True]

    output_dir.mkdir(parents=True, exist_ok=True)

    for _, row in top_20.iterrows():
        macro_route  = row["macro_route"]
        total        = int(row["total_flights"])
        passed       = int(row["passed_flights"])
        survival     = row["survival_pct"]

        logger.info(f"Plotting macro {macro_route} ({passed}/{total} flights survive, {survival:.1f}%)")

        # macro_route is always "XX-XX" — plain split, no split_route_string needed
        dep_prefix, arr_prefix = macro_route.split("-", 1)

        dep_airports = df_airports[df_airports["icao"].str.startswith(dep_prefix, na=False)]
        arr_airports = df_airports[df_airports["icao"].str.startswith(arr_prefix, na=False)]

        fig = plt.figure(figsize=(10, 8))
        ax  = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
        cache.add_features_to_axes(ax)

        if not dep_airports.empty:
            ax.scatter(
                dep_airports["lon"], dep_airports["lat"],
                color="forestgreen", marker="o", s=30, edgecolor="black",
                transform=ccrs.PlateCarree(), zorder=4,
                label=f"{dep_prefix}* (Departures)",
            )
        if not arr_airports.empty:
            ax.scatter(
                arr_airports["lon"], arr_airports["lat"],
                color="darkred", marker="s", s=30, edgecolor="black",
                transform=ccrs.PlateCarree(), zorder=4,
                label=f"{arr_prefix}* (Arrivals)",
            )
        if not dep_airports.empty and not arr_airports.empty:
            for _, d in dep_airports.iterrows():
                for _, a in arr_airports.iterrows():
                    ax.plot(
                        [d["lon"], a["lon"]], [d["lat"], a["lat"]],
                        color="gray", linewidth=0.5, alpha=0.1,
                        transform=ccrs.PlateCarree(), zorder=3,
                    )

        ax.legend(loc="lower left", fontsize=9)
        ax.set_title(
            f"Macro Route: {macro_route}\n"
            f"Survived: {passed}/{total} flights ({survival:.1f}%)",
            fontsize=12, fontweight="bold",
        )

        out_path = output_dir / f"{macro_route}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.info(f"Saved {out_path}")

    logger.info("Finished generating all macro plots.")


def main() -> None:
    setup_file_logger(log_filename="analysis.log")
    parser = argparse.ArgumentParser(description="Plot top 20 wiped-out macro routes")
    parser.add_argument(
        "--csv-path", type=str, default=str(_DEFAULT_CSV),
        help="Path to stage7_config_exclusion_results.csv",
    )
    parser.add_argument(
        "--output-dir", type=str, default=str(_DEFAULT_OUT),
        help="Output directory for PNG plots",
    )
    args = parser.parse_args()
    plot_top_wiped_out_routes(Path(args.csv_path), Path(args.output_dir))


if __name__ == "__main__":
    main()
