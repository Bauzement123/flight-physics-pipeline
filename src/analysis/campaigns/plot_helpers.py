"""
Visualization and Caching Helpers for Clustered Flight Cohort Plots.
Refactored to decouple worker-safe data generation from main-thread rendering.
"""

import logging
import multiprocessing
from pathlib import Path
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.common.config import (
    BASE_DIR,
    CALIBRATION_PLOT_REGISTRY,
    CALIBRATION_PLOTS_DIR,
    GLOBAL_FLIGHT_CLUSTER_MAP,
    CALIBRATION_FLIGHT_CLUSTER_MAP
)
from src.core.corridor.stability_worker import _load_route_flights
from src.core.corridor.pca_compressor import classify_and_normalize_cohort, vectorize_flight

logger = logging.getLogger(__name__)


def extract_cohort_plot_data(
    route_id: str,
    config_type: str,
    n0: int,
    tau: float,
    kmax: int,
    replicate: int,
    registry_df: pd.DataFrame = None,
) -> dict:
    """
    Worker-safe extraction function that retrieves flight mapping and normalizes trajectories.
    Returns a picklable dictionary with lists of coordinates. No Matplotlib figure creation.
    """
    from src.common.registry_utils import load_trajectory_registry

    if registry_df is None:
        registry_df = load_trajectory_registry()

    # 1. Load flight cluster mappings
    if config_type.upper() == "ORACLE":
        oracle_route_id = f"ORACLE_{route_id}"
        if not GLOBAL_FLIGHT_CLUSTER_MAP.exists():
            raise FileNotFoundError(f"Oracle cluster map not found: {GLOBAL_FLIGHT_CLUSTER_MAP}")
        df_map = pd.read_parquet(GLOBAL_FLIGHT_CLUSTER_MAP)
        df_map = df_map[df_map["route_id"] == oracle_route_id]
        if df_map.empty:
            raise ValueError(f"No oracle mappings found for route {oracle_route_id} in {GLOBAL_FLIGHT_CLUSTER_MAP}")
    else:
        if not CALIBRATION_FLIGHT_CLUSTER_MAP.exists():
            raise FileNotFoundError(f"Calibration cluster map not found: {CALIBRATION_FLIGHT_CLUSTER_MAP}")
        df_map = pd.read_parquet(CALIBRATION_FLIGHT_CLUSTER_MAP)
        df_map = df_map[
            (df_map["route_id"] == route_id) &
            (df_map["N_0"] == n0) &
            (df_map["tau"] == tau) &
            (df_map["K_max"] == kmax) &
            (df_map["replicate"] == replicate)
        ]
        if df_map.empty:
            raise ValueError(
                f"No calibration mappings found for {route_id} (N0={n0}, tau={tau}, K={kmax}, rep={replicate})"
            )

    # Convert mapping to lookup dict
    mapping = {row["flight_id"]: (row["cluster_id"], row["is_medoid"]) for _, row in df_map.iterrows()}

    # 2. Load and normalize flights for the route
    flights = _load_route_flights(route_id, n_target=9999, registry_df=registry_df)
    if not flights:
        raise RuntimeError(f"No raw flights found for route {route_id}")

    norm_flights, _ = classify_and_normalize_cohort(flights)
    if not norm_flights:
        raise RuntimeError(f"No flights survived normalization for route {route_id}")

    # Match flights and vectorize
    flights_data = []
    for fl in norm_flights:
        try:
            fid = fl.flight_id
            if fid is None:
                raise AttributeError("flight_id is None")
        except AttributeError:
            fid = getattr(fl, "callsign", None)
            if fid is not None:
                logger.warning(
                    f"flight_id not set on TrafficFlight object; falling back to callsign='{fid}'."
                )

        if fid is None or fid not in mapping:
            continue

        cluster_id, is_medoid = mapping[fid]
        
        # Resample to 100 points
        vec = vectorize_flight(fl)
        lats = vec[:100]
        lons = vec[100:200]
        alts = vec[200:300]

        flights_data.append({
            "flight_id": str(fid),
            "lons": lons.tolist(),
            "lats": lats.tolist(),
            "alts": alts.tolist(),
            "cluster_id": int(cluster_id),
            "is_medoid": bool(is_medoid)
        })

    if not flights_data:
        raise RuntimeError(f"None of the mapped flight IDs were found in normalized flights for {route_id}")

    return {
        "route_id": route_id,
        "config_type": config_type,
        "n0": n0,
        "tau": tau,
        "kmax": kmax,
        "replicate": replicate,
        "flights_data": flights_data,
        "success": True
    }


