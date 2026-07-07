"""
Ground Truth Geometric Error vs. Stability Metric Sweep
=========================================================
Benchmarks two candidate stability metrics (X_pca split-half vs X_scaled split-half)
against the Oracle Ground Truth Corridor Error across 6 fully fetched calibration routes.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from src.common.config import BASE_DIR, D_PCA, SILHOUETTE_THRESHOLD, CALIBRATION_ROUTES, GLOBAL_MODEL_REGISTRY, GLOBAL_FLIGHT_CLUSTER_MAP, ORACLE_COHORT_CACHE_DIR
from src.common.registry_utils import load_trajectory_registry
from src.common.utils import setup_file_logger
from src.core.corridor.pca_compressor import (
    classify_and_normalize_cohort,
    normalize_vectors,
    vectorize_cohort,
    calculate_delta_cv,
    fit_pca,
    apply_pca,
)
from src.core.corridor.clustering_worker import _evaluate_optimal_k, _select_medoid
from src.core.corridor.stability_worker import _load_route_flights

logger = logging.getLogger(__name__)


DEFAULT_N_VALUES = [16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 400]
DEFAULT_K_REPLICATES = 30
CALIBRATION_OUT_DIR = BASE_DIR / "data" / "calibration"


def _trajectory_distance_km(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """Computes mean 3D spatial waypoint deviation in kilometers between two 300-D flight vectors."""
    lat1, lon1, alt1 = vec_a[:100], vec_a[100:200], vec_a[200:300]
    lat2, lon2, alt2 = vec_b[:100], vec_b[100:200], vec_b[200:300]

    R_earth = 6371.0
    lat_mid_rad = np.radians(0.5 * (lat1 + lat2))
    dlat_rad = np.radians(lat1 - lat2)
    dlon_rad = np.radians(lon1 - lon2)

    dx_km = R_earth * dlon_rad * np.cos(lat_mid_rad)
    dy_km = R_earth * dlat_rad
    dh_km = np.sqrt(dx_km**2 + dy_km**2)

    # Vertical distance in km (altitudes in feet)
    dz_km = np.abs(alt1 - alt2) * 0.3048 / 1000.0

    dist_3d_km = np.sqrt(dh_km**2 + dz_km**2)
    return float(np.mean(dist_3d_km))


def _compute_geometric_error(sample_medoid_vecs: np.ndarray, oracle_medoid_vecs: np.ndarray) -> float:
    """Computes symmetric Chamfer distance between sample and oracle medoids."""
    if len(sample_medoid_vecs) == 0 or len(oracle_medoid_vecs) == 0:
        return 0.0

    err_s_to_o = []
    for s_vec in sample_medoid_vecs:
        min_d = min(_trajectory_distance_km(s_vec, o_vec) for o_vec in oracle_medoid_vecs)
        err_s_to_o.append(min_d)

    err_o_to_s = []
    for o_vec in oracle_medoid_vecs:
        min_d = min(_trajectory_distance_km(o_vec, s_vec) for s_vec in sample_medoid_vecs)
        err_o_to_s.append(min_d)

    return 0.5 * (float(np.mean(err_s_to_o)) + float(np.mean(err_o_to_s)))


def _save_oracle_corridor(
    flight,
    dep: str,
    arr: str,
    cluster_id: int,
    route_class: int,
    optimal_k: int,
    time_grid_seconds: int = 60,
) -> Path:
    from src.common.config import CORRIDOR_PATHS_DIR
    from src.common.adapters import traffic_to_pycontrails, pycontrails_to_parquet
    from pycontrails import Flight

    corridor_flight_id = f"oracle_{dep}-{arr}_corridor_c{cluster_id}"
    out_path = CORRIDOR_PATHS_DIR / f"oracle_{dep}-{arr}_corridor_c{cluster_id}.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    attrs = {
        "flight_id": corridor_flight_id,
        "icao24": "ORACLE",
        "callsign": "ORACLE",
        "route_class": route_class,
        "cluster_id": cluster_id,
        "optimal_k": optimal_k,
    }

    pyc_flight = traffic_to_pycontrails(flight, typecode="B738", drop_kinematics=True, **attrs)
    resampled = pyc_flight.resample_and_fill(freq=f"{time_grid_seconds}s")

    df_final = resampled.to_dataframe()
    delta = df_final["time"] - df_final["time"].min()
    df_final["time"] = pd.Timestamp("2025-01-01 00:00:00") + delta
    df_final["route_class"] = route_class
    df_final["cluster_id"] = cluster_id

    final_flight = Flight(data=df_final, crs="EPSG:4326", **attrs)
    pycontrails_to_parquet(final_flight, out_path)
    return out_path


class _CachedFlightProxy:
    """Lightweight proxy object representing a historical flight for downstream metadata mapping."""
    def __init__(self, flight_id: str):
        self.flight_id = flight_id
        self.callsign = flight_id


def _save_oracle_npz(route_id: str, res: dict) -> None:
    """Saves preprocessed cohort tensors and metadata to atomic .npz cache."""
    try:
        ORACLE_COHORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        flight_ids = []
        for idx, fl in enumerate(res["norm_flights"]):
            fid = getattr(fl, "flight_id", None) or getattr(fl, "callsign", None) or str(idx)
            flight_ids.append(str(fid))
            
        np.savez_compressed(
            ORACLE_COHORT_CACHE_DIR / f"{route_id}.npz",
            X_raw=res["X_raw"],
            X_scaled=res["X_scaled"],
            is_clean=np.array(res["is_clean"], dtype=bool),
            flight_ids=np.array(flight_ids),
            oracle_medoid_vecs=res["oracle_medoid_vecs"],
            k_oracle=res["k_oracle"],
            route_class=res["route_class"],
            n_flights=res["n_flights"],
        )
        logger.info(f"  [{route_id}] Saved precomputed Oracle cohort tensor to .npz cache.")
    except Exception as e:
        logger.warning(f"  [{route_id}] Could not save .npz cache: {e}")


def _prepare_oracle(route_id: str, registry_df: pd.DataFrame) -> dict:
    """Loads all flights for route_id and computes Oracle ground truth corridors."""
    # 1. Tier 1 Fast Path: Check .npz tensor cache
    cache_path = ORACLE_COHORT_CACHE_DIR / f"{route_id}.npz"
    if cache_path.exists():
        try:
            logger.info(f"  [{route_id}] Loading precomputed Oracle cohort tensor from .npz cache: {cache_path}")
            with np.load(cache_path, allow_pickle=True) as cached:
                flight_ids = [str(fid) for fid in cached["flight_ids"]]
                norm_flights = [_CachedFlightProxy(fid) for fid in flight_ids]
                return {
                    "route_id": route_id,
                    "n_flights": int(cached["n_flights"]),
                    "norm_flights": norm_flights,
                    "is_clean": list(cached["is_clean"]),
                    "X_raw": cached["X_raw"],
                    "X_scaled": cached["X_scaled"],
                    "k_oracle": int(cached["k_oracle"]),
                    "route_class": int(cached["route_class"]),
                    "oracle_medoid_vecs": cached["oracle_medoid_vecs"],
                }
        except Exception as e:
            logger.warning(f"  [{route_id}] Failed to load .npz cache ({e}). Proceeding to tier 2 lookup...")

    # 2. Tier 2: Check GLOBAL_MODEL_REGISTRY and parquet corridor files
    oracle_route_id = f"ORACLE_{route_id}"
    from src.common.registry_utils import load_model_registry
    
    cached_model = None
    if GLOBAL_MODEL_REGISTRY.exists():
        try:
            df_model = load_model_registry()
            df_route = df_model[df_model["route_id"] == oracle_route_id]
            if not df_route.empty:
                cached_model = df_route
        except Exception as e:
            logger.warning(f"Could not read model registry for Oracle cache check: {e}")
            
    cache_hit = False
    if cached_model is not None:
        cache_hit = True
        for _, row in cached_model.iterrows():
            fp = BASE_DIR / row["file_path"]
            if not fp.exists():
                cache_hit = False
                break
                
    if cache_hit:
        logger.info(f"  [{route_id}] Oracle loaded from cache (Registry & Corridor Parquet files exist).")
        k_oracle = int(cached_model["optimal_k"].iloc[0])
        r_class = int(cached_model["route_class"].iloc[0])
        n_flights = int(cached_model["cluster_size"].sum())
        
        oracle_medoid_flights = []
        for _, row in cached_model.iterrows():
            fp = BASE_DIR / row["file_path"]
            from src.common.adapters import parquet_to_pycontrails, pycontrails_to_traffic
            flights_dict = parquet_to_pycontrails(str(fp))
            for fl in flights_dict.values():
                oracle_medoid_flights.append(pycontrails_to_traffic(fl))
        
        oracle_medoid_vecs = vectorize_cohort(oracle_medoid_flights)
        
        # Load raw cohort details anyway, as they are needed for parameter sweep subsampling
        logger.info(f"  [{route_id}] Loading cohort for variational sweep...")
        flights = _load_route_flights(route_id, n_target=9999, registry_df=registry_df)
        if not flights:
            raise RuntimeError(f"No flights found for route {route_id}")
        norm_flights, is_clean = classify_and_normalize_cohort(flights)
        if not norm_flights:
            raise RuntimeError(f"No flights survived normalization for {route_id}")
        X_raw = vectorize_cohort(norm_flights)
        X_scaled, mean_vec, std_vec = normalize_vectors(X_raw)
        
        res = {
            "route_id": route_id,
            "n_flights": n_flights,
            "norm_flights": norm_flights,
            "is_clean": is_clean,
            "X_raw": X_raw,
            "X_scaled": X_scaled,
            "k_oracle": k_oracle,
            "route_class": r_class,
            "oracle_medoid_vecs": oracle_medoid_vecs,
        }
        _save_oracle_npz(route_id, res)
        return res

    # 3. Tier 3: Compute from scratch
    logger.info(f"  [{route_id}] computing Oracle Ground Truth from scratch...")
    flights = _load_route_flights(route_id, n_target=9999, registry_df=registry_df)
    if not flights:
        raise RuntimeError(f"No flights found for route {route_id}")

    norm_flights, is_clean = classify_and_normalize_cohort(flights)
    if not norm_flights:
        raise RuntimeError(f"No flights survived normalization for {route_id}")

    X_raw = vectorize_cohort(norm_flights)
    X_scaled, mean_vec, std_vec = normalize_vectors(X_raw)

    n_avail = len(norm_flights)
    d_comp = min(D_PCA, n_avail - 1)
    pca_model = fit_pca(X_scaled, n_components=d_comp)
    X_pca = apply_pca(pca_model, X_scaled)

    k_oracle, labels_oracle, sil_oracle, r_class = _evaluate_optimal_k(X_pca)

    oracle_medoid_indices = []
    for c_id in range(k_oracle):
        m_idx = _select_medoid(X_pca, labels_oracle == c_id, is_clean)
        oracle_medoid_indices.append(m_idx)

    oracle_medoid_vecs = X_raw[oracle_medoid_indices]

    logger.info(f"  [{route_id}] Oracle established: N={n_avail}, k={k_oracle}, class={r_class}")

    # Save Oracle corridors and build corridors data for registration
    dep, arr = route_id.split("-", 1)
    corridor_records = []
    for c_id in range(k_oracle):
        m_idx = oracle_medoid_indices[c_id]
        medoid_flight = norm_flights[m_idx]
        medoid_flight_id = getattr(medoid_flight, "flight_id", None) or str(m_idx)
        
        out_path = _save_oracle_corridor(medoid_flight, dep, arr, c_id, r_class, k_oracle)
        
        try:
            rel_path = out_path.resolve().relative_to(BASE_DIR).as_posix()
        except ValueError:
            rel_path = out_path.resolve().as_posix()
            
        corridor_records.append({
            "cluster_id": c_id,
            "cluster_size": int(np.sum(labels_oracle == c_id)),
            "medoid_historical_flight_id": medoid_flight_id,
            "corridor_flight_id": f"oracle_{route_id}_corridor_c{c_id}",
            "file_path": rel_path
        })

    # Register in GLOBAL_MODEL_REGISTRY using batch_register_corridors
    from src.common.registry_utils import batch_register_corridors
    oracle_res = {
        "route_id": f"ORACLE_{route_id}",
        "optimal_k": k_oracle,
        "silhouette_score": float(sil_oracle) if not np.isnan(sil_oracle) else None,
        "route_class": r_class,
        "corridors": corridor_records,
        "status": "ok"
    }
    batch_register_corridors([oracle_res])

    # Register flight assignments in GLOBAL_FLIGHT_CLUSTER_MAP
    from src.common.registry_utils import batch_register_flight_cluster_map
    oracle_flight_mappings = []
    medoid_flight_ids = {corr["medoid_historical_flight_id"] for corr in corridor_records}
    for idx_fl, flight in enumerate(norm_flights):
        flight_id = getattr(flight, "flight_id", None) or str(idx_fl)
        cluster_id = int(labels_oracle[idx_fl])
        oracle_flight_mappings.append({
            "flight_id": flight_id,
            "route_id": f"ORACLE_{route_id}",
            "cluster_id": cluster_id,
            "route_class": r_class,
            "is_medoid": flight_id in medoid_flight_ids
        })
    
    batch_register_flight_cluster_map([{
        "flight_mappings": oracle_flight_mappings
    }])

    res = {
        "route_id": route_id,
        "n_flights": n_avail,
        "norm_flights": norm_flights,
        "is_clean": is_clean,
        "X_raw": X_raw,
        "X_scaled": X_scaled,
        "k_oracle": k_oracle,
        "route_class": r_class,
        "oracle_medoid_vecs": oracle_medoid_vecs,
    }
    _save_oracle_npz(route_id, res)
    return res


def run_gt_sweep(route_cache: dict, n_values: list, k_replicates: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Runs bootstrap evaluation comparing stability metrics against geometric error."""
    raw_records = []
    logger.info(f"Running GT benchmark across {len(route_cache)} routes x {len(n_values)} N-values x {k_replicates} seeds...")

    for route_id, data in route_cache.items():
        X_raw = data["X_raw"]
        X_scaled = data["X_scaled"]
        is_clean = data["is_clean"]
        oracle_vecs = data["oracle_medoid_vecs"]
        n_avail = data["n_flights"]

        for N in n_values:
            N_eff = min(N, n_avail)
            if N_eff < 10:
                continue

            for seed in range(k_replicates):
                rng = np.random.default_rng(seed=(abs(hash(route_id)) + N * 1000 + seed) % 2**32)
                idx = rng.choice(n_avail, size=N_eff, replace=False)

                sample_scaled = X_scaled[idx]
                sample_raw = X_raw[idx]
                sample_clean = [is_clean[i] for i in idx]

                # 1. Metric A: dcv_scaled (split-half on X_scaled)
                half = N_eff // 2
                dcv_scaled = float(calculate_delta_cv(
                    np.var(sample_scaled[:half], axis=0),
                    np.var(sample_scaled[half:], axis=0)
                ))

                # 2. Metric B: dcv_pca (fit PCA on sample, split-half on X_pca)
                d_comp = min(D_PCA, N_eff - 1)
                pca_sample = PCA(n_components=d_comp)
                sample_pca = pca_sample.fit_transform(sample_scaled)
                dcv_pca = float(calculate_delta_cv(
                    np.var(sample_pca[:half], axis=0),
                    np.var(sample_pca[half:], axis=0)
                ))

                # 3. Sample Clustering & Geometric Error
                k_sample, labels_sample, _, _ = _evaluate_optimal_k(sample_pca)
                sample_medoid_indices = []
                for c_id in range(k_sample):
                    m_idx = _select_medoid(sample_pca, labels_sample == c_id, sample_clean)
                    sample_medoid_indices.append(m_idx)

                sample_medoid_vecs = sample_raw[sample_medoid_indices]
                geom_err = _compute_geometric_error(sample_medoid_vecs, oracle_vecs)

                raw_records.append({
                    "route": route_id,
                    "N": N,
                    "N_eff": N_eff,
                    "seed": seed,
                    "dcv_scaled": dcv_scaled,
                    "dcv_pca": dcv_pca,
                    "k_sample": k_sample,
                    "geom_error_km": geom_err,
                })

    df_raw = pd.DataFrame(raw_records)

    # Summary table aggregated by N
    summary_rows = []
    for N in n_values:
        sub = df_raw[df_raw["N"] == N]
        if sub.empty:
            continue
        summary_rows.append({
            "N": N,
            "median_geom_error_km": round(sub["geom_error_km"].median(), 2),
            "p90_geom_error_km": round(sub["geom_error_km"].quantile(0.90), 2),
            "median_dcv_scaled": round(sub["dcv_scaled"].median(), 3),
            "median_dcv_pca": round(sub["dcv_pca"].median(), 3),
            "pct_dcv_scaled_lt_030": round((sub["dcv_scaled"] < 0.30).mean() * 100, 1),
            "pct_dcv_pca_lt_030": round((sub["dcv_pca"] < 0.30).mean() * 100, 1),
            "pct_dcv_pca_lt_015": round((sub["dcv_pca"] < 0.15).mean() * 100, 1),
        })

    df_summary = pd.DataFrame(summary_rows)
    return df_raw, df_summary


