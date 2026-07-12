import logging
from pathlib import Path
import numpy as np
import pandas as pd
import scipy.stats
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from src.common.config import GLOBAL_EKF_DIAG_REGISTRY
from src.common.exceptions import DiagnosticsIOError
from src.analysis.campaigns.phase_quality.diagnostics import (
    CHI2_LOWER, CHI2_UPPER, CHI2_DF, STATE_COLS
)

logger = logging.getLogger(__name__)


def load_registry() -> pd.DataFrame:
    if not GLOBAL_EKF_DIAG_REGISTRY.exists():
        raise DiagnosticsIOError(f"Registry not found: {GLOBAL_EKF_DIAG_REGISTRY}")
    try:
        return pd.read_parquet(GLOBAL_EKF_DIAG_REGISTRY)
    except Exception as e:
        raise DiagnosticsIOError(f"Failed loading registry: {e}")


def save_tensor_archive(route_id: str, tensor_dict: dict, out_dir: Path) -> None:
    try:
        out_path = out_dir / f"{route_id}_autopsy_tensors.npz"
        np.savez_compressed(out_path, **tensor_dict)
        logger.info(f"[{route_id}] Saved time-series tensor archive at: {out_path}")
    except Exception as e:
        raise DiagnosticsIOError(f"Failed saving tensor archive for {route_id}: {e}")


def save_flat_metrics(df_flat: pd.DataFrame, out_path: Path) -> None:
    try:
        df_flat.to_parquet(out_path, index=False)
        logger.info(f"Saved master EKF autopsy flat metrics table: {out_path}")
    except Exception as e:
        raise DiagnosticsIOError(f"Failed saving flat metrics table: {e}")


def write_route_summary(df_summary: pd.DataFrame, out_path: Path) -> None:
    try:
        df_summary.to_csv(out_path, index=False)
        logger.info(f"Saved EKF autopsy route summary table: {out_path}")
    except Exception as e:
        raise DiagnosticsIOError(f"Failed writing route summary CSV: {e}")


def resolve_route_id(diag_path: str) -> str:
    parts = Path(diag_path).parts
    for pt in parts:
        if pt.startswith("rank_"):
            subparts = pt.split("_")
            if len(subparts) >= 3:
                return subparts[2]
    return "UNKNOWN"


def filter_by_route(df: pd.DataFrame, route_id: str) -> pd.DataFrame:
    df_copy = df.copy()
    df_copy["route_id"] = df_copy["diag_file_path"].apply(resolve_route_id)
    filtered = df_copy[df_copy["route_id"] == route_id].copy()
    if filtered.empty:
        raise DiagnosticsIOError(f"No registered diagnostic archives found for route: {route_id}")
    return filtered