def assemble_and_save_plot(payload: dict, out_path: Path) -> None:
    """
    Main-thread only: constructs the Cartopy GeoAxes, addscached basemap features,
    plots flights/medoids/airports, and saves the final PNG.
    """
    import cartopy.crs as ccrs
    from src.common.map_cache import EuropeanMapCache

    route_id = payload["route_id"]
    flights_data = payload["flights_data"]

    # 1. Initialize Figure with Cartopy left panel and Standard right panel
    fig = plt.figure(figsize=(11, 4.5))
    ax1 = fig.add_subplot(1, 2, 1, projection=ccrs.PlateCarree())
    ax2 = fig.add_subplot(1, 2, 2)

    # 2. Add cached European basemap features
    map_cache = EuropeanMapCache().initialize()
    map_cache.add_features_to_axes(ax1)



    # 3. Add background airport markers
    if not map_cache.airports_df.empty:
        ax1.scatter(
            map_cache.airports_df["lon"],
            map_cache.airports_df["lat"],
            color="#6c757d",
            s=12,
            alpha=0.3,
            edgecolors="none",
            transform=ccrs.PlateCarree(),
            zorder=2
        )

    # 4. Plot trajectories
    colors = plt.cm.tab10.colors
    medoid_handles = {}

    for fl_entry in flights_data:
        lons = np.array(fl_entry["lons"])
        lats = np.array(fl_entry["lats"])
        alts = np.array(fl_entry["alts"])
        cluster_id = fl_entry["cluster_id"]
        is_medoid = fl_entry["is_medoid"]

        color = colors[cluster_id % len(colors)]

        if is_medoid:
            line, = ax1.plot(lons, lats, color=color, linewidth=2.5, alpha=1.0, zorder=5, transform=ccrs.PlateCarree())
            ax2.plot(range(100), alts, color=color, linewidth=2.5, alpha=1.0, zorder=5)
            medoid_handles[cluster_id] = line
        else:
            ax1.plot(lons, lats, color=color, linewidth=0.8, alpha=0.35, zorder=3, transform=ccrs.PlateCarree())
            ax2.plot(range(100), alts, color=color, linewidth=0.8, alpha=0.35, zorder=3)

    # Calculate the bounding box of all plotted trajectories
    min_lon, max_lon = 180.0, -180.0
    min_lat, max_lat = 90.0, -90.0
    
    for fl_entry in flights_data:
        min_lon = min(min_lon, min(fl_entry["lons"]))
        max_lon = max(max_lon, max(fl_entry["lons"]))
        min_lat = min(min_lat, min(fl_entry["lats"]))
        max_lat = max(max_lat, max(fl_entry["lats"]))

    # Apply explicit crop with a small visual padding (e.g., 1.5 degrees)
    padding = 1.5
    ax1.set_extent(
        [min_lon - padding, max_lon + padding, min_lat - padding, max_lat + padding],
        crs=ccrs.PlateCarree()
    )
