"""
3D Variational Parameter Orchestrator (Per-Route Iterative Mode).
=================================================================
Sweeps across N_0, tau, and K_max iteratively for each calibration route,
producing individual summary CSVs and multi-page PDF reports per route.
"""

import os
import argparse
import logging
import sys
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from src.analysis.campaigns.variational.gt_stability_sweep import _compute_geometric_error, _prepare_oracle
from src.analysis.campaigns.variational.variational_plots import generate_route_pdf_report
from src.common.config import BASE_DIR, CALIBRATION_ROUTES, D_PCA, SILHOUETTE_THRESHOLD, CALIBRATION_FLIGHT_CLUSTER_MAP
from src.common.registry_utils import load_trajectory_registry
from src.common.utils import setup_file_logger
from src.core.corridor.clustering_worker import _select_medoid
from src.core.corridor.pca_compressor import calculate_delta_cv

logger = logging.getLogger(__name__)

DEFAULT_N0_VALUES = [16, 24, 32, 48, 64]
DEFAULT_TAU_VALUES = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]
DEFAULT_KMAX_VALUES = [1, 2, 3, 4]  # Reduced for faster evaluation
DEFAULT_REPLICATES = 30
MAX_RERUNS = 2
CALIBRATION_OUT_DIR = BASE_DIR / "data" / "calibration"


def _evaluate_custom_k(X_pca: np.ndarray, max_k_limit: int) -> tuple:
    """Evaluates optimal k up to max_k_limit using Silhouette score."""
    n = len(X_pca)
    best_k = 1
    best_labels = np.zeros(n, dtype=int)

    if max_k_limit <= 1:
        return best_k, best_labels

    max_k = min(max_k_limit, n - 1)
    if max_k >= 2:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score

        best_score = -1.0
        for k in range(2, max_k + 1):
            try:
                km = KMeans(n_clusters=k, random_state=42, n_init=5).fit(X_pca)
                score = silhouette_score(X_pca, km.labels_)
                if score > best_score and score >= SILHOUETTE_THRESHOLD:
                    best_score = score
                    best_k = k
                    best_labels = km.labels_
            except Exception:
                continue

    return best_k, best_labels


