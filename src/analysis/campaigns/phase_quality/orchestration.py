import logging
import time
from pathlib import Path
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

from src.common.config import BASE_DIR, PhaseControl
from src.common.utils import setup_file_logger
from src.analysis.campaigns.phase_quality import io, diagnostics

logger = logging.getLogger(__name__)


def _worker_init() -> None:
    """Initializes logging handlers and numeric thread limits inside spawned child workers."""
    setup_file_logger(log_filename="calibration.log")
    from src.common.concurrency import limit_numeric_threads
    limit_numeric_threads(1)


def _worker_autopsy_flight(args) -> dict:
    """Worker target evaluating EKF tensors from a single .npz file."""
    # Ensure worker process logs to centralized calibration log
    setup_file_logger(log_filename="calibration.log")
    
    flight_id, route_id, abs_filepath_str, cfg = args
    abs_path = Path(abs_filepath_str)
    
    result = {
        "flight_id": flight_id,
        "route_id": route_id,
        "success": False,
        "error_msg": "",
        "scalars": {},
        "arrays": {}
    }
    
    if not abs_path.exists():
        result["error_msg"] = f"File not found on disk: {abs_path}"
        return result
        
    try:
        with np.load(abs_path, allow_pickle=True) as data:
            required = ["timestamps", "e_k", "S_k", "P_k"]
            for key in required:
                if key not in data:
                    raise KeyError(f"Missing required array '{key}' in NPZ archive.")
                    
            timestamps = np.array(data["timestamps"])
            e_k = np.array(data["e_k"])
            S_k = np.array(data["S_k"])
            P_k = np.array(data["P_k"])

        
        T = len(timestamps)
        if T == 0:
            raise ValueError("Telemetry time-series has zero steps.")
            
        scalars = {}
        arrays = {}
        
        # 1. NIS consistency (Phase 1)
        if cfg.ENABLE_NIS:
            p1_res = diagnostics.run_nis(timestamps, e_k, S_k)
            scalars.update(p1_res["scalars"])
            arrays.update(p1_res["arrays"])
            
        # 2. Residual Whiteness (Phase 2)
        if cfg.ENABLE_RESIDUALS:
            p2_res = diagnostics.run_residuals(e_k)
            scalars.update(p2_res["scalars"])
            arrays.update(p2_res["arrays"])
            
        # 3. Covariance Condition & Drift (Phase 3)
        if cfg.ENABLE_CONDITION:
            p3_res = diagnostics.run_condition(P_k, S_k, timestamps)
            scalars.update(p3_res["scalars"])
            arrays.update(p3_res["arrays"])
            
        # 4. Formulate Flight-Level Soundness Flag (ignore disabled phases)
        nis_ok = not (cfg.ENABLE_NIS and (scalars.get("pct_nis_in_95", 0.0) < 90.0 or scalars.get("max_sustained_high_nis_sec", 0.0) >= 120.0))
        cond_ok = not (cfg.ENABLE_CONDITION and scalars.get("is_ill_conditioned", False))
        acf_ok = not (cfg.ENABLE_RESIDUALS and scalars.get("max_acf_lag1", 0.0) > 0.4)
        
        scalars["is_mathematically_sound"] = bool(nis_ok and cond_ok and acf_ok)
        
        # Subsampled arrays for storage efficiency
        idx_sub = np.linspace(0, T - 1, min(T, 150), dtype=int)
        arrays["t_T_subsample"] = idx_sub / (T - 1) if T > 1 else np.array([0.0])
        
        if cfg.ENABLE_NIS and "epsilon_k" in arrays:
            arrays["epsilon_k_subsample"] = arrays["epsilon_k"][idx_sub]
        if cfg.ENABLE_CONDITION and "cond_P_series" in arrays:
            arrays["cond_P_subsample"] = arrays["cond_P_series"][idx_sub]
            
        # Add required raw arrays for plotting if saved
        if cfg.ENABLE_RESIDUALS:
            arrays["res_series"] = e_k
        if cfg.ENABLE_CONDITION:
            arrays["diag_P_series"] = np.array([np.diagonal(P_k[i]) for i in range(T)])
            
        # For Phase 1 plotting, we need the epsilon_k key in target NPZ tensors
        if cfg.ENABLE_NIS:
            arrays["epsilon_k"] = arrays.get("epsilon_k", np.array([]))
            
        result["scalars"] = scalars
        result["arrays"] = arrays
        result["success"] = True
        
    except Exception as e:
        result["error_msg"] = str(e)
        
    return result