# 5. Gridlines and Labels
    try:
        from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER
        
        # Let Cartopy handle dynamic gridlines AND labels based on the auto-cropped area
        gl = ax1.gridlines(
            draw_labels=True,  # Dynamically creates ticks based on view extent
            linestyle="--",
            color="dimgray",
            linewidth=0.8,
            alpha=0.6
        )
        
        # Only show labels on the bottom and left axes to look like standard matplotlib ticks
        gl.top_labels = False
        gl.right_labels = False
        
        # Format them with the nice degree symbols (E/W/N/S)
        gl.xformatter = LONGITUDE_FORMATTER
        gl.yformatter = LATITUDE_FORMATTER
        gl.xlabel_style = {'size': 8}
        gl.ylabel_style = {'size': 8}

    except Exception as e:
        logger.warning(f"Failed to draw dynamic gridlines: {e}")
        ax1.grid(True, linestyle="--", alpha=0.6, color="dimgray")

    ax1.set_xlabel("Longitude (deg)", fontsize=10)
    ax1.set_ylabel("Latitude (deg)", fontsize=10)
    ax1.set_title(f"Ground Track — {route_id}", fontsize=11, fontweight="bold")

    ax2.set_xlabel("Normalized Time Point", fontsize=10)
    ax2.set_ylabel("Altitude (ft)", fontsize=10)
    ax2.set_title("Vertical Profile", fontsize=11, fontweight="bold")
    ax2.grid(True, linestyle="--", alpha=0.5)

    # Add legend using medoid handles
    legend_labels = [f"Cluster {c} (Medoid)" for c in sorted(medoid_handles.keys())]
    legend_lines = [medoid_handles[c] for c in sorted(medoid_handles.keys())]
    if legend_lines:
        ax1.legend(legend_lines, legend_labels, loc="best", fontsize=8)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _render_plot_if_needed(task: dict, registry_df: pd.DataFrame = None) -> dict:
    """
    Worker-safe helper: checks cache, extracts plot data payload, and returns it.
    Does NOT write any files or perform matplotlib figure operations in the worker.
    """
    try:
        route_id = task["route_id"]
        config_type = str(task["config_type"]).upper()
        n0 = int(task["n0"])
        tau = float(task["tau"])
        kmax = int(task["kmax"])
        replicate = int(task["replicate"])

        if config_type == "ORACLE":
            fn = f"{route_id}_oracle.png"
        else:
            fn = f"{route_id}_N{n0}_tau{tau:.2f}_K{kmax}_rep{replicate}.png"
        out_path = CALIBRATION_PLOTS_DIR / fn

        # 1. Cache Check
        if CALIBRATION_PLOT_REGISTRY.exists():
            try:
                df_reg = pd.read_parquet(CALIBRATION_PLOT_REGISTRY)
                match = df_reg[
                    (df_reg["route_id"] == route_id) &
                    (df_reg["config_type"] == config_type) &
                    (df_reg["N_0"] == n0) &
                    (df_reg["tau"] == tau) &
                    (df_reg["K_max"] == kmax) &
                    (df_reg["replicate"] == replicate)
                ]
                if not match.empty:
                    saved_path = BASE_DIR / match["file_path"].iloc[0]
                    if saved_path.exists():
                        return {
                            "status": "success",
                            "task": task,
                            "path": str(saved_path),
                            "newly_generated": False,
                            "rel_path": match["file_path"].iloc[0],
                        }
            except Exception as e:
                logger.warning(f"Could not read calibration plot registry during render check: {e}")

        # 2. Extract cohort data (no plotting)
        logger.info(f"Extracting plot data for {route_id} ({config_type}: N0={n0}, tau={tau:.2f}, K={kmax})")
        payload = extract_cohort_plot_data(route_id, config_type, n0, tau, kmax, replicate, registry_df)
        
        rel_path = out_path.relative_to(BASE_DIR).as_posix()
        return {
            "status": "success",
            "task": task,
            "payload": payload,
            "newly_generated": True,
            "rel_path": rel_path,
        }
    except Exception as e:
        logger.error(f"Failed to extract plot data for task {task}: {e}", exc_info=True)
        return {"status": "failed", "task": task, "error": str(e)}