def generate_route_report(
    route_id: str,
    df_route_metrics: pd.DataFrame,
    tensor_archive_path: Path,
    out_pdf: Path,
    plots_dir: Path,
) -> None:
    """Generates the 4-page Route Autopsy PDF report and high-res PNG plots.

    # TODO: fix plotting bug - currently only flights passing all filters are plotted;
    # include filtered-out flights in future revision to allow visual comparison of
    # rejected vs. accepted flight EKF characteristics within the same corridor PDF.
    """
    logger.info(f"[{route_id}] Loading tensor arrays for plotting and executive summary...")
    
    tensors = {}
    if tensor_archive_path.exists():
        try:
            with np.load(tensor_archive_path, allow_pickle=True) as loader:
                tensors = {k: loader[k] for k in loader.files}
        except Exception as e:
            logger.error(f"[{route_id}] Failed to load tensor archive: {e}")

    
    N_flights = len(df_route_metrics)
    med_quality = df_route_metrics["ekf_quality_score"].median() if "ekf_quality_score" in df_route_metrics.columns else np.nan
    sound_count = df_route_metrics["is_mathematically_sound"].sum() if "is_mathematically_sound" in df_route_metrics.columns else 0
    sound_pct = (sound_count / N_flights * 100) if N_flights > 0 else 0.0
    
    all_eps = []
    for fid in df_route_metrics["flight_id"]:
        eps_key = f"{fid}/epsilon_k"
        if eps_key in tensors:
            all_eps.append(tensors[eps_key])
            
    if all_eps:
        flat_eps = np.concatenate(all_eps)
        flat_eps_valid = flat_eps[~np.isnan(flat_eps)]
        if len(flat_eps_valid) > 0:
            pct_steps_in_95 = float(np.sum((flat_eps_valid >= CHI2_LOWER) & (flat_eps_valid <= CHI2_UPPER)) / len(flat_eps_valid) * 100)
            pct_steps_high = float(np.sum(flat_eps_valid > CHI2_UPPER) / len(flat_eps_valid) * 100)
        else:
            pct_steps_in_95, pct_steps_high = 0.0, 0.0
    else:
        pct_steps_in_95, pct_steps_high = 0.0, 0.0
        
    mean_res_alt = df_route_metrics["mean_res_alt"].mean() if "mean_res_alt" in df_route_metrics.columns else np.nan
    std_res_alt = df_route_metrics["mean_res_alt"].std() if "mean_res_alt" in df_route_metrics.columns else np.nan
    mean_res_v = df_route_metrics["mean_res_v"].mean() if "mean_res_v" in df_route_metrics.columns else np.nan
    std_res_v = df_route_metrics["mean_res_v"].std() if "mean_res_v" in df_route_metrics.columns else np.nan
    med_acf_lag1 = df_route_metrics["max_acf_lag1"].median() if "max_acf_lag1" in df_route_metrics.columns else np.nan
    ill_cond_count = df_route_metrics["is_ill_conditioned"].sum() if "is_ill_conditioned" in df_route_metrics.columns else 0
    
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.size": 10,
        "grid.color": "#e0e0e0",
        "grid.linestyle": "--"
    })
    
    pdf_pages = PdfPages(out_pdf)
    plots_dir.mkdir(parents=True, exist_ok=True)
    
    # Page 1: Summary Table & Route Scatter
    fig, (ax_table, ax_scatter) = plt.subplots(2, 1, figsize=(8.5, 11), gridspec_kw={"height_ratios": [0.4, 0.6]})
    fig.suptitle(f"EKF Diagnostic Autopsy Report: Route {route_id}", fontsize=16, fontweight="bold", y=0.96)
    
    ax_table.axis("off")
    ax_table.set_title("Executive Corridor Health Summary", fontsize=12, fontweight="bold", pad=10)
    
    table_data = [
        ["Total Flights Evaluated", f"{N_flights}"],
        ["Median EKF Quality Rank", f"{med_quality:.4f}" if not np.isnan(med_quality) else "N/A"],
        ["Flights Mathematically Sound / Pristine", f"{sound_count} / {N_flights} ({sound_pct:.1f}%)" if "is_mathematically_sound" in df_route_metrics.columns else "N/A (Disabled)"],
        ["Chi-Square Time-Step Consistency (bounds: [1.237, 14.449])", f"{pct_steps_in_95:.1f}% inside, {pct_steps_high:.1f}% high-tail" if all_eps else "N/A (Disabled)"],
        ["Systematic Altitude Residual Bias (Mean ± Std)", f"{mean_res_alt:.2f} ± {std_res_alt:.2f} m" if not np.isnan(mean_res_alt) else "N/A (Disabled)"],
        ["Systematic Velocity Residual Bias (Mean ± Std)", f"{mean_res_v:.2f} ± {std_res_v:.2f} m/s" if not np.isnan(mean_res_v) else "N/A (Disabled)"],
        ["Median Cohort Lag-1 Autocorrelation (ACF)", f"{med_acf_lag1:.3f}" if not np.isnan(med_acf_lag1) else "N/A (Disabled)"],
        ["Ill-Conditioned Covariance Alert Count", f"{ill_cond_count} flights (kappa > 10^6)" if "is_ill_conditioned" in df_route_metrics.columns else "N/A (Disabled)"],
    ]
    summary_table = ax_table.table(cellText=table_data, colLabels=["Corridor Health Indicator", "Diagnostic Assessment"], loc="center", cellLoc="left")
    summary_table.auto_set_font_size(False)
    summary_table.set_fontsize(9)
    summary_table.scale(1.0, 1.4)
    for pos, cell in summary_table.get_celld().items():
        cell.set_edgecolor("#cccccc")
        if pos[0] == 0:
            cell.set_text_props(weight="bold")
            cell.set_facecolor("#f2f2f2")
            
    ax_scatter.grid(True)
    ax_scatter.set_title("Filter Consistency vs. State Covariance Bounds", fontsize=12, fontweight="bold", pad=10)
    
    ekf_mean_nis = df_route_metrics["ekf_mean_nis"] if "ekf_mean_nis" in df_route_metrics.columns else np.zeros(N_flights)
    ekf_max_trace_p = df_route_metrics["ekf_max_trace_p"] if "ekf_max_trace_p" in df_route_metrics.columns else np.ones(N_flights)
    ekf_quality_score = df_route_metrics["ekf_quality_score"] if "ekf_quality_score" in df_route_metrics.columns else np.zeros(N_flights)
    
    scatter = ax_scatter.scatter(
        ekf_mean_nis,
        np.log10(ekf_max_trace_p.clip(lower=1e-10)),
        c=ekf_quality_score,
        cmap="plasma", alpha=0.8, edgecolors="none", s=30
    )
    ax_scatter.set_xlabel("Mean EKF NIS (Normalized Innovation Squared)")
    ax_scatter.set_ylabel("log10(Max State Covariance Trace P)")
    ax_scatter.axvline(6.0, color="#d9534f", linestyle="--", alpha=0.7, label="Chi-Square df=6 Median (6.0)")
    ax_scatter.legend(loc="upper right", framealpha=0.9)
    cbar = fig.colorbar(scatter, ax=ax_scatter, pad=0.02)
    cbar.set_label("EKF Quality Score")
    
    plt.tight_layout()
    pdf_pages.savefig(fig)
    plt.savefig(plots_dir / "01_executive_summary.png", dpi=200)
    plt.close()
    
    # Page 2: NIS Distribution
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    ax.grid(True)
    ax.set_title(f"Pooled Corridor NIS Distribution vs. Theoretical $\\chi^2_6$ Density", fontsize=12, fontweight="bold")
    if all_eps and len(flat_eps_valid) > 0:
        plot_eps = flat_eps_valid[flat_eps_valid < 30.0]
        ax.hist(plot_eps, bins=60, density=True, alpha=0.6, color="#4a90e2", label="Empirical NIS (pooled steps)")
        x_val = np.linspace(0.01, 30.0, 300)
        y_val = scipy.stats.chi2.pdf(x_val, df=CHI2_DF)
        ax.plot(x_val, y_val, color="#d0021b", linewidth=2.0, label="Theoretical $\\chi^2_6$ Density")
        ax.axvline(CHI2_LOWER, color="black", linestyle=":", alpha=0.8)
        ax.axvline(CHI2_UPPER, color="black", linestyle=":", alpha=0.8)
        ax.text(CHI2_LOWER - 0.5, ax.get_ylim()[1] * 0.8, "1.237\n(95% lower)", color="black", ha="right", fontsize=8)
        ax.text(CHI2_UPPER + 0.5, ax.get_ylim()[1] * 0.8, "14.449\n(95% upper)", color="black", ha="left", fontsize=8)
        ax.set_xlim(0, 30)
        ax.set_xlabel("Normalized Innovation Squared (NIS)")
        ax.set_ylabel("Probability Density")
        ax.legend(loc="upper right")
    else:
        ax.text(0.5, 0.5, "No computed NIS values available (Phase 1 disabled).", ha="center", va="center", fontsize=12, color="gray")
    plt.tight_layout()
    pdf_pages.savefig(fig)
    plt.savefig(plots_dir / "02_nis_distribution.png", dpi=200)
    plt.close()
    
    # Page 3: Violin Plots per State Axis
    fig, axes = plt.subplots(3, 2, figsize=(8.5, 11))
    fig.suptitle(f"Innovation Residual Distribution per State Axis Across Cohort", fontsize=14, fontweight="bold", y=0.96)
    axes_flat = axes.flatten()
    all_res_arrays = {col: [] for col in STATE_COLS}
    
    has_res_data = False
    for fid in df_route_metrics["flight_id"]:
        res_key = f"{fid}/res_series"
        if res_key in tensors:
            has_res_data = True
            res_val = tensors[res_key]
            for col_idx, col_name in enumerate(STATE_COLS):
                all_res_arrays[col_name].append(res_val[:, col_idx])
                
    for col_idx, col_name in enumerate(STATE_COLS):
        ax = axes_flat[col_idx]
        ax.grid(True)
        ax.set_title(f"Residual Axis: '{col_name}'", fontsize=10, fontweight="bold")
        if has_res_data and all_res_arrays[col_name]:
            pooled_res = np.concatenate(all_res_arrays[col_name])
            pooled_res = pooled_res[~np.isnan(pooled_res)]
            if len(pooled_res) > 0:
                parts = ax.violinplot(pooled_res, showmedians=True, showextrema=False)
                for pc in parts["bodies"]:
                    pc.set_facecolor("#50e3c2")
                    pc.set_edgecolor("#10ab87")
                    pc.set_alpha(0.6)
                ax.set_xticks([])
                ax.set_ylabel("Residual Error")
            else:
                ax.text(0.5, 0.5, "No residual data available.", ha="center", va="center", color="gray")
        else:
            ax.text(0.5, 0.5, "No residual data available (Phase 2 disabled).", ha="center", va="center", color="gray")
    plt.tight_layout(rect=[0, 0.03, 1, 0.95])
    pdf_pages.savefig(fig)
    plt.savefig(plots_dir / "03_residual_profiles.png", dpi=200)
    plt.close()
    
    # Page 4: ACF Profiles
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    ax.grid(True)
    ax.set_title("Cohort Autocorrelation Function (ACF) Profiles (Lags 1-10)", fontsize=12, fontweight="bold")
    all_acf_arrays = []
    for fid in df_route_metrics["flight_id"]:
        acf_key = f"{fid}/acf_curves"
        if acf_key in tensors:
            all_acf_arrays.append(tensors[acf_key])
            
    if all_acf_arrays:
        stacked_acf = np.stack(all_acf_arrays, axis=0)
        lags = np.arange(1, 11)
        colors = ["#4a90e2", "#f5a623", "#7ed321", "#bd10e0", "#f8e71c", "#9013fe"]
        for axis_idx, col_name in enumerate(STATE_COLS):
            med_curve = np.median(stacked_acf[:, axis_idx, :], axis=0)
            q25 = np.percentile(stacked_acf[:, axis_idx, :], 25, axis=0)
            q75 = np.percentile(stacked_acf[:, axis_idx, :], 75, axis=0)
            ax.plot(lags, med_curve, label=col_name, color=colors[axis_idx], linewidth=1.5)
            ax.fill_between(lags, q25, q75, color=colors[axis_idx], alpha=0.1)
        ax.set_xticks(lags)
        ax.set_xlabel("Lag (Time Steps)")
        ax.set_ylabel("Autocorrelation Coefficient")
        ax.axhline(0.3, color="#d9534f", linestyle="--", alpha=0.5, label="Whiteness Threshold (0.3)")
        ax.axhline(0.0, color="gray", linestyle="-", alpha=0.3)
        ax.legend(loc="upper right", ncol=2)
    else:
        ax.text(0.5, 0.5, "No autocorrelation data available (Phase 2 disabled).", ha="center", va="center", fontsize=12, color="gray")
    plt.tight_layout()
    pdf_pages.savefig(fig)
    plt.savefig(plots_dir / "04_acf_profiles.png", dpi=200)
    plt.close()
    
    pdf_pages.close()
    logger.info(f"[{route_id}] Successfully generated report to: {out_pdf}")