def run_route_variational_sweep(
    route_id: str,
    route_data: dict,
    n0_vals: list[int],
    tau_vals: list[float],
    kmax_vals: list[int],
    replicates: int,
    out_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Runs the 3D grid sweep simulation for a single route."""
    raw_records = []
    flight_mappings = []
    total_cells = len(n0_vals) * len(tau_vals) * len(kmax_vals)
    logger.info(f"[{route_id}] Sweeping {total_cells} parameter cells across {replicates} replicates...")

    X_raw = route_data["X_raw"]
    X_scaled = route_data["X_scaled"]
    is_clean = route_data["is_clean"]
    norm_flights = route_data["norm_flights"]
    oracle_vecs = route_data["oracle_medoid_vecs"]
    n_avail = route_data["n_flights"]

    raw_path = out_dir / f"{route_id}_variational_raw.csv"
    df_existing_raw = None
    if raw_path.exists():
        try:
            df_existing_raw = pd.read_csv(raw_path)
            logger.info(f"[{route_id}] Found existing raw sweep records. Scanning for cache hits...")
        except Exception:
            pass

    for n0 in n0_vals:
        for tau in tau_vals:
            for kmax in kmax_vals:
                # Check cache first
                cell_cached = False
                if df_existing_raw is not None:
                    match = df_existing_raw[
                        (df_existing_raw["N_0"] == n0) &
                        (df_existing_raw["tau"] == tau) &
                        (df_existing_raw["K_max"] == kmax)
                    ]
                    if len(match) >= replicates:
                        cell_cached = True
                        logger.debug(f"  [{route_id}] Cell (N0={n0}, tau={tau:.2f}, Kmax={kmax}) cache hit.")
                        for _, row in match.head(replicates).iterrows():
                            raw_records.append({
                                "N_0": int(row["N_0"]),
                                "tau": float(row["tau"]),
                                "K_max": int(row["K_max"]),
                                "route": str(row["route"]),
                                "seed": int(row["seed"]),
                                "final_N": int(row["final_N"]),
                                "converged_round": int(row["converged_round"]),
                                "dcv_pca": float(row["dcv_pca"]),
                                "k_sample": int(row["k_sample"]),
                                "geom_err_km": float(row["geom_err_km"]),
                            })

                if cell_cached:
                    continue

                for seed in range(replicates):
                    current_n = n0
                    converged_round = MAX_RERUNS

                    for r_idx in range(MAX_RERUNS + 1):
                        n_eff = min(current_n, n_avail)
                        rng = np.random.default_rng(seed=(abs(hash(route_id)) + n_eff * 1000 + seed) % 2**32)
                        idx = rng.choice(n_avail, size=n_eff, replace=False)

                        sample_scaled = X_scaled[idx]
                        sample_raw = X_raw[idx]
                        sample_clean = [is_clean[i] for i in idx]

                        d_comp = min(D_PCA, n_eff - 1)
                        pca_sample = PCA(n_components=d_comp)
                        sample_pca = pca_sample.fit_transform(sample_scaled)

                        half = n_eff // 2
                        dcv_pca = float(calculate_delta_cv(
                            np.var(sample_pca[:half], axis=0),
                            np.var(sample_pca[half:], axis=0)
                        ))

                        if dcv_pca < tau or r_idx == MAX_RERUNS:
                            converged_round = r_idx
                            k_sample, labels_sample = _evaluate_custom_k(sample_pca, kmax)

                            sample_medoid_indices = []
                            for c_id in range(k_sample):
                                m_idx = _select_medoid(sample_pca, labels_sample == c_id, sample_clean)
                                sample_medoid_indices.append(m_idx)

                            sample_medoid_vecs = sample_raw[sample_medoid_indices]
                            geom_err = _compute_geometric_error(sample_medoid_vecs, oracle_vecs)

                            raw_records.append({
                                "N_0": n0,
                                "tau": tau,
                                "K_max": kmax,
                                "route": route_id,
                                "seed": seed,
                                "final_N": n_eff,
                                "converged_round": converged_round,
                                "dcv_pca": dcv_pca,
                                "k_sample": k_sample,
                                "geom_err_km": geom_err,
                            })

                            # Capture flight cluster mappings for this sweep configuration cell
                            sample_medoid_indices_set = set(sample_medoid_indices)
                            for j, global_idx in enumerate(idx):
                                flight_obj = norm_flights[global_idx]
                                flight_id = getattr(flight_obj, "flight_id", None) or str(global_idx)
                                flight_mappings.append({
                                    "route_id": route_id,
                                    "N_0": n0,
                                    "tau": tau,
                                    "K_max": kmax,
                                    "replicate": seed,
                                    "flight_id": flight_id,
                                    "cluster_id": int(labels_sample[j]),
                                    "is_medoid": j in sample_medoid_indices_set
                                })
                            break

                        current_n *= 2

    df_raw = pd.DataFrame(raw_records)

    summary_rows = []
    for (n0, tau, kmax), grp in df_raw.groupby(["N_0", "tau", "K_max"]):
        summary_rows.append({
            "N_0": n0,
            "tau": tau,
            "K_max": kmax,
            "avg_queries": round(grp["final_N"].mean(), 1),
            "median_geom_err_km": round(grp["geom_err_km"].median(), 2),
            "p90_geom_err_km": round(grp["geom_err_km"].quantile(0.90), 2),
            "pct_conv_round0": round((grp["converged_round"] == 0).mean() * 100, 1),
            "pct_maxed_out": round((grp["converged_round"] == MAX_RERUNS).mean() * 100, 1),
            "avg_final_k": round(grp["k_sample"].mean(), 2),
        })

    df_summary = pd.DataFrame(summary_rows)
    return df_raw, df_summary, flight_mappings


def _is_oom_error(exc: Exception) -> bool:
    """Returns True if the exception looks like an out-of-memory or broken worker pool failure."""
    import concurrent.futures
    if isinstance(exc, (MemoryError, concurrent.futures.process.BrokenProcessPool)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in [
        "memoryerror",
        "unable to allocate",
        "terminated abruptly",
        "brokenprocesspool",
        "memory limit exceeded",
    ])


def _save_route_results(
    route_id: str,
    df_raw: pd.DataFrame,
    df_summary: pd.DataFrame,
    flight_mappings: list,
    out_dir: Path,
    route_data_cache: dict,
    dry_run: bool,
) -> None:
    """Saves CSVs, generates cohort plots, and compiles the PDF report."""
    df_raw.to_csv(out_dir / f"{route_id}_variational_raw.csv", index=False)
    df_summary.to_csv(out_dir / f"{route_id}_variational_summary.csv", index=False)

    # 1. Save flight mappings to CALIBRATION_FLIGHT_CLUSTER_MAP first
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
        df_updated.to_parquet(CALIBRATION_FLIGHT_CLUSTER_MAP, index=False)
        logger.info(f"Saved {len(df_new)} flight mappings to calibration cluster map.")

    # 2. Identify Pareto frontier points and batch generate plots post-simulation
    if not dry_run:
        pts = df_summary.sort_values("avg_queries")
        pareto_pts = []
        min_err_so_far = float("inf")
        for _, row in pts.iterrows():
            if row["median_geom_err_km"] < min_err_so_far:
                pareto_pts.append(row)
                min_err_so_far = row["median_geom_err_km"]

        pareto_df = pd.DataFrame(pareto_pts) if pareto_pts else pd.DataFrame()

        # Build task list
        plot_tasks = []
        # Add Oracle task
        plot_tasks.append({
            "route_id": route_id,
            "config_type": "ORACLE",
            "n0": -1,
            "tau": -1.0,
            "kmax": -1,
            "replicate": -1
        })
        # Add Pareto tasks (replicate 0)
        for _, p_row in pareto_df.iterrows():
            plot_tasks.append({
                "route_id": route_id,
                "config_type": "PARETO",
                "n0": int(p_row["N_0"]),
                "tau": float(p_row["tau"]),
                "kmax": int(p_row["K_max"]),
                "replicate": 0
            })

        # Run batch plot generation
        from src.analysis.campaigns.common.plot_helpers import batch_generate_plots
        try:
            batch_generate_plots(plot_tasks)
        except Exception as e:
            logger.error(f"Error during batch plot generation for {route_id}: {e}", exc_info=True)

        # 3. Generate PDF Report
        pdf_path = generate_route_pdf_report(
            route_id, df_summary, route_data_cache[route_id], out_dir, pareto_df=pareto_df
        )
        if pdf_path:
            print(f"  -> Generated PDF Report: {pdf_path.name}")

    best_rows = df_summary[df_summary["median_geom_err_km"] <= 15.0].sort_values("avg_queries").head(3)
    print(f"  Top Sub-15km Configs for {route_id}:")
    for _, r in best_rows.iterrows():
        print(f"    (N0={int(r['N_0'])}, tau={r['tau']:.2f}, Kmax={int(r['K_max'])}) -> "
              f"AvgQueries={r['avg_queries']:.1f}, MedErr={r['median_geom_err_km']:.2f} km")


def main() -> None:
    parser = argparse.ArgumentParser(description="3D Variational Parameter Orchestrator (Iterative Mode)")
    parser.add_argument("--replicates", type=int, default=DEFAULT_REPLICATES,
                        help="Bootstrap replicates per parameter cell (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run 1 route, 2 replicates for sanity testing (no PDF output)")
    parser.add_argument("--max-workers", type=int, default=None,
                        help="Override the starting number of parallel process workers")
    parser.add_argument("--disable-scale-up", action="store_true",
                        help="Disable dynamic worker count-up scaling after successful batches")
    parser.add_argument("--out-dir", type=str, default=None,
                        help="Output directory for CSVs and PDFs (default: data/calibration/)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else CALIBRATION_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    registry_df = load_trajectory_registry()

    routes_to_run = CALIBRATION_ROUTES[:1] if args.dry_run else CALIBRATION_ROUTES
    reps = 2 if args.dry_run else args.replicates

    # Check which routes are fully cached
    routes_needing_run = []
    completed_raw = []
    completed_summary = []

    for route_id in routes_to_run:
        raw_path = out_dir / f"{route_id}_variational_raw.csv"
        summary_path = out_dir / f"{route_id}_variational_summary.csv"
        cache_hit = False
        if raw_path.exists() and summary_path.exists():
            try:
                df_raw = pd.read_csv(raw_path)
                df_summary = pd.read_csv(summary_path)

                # Check if all cells exist with reps rows
                all_cells_exist = True
                for n0 in DEFAULT_N0_VALUES:
                    for tau in DEFAULT_TAU_VALUES:
                        for kmax in DEFAULT_KMAX_VALUES:
                            match = df_raw[
                                (df_raw["N_0"] == n0) &
                                (df_raw["tau"] == tau) &
                                (df_raw["K_max"] == kmax)
                            ]
                            if len(match) < reps:
                                all_cells_exist = False
                                break
                        if not all_cells_exist:
                            break
                    if not all_cells_exist:
                        break

                if all_cells_exist:
                    cache_hit = True
                    completed_raw.append(df_raw)
                    completed_summary.append(df_summary)
                    logger.info(f"[{route_id}] Sweep is 100% cached. Skipping simulation.")
            except Exception as e:
                logger.warning(f"Could not read existing sweep files for {route_id} cache check: {e}")

        if not cache_hit:
            routes_needing_run.append(route_id)

    # 1. Prepare Oracle baselines sequentially (avoids concurrent parquet I/O contention)
    route_data_cache = {}
    if routes_needing_run:
        print("\n" + "="*95)
        print("PREPARING ORACLE BASELINES")
        print("="*95)
        for route_id in routes_needing_run:
            try:
                print(f"  Preparing Oracle for {route_id}...")
                route_data_cache[route_id] = _prepare_oracle(route_id, registry_df)
            except Exception as exc:
                logger.error(f"Failed to prepare Oracle for {route_id}: {exc}")

    if not route_data_cache and not completed_raw:
        logger.error("No routes prepared successfully and no cached runs found. Aborting.")
        return

    # 2. Determine starting worker count from available memory and CPU count
    if args.max_workers is not None:
        current_workers = min(len(route_data_cache), args.max_workers)
    else:
        import psutil
        total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        free_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        # Each worker loads ~500-700 MB of scientific libraries into a fresh process.
        # Estimate safe concurrency from available memory, bounded by CPU count and route count.
        ram_based = max(1, int(free_ram_gb / 0.6))
        cpu_based = os.cpu_count() or 1
        current_workers = min(len(route_data_cache), ram_based, cpu_based)
        logger.info(
            f"System: {total_ram_gb:.1f} GB total RAM, {free_ram_gb:.1f} GB free. "
            f"Starting with max_workers={current_workers}."
        )

    # 3. OOM-resilient dispatch loop
    # Maintains a queue of pending route IDs. On MemoryError, scales workers
    # down by 1 and re-queues the failed route. Continues until all routes
    # finish or worker count reaches 1 and still OOMs (gives up on that route).
    pending_routes = list(route_data_cache.keys())
    start_times = {r: time.perf_counter() for r in pending_routes}
    
    limit_reached = False

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
                    run_route_variational_sweep,
                    route_id,
                    route_data_cache[route_id],
                    DEFAULT_N0_VALUES,
                    DEFAULT_TAU_VALUES,
                    DEFAULT_KMAX_VALUES,
                    reps,
                    out_dir,
                ): route_id
                for route_id in batch
            }

            for future in as_completed(futures):
                route_id = futures[future]
                try:
                    df_raw, df_summary, flight_mappings = future.result()
                    elapsed = time.perf_counter() - start_times[route_id]
                    logger.info(f"[{route_id}] Sweep completed in {elapsed:.2f}s.")

                    _save_route_results(
                        route_id, df_raw, df_summary, flight_mappings, out_dir, route_data_cache, args.dry_run
                    )
                    completed_raw.append(df_raw)
                    completed_summary.append(df_summary)

                except Exception as exc:
                    if _is_oom_error(exc):
                        logger.warning(
                            f"[{route_id}] OOM/MemoryError at max_workers={current_workers}: {exc}"
                        )
                        pending_routes.append(route_id)
                        start_times[route_id] = time.perf_counter()  # reset timer for retry
                        oom_occurred = True
                    else:
                        logger.error(f"[{route_id}] Fatal sweep error (not OOM, not retrying): {exc}")

        if oom_occurred:
            limit_reached = True
            new_workers = max(1, current_workers - 1)
            if new_workers < current_workers:
                logger.warning(
                    f"OOM detected. Scaling workers {current_workers} -> {new_workers}. "
                    f"Re-queuing {len(pending_routes)} route(s): {pending_routes}"
                )
                current_workers = new_workers
            else:
                # Already at 1 worker and still OOM
                logger.error(
                    f"OOM persists at max_workers=1. Cannot scale further. "
                    f"Aborting {len(pending_routes)} route(s): {pending_routes}"
                )
                break
        elif not limit_reached and not args.disable_scale_up:
            new_workers = min(current_workers + 1, os.cpu_count() or 1, len(route_data_cache))
            if new_workers > current_workers:
                logger.info(f"No OOM detected. Aggressively scaling workers {current_workers} -> {new_workers}.")
                current_workers = new_workers


    # 4. Write combined summary
    if completed_raw and not args.dry_run:
        df_all_raw = pd.concat(completed_raw, ignore_index=True)
        df_all_summary = pd.concat(completed_summary, ignore_index=True)
        df_all_raw.to_csv(out_dir / "all_routes_variational_raw.csv", index=False)
        df_all_summary.to_csv(out_dir / "all_routes_variational_summary.csv", index=False)
        print("\n" + "="*95)
        print(f"ALL ROUTES COMPLETED. Combined summary saved to {out_dir}/")
        print("="*95)


if __name__ == "__main__":
    setup_file_logger(log_filename="calibration.log")
    setup_file_logger("variational_orchestrator.log")
    main()