def _register_plots(results: list[dict]) -> None:
    """
    Main-thread only: updates CALIBRATION_PLOT_REGISTRY for newly generated plots.
    """
    new_rows = []
    for res in results:
        if res.get("status") == "success" and res.get("newly_generated", False):
            task = res["task"]
            new_rows.append({
                "route_id": task["route_id"],
                "config_type": str(task["config_type"]).upper(),
                "N_0": int(task["n0"]),
                "tau": float(task["tau"]),
                "K_max": int(task["kmax"]),
                "replicate": int(task["replicate"]),
                "file_path": res["rel_path"],
            })

    if not new_rows:
        return

    df_new = pd.DataFrame(new_rows)
    if CALIBRATION_PLOT_REGISTRY.exists():
        try:
            df_existing = pd.read_parquet(CALIBRATION_PLOT_REGISTRY)
            keys = ["route_id", "config_type", "N_0", "tau", "K_max", "replicate"]
            df_updated = pd.concat([df_existing, df_new]).drop_duplicates(subset=keys, keep="last")
        except Exception as e:
            logger.warning(f"Could not update existing calibration plot registry, overwriting: {e}")
            df_updated = df_new
    else:
        df_updated = df_new

    CALIBRATION_PLOT_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    df_updated.to_parquet(CALIBRATION_PLOT_REGISTRY, index=False)
    logger.info(f"Registered {len(new_rows)} plot(s) to calibration plot registry.")


def get_or_create_config_plot(
    route_id: str,
    config_type: str,
    n0: int,
    tau: float,
    kmax: int,
    replicate: int,
    registry_df: pd.DataFrame = None,
) -> Path:
    """
    Thread-safe lookup of calibration plot in CALIBRATION_PLOT_REGISTRY.
    If cached, returns path. Otherwise, extracts data and draws/registers it.
    """
    task = {
        "route_id": route_id,
        "config_type": config_type,
        "n0": n0,
        "tau": tau,
        "kmax": kmax,
        "replicate": replicate,
    }
    res = _render_plot_if_needed(task, registry_df=registry_df)
    if res.get("status") != "success":
        raise RuntimeError(f"Plot generation failed: {res.get('error')}")
    
    out_path = CALIBRATION_PLOTS_DIR / Path(res["rel_path"]).name
    if res.get("newly_generated", False):
        assemble_and_save_plot(res["payload"], out_path)
        _register_plots([res])
        return out_path
    
    return Path(res["path"])


def _worker_generate_plot(task: dict) -> dict:
    """Helper target for parallel processing pool."""
    return _render_plot_if_needed(task)


def batch_generate_plots(plot_tasks: list[dict], max_workers: int = None) -> list:
    """
    Executes multiple plot data extractions in parallel, then renders them
    sequentially on the main thread to ensure absolute thread-safety.
    """
    if not plot_tasks:
        return []

    if max_workers is None:
        max_workers = min(4, multiprocessing.cpu_count())

    logger.info(f"Batch extracting data for {len(plot_tasks)} plots with {max_workers} workers...")
    
    results = []
    if max_workers <= 1:
        for task in plot_tasks:
            results.append(_worker_generate_plot(task))
    else:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=max_workers) as pool:
            results = pool.map(_worker_generate_plot, plot_tasks)

    # Render newly generated plots sequentially on the main thread
    for res in results:
        if res.get("status") == "success" and res.get("newly_generated", False):
            out_path = CALIBRATION_PLOTS_DIR / Path(res["rel_path"]).name
            try:
                assemble_and_save_plot(res["payload"], out_path)
                res["path"] = str(out_path)
            except Exception as e:
                logger.error(f"Failed to assemble and save plot to {out_path}: {e}", exc_info=True)
                res["status"] = "failed"
                res["error"] = str(e)
                res["newly_generated"] = False

    _register_plots(results)
    return results
