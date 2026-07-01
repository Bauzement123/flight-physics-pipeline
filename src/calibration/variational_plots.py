"""
Visualization & PDF Report Helpers for Variational Parameter Sweeps.
====================================================================
Generates per-route multi-page PDF reports containing Pareto frontiers,
summary tables, and cost-contour error heatmaps.
"""

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def generate_route_pdf_report(
    route_id: str,
    df_summary: pd.DataFrame,
    oracle_info: dict,
    out_dir: Path,
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

    with PdfPages(pdf_path) as pdf:
        # ---------------------------------------------------------
        # PAGE 1: Title & Executive Summary Table (Top 15 Pareto)
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

        # Top 15 sorted by lowest geometric error under query budget, or overall Pareto
        top_df = df_summary.sort_values(by=["median_geom_err_km", "avg_queries"]).head(15)

        # Prepare table data
        col_labels = [
            "N_0", "Tau", "K_max", "Avg Queries", 
            "Med Error (km)", "P90 Error (km)", "Conv R0 (%)", "Max Out (%)"
        ]
        table_vals = []
        for _, row in top_df.iterrows():
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
            "Top 15 Parameter Configurations ranked by Median 3D Geometric Error (km) against Oracle Ground Truth.",
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

        # Compute non-dominated Pareto frontier
        pts = df_summary[["avg_queries", "median_geom_err_km", "N_0", "tau", "K_max"]].sort_values("avg_queries")
        pareto_pts = []
        min_err_so_far = float("inf")
        for _, row in pts.iterrows():
            if row["median_geom_err_km"] < min_err_so_far:
                pareto_pts.append(row)
                min_err_so_far = row["median_geom_err_km"]

        if pareto_pts:
            p_df = pd.DataFrame(pareto_pts)
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

        ax.axhline(5.0, color="gray", linestyle=":", label="5 km Target Target")
        ax.set_xlabel("Expected Trino Query Cost (Average Flights Queried)", fontsize=12)
        ax.set_ylabel("Median 3D Geometric Error (km)", fontsize=12)
        ax.set_title(f"Pareto Frontier: Query Cost vs Physical Error ({route_id})", fontsize=15, fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend(loc="upper right", fontsize=10)
        fig.tight_layout(pad=3.0)
        pdf.savefig(fig)
        plt.close(fig)

        # ---------------------------------------------------------
        # PAGES 3+: Cost-Contour Heatmaps per K_max
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
