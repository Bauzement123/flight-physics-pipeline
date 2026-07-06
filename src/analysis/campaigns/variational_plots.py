"""
Visualization & PDF Report Helpers for Variational Parameter Sweeps.
====================================================================
Generates per-route multi-page PDF reports containing Pareto frontiers,
summary tables, cost-contour error heatmaps, and stacked cohort visualizations.
"""

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import numpy as np
import pandas as pd

from src.analysis.campaigns.plot_helpers import get_or_create_config_plot

logger = logging.getLogger(__name__)


def generate_route_pdf_report(
    route_id: str,
    df_summary: pd.DataFrame,
    oracle_info: dict,
    out_dir: Path,
    plots_per_page: int = 3,
    pareto_df: pd.DataFrame = None,
) -> Path:
    """
    Compiles a comprehensive multi-page PDF report for a single route.
    """
    try:
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
    except ImportError as exc:
        logger.error(f"Matplotlib required for PDF generation: {exc}")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{route_id}_variational_summary.pdf"

    # 1. Compute or use provided non-dominated Pareto frontier points
    if pareto_df is not None:
        p_df = pareto_df
    else:
        pts = df_summary[["avg_queries", "median_geom_err_km", "N_0", "tau", "K_max", "p90_geom_err_km", "pct_conv_round0", "pct_maxed_out"]].sort_values("avg_queries")
        pareto_pts = []
        min_err_so_far = float("inf")
        for _, row in pts.iterrows():
            if row["median_geom_err_km"] < min_err_so_far:
                pareto_pts.append(row)
                min_err_so_far = row["median_geom_err_km"]

        p_df = pd.DataFrame(pareto_pts) if pareto_pts else pd.DataFrame()

    with PdfPages(pdf_path) as pdf:
        # ---------------------------------------------------------
        # PAGE 1: Title & Executive Summary Table (Pareto Front Only)
        # ---------------------------------------------------------
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.axis("off")

        # Header text
        title_text = f"Variational Parameter Sweep Report: {route_id}"
        oracle_text = (
            f"Oracle Ground Truth Baseline: N={oracle_info.get('n_flights', 400)}, "
            f"Optimal K={oracle_info.get('k_oracle', 'N/A')}, "
            f"Class={oracle_info.get('route_class', 'N/A')}"
        )
        ax.text(0.5, 0.95, title_text, ha="center", va="top", fontsize=18, fontweight="bold")
        ax.text(0.5, 0.90, oracle_text, ha="center", va="top", fontsize=12, style="italic", color="#333333")

        # Prepare table data from Pareto front points
        col_labels = [
            "N_0", "Tau", "K_max", "Avg Queries", 
            "Med Error (km)", "P90 Error (km)", "Conv R0 (%)", "Max Out (%)"
        ]
        table_vals = []
        if not p_df.empty:
            for _, row in p_df.iterrows():
                table_vals.append([
                    int(row["N_0"]),
                    f"{row['tau']:.2f}",
                    int(row["K_max"]),
                    f"{row['avg_queries']:.1f}",
                    f"{row['median_geom_err_km']:.2f}",
                    f"{row['p90_geom_err_km']:.2f}",
                    f"{row['pct_conv_round0']:.1f}%",
                    f"{row['pct_maxed_out']:.1f}%",
                ])
        else:
            table_vals.append(["N/A"] * len(col_labels))

        table = ax.table(
            cellText=table_vals,
            colLabels=col_labels,
            loc="center",
            cellLoc="center",
            bbox=[0.02, 0.15, 0.96, 0.70]
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        # Style table header
        for (i, j), cell in table.get_celld().items():
            if i == 0:
                cell.set_facecolor("#2b5c8f")
                cell.set_text_props(color="white", fontweight="bold")
            elif i % 2 == 1:
                cell.set_facecolor("#f2f6fa")

        ax.text(
            0.5, 0.08,
            "Non-Dominated Pareto-Optimal Parameter Configurations (Query Cost vs. Physical 3D Error).",
            ha="center", va="center", fontsize=10, style="italic"
        )
        pdf.savefig(fig)
        plt.close(fig)

        # ---------------------------------------------------------
        # PAGE 2: Pareto Frontier Plot (Query Budget vs Error)
        # ---------------------------------------------------------
        fig, ax = plt.subplots(figsize=(11, 8.5))
        k_vals = sorted(df_summary["K_max"].unique())
        colors = plt.cm.viridis(np.linspace(0.1, 0.9, len(k_vals)))

        for idx, k in enumerate(k_vals):
            sub = df_summary[df_summary["K_max"] == k]
            ax.scatter(
                sub["avg_queries"],
                sub["median_geom_err_km"],
                label=f"K_max = {k}",
                color=colors[idx],
                s=60,
                alpha=0.8,
                edgecolors="k",
            )

        if not p_df.empty:
            ax.plot(p_df["avg_queries"], p_df["median_geom_err_km"], "r--", linewidth=2, label="Pareto Frontier")
            for _, r in p_df.iterrows():
                ax.annotate(
                    f"({int(r['N_0'])}, {r['tau']:.2f}, K{int(r['K_max'])})",
                    (r["avg_queries"], r["median_geom_err_km"]),
                    textcoords="offset points",
                    xytext=(5, 5),
                    fontsize=8,
                    bbox=dict(boxstyle="round,pad=0.2", fc="yellow", alpha=0.3)
                )

        ax.axhline(5.0, color="gray", linestyle=":", label="5 km Target")
        ax.set_xlabel("Expected Trino Query Cost (Average Flights Queried)", fontsize=12)
        ax.set_ylabel("Median 3D Geometric Error (km)", fontsize=12)
        ax.set_title(f"Pareto Frontier: Query Cost vs Physical Error ({route_id})", fontsize=15, fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend(loc="upper right", fontsize=10)
        fig.tight_layout(pad=3.0)
        pdf.savefig(fig)
        plt.close(fig)

        # ---------------------------------------------------------
        # PAGES 3+: Stacked Cohort Visualizations
        # ---------------------------------------------------------
        # 3.1. Build list of items to visualize: Oracle first, then Pareto configs
        vis_items = []
        
        # Load Oracle
        try:
            oracle_path = get_or_create_config_plot(
                route_id=route_id,
                config_type="ORACLE",
                n0=-1,
                tau=-1.0,
                kmax=-1,
                replicate=-1
            )
            if oracle_path.exists():
                vis_items.append({
                    "title": f"Oracle Ground Truth Baseline (N = {oracle_info.get('n_flights', 400)}, K = {oracle_info.get('k_oracle', 'N/A')}, Class = {oracle_info.get('route_class', 'N/A')})",
                    "path": oracle_path
                })
        except Exception as e:
            logger.error(f"Could not load/create Oracle plot for PDF report: {e}")

        # Load Pareto points
        if not p_df.empty:
            for _, row in p_df.iterrows():
                n0_val = int(row["N_0"])
                tau_val = float(row["tau"])
                kmax_val = int(row["K_max"])
                try:
                    p_path = get_or_create_config_plot(
                        route_id=route_id,
                        config_type="PARETO",
                        n0=n0_val,
                        tau=tau_val,
                        kmax=kmax_val,
                        replicate=0
                    )
                    if p_path.exists():
                        vis_items.append({
                            "title": f"Pareto Config: N0 = {n0_val}, tau = {tau_val:.2f}, Kmax = {kmax_val} | Avg Queries: {row['avg_queries']:.1f} | Med Error: {row['median_geom_err_km']:.2f} km",
                            "path": p_path
                        })
                except Exception as e:
                    logger.error(f"Could not load/create Pareto plot ({n0_val}, {tau_val:.2f}, {kmax_val}) for PDF report: {e}")

        # 3.2. Chunk items and render them stacked vertically
        num_items = len(vis_items)
        if num_items > 0:
            for idx_start in range(0, num_items, plots_per_page):
                fig, axes = plt.subplots(plots_per_page, 1, figsize=(8.5, 11))
                if plots_per_page == 1:
                    axes = [axes]
                
                # Page Header
                fig.suptitle(f"Cohort Clustering Visualizations: {route_id}", fontsize=14, fontweight="bold", y=0.98)

                for box_idx in range(plots_per_page):
                    ax = axes[box_idx]
                    item_idx = idx_start + box_idx
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

        # ---------------------------------------------------------
        # PAGES X+: Cost-Contour Heatmaps per K_max
        # ---------------------------------------------------------
        for k in k_vals:
            sub = df_summary[df_summary["K_max"] == k]
            if sub.empty:
                continue

            piv_err = sub.pivot(index="N_0", columns="tau", values="median_geom_err_km")
            piv_cost = sub.pivot(index="N_0", columns="tau", values="avg_queries")

            fig, ax = plt.subplots(figsize=(11, 8.5))
            im = ax.imshow(piv_err.values, cmap="YlOrRd", aspect="auto", origin="lower")
            cbar = fig.colorbar(im, ax=ax)
            cbar.set_label("Median 3D Geometric Error (km)", fontsize=12)

            ax.set_xticks(range(len(piv_err.columns)))
            ax.set_xticklabels([f"{t:.2f}" for t in piv_err.columns], fontsize=11)
            ax.set_yticks(range(len(piv_err.index)))
            ax.set_yticklabels([str(int(n)) for n in piv_err.index], fontsize=11)
            ax.set_xlabel("Stability Threshold (tau)", fontsize=13)
            ax.set_ylabel("Initial Sample Size (N_0)", fontsize=13)
            ax.set_title(
                f"Error Heatmap & Overlaid Query Cost Contours ({route_id} | K_max = {k})",
                fontsize=15,
                fontweight="bold"
            )

            # Overlay query cost contours
            X, Y = np.meshgrid(range(len(piv_cost.columns)), range(len(piv_cost.index)))
            cs = ax.contour(X, Y, piv_cost.values, colors="blue", linewidths=2.0)
            ax.clabel(cs, inline=True, fontsize=10, fmt="N=%1.0f")

            fig.tight_layout(pad=3.0)
            pdf.savefig(fig)
            plt.close(fig)

    logger.info(f"Successfully compiled summary PDF for {route_id}: {pdf_path}")
    return pdf_path