def run(
    registry_df: pd.DataFrame,
    out_root: Path,
    worker_count: int,
    cfg: PhaseControl,
    test_limit: int | None = None,
) -> None:
    # STRICT SAFETY CHECK: Ensure out_root does not point inside src/
    if "src" in out_root.parts:
        raise ValueError(f"Strict Security Violation: Generated outputs cannot be written to src/ ({out_root})")
        
    tables_dir = out_root / "tables"
    tensors_dir = out_root / "tensors"
    reports_dir = out_root / "reports"
    plots_root_dir = out_root / "plots"
    
    for d in [tables_dir, tensors_dir, reports_dir, plots_root_dir]:
        d.mkdir(parents=True, exist_ok=True)
        
    tasks = []
    for idx, row in registry_df.iterrows():
        fid = row["flight_id"]
        route = row["route_id"]
        rel_path = row["diag_file_path"]
        abs_path = BASE_DIR / rel_path
        tasks.append((fid, route, str(abs_path), cfg))
        
    if test_limit:
        tasks = tasks[:test_limit]
        
    logger.info(f"Loaded {len(tasks)} EKF diagnostic autopsy tasks. Spawning {worker_count} workers...")
    autopsy_results = []
    start_time = time.time()
    
    with ProcessPoolExecutor(max_workers=worker_count, initializer=_worker_init) as executor:
        futures = {executor.submit(_worker_autopsy_flight, task): task for task in tasks}
        completed_count = 0
        for fut in as_completed(futures):
            res = fut.result()
            completed_count += 1
            if res["success"]:
                autopsy_results.append(res)
                if completed_count % 100 == 0 or completed_count == len(tasks):
                    logger.info(f"Processed {completed_count}/{len(tasks)} EKF tensor archives...")
            else:
                logger.error(f"Autopsy worker failed for flight {res['flight_id']}: {res['error_msg']}")
                
    elapsed = time.time() - start_time
    logger.info(f"Completed tensor processing in {elapsed:.2f} seconds. Successful autopsies: {len(autopsy_results)}")
    
    if not autopsy_results:
        logger.critical("All EKF tensor autopsies failed. Aborting report generation.")
        return
        
    # Group results by route to generate dual-storage archives
    grouped_results = {}
    for res in autopsy_results:
        route = res["route_id"]
        if route not in grouped_results:
            grouped_results[route] = []
        grouped_results[route].append(res)
        
    all_flat_rows = []
    for route, res_list in grouped_results.items():
        if cfg.ENABLE_TENSOR_SAVE:
            route_tensor_dict = {}
            for res in res_list:
                fid = res["flight_id"]
                arrays = res["arrays"]
                for arr_k, arr_val in arrays.items():
                    route_tensor_dict[f"{fid}/{arr_k}"] = arr_val
            io.save_tensor_archive(route, route_tensor_dict, tensors_dir)
            
        for res in res_list:
            fid = res["flight_id"]
            row_dict = {"flight_id": fid, "route_id": route}
            reg_row = registry_df[registry_df["flight_id"] == fid]
            row_dict["ekf_quality_score"] = float(reg_row["ekf_quality_score"].values[0])
            row_dict["ekf_mean_nis"] = float(reg_row["ekf_mean_nis"].values[0])
            row_dict["ekf_max_trace_p"] = float(reg_row["ekf_max_trace_p"].values[0])
            row_dict.update(res["scalars"])
            all_flat_rows.append(row_dict)
            
    df_autopsy_flat = pd.DataFrame(all_flat_rows)
    if cfg.ENABLE_FLAT_TABLE:
        io.save_flat_metrics(df_autopsy_flat, tables_dir / "ekf_autopsy_flight_metrics.parquet")
        
        # Route summary statistics
        route_summaries = []
        for r_id, df_sub in df_autopsy_flat.groupby("route_id"):
            r_sum = {
                "route_id": r_id,
                "total_flights": len(df_sub),
                "pct_flights_sound": float(df_sub["is_mathematically_sound"].sum() / len(df_sub) * 100) if len(df_sub) > 0 and "is_mathematically_sound" in df_sub.columns else 0.0,
                "median_quality_score": float(df_sub["ekf_quality_score"].median()) if "ekf_quality_score" in df_sub.columns else np.nan,
                "median_pct_nis_in_95": float(df_sub["pct_nis_in_95"].median()) if "pct_nis_in_95" in df_sub.columns and not df_sub["pct_nis_in_95"].isna().all() else np.nan,
                "median_max_sustained_high_nis_sec": float(df_sub["max_sustained_high_nis_sec"].median()) if "max_sustained_high_nis_sec" in df_sub.columns and not df_sub["max_sustained_high_nis_sec"].isna().all() else np.nan,
                "median_max_acf_lag1": float(df_sub["max_acf_lag1"].median()) if "max_acf_lag1" in df_sub.columns and not df_sub["max_acf_lag1"].isna().all() else np.nan,
                "pct_flights_ill_conditioned": float(df_sub["is_ill_conditioned"].sum() / len(df_sub) * 100) if len(df_sub) > 0 and "is_ill_conditioned" in df_sub.columns else 0.0,
                "mean_res_alt": float(df_sub["mean_res_alt"].mean()) if "mean_res_alt" in df_sub.columns and not df_sub["mean_res_alt"].isna().all() else np.nan,
                "std_res_alt": float(df_sub["mean_res_alt"].std()) if "mean_res_alt" in df_sub.columns and not df_sub["mean_res_alt"].isna().all() else np.nan,
                "mean_res_v": float(df_sub["mean_res_v"].mean()) if "mean_res_v" in df_sub.columns and not df_sub["mean_res_v"].isna().all() else np.nan,
                "std_res_v": float(df_sub["mean_res_v"].std()) if "mean_res_v" in df_sub.columns and not df_sub["mean_res_v"].isna().all() else np.nan,
            }
            route_summaries.append(r_sum)
        df_route_summary = pd.DataFrame(route_summaries)
        io.write_route_summary(df_route_summary, tables_dir / "ekf_autopsy_route_summary.csv")
        
    if cfg.ENABLE_REPORTING:
        for r_id, df_sub in df_autopsy_flat.groupby("route_id"):
            route_npz_path = tensors_dir / f"{r_id}_autopsy_tensors.npz"
            out_pdf = reports_dir / f"{r_id}_ekf_autopsy_report.pdf"
            plots_route_dir = plots_root_dir / r_id
            try:
                io.generate_route_report(
                    route_id=r_id,
                    df_route_metrics=df_sub,
                    tensor_archive_path=route_npz_path,
                    out_pdf=out_pdf,
                    plots_dir=plots_route_dir,
                )
            except Exception as e:
                logger.error(f"Failed to generate autopsy report for route {r_id}: {e}", exc_info=True)
