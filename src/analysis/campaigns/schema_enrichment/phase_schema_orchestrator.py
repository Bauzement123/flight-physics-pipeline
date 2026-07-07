"""
Phase Schema Calibration Orchestrator.
======================================
Evaluates the query cost, acceptance rate, cluster structure, and geometric error
when filtering candidate trajectories by clean flight phase progression:
ONGROUND -> CLIMB -> CRUISE -> DESCENT -> ONGROUND.

Sweeps across N_0 (target sample size) and K_max (maximum number of clusters)
for each calibration route across bootstrap replicates.
"""

import os
import argparse
import logging
import sys
import time
import multiprocessing
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from sklearn.decomposition import PCA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from src.analysis.campaigns.variational.gt_stability_sweep import _compute_geometric_error, _prepare_oracle
from src.analysis.campaigns.variational.variational_orchestrator import _evaluate_custom_k, _is_oom_error
from src.analysis.campaigns.common.plot_helpers import batch_generate_plots
from src.common.config import (
    BASE_DIR,
    CALIBRATION_ROUTES,
    D_PCA,
    CALIBRATION_FLIGHT_CLUSTER_MAP,
    CALIBRATION_PLOTS_DIR,
)
from src.common.registry_utils import load_trajectory_registry
from src.common.utils import setup_file_logger
from src.core.corridor.clustering_worker import _select_medoid
from src.core.corridor.pca_compressor import classify_and_normalize_cohort, vectorize_cohort, normalize_vectors
from src.core.corridor.stability_worker import _load_route_flights_full_phase

logger = logging.getLogger(__name__)

DEFAULT_N0_VALUES = [16, 24, 32, 48, 64]
DEFAULT_KMAX_VALUES = [1, 2, 3, 4]
DEFAULT_REPLICATES = 30
DEFAULT_OUT_DIR = BASE_DIR / "data" / "calibration" / "phase_schema"