def plot_gt_results(df_raw: pd.DataFrame, df_summary: pd.DataFrame, out_dir: Path) -> None:
    """Generates visual benchmarks comparing error vs sample size and metrics."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Geometric Error vs Sample Size N
    plt.figure(figsize=(9, 6))
    routes = df_raw["route"].unique()
    for route_id in routes:
        sub = df_raw[df_raw["route"] == route_id]
        grp = sub.groupby("N")["geom_error_km"].median()
        plt.plot(grp.index, grp.values, marker="o", label=route_id)

    plt.axhline(5.0, color="red", linestyle="--", alpha=0.7, label="5 km Error Goal")
    plt.xlabel("N (Sample Size)")
    plt.ylabel("Geometric Error vs Oracle (km)")
    plt.title("Corridor Geometric Error vs Sample Size across 6 Routes")
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_dir / "gt_error_vs_N.png", dpi=150)
    plt.close()

    # 2. Geometric Error vs dcv_pca and dcv_scaled
    plt.figure(figsize=(10, 5))
    plt.subplot(1, 2, 1)
    plt.scatter(df_raw["dcv_scaled"], df_raw["geom_error_km"], alpha=0.2, color="blue", s=10)
    plt.axvline(0.30, color="red", linestyle="--", label="tau=0.30")
    plt.axhline(5.0, color="green", linestyle="--", label="5 km Error")
    plt.xlabel("dcv_scaled (300-D)")
    plt.ylabel("Geometric Error (km)")
    plt.title("Geometric Error vs X_scaled Stability")
    plt.xlim(0, 2.0)
    plt.grid(True, alpha=0.3)
    plt.legend()

    plt.subplot(1, 2, 2)
    plt.scatter(df_raw["dcv_pca"], df_raw["geom_error_km"], alpha=0.2, color="purple", s=10)
    plt.axvline(0.15, color="red", linestyle="--", label="tau=0.15")
    plt.axhline(5.0, color="green", linestyle="--", label="5 km Error")
    plt.xlabel("dcv_pca (13-D)")
    plt.ylabel("Geometric Error (km)")
    plt.title("Geometric Error vs X_pca Stability")
    plt.xlim(0, 0.6)
    plt.grid(True, alpha=0.3)
    plt.legend()

    plt.tight_layout()
    plt.savefig(out_dir / "dcv_vs_gt_error.png", dpi=150)
    plt.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ground Truth vs Stability Metric Sweep")
    parser.add_argument("--k-replicates", type=int, default=DEFAULT_K_REPLICATES)
    parser.add_argument("--table-only", action="store_true")
    args = parser.parse_args()

    CALIBRATION_OUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Loading global trajectory registry...")
    registry_df = load_trajectory_registry()

    route_cache = {}
    for r in CALIBRATION_ROUTES:
        try:
            route_cache[r] = _prepare_oracle(r, registry_df)
        except Exception as exc:
            logger.error(f"Failed to prepare Oracle for {r}: {exc}")

    if not route_cache:
        logger.error("No Oracle routes prepared. Aborting.")
        sys.exit(1)

    t0 = time.perf_counter()
    df_raw, df_summary = run_gt_sweep(route_cache, DEFAULT_N_VALUES, args.k_replicates)
    elapsed = time.perf_counter() - t0
    logger.info(f"GT sweep completed in {elapsed:.2f} seconds.")

    raw_path = CALIBRATION_OUT_DIR / "gt_vs_stability_raw.csv"
    sum_path = CALIBRATION_OUT_DIR / "gt_vs_stability_summary.csv"
    df_raw.to_csv(raw_path, index=False)
    df_summary.to_csv(sum_path, index=False)

    if not args.table_only:
        plot_gt_results(df_raw, df_summary, CALIBRATION_OUT_DIR)

    print("\n" + "="*95)
    print("GROUND TRUTH GEOMETRIC ERROR vs STABILITY METRIC SUMMARY (across 6 routes)")
    print("="*95)
    print(df_summary.to_string(index=False))
    print("="*95)


if __name__ == "__main__":
    setup_file_logger(log_filename="calibration.log")
    setup_file_logger(log_filename="gt_stability_sweep.log")
    main()
