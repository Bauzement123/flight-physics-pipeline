"""
Verification Module: High-Performance Country Trajectory & Medoid Visualization

Memory-optimized & multi-threaded batch streaming architecture:
  - Pre-loads flight-to-filepath registry index once into memory (~15 MB).
  - Streams parquet files in parallel thread batches, extracting only float32 coordinate arrays.
  - Keeps only plotted line segments in memory (< 250 MB peak RAM even for 30k flights).
  - Renders 3-way color scheme (Inbound, Outbound, Domestic) with wider translucent regular lines
    and thinner crisp medoids over EuropeanMapCache NaturalEarth basemaps.
  - Runs gc.collect() between file batches and per-country iterations to prevent memory leaks.
"""

from __future__ import annotations

import argparse
import gc
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import cartopy.crs as ccrs

from src.common.config import (
    BASE_DIR,
    DATA_DIR,
    EUR_LAT_MIN,
    EUR_LAT_MAX,
    EUR_LON_MIN,
    EUR_LON_MAX,
    GLOBAL_FLIGHT_CLUSTER_MAP,
    GLOBAL_CLEAN_REGISTRY,
    GLOBAL_TRAJECTORY_REGISTRY,
)
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger
from src.data_manager.io_utils import read_flight_filepaths

logger = logging.getLogger(__name__)

# Standard output directory for country trajectory visualizations
DEFAULT_OUTPUT_DIR = DATA_DIR / "analysis" / "plots" / "country_maps"

# Standard 2-letter ICAO prefix to human-readable country names for plot titling
ICAO_COUNTRY_NAMES: Dict[str, str] = {
    "BI": "Iceland",
    "EB": "Belgium",
    "ED": "Germany",
    "EE": "Estonia",
    "EF": "Finland",
    "EG": "United Kingdom",
    "EH": "Netherlands",
    "EI": "Ireland",
    "EK": "Denmark",
    "EL": "Luxembourg",
    "EN": "Norway",
    "EP": "Poland",
    "ES": "Sweden",
    "EV": "Latvia",
    "EY": "Lithuania",
    "LB": "Bulgaria",
    "LC": "Cyprus",
    "LD": "Croatia",
    "LE": "Spain",
    "LF": "France",
    "LG": "Greece",
    "LH": "Hungary",
    "LI": "Italy",
    "LJ": "Slovenia",
    "LK": "Czech Republic",
    "LL": "Israel",
    "LM": "Malta",
    "LO": "Austria",
    "LP": "Portugal",
    "LR": "Romania",
    "LS": "Switzerland",
    "LT": "Turkey",
    "LW": "North Macedonia",
    "LX": "Gibraltar",
    "LY": "Serbia / Montenegro",
    "LZ": "Slovakia",
}