def generate_phase_schema_pdf_report(
    route_id: str,
    df_summary: pd.DataFrame,
    oracle_data: dict,
    out_dir: Path,
    pareto_df: pd.DataFrame = None,
) -> Path:
    """
    Compiles a multi-page PDF report for the phase schema calibration campaign.
    Page 1: Executive Summary & Metrics Table.
    Page 2: 2x2 Dashboard of Query Cost, Acceptance Rate, Exhaustion Rate, and Error Curves.
    Pages 3+: Visual cluster maps (Oracle and Pareto configs).
    """
    pdf_path = out_dir / f"{route_id}_phase_schema_report.pdf"
    logger.info(f"[{route_id}] Compiling PDF report to {pdf_path}...")

    with PdfPages(pdf_path) as pdf:
        # ---------------------------------------------------------
        # PAGE 1: Executive Summary & Metrics Table
        # ---------------------------------------------------------
        fig = plt.figure(figsize=(11, 8.5))
        fig.suptitle(
            f"Phase Schema Calibration Report: {route_id}",
            fontsize=18,
            fontweight="bold",
            y=0.95
        )
        ax_text = fig.add_axes([0.05, 0.82, 0.90, 0.10])
        ax_text.axis("off")
        
        n_avail = oracle_data.get("n_flights", "N/A")
        summary_text = (
            f"Schema Pattern: ONGROUND -> CLIMB -> CRUISE -> DESCENT -> ONGROUND\n"
            f"Oracle Baseline Cohort Size: {n_avail} flights | "
            f"Total Sweep Configurations: {len(df_summary)} cells\n"
            f"This campaign measures database query cost and acceptance rates to obtain N_0 usable trajectories."
        )
        ax_text.text(0.0, 0.5, summary_text, fontsize=11, verticalalignment="center")

        # Table of metrics
        ax_table = fig.add_axes([0.05, 0.05, 0.90, 0.75])
        ax_table.axis("off")

        cols_to_show = [
            "N_0", "K_max", "avg_queries", "avg_valid", "avg_acceptance_rate",
            "exhaustion_rate", "median_geom_err_km", "iqr_geom_err_km", "avg_final_k"
        ]
        col_headers = [
            "Target N0", "K max", "Avg Queries", "Avg Valid", "Accept %",
            "Exhaust %", "Med Error (km)", "IQR Error (km)", "Avg K"
        ]
        
        display_df = df_summary[cols_to_show].copy().sort_values(["N_0", "K_max"])
        table_data = display_df.values.tolist()

        tbl = ax_table.table(
            cellText=table_data,
            colLabels=col_headers,
            loc="center",
            cellLoc="center"
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        tbl.scale(1.0, 1.4)

        # Style header
        for col_idx in range(len(col_headers)):
            cell = tbl[0, col_idx]
            cell.set_facecolor("#2c3e50")
            cell.set_text_props(color="white", fontweight="bold")

        # Striping
        for row_idx in range(len(table_data)):
            for col_idx in range(len(col_headers)):
                cell = tbl[row_idx + 1, col_idx]
                if row_idx % 2 == 1:
                    cell.set_facecolor("#f8f9fa")

        pdf.savefig(fig)
        plt.close(fig)

        # ---------------------------------------------------------
        # PAGE 2: 2x2 Visual Dashboard
        # ---------------------------------------------------------
        fig, axes = plt.subplots(2, 2, figsize=(11, 8.5))
        fig.suptitle(f"Phase Schema Calibration Dashboard: {route_id}", fontsize=16, fontweight="bold")

        # Top-Left: Query Cost vs Target N_0
        ax = axes[0, 0]
        for kmax, grp in df_summary.groupby("K_max"):
            ax.plot(grp["N_0"], grp["avg_queries"], marker="o", label=f"K_max={kmax}")
        ax.set_xlabel("Target Sample Size (N_0)")
        ax.set_ylabel("Avg Database Queries (Q)")
        ax.set_title("Query Cost vs Target N_0", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend()

        # Top-Right: Geometric Error vs Target N_0
        ax = axes[0, 1]
        for kmax, grp in df_summary.groupby("K_max"):
            ax.plot(grp["N_0"], grp["median_geom_err_km"], marker="s", label=f"K_max={kmax}")
        ax.set_xlabel("Target Sample Size (N_0)")
        ax.set_ylabel("Median Geometric Error (km)")
        ax.set_title("Geometric Error vs Oracle Baseline", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend()

        # Bottom-Left: Acceptance Rate & Exhaustion Rate vs Target N_0
        ax = axes[1, 0]
        agg_n0 = df_summary.groupby("N_0")[["avg_acceptance_rate", "exhaustion_rate"]].mean().reset_index()
        ax.plot(agg_n0["N_0"], agg_n0["avg_acceptance_rate"], marker="^", color="green", label="Acceptance Rate (%)")
        ax.plot(agg_n0["N_0"], agg_n0["exhaustion_rate"], marker="x", color="red", linestyle="--", label="Exhaustion Rate (%)")
        ax.set_xlabel("Target Sample Size (N_0)")
        ax.set_ylabel("Percentage (%)")
        ax.set_title("Acceptance & Exhaustion Rates", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend()

        # Bottom-Right: Optimal Clusters Selected vs Target N_0
        ax = axes[1, 1]
        for kmax, grp in df_summary.groupby("K_max"):
            ax.plot(grp["N_0"], grp["avg_final_k"], marker="d", label=f"K_max={kmax}")
        ax.set_xlabel("Target Sample Size (N_0)")
        ax.set_ylabel("Average Optimal k_sample")
        ax.set_title("Optimal Clusters Selected (Silhouette)", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend()

        fig.tight_layout(rect=[0, 0, 1, 0.95])
        pdf.savefig(fig)
        plt.close(fig)

        # ---------------------------------------------------------
        # PAGES 3+: Visual Cluster Maps (Oracle & Pareto)
        # ---------------------------------------------------------
        vis_items = []
        # Check for Oracle plot
        oracle_plot = CALIBRATION_PLOTS_DIR / f"{route_id}_ORACLE_N-1_tau-1.0_k-1_rep-1.png"
        if oracle_plot.exists():
            vis_items.append({"title": f"Oracle Baseline Ground Truth ({route_id})", "path": oracle_plot})

        if pareto_df is not None and not pareto_df.empty:
            for _, p_row in pareto_df.iterrows():
                n0_val = int(p_row["N_0"])
                kmax_val = int(p_row["K_max"])
                plot_path = CALIBRATION_PLOTS_DIR / f"{route_id}_PARETO_N{n0_val}_tau-1.0_k{kmax_val}_rep0.png"
                if plot_path.exists():
                    vis_items.append({
                        "title": f"Pareto Config: N0={n0_val}, Kmax={kmax_val} | Queries: {p_row['avg_queries']:.1f} | Error: {p_row['median_geom_err_km']:.2f} km",
                        "path": plot_path
                    })

        if vis_items:
            num_items = len(vis_items)
            items_per_page = 2
            for page_idx in range(0, num_items, items_per_page):
                fig, axes = plt.subplots(items_per_page, 1, figsize=(11, 8.5))
                if items_per_page == 1:
                    axes = [axes]
                fig.suptitle(f"Cluster & Medoid Visualizations ({route_id})", fontsize=16, fontweight="bold")
                
                for box_idx in range(items_per_page):
                    ax = axes[box_idx]
                    item_idx = page_idx + box_idx
                    if item_idx < num_items:
                        item = vis_items[item_idx]
                        try:
                            img = plt.imread(str(item["path"]))
                            ax.imshow(img)
                            ax.set_title(item["title"], fontsize=10, fontweight="bold", pad=5)
                        except Exception as e:
                            logger.error(f"Failed to display image in PDF sub-box: {e}")
                            ax.text(0.5, 0.5, f"Error loading plot image:\n{item['path'].name}", ha="center", va="center")
                    ax.axis("off")
                
                fig.tight_layout(rect=[0, 0, 1, 0.95])
                pdf.savefig(fig, dpi=150)
                plt.close(fig)

    logger.info(f"Successfully compiled report for {route_id}: {pdf_path}")
    return pdf_path


def run_route_phase_schema_sweep(
    route_id: str,
    oracle_data: dict,
    registry_df: pd.DataFrame,
    n0_vals: list[int],
    kmax_vals: list[int],
    replicates: int,
    out_dir: Path,
    level_as_cruise: bool = True,
    min_phase_run_points: int = 1,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Runs the phase schema calibration sweep simulation for a single route."""
    raw_records = []
    flight_mappings = []
    total_cells = len(n0_vals) * len(kmax_vals)
    logger.info(f"[{route_id}] Sweeping {total_cells} phase schema parameter cells across {replicates} replicates...")

    oracle_vecs = oracle_data["oracle_medoid_vecs"]

    # 1. Load and validate phase schema for all candidate flights ONCE per route
    logger.info(f"[{route_id}] Scanning database for candidate trajectories and validating phase progression...")
    all_records, scan_stats = _load_route_flights_full_phase(
        route_id,
        registry_df,
        level_as_cruise=level_as_cruise,
        min_phase_run_points=min_phase_run_points,
    )

    valid_records = [r for r in all_records if r["is_valid"]]
    valid_flights = [r["flight_obj"] for r in valid_records]

    # Pre-vectorize and normalize all valid airborne trajectories
    if valid_flights:
        norm_flights, is_clean = classify_and_normalize_cohort(valid_flights)
        if norm_flights:
            X_raw = vectorize_cohort(norm_flights)
            X_scaled, mean_vec, std_vec = normalize_vectors(X_raw)
        else:
            X_raw = np.empty((0, D_PCA))
            X_scaled = np.empty((0, D_PCA))
            is_clean = []
    else:
        norm_flights = []
        is_clean = []
        X_raw = np.empty((0, D_PCA))
        X_scaled = np.empty((0, D_PCA))

    # Map each flight ID that survived normalization to its array index
    fid_to_idx = {}
    for i, fl in enumerate(norm_flights):
        fid = getattr(fl, "flight_id", None)
        if fid is None and hasattr(fl, "data") and "flight_id" in fl.data.columns:
            fid = str(fl.data["flight_id"].iloc[0])
        elif fid is not None:
            fid = str(fid)
        if fid is not None:
            fid_to_idx[fid] = i

    candidate_fids = [str(r["flight_id"]) for r in all_records]

    for seed in range(replicates):
        # Shuffling candidate flight IDs simulates random database queries without replacement
        rng = np.random.default_rng(seed=(abs(hash(route_id)) + seed * 1000) % 2**32)
        shuffled_fids = list(candidate_fids)
        rng.shuffle(shuffled_fids)

        for n0 in n0_vals:
            # Simulate scanning database until n0 valid trajectories are found
            valid_sampled_indices = []
            queries_needed = 0
            for fid in shuffled_fids:
                queries_needed += 1
                if fid in fid_to_idx:
                    valid_sampled_indices.append(fid_to_idx[fid])
                    if len(valid_sampled_indices) == n0:
                        break

            n_eff = len(valid_sampled_indices)
            exhausted = bool(n_eff < n0)
            acceptance_rate = float(n_eff / queries_needed) if queries_needed > 0 else 0.0

            for kmax in kmax_vals:
                if n_eff == 0:
                    raw_records.append({
                        "N_0": int(n0),
                        "tau": -1.0,
                        "K_max": int(kmax),
                        "route": route_id,
                        "seed": int(seed),
                        "queried_flights": int(queries_needed),
                        "valid_flights": 0,
                        "acceptance_rate": 0.0,
                        "exhausted": True,
                        "k_sample": 1,
                        "geom_err_km": np.nan,
                    })
                    continue

                idx = valid_sampled_indices
                sample_scaled = X_scaled[idx]
                sample_raw = X_raw[idx]
                sample_clean = [is_clean[i] for i in idx]
                sample_flights = [norm_flights[i] for i in idx]

                d_comp = min(D_PCA, n_eff - 1) if n_eff > 1 else 1
                if n_eff > 1:
                    pca_sample = PCA(n_components=d_comp)
                    sample_pca = pca_sample.fit_transform(sample_scaled)
                else:
                    sample_pca = sample_scaled

                k_sample, labels_sample = _evaluate_custom_k(sample_pca, kmax)

                sample_medoid_indices = []
                for c_id in range(k_sample):
                    m_idx = _select_medoid(sample_pca, labels_sample == c_id, sample_clean)
                    sample_medoid_indices.append(m_idx)

                sample_medoid_vecs = sample_raw[sample_medoid_indices]
                geom_err = _compute_geometric_error(sample_medoid_vecs, oracle_vecs)

                raw_records.append({
                    "N_0": int(n0),
                    "tau": -1.0,
                    "K_max": int(kmax),
                    "route": route_id,
                    "seed": int(seed),
                    "queried_flights": int(queries_needed),
                    "valid_flights": int(n_eff),
                    "acceptance_rate": float(acceptance_rate),
                    "exhausted": bool(exhausted),
                    "k_sample": int(k_sample),
                    "geom_err_km": float(geom_err) if geom_err is not None else np.nan,
                })

                # Record cluster mappings for plotting
                sample_medoid_indices_set = set(sample_medoid_indices)
                for j, global_idx in enumerate(idx):
                    flight_obj = norm_flights[global_idx]
                    flight_id = getattr(flight_obj, "flight_id", None) or str(global_idx)
                    flight_mappings.append({
                        "route_id": route_id,
                        "N_0": int(n0),
                        "tau": -1.0,
                        "K_max": int(kmax),
                        "replicate": int(seed),
                        "flight_id": str(flight_id),
                        "cluster_id": int(labels_sample[j]),
                        "is_medoid": j in sample_medoid_indices_set,
                    })

    df_raw = pd.DataFrame(raw_records)

    summary_rows = []
    for (n0, kmax), grp in df_raw.groupby(["N_0", "K_max"]):
        summary_rows.append({
            "route": route_id,
            "N_0": int(n0),
            "tau": -1.0,
            "K_max": int(kmax),
            "replicates": len(grp),
            "avg_queries": round(grp["queried_flights"].mean(), 1),
            "std_queries": round(grp["queried_flights"].std(), 1) if len(grp) > 1 else 0.0,
            "avg_valid": round(grp["valid_flights"].mean(), 1),
            "avg_acceptance_rate": round(grp["acceptance_rate"].mean() * 100.0, 1),
            "exhaustion_rate": round(grp["exhausted"].mean() * 100.0, 1),
            "median_geom_err_km": round(grp["geom_err_km"].median(), 2),
            "iqr_geom_err_km": round(grp["geom_err_km"].quantile(0.75) - grp["geom_err_km"].quantile(0.25), 2),
            "min_geom_err_km": round(grp["geom_err_km"].min(), 2),
            "max_geom_err_km": round(grp["geom_err_km"].max(), 2),
            "avg_final_k": round(grp["k_sample"].mean(), 2),
            "pct_conv_round0": round((grp["exhausted"] == False).mean() * 100.0, 1),
            "pct_maxed_out": round((grp["exhausted"] == True).mean() * 100.0, 1),
        })

    df_summary = pd.DataFrame(summary_rows)
    return df_raw, df_summary, flight_mappings


def _save_route_results(
    route_id: str,
    df_raw: pd.DataFrame,
    df_summary: pd.DataFrame,
    flight_mappings: list,
    out_dir: Path,
    oracle_data: dict,
    dry_run: bool,
    crop_airports: bool = False,
    crop_padding: float = 1.5,
) -> None:
    """Saves CSVs, updates cluster mappings, generates plots, and compiles the PDF report."""
    df_raw.to_csv(out_dir / f"{route_id}_phase_schema_raw.csv", index=False)
    df_summary.to_csv(out_dir / f"{route_id}_phase_schema_summary.csv", index=False)
    logger.info(f"[{route_id}] Saved raw and summary CSVs to {out_dir}")

    # 1. Save flight mappings to CALIBRATION_FLIGHT_CLUSTER_MAP
    if flight_mappings:
        df_new = pd.DataFrame(flight_mappings)
        if CALIBRATION_FLIGHT_CLUSTER_MAP.exists():
            try:
                df_existing = pd.read_parquet(CALIBRATION_FLIGHT_CLUSTER_MAP)
                keys = ["route_id", "N_0", "tau", "K_max", "replicate", "flight_id"]
                df_updated = pd.concat([df_existing, df_new]).drop_duplicates(subset=keys, keep="last")
            except Exception as e:
                logger.warning(f"Could not read existing calibration cluster map, overwriting: {e}")
                df_updated = df_new
        else:
            df_updated = df_new

        CALIBRATION_FLIGHT_CLUSTER_MAP.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write pattern for Windows Drive FUSE
        temp_file = CALIBRATION_FLIGHT_CLUSTER_MAP.with_suffix(".tmp.parquet")
        df_updated.to_parquet(temp_file, index=False)
        temp_file.replace(CALIBRATION_FLIGHT_CLUSTER_MAP)
        logger.info(f"Saved {len(df_new)} flight mappings to calibration cluster map.")

    # 2. Identify Pareto frontier points and batch generate plots post-simulation
    pareto_df = pd.DataFrame()
    if not dry_run and not df_summary.empty:
        pts = df_summary.sort_values("avg_queries")
        pareto_pts = []
        min_err_so_far = float("inf")
        for _, row in pts.iterrows():
            if row["median_geom_err_km"] < min_err_so_far:
                pareto_pts.append(row)
                min_err_so_far = row["median_geom_err_km"]

        pareto_df = pd.DataFrame(pareto_pts) if pareto_pts else pd.DataFrame()

        plot_tasks = []
        # Add Oracle task
        plot_tasks.append({
            "route_id": route_id,
            "config_type": "ORACLE",
            "n0": -1,
            "tau": -1.0,
            "kmax": -1,
            "replicate": -1,
            "crop_airports": crop_airports,
            "crop_padding": crop_padding,
        })
        # Add Pareto tasks (replicate 0)
        for _, p_row in pareto_df.iterrows():
            plot_tasks.append({
                "route_id": route_id,
                "config_type": "PARETO",
                "n0": int(p_row["N_0"]),
                "tau": -1.0,
                "kmax": int(p_row["K_max"]),
                "replicate": 0,
                "crop_airports": crop_airports,
                "crop_padding": crop_padding,
            })

        try:
            batch_generate_plots(plot_tasks)
        except Exception as e:
            logger.error(f"Error during batch plot generation for {route_id}: {e}", exc_info=True)

        # 3. Generate PDF Report
        pdf_path = generate_phase_schema_pdf_report(
            route_id, df_summary, oracle_data, out_dir, pareto_df=pareto_df
        )
        if pdf_path:
            print(f"  -> Generated Phase Schema Report: {pdf_path.name}")

    if not df_summary.empty:
        best_rows = df_summary[df_summary["median_geom_err_km"] <= 15.0].sort_values("avg_queries").head(3)
        print(f"  Top Sub-15km Configs for {route_id}:")
        for _, r in best_rows.iterrows():
            print(
                f"    (N0={int(r['N_0'])}, Kmax={int(r['K_max'])}) -> "
                f"AvgQueries={r['avg_queries']:.1f}, Accept={r['avg_acceptance_rate']:.1f}%, MedErr={r['median_geom_err_km']:.2f} km"
            )


def main() -> None:
    setup_file_logger("calibration")
    parser = argparse.ArgumentParser(description="Phase Schema Calibration Orchestrator")
    parser.add_argument("--replicates", type=int, default=DEFAULT_REPLICATES,
                        help="Bootstrap replicates per parameter cell (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run 1 route, 2 replicates for sanity testing")
    parser.add_argument("--max-workers", type=int, default=None,
                        help="Override the starting number of parallel process workers")
    parser.add_argument("--out-dir", type=str, default=None,
                        help="Output directory for CSVs and PDFs")
    parser.add_argument("--level-as-cruise", type=lambda x: str(x).lower() in ["true", "1", "yes"],
                        default=True, help="Map LVL segments to CRUISE (default: True)")
    parser.add_argument("--min-phase-run-points", type=int, default=3,
                        help="Minimum contiguous points in a phase run to avoid denoising (default: 3)")
    parser.add_argument("--crop-airports", action="store_true",
                        help="Crop generated plot maps to airport bounding box")
    parser.add_argument("--crop-padding", type=float, default=1.5,
                        help="Padding in degrees around airports when cropping (default: 1.5)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    registry_df = load_trajectory_registry()

    routes_to_run = CALIBRATION_ROUTES[:1] if args.dry_run else CALIBRATION_ROUTES
    reps = 2 if args.dry_run else args.replicates
    n0_vals = DEFAULT_N0_VALUES[:2] if args.dry_run else DEFAULT_N0_VALUES
    kmax_vals = DEFAULT_KMAX_VALUES[:2] if args.dry_run else DEFAULT_KMAX_VALUES

    # 1. Prepare Oracle baselines sequentially
    oracle_cache = {}
    print("\n" + "="*95)
    print("PREPARING ORACLE BASELINES FOR PHASE SCHEMA SWEEP")
    print("="*95)
    for route_id in routes_to_run:
        try:
            print(f"  Preparing Oracle for {route_id}...")
            oracle_cache[route_id] = _prepare_oracle(route_id, registry_df)
        except Exception as exc:
            logger.error(f"Failed to prepare Oracle for {route_id}: {exc}")

    if not oracle_cache:
        logger.error("No routes prepared successfully. Aborting.")
        return

    # 2. Determine starting worker count
    if args.max_workers is not None:
        current_workers = min(len(oracle_cache), args.max_workers)
    else:
        import psutil
        free_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        ram_based = max(1, int(free_ram_gb / 0.8))
        cpu_based = os.cpu_count() or 1
        current_workers = min(len(oracle_cache), ram_based, cpu_based)
        logger.info(f"Starting phase schema sweep with max_workers={current_workers}.")

    # 3. OOM-resilient dispatch loop
    pending_routes = list(oracle_cache.keys())
    start_times = {r: time.perf_counter() for r in pending_routes}

    while pending_routes:
        batch = pending_routes[:current_workers]
        pending_routes = pending_routes[current_workers:]

        print("\n" + "="*95)
        print(f"DISPATCHING {len(batch)} ROUTE(S) (max_workers={current_workers})")
        print("="*95)

        oom_occurred = False

        with ProcessPoolExecutor(max_workers=current_workers) as executor:
            futures = {
                executor.submit(
                    run_route_phase_schema_sweep,
                    route_id,
                    oracle_cache[route_id],
                    registry_df,
                    n0_vals,
                    kmax_vals,
                    reps,
                    out_dir,
                    args.level_as_cruise,
                    args.min_phase_run_points,
                ): route_id
                for route_id in batch
            }

            for future in as_completed(futures):
                route_id = futures[future]
                try:
                    df_raw, df_summary, flight_mappings = future.result()
                    _save_route_results(
                        route_id,
                        df_raw,
                        df_summary,
                        flight_mappings,
                        out_dir,
                        oracle_cache[route_id],
                        args.dry_run,
                        crop_airports=args.crop_airports,
                        crop_padding=args.crop_padding,
                    )
                    elapsed = time.perf_counter() - start_times[route_id]
                    print(f"[SUCCESS] Route {route_id} completed in {elapsed:.1f}s.")
                except Exception as exc:
                    if _is_oom_error(exc) and current_workers > 1:
                        logger.warning(f"OOM error on {route_id}. Scaling down workers and requeuing.")
                        oom_occurred = True
                        pending_routes.append(route_id)
                    else:
                        logger.error(f"[FAILURE] Route {route_id} failed: {exc}", exc_info=True)
                        print(f"[FAILURE] Route {route_id} failed: {exc}")

        if oom_occurred and current_workers > 1:
            current_workers -= 1
            logger.info(f"Scaled worker count down to {current_workers}.")

    print("\n" + "="*95)
    print("PHASE SCHEMA CALIBRATION SWEEP COMPLETE")
    print("="*95)


if __name__ == "__main__":
    main()
