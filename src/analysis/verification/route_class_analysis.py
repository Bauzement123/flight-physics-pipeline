"""
Analysis Module: Route Class Distribution Analysis
Analyzes and visualizes the percentage distribution of synthesized route corridors
and generated path baselines across the 4 route class buckets (0, 1, 2, 3).
"""

import os
import sys
import argparse
import logging
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# Import Central Configurations
from src.common.config import BASE_DIR, GLOBAL_MODEL_REGISTRY
from src.common.registry_utils import load_model_registry

logger = logging.getLogger(__name__)

def load_data(registry_path: Path) -> pd.DataFrame:
    """Loads the synthesized registry database."""
    logger.info(f"Loading Synthesized Registry: {registry_path}")
    if not registry_path.exists():
        logger.error(f"Synthesized registry not found at: {registry_path}")
        return pd.DataFrame()

    try:
        df = load_model_registry()
        return df
    except Exception as e:
        logger.error(f"Failed to read synthesized registry: {e}")
        return pd.DataFrame()

def aggregate_route_classes(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregates route classes, computes raw counts and relative percentages (adding to 100%)."""
    if df.empty or 'route_class' not in df.columns:
        return pd.DataFrame()

    # Group by route_class and aggregate unique corridors and total paths
    # Reindex to ensure all 4 classes (1, 2, 3, 4) are represented, filling missing with 0
    df_dist = df.groupby('route_class').agg(
        unique_routes=('route', 'nunique'),
        total_paths=('file_path', 'count')
    ).reindex([1, 2, 3, 4], fill_value=0).reset_index()

    # Calculate total sums
    total_unique_routes = df_dist['unique_routes'].sum()
    total_generated_paths = df_dist['total_paths'].sum()

    # Calculate percentage distributions (so they sum up to 100.0%)
    df_dist['unique_pct'] = (df_dist['unique_routes'] / total_unique_routes * 100.0) if total_unique_routes > 0 else 0.0
    df_dist['paths_pct'] = (df_dist['total_paths'] / total_generated_paths * 100.0) if total_generated_paths > 0 else 0.0

    return df_dist

def plot_distribution(df_dist: pd.DataFrame, output_path: Path):
    """Generates a bar chart histogram where values represent relative percentages adding up to 100%."""
    if df_dist.empty:
        logger.error("No aggregated data available to plot.")
        return

    logger.info("Generating route class distribution plot...")
    fig, ax = plt.subplots(figsize=(10, 6.2))

    x = np.arange(4) # 4 classes (0, 1, 2, 3)
    width = 0.35     # Bar width

    # Plot unique routes percentage
    bars_routes = ax.bar(
        x - width/2, 
        df_dist['unique_pct'], 
        width, 
        label='Unique Route Corridors (%)', 
        color='#1f77b4', 
        edgecolor='#104e73', 
        alpha=0.85
    )

    # Plot total paths percentage
    bars_paths = ax.bar(
        x + width/2, 
        df_dist['paths_pct'], 
        width, 
        label='Total Generated Paths (%)', 
        color='#2ca02c', 
        edgecolor='#196119', 
        alpha=0.85
    )

    # Custom visual aesthetics
    plt.title("Synthesized Route & Path Distribution Across Route Classes", fontsize=13, fontweight='bold', pad=18)
    ax.set_xlabel("Synthesized Route Class Bucket", fontsize=11, labelpad=10)
    ax.set_ylabel("Relative Percentage of Total (%)", fontsize=11, labelpad=8)
    
    # Tick formatting
    ax.set_xticks(x)
    ax.set_xticklabels([f"Class 1", f"Class 2", f"Class 3", f"Class 4"], fontsize=10)
    ax.set_ylim(0, max(df_dist['unique_pct'].max(), df_dist['paths_pct'].max()) * 1.15)
    
    # Format Y-axis to show percentages
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda val, pos: f"{int(val)}%"))

    # Gridlines
    ax.grid(True, which='major', axis='y', linestyle='--', alpha=0.5, color='#cccccc')
    ax.set_axisbelow(True)

    # Annotate percentage labels on top of each bar (making them sum up to 100%)
    for bar in bars_routes:
        height = bar.get_height()
        ax.annotate(
            f'{height:.1f}%',
            xy=(bar.get_x() + bar.get_width() / 2, height),
            xytext=(0, 3),  # 3 points vertical offset
            textcoords="offset points",
            ha='center', va='bottom', fontsize=9, fontweight='bold', color='#104e73'
        )

    for bar in bars_paths:
        height = bar.get_height()
        ax.annotate(
            f'{height:.1f}%',
            xy=(bar.get_x() + bar.get_width() / 2, height),
            xytext=(0, 3),  # 3 points vertical offset
            textcoords="offset points",
            ha='center', va='bottom', fontsize=9, fontweight='bold', color='#196119'
        )

    # Legend
    ax.legend(loc='upper right', framealpha=0.9)

    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, format='svg')
    plt.close()

    logger.info(f"✓ Route class distribution plot successfully saved to: {output_path}")

def print_report_table(df_dist: pd.DataFrame, total_routes: int, total_paths: int):
    """Prints a formatted report table to the console."""
    if df_dist.empty:
        return

    print("\n" + "="*75)
    print("           ROUTE CLASS BUCKETS DISTRIBUTION REPORT (SUMS TO 100%)")
    print("="*75)
    print(f"Total Unique Route Corridors : {total_routes:,}")
    print(f"Total Generated Path Centroids: {total_paths:,}")
    print("-"*75)
    print(f"{'Route Class':<12} | {'Unique Routes':<13} | {'Unique %':<9} | {'Total Paths':<11} | {'Paths %':<8}")
    print("-"*75)
    for _, row in df_dist.iterrows():
        c_name = f"Class {int(row['route_class'])}"
        print(f"{c_name:<12} | {int(row['unique_routes']):<13,} | {row['unique_pct']:<8.1f}% | {int(row['total_paths']):<11,} | {row['paths_pct']:<7.1f}%")
    print("-"*75)
    print(f"{'SUM':<12} | {total_routes:<13,} | 100.0%    | {total_paths:<11,} | 100.0%")
    print("="*75 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Analyze route class distribution in synthesized registry.")
    parser.add_argument(
        "--registry", 
        default=str(GLOBAL_MODEL_REGISTRY), 
        help="Path to synthesized trajectory registry parquet file"
    )
    parser.add_argument(
        "--output-plot", 
        default=str(BASE_DIR / "data" / "analysis" / "plots" / "route_class_distribution.svg"), 
        help="Output destination path for the distribution plot"
    )
    args = parser.parse_args()

    registry_path = Path(args.registry)
    output_plot_path = Path(args.output_plot)

    logger.info("Initializing Route Class Distribution Analysis workflow...")
    
    # Load
    df = load_data(registry_path)
    if df.empty:
        logger.error("Registry is empty or failed to load. Program aborted.")
        sys.exit(1)

    total_unique_routes = df['route'].nunique()
    total_generated_paths = len(df)

    # Aggregate
    df_dist = aggregate_route_classes(df)
    if df_dist.empty:
        logger.error("Aggregation failed. Program aborted.")
        sys.exit(1)

    # Print
    print_report_table(df_dist, total_unique_routes, total_generated_paths)

    # Plot
    plot_distribution(df_dist, output_plot_path)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - [CLASS_DIST] - %(levelname)s - %(message)s")
    main()