def parse_route_airports(route_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Extracts departure and arrival airport ICAO codes from a route identifier."""
    if not isinstance(route_str, str):
        return None, None
    
    cleaned = route_str.strip()
    if "-" in cleaned:
        parts = cleaned.split("-", 1)
        dep = parts[0].strip().upper()
        arr = parts[1].strip().upper()
        return (dep if len(dep) >= 2 else None), (arr if len(arr) >= 2 else None)
    elif "->" in cleaned:
        parts = cleaned.split("->", 1)
        dep = parts[0].strip().upper()
        arr = parts[1].strip().upper()
        return (dep if len(dep) >= 2 else None), (arr if len(arr) >= 2 else None)
    elif "_" in cleaned:
        parts = cleaned.split("_", 1)
        dep = parts[0].strip().upper()
        arr = parts[1].strip().upper()
        return (dep if len(dep) >= 2 else None), (arr if len(arr) >= 2 else None)
    
    return None, None


def enrich_flight_cluster_map(df_map: pd.DataFrame) -> pd.DataFrame:
    """Enriches flight cluster map with dep_country and arr_country columns."""
    df = df_map.copy()
    
    # Standardize route column name
    if "route" in df.columns and "route_id" not in df.columns:
        df = df.rename(columns={"route": "route_id"})
    
    dep_countries = []
    arr_countries = []
    
    for route_val in df["route_id"]:
        dep_ap, arr_ap = parse_route_airports(str(route_val))
        dep_c = dep_ap[:2] if dep_ap and len(dep_ap) >= 2 else None
        arr_c = arr_ap[:2] if arr_ap and len(arr_ap) >= 2 else None
        dep_countries.append(dep_c)
        arr_countries.append(arr_c)
        
    df["dep_country"] = dep_countries
    df["arr_country"] = arr_countries
    
    return df


def _read_file_segments(item: Tuple[Path, List[str]]) -> List[Tuple[str, np.ndarray]]:
    """Worker task to read a single parquet file and extract float32 trajectory segments."""
    fpath, fids = item
    if not fpath.exists():
        return []
    
    try:
        tbl = pq.read_table(fpath, columns=["flight_id", "longitude", "latitude"])
        df_f = tbl.to_pandas()
        extracted = []
        for fid in fids:
            sub = df_f[df_f["flight_id"] == fid]
            if not sub.empty:
                lons = sub["longitude"].to_numpy(dtype=np.float32)
                lats = sub["latitude"].to_numpy(dtype=np.float32)
                mask = np.isfinite(lons) & np.isfinite(lats)
                if mask.sum() >= 2:
                    coords = np.column_stack([lons[mask], lats[mask]])
                    extracted.append((fid, coords))
        return extracted
    except Exception as exc:
        logger.debug("Failed reading %s: %s", fpath, exc)
        return []


def stream_country_segments(
    file_to_fids: Dict[Path, List[str]],
    df_country_flights: pd.DataFrame,
    country_code: str,
    io_threads: int = 8,
    batch_size: int = 1000,
) -> Dict[str, any]:
    """Streams parquet files in multi-threaded batches, returning categorized coordinate segments."""
    # Lookup metadata per flight_id
    fid_to_row = df_country_flights.set_index("flight_id").to_dict(orient="index")

    segments_reg_to: List[np.ndarray] = []
    segments_reg_from: List[np.ndarray] = []
    segments_reg_dom: List[np.ndarray] = []

    medoids_to: List[Tuple[str, np.ndarray]] = []
    medoids_from: List[Tuple[str, np.ndarray]] = []
    medoids_dom: List[Tuple[str, np.ndarray]] = []

    min_lon, max_lon = 180.0, -180.0
    min_lat, max_lat = 90.0, -90.0
    plotted_count = 0

    items = list(file_to_fids.items())
    total_files = len(items)

    for i in range(0, total_files, batch_size):
        chunk = items[i : i + batch_size]
        with ThreadPoolExecutor(max_workers=io_threads) as pool:
            batch_results = list(pool.map(_read_file_segments, chunk))

        for file_segs in batch_results:
            for fid, coords in file_segs:
                row = fid_to_row.get(fid)
                if not row:
                    continue

                dep_c = row["dep_country"]
                arr_c = row["arr_country"]
                is_med = bool(row.get("is_medoid", False))

                # Track geographic bounding box
                min_lon = min(min_lon, float(np.min(coords[:, 0])))
                max_lon = max(max_lon, float(np.max(coords[:, 0])))
                min_lat = min(min_lat, float(np.min(coords[:, 1])))
                max_lat = max(max_lat, float(np.max(coords[:, 1])))
                plotted_count += 1

                if dep_c == country_code and arr_c == country_code:
                    # Domestic
                    if is_med:
                        medoids_dom.append((fid, coords))
                    else:
                        segments_reg_dom.append(coords)
                elif arr_c == country_code:
                    # Inbound (TO)
                    if is_med:
                        medoids_to.append((fid, coords))
                    else:
                        segments_reg_to.append(coords)
                elif dep_c == country_code:
                    # Outbound (FROM)
                    if is_med:
                        medoids_from.append((fid, coords))
                    else:
                        segments_reg_from.append(coords)

        # Batch-level garbage collection
        del batch_results
        gc.collect()

    return {
        "segments_reg_to": segments_reg_to,
        "segments_reg_from": segments_reg_from,
        "segments_reg_dom": segments_reg_dom,
        "medoids_to": medoids_to,
        "medoids_from": medoids_from,
        "medoids_dom": medoids_dom,
        "bbox": (min_lon, max_lon, min_lat, max_lat),
        "plotted_count": plotted_count,
    }


def render_and_save_country_map(
    country_code: str,
    country_name: str,
    stream_res: Dict[str, any],
    df_country_flights: pd.DataFrame,
    out_file: Path,
    dpi: int = 300,
    medoids_only: bool = False,
) -> None:
    """Renders vectorized LineCollections and medoid highlights to high-res PNG."""
    fig = plt.figure(figsize=(14, 10))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    # Pre-loaded NaturalEarth basemap features from EuropeanMapCache
    map_cache = EuropeanMapCache().initialize()
    map_cache.add_features_to_axes(ax)

    # 3-Way Color Palette Definitions
    color_reg_to = "#2b83ba"       # Ocean Blue (Inbound)
    color_reg_from = "#fdae61"     # Coral Orange (Outbound)
    color_reg_dom = "#7a0177"      # Deep Violet (Domestic)

    color_med_to = "#08519c"       # Deep Royal Blue (Inbound Medoids)
    color_med_from = "#d73027"     # Vivid Crimson Red (Outbound Medoids)
    color_med_dom = "#49006a"      # Dark Violet-Purple (Domestic Medoids)

    # Linewidth and opacity specifications
    lw_normal = 1.15               # Normal flights: wider translucent lines
    alpha_normal = 0.20
    lw_medoid = 1.15 if medoids_only else 0.85  # Same linewidth as usual flights when medoids-only
    alpha_medoid = alpha_normal if medoids_only else 0.95  # Match regular flight opacity in medoids-only mode

    # 1. Regular Inbound (TO)
    if not medoids_only and stream_res["segments_reg_to"]:
        lc_to = LineCollection(
            stream_res["segments_reg_to"],
            colors=color_reg_to,
            alpha=alpha_normal,
            linewidths=lw_normal,
            zorder=3,
            transform=ccrs.PlateCarree(),
        )
        ax.add_collection(lc_to)

    # 2. Regular Outbound (FROM)
    if not medoids_only and stream_res["segments_reg_from"]:
        lc_from = LineCollection(
            stream_res["segments_reg_from"],
            colors=color_reg_from,
            alpha=alpha_normal,
            linewidths=lw_normal,
            zorder=3,
            transform=ccrs.PlateCarree(),
        )
        ax.add_collection(lc_from)

    # 3. Regular Domestic (INTERNAL)
    if not medoids_only and stream_res["segments_reg_dom"]:
        lc_dom = LineCollection(
            stream_res["segments_reg_dom"],
            colors=color_reg_dom,
            alpha=alpha_normal,
            linewidths=lw_normal,
            zorder=3,
            transform=ccrs.PlateCarree(),
        )
        ax.add_collection(lc_dom)

    # 4. Highlight Inbound Medoids (no airport scatter markers)
    for fid, coords in stream_res["medoids_to"]:
        ax.plot(
            coords[:, 0], coords[:, 1],
            color=color_med_to, alpha=alpha_medoid, linewidth=lw_medoid, zorder=6,
            transform=ccrs.PlateCarree()
        )

    # 5. Highlight Outbound Medoids (no airport scatter markers)
    for fid, coords in stream_res["medoids_from"]:
        ax.plot(
            coords[:, 0], coords[:, 1],
            color=color_med_from, alpha=alpha_medoid, linewidth=lw_medoid, zorder=6,
            transform=ccrs.PlateCarree()
        )

    # 6. Highlight Domestic Medoids (no airport scatter markers)
    for fid, coords in stream_res["medoids_dom"]:
        ax.plot(
            coords[:, 0], coords[:, 1],
            color=color_med_dom, alpha=alpha_medoid, linewidth=lw_medoid, zorder=6,
            transform=ccrs.PlateCarree()
        )

    # Always plot within the European Bounding Box + 5 deg
    ax.set_extent(
        [EUR_LON_MIN - 5.0, EUR_LON_MAX + 5.0, EUR_LAT_MIN - 5.0, EUR_LAT_MAX + 5.0],
        crs=ccrs.PlateCarree()
    )

    # Dynamic Gridlines
    gl = ax.gridlines(draw_labels=True, linewidth=0.5, color="gray", alpha=0.4, linestyle="--")
    gl.top_labels = False
    gl.right_labels = False

    # Metadata Header
    total_flights = len(df_country_flights)
    plotted_count = stream_res["plotted_count"]
    med_to_count = len(stream_res["medoids_to"])
    med_from_count = len(stream_res["medoids_from"])
    med_dom_count = len(stream_res["medoids_dom"])
    total_medoids = med_to_count + med_from_count + med_dom_count
    unique_routes = df_country_flights["route_id"].nunique()

    title_prefix = "Corridor Medoids" if medoids_only else "Clean Trajectories & Medoids"
    ax.set_title(
        f"{title_prefix} — {country_name} (ICAO: {country_code})\n"
        f"Total Flights: {total_flights:,} (Plotted: {plotted_count:,}) | Routes: {unique_routes} | "
        f"Medoids: {total_medoids} (Inbound: {med_to_count}, Outbound: {med_from_count}, Domestic: {med_dom_count})",
        fontsize=12.5,
        pad=14,
        fontweight="bold",
    )

    # Legend (Pure line aesthetics without airport markers)
    legend_elements = [
        Line2D([0], [0], color=color_med_to, lw=lw_medoid * 2.0,
               label=f"Inbound Medoid (TO {country_code}, n={med_to_count})"),
    ]
    if not medoids_only:
        legend_elements.append(
            Line2D([0], [0], color=color_reg_to, lw=lw_normal * 1.5, alpha=0.7,
                   label=f"Inbound Flights (TO {country_code}, n={len(stream_res['segments_reg_to'])})")
        )
    legend_elements.append(
        Line2D([0], [0], color=color_med_from, lw=lw_medoid * 2.0,
               label=f"Outbound Medoid (FROM {country_code}, n={med_from_count})")
    )
    if not medoids_only:
        legend_elements.append(
            Line2D([0], [0], color=color_reg_from, lw=lw_normal * 1.5, alpha=0.7,
                   label=f"Outbound Flights (FROM {country_code}, n={len(stream_res['segments_reg_from'])})")
        )
    if stream_res["medoids_dom"] or (not medoids_only and stream_res["segments_reg_dom"]):
        legend_elements.append(
            Line2D([0], [0], color=color_med_dom, lw=lw_medoid * 2.0,
                   label=f"Domestic Medoid ({country_code}-{country_code}, n={med_dom_count})")
        )
        if not medoids_only:
            legend_elements.append(
                Line2D([0], [0], color=color_reg_dom, lw=lw_normal * 1.5, alpha=0.7,
                       label=f"Domestic Flights ({country_code}-{country_code}, n={len(stream_res['segments_reg_dom'])})")
            )

    ax.legend(
        handles=legend_elements,
        loc="lower left",
        frameon=True,
        facecolor="white",
        framealpha=0.92,
        fontsize=9.0,
        borderpad=0.8,
    )

    # Save PNG
    out_file.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_file, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    logger.info("Saved country trajectory plot: %s (DPI=%d)", out_file, dpi)


def plot_country_trajectories(
    countries: Optional[List[str]] = None,
    out_dir: Optional[Path] = None,
    registry_path: Optional[Path] = None,
    dpi: int = 300,
    io_threads: int = 8,
    batch_size: int = 1000,
    medoids_only: bool = False,
) -> None:
    """Orchestrates country-wise trajectory map generation with memory streaming."""
    logger.info("Loading flight cluster map from %s...", GLOBAL_FLIGHT_CLUSTER_MAP)
    if not GLOBAL_FLIGHT_CLUSTER_MAP.exists():
        logger.critical("GLOBAL_FLIGHT_CLUSTER_MAP does not exist at %s", GLOBAL_FLIGHT_CLUSTER_MAP)
        raise FileNotFoundError(f"Cluster map not found: {GLOBAL_FLIGHT_CLUSTER_MAP}")

    df_raw_map = pd.read_parquet(GLOBAL_FLIGHT_CLUSTER_MAP)
    logger.info("Loaded %d rows from flight cluster map.", len(df_raw_map))

    # Enrich with dep_country and arr_country
    df_map = enrich_flight_cluster_map(df_raw_map)

    # Derive unique country ICAOs
    dep_set = set(df_map["dep_country"].dropna().unique())
    arr_set = set(df_map["arr_country"].dropna().unique())
    all_countries = sorted(dep_set | arr_set)
    logger.info("Found %d unique country ICAO prefix(es): %s", len(all_countries), ", ".join(all_countries))

    # Target countries
    target_countries = all_countries
    if countries:
        selected = [c.strip().upper() for c in countries if c.strip().upper() in all_countries]
        if not selected:
            logger.error("None of the requested countries %s were found in cluster map.", countries)
            return
        target_countries = selected

    output_dir = out_dir or DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Pre-load registry filepaths index once into memory (~15 MB)
    reg_path = registry_path or GLOBAL_CLEAN_REGISTRY
    logger.info("Pre-loading registry filepath index from %s...", reg_path.name)
    df_reg = read_flight_filepaths(registry_path=reg_path)
    reg_dict: Dict[str, str] = dict(zip(df_reg["flight_id"], df_reg["file_path"]))
    logger.info("Indexed %d flight paths in memory.", len(reg_dict))

    # Optional fallback indexing if clean registry lacks some flights
    if reg_path != GLOBAL_TRAJECTORY_REGISTRY and GLOBAL_TRAJECTORY_REGISTRY.exists():
        df_raw_reg = read_flight_filepaths(registry_path=GLOBAL_TRAJECTORY_REGISTRY)
        raw_dict = dict(zip(df_raw_reg["flight_id"], df_raw_reg["file_path"]))
        # Merge missing
        for k, v in raw_dict.items():
            if k not in reg_dict:
                reg_dict[k] = v
        del df_raw_reg
        gc.collect()

    del df_reg
    gc.collect()

    logger.info(
        "Generating trajectory maps for %d country/countries (Output: %s, Threads: %d, Medoids-Only: %s)...",
        len(target_countries), output_dir, io_threads, medoids_only
    )

    for idx, ccode in enumerate(target_countries, 1):
        cname = ICAO_COUNTRY_NAMES.get(ccode, ccode)
        logger.info("[%d/%d] Processing country: %s (%s)...", idx, len(target_countries), ccode, cname)

        # Filter flights TO or FROM this country
        df_country = df_map[(df_map["dep_country"] == ccode) | (df_map["arr_country"] == ccode)].copy()
        if medoids_only:
            df_country = df_country[df_country["is_medoid"] == True].copy()

        if df_country.empty:
            logger.warning("No flights found for country %s — skipping.", ccode)
            continue

        # Group flight IDs by file_path
        file_to_fids: Dict[Path, List[str]] = defaultdict(list)
        for fid in df_country["flight_id"]:
            if fid in reg_dict:
                rel_path = reg_dict[fid]
                fpath = Path(rel_path) if Path(rel_path).is_absolute() else BASE_DIR / rel_path
                file_to_fids[fpath].append(fid)

        logger.info("  %s: %d flights across %d unique parquet files. Streaming segments...", ccode, len(df_country), len(file_to_fids))

        # Stream & batch extract only float32 line segments into memory
        stream_res = stream_country_segments(
            file_to_fids=file_to_fids,
            df_country_flights=df_country,
            country_code=ccode,
            io_threads=io_threads,
            batch_size=batch_size,
        )

        # Render & Save PNG
        suffix = "_medoids" if medoids_only else ""
        out_png = output_dir / f"trajectories_{ccode}{suffix}.png"
        render_and_save_country_map(
            country_code=ccode,
            country_name=cname,
            stream_res=stream_res,
            df_country_flights=df_country,
            out_file=out_png,
            dpi=dpi,
            medoids_only=medoids_only,
        )

        # Explicit garbage collection per country
        del stream_res
        del df_country
        del file_to_fids
        gc.collect()

    logger.info("All country trajectory visualizations completed successfully.")


def parse_args() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generates high-resolution geographical trajectory maps per country from flight cluster map.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--country",
        nargs="+",
        default=None,
        help="One or more 2-letter country ICAO prefixes (e.g. ED LF EG). Default is all countries.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where output PNG plots will be saved.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=GLOBAL_CLEAN_REGISTRY,
        help="Parquet registry used to resolve trajectory file paths (defaults to global_clean_registry.parquet).",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="Resolution in DPI for generated PNG figures.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=8,
        help="Number of concurrent IO threads for batch reading parquet files.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of parquet files to read per garbage-collected batch.",
    )
    parser.add_argument(
        "--medoids-only",
        action="store_true",
        help="Plot only corridor medoid trajectories, skipping regular clean flights.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    setup_file_logger("verification.log")
    args = parse_args()
    plot_country_trajectories(
        countries=args.country,
        out_dir=args.out_dir,
        registry_path=args.registry,
        dpi=args.dpi,
        io_threads=args.threads,
        batch_size=args.batch_size,
        medoids_only=args.medoids_only,
    )
