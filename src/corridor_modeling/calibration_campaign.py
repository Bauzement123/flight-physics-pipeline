"""
Module: Calibration Campaign
=============================
Offline, standalone simulation of the corridor modeling pipeline.
Run once on 6 oversampled calibration routes (~400 flights each) to
derive the three universal pipeline constants:

    D_PCA               — PCA dimensionality  (Phase A)
    N_STANDARD          — per-route query budget = 5 × D_PCA  (Phase A)
    DELTA_CV_THRESHOLD  — ΔRSD convergence threshold  (Phase B)

Also produces:
    Phase C — expected Trino requery rate per candidate threshold
    Phase D — CPU computation timing profile (Trino timing from fetch log at Step 6)

Usage
-----
    # Full run (review only — no config writes)
    python -m src.corridor_modeling.calibration_campaign

    # Selected phases only
    python -m src.corridor_modeling.calibration_campaign --phases A,B

    # Commit results to config.py after reviewing output tables
    python -m src.corridor_modeling.calibration_campaign --write-config

Output artefacts
----------------
    data/calibration/phase_a_variance_table.csv
    data/calibration/phase_b_raw_bootstrap.csv
    data/calibration/phase_b_confidence_table.csv
    data/calibration/phase_c_requery_table.csv
    data/calibration/phase_d_timings.csv

Naming note
-----------
    Throughout this module the stability metric is called ΔRSD (Relative
    Standard Deviation change).  The stability registry column and config
    constant retain the legacy name ``delta_cv`` / ``DELTA_CV_THRESHOLD``
    for backward compatibility.  They refer to the same quantity:

        ΔRSD = || (σ_new − σ_old) / (σ_old + ε) ||₂
"""

import argparse
import logging
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.common.config import (
    BASE_DIR,
    CLUSTERING_MAX_K,
    STABILITY_MAX_RESAMPLE_ROUNDS,
    STABILITY_RESAMPLE_MULTIPLIER,
)
from src.common.registry_utils import load_trajectory_registry
from src.common.utils import setup_file_logger
from src.corridor_modeling.pca_compressor import (
    calculate_delta_cv,
    classify_and_normalize_cohort,
    find_d_pca,
    normalize_vectors,
    update_running_stats,
    vectorize_cohort,
)
from src.corridor_modeling.stability_worker import (
    _DELTA_CV_BATCH_SIZE,
    _compute_stability,
    _load_route_flights,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Campaign constants
# ---------------------------------------------------------------------------

CALIBRATION_ROUTES = [
    "EDDF-LIRF",
    "EGLL-BIKF",
    "ESSA-LEMD",
    "ESSA-EHAM",
    "LFRS-LFMN",
]

VARIANCE_THRESHOLDS  = [0.90, 0.92, 0.95, 0.97, 0.99]
PRIMARY_THRESHOLD    = 0.95          # threshold used to select D_PCA
N_STANDARD_FACTOR    = 5             # N_STANDARD = N_STANDARD_FACTOR × D_PCA

GT_ERROR_BOUND       = 0.05          # "good" = GT error < 5 % of ||var_gt||
CONFIDENCE_TARGET    = 0.90          # want P(good) ≥ 90 %

DEFAULT_CANDIDATE_THRESHOLDS = [0.05, 0.02, 0.01, 0.005, 0.001]
DEFAULT_BOOTSTRAP_K  = 50

CALIBRATION_OUT_DIR  = BASE_DIR / "data" / "calibration"


# ---------------------------------------------------------------------------
# Shared data-loading helper
# ---------------------------------------------------------------------------

def _prepare_route(
    route_id: str,
    registry_df: pd.DataFrame,
    d_pca: int | None = None,
) -> dict:
    """
    Loads ALL available flights for *route_id*, normalises, vectorises, and
    fits a PCA model on the full cohort.

    Parameters
    ----------
    route_id    : str   e.g. ``"EDDF-LIRF"``
    registry_df : pd.DataFrame
    d_pca       : int | None
        If given, also produces ``X_pca`` (shape N × d_pca) and ``var_gt``.

    Returns
    -------
    dict with keys:
        n_flights, flights, is_clean,
        X_scaled, mean_vec, std_vec,
        pca_full  (PCA fitted on None components),
        X_pca     (None if d_pca is None),
        var_gt    (None if d_pca is None)
    """
    logger.info(f"  [{route_id}] loading all available flights …")
    flights = _load_route_flights(route_id, n_target=9_999, registry_df=registry_df)
    if not flights:
        raise RuntimeError(f"No flights found for calibration route {route_id}")

    norm_flights, is_clean = classify_and_normalize_cohort(flights)
    if not norm_flights:
        raise RuntimeError(f"No flights survive normalisation for {route_id}")

    X_raw = vectorize_cohort(norm_flights)
    X_scaled, mean_vec, std_vec = normalize_vectors(X_raw)

    # Full PCA (all components) — used for variance curve in Phase A
    n_max = min(X_scaled.shape[0] - 1, X_scaled.shape[1])
    pca_full = PCA(n_components=n_max, random_state=42)
    pca_full.fit(X_scaled)

    result = dict(
        n_flights=len(norm_flights),
        flights=norm_flights,
        is_clean=is_clean,
        X_scaled=X_scaled,
        mean_vec=mean_vec,
        std_vec=std_vec,
        pca_full=pca_full,
        X_pca=None,
        var_gt=None,
    )

    if d_pca is not None:
        X_pca = pca_full.transform(X_scaled)[:, :d_pca]
        result["X_pca"]   = X_pca
        result["var_gt"]  = np.var(X_pca, axis=0)

    logger.info(f"  [{route_id}] {len(norm_flights)} flights ready.")
    return result


# ---------------------------------------------------------------------------
# Phase A — PCA Dimensionality
# ---------------------------------------------------------------------------

def run_phase_a(registry_df: pd.DataFrame) -> tuple:
    """
    Fits full PCA on each calibration route, logs the cumulative variance
    table for a range of thresholds, selects D_PCA and N_STANDARD.

    Returns
    -------
    d_pca          : int
    n_standard     : int
    route_cache    : dict[str, dict]   prepared route data (including X_pca, var_gt)
    summary_df     : pd.DataFrame
    """
    logger.info("\n%s\nPHASE A  —  PCA Dimensionality Calibration\n%s", "="*60, "="*60)
    CALIBRATION_OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    route_cache_raw: dict = {}   # without d_pca

    for route_id in CALIBRATION_ROUTES:
        try:
            data = _prepare_route(route_id, registry_df, d_pca=None)
        except RuntimeError as exc:
            logger.error(f"  Skipping {route_id}: {exc}")
            continue

        pca_full = data["pca_full"]
        cumvar   = np.cumsum(pca_full.explained_variance_ratio_)
        n_comp   = len(cumvar)

        row = {"route": route_id, "n_flights": data["n_flights"]}
        for thresh in VARIANCE_THRESHOLDS:
            idx  = int(np.searchsorted(cumvar, thresh))
            d    = min(idx + 1, n_comp)
            row[f"d@{int(thresh * 100)}pct"] = d

        rows.append(row)
        route_cache_raw[route_id] = data

        logger.info(
            f"  {route_id:12s}  n={data['n_flights']:4d}  " +
            "  ".join(
                f"d@{int(t*100)}%={row[f'd@{int(t*100)}pct']:3d}"
                for t in VARIANCE_THRESHOLDS
            )
        )

    if not rows:
        raise RuntimeError("Phase A: no calibration routes could be loaded.")

    summary_df = pd.DataFrame(rows)
    col_95     = f"d@{int(PRIMARY_THRESHOLD * 100)}pct"
    d_pca      = int(summary_df[col_95].max())   # conservative: max across routes
    n_standard = N_STANDARD_FACTOR * d_pca

    logger.info(
        f"\n  ✓  D_PCA      = {d_pca}  "
        f"(max d at {int(PRIMARY_THRESHOLD*100)}% variance across all routes)"
    )
    logger.info(f"  ✓  N_STANDARD = {n_standard}  (= {N_STANDARD_FACTOR} × D_PCA)")

    out_csv = CALIBRATION_OUT_DIR / "phase_a_variance_table.csv"
    summary_df.to_csv(out_csv, index=False)
    logger.info(f"  Saved: {out_csv}")

    # Attach projected PCA coordinates now that D_PCA is known
    route_cache: dict = {}
    for route_id, data in route_cache_raw.items():
        X_pca         = data["pca_full"].transform(data["X_scaled"])[:, :d_pca]
        data["X_pca"]  = X_pca
        data["var_gt"] = np.var(X_pca, axis=0)
        route_cache[route_id] = data

    return d_pca, n_standard, route_cache, summary_df


# ---------------------------------------------------------------------------
# Phase B — ΔRSD Threshold Calibration  (bootstrapped)
# ---------------------------------------------------------------------------

def _terminal_delta_rsd(X_pca_sample: np.ndarray) -> float:
    """
    Computes the terminal ΔRSD of *X_pca_sample* using the same mini-batch
    accumulation logic as the production stability worker.
    Delegates to _compute_stability (imported from stability_worker).
    """
    delta_cv, *_ = _compute_stability(X_pca_sample, _DELTA_CV_BATCH_SIZE)
    return delta_cv


def run_phase_b(
    route_cache: dict,
    d_pca: int,
    n_standard: int,
    candidate_thresholds: list = DEFAULT_CANDIDATE_THRESHOLDS,
    bootstrap_k: int = DEFAULT_BOOTSTRAP_K,
) -> tuple:
    """
    Bootstrap simulation mapping ΔRSD threshold values to ground-truth
    approximation error.

    For each calibration route, K independent random sub-samples are drawn at
    several N values (N_STANDARD × [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]).
    For each draw the terminal ΔRSD and normalised GT error are recorded.

    Returns
    -------
    confidence_df   : pd.DataFrame   summary table (one row per threshold)
    selected_tau    : float          auto-selected DELTA_CV_THRESHOLD
    """
    logger.info("\n%s\nPHASE B  —  ΔRSD Threshold Calibration\n%s", "="*60, "="*60)
    CALIBRATION_OUT_DIR.mkdir(parents=True, exist_ok=True)

    # N values to explore, ensuring each is at least 2 × batch size
    n_values = sorted({
        max(_DELTA_CV_BATCH_SIZE * 2 + 1, int(n_standard * f))
        for f in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
    })

    raw_records = []

    for route_id, data in route_cache.items():
        X_pca  = data["X_pca"]
        var_gt = data["var_gt"]
        gt_mag = float(np.linalg.norm(var_gt) + 1e-8)
        n_avail = len(X_pca)
        rng = np.random.default_rng(seed=abs(hash(route_id)) % 2**32)

        logger.info(
            f"  [{route_id}]  {bootstrap_k} draws × {len(n_values)} N-values …"
        )

        for N in n_values:
            N_eff = min(N, n_avail)   # can't draw more than we have

            for k in range(bootstrap_k):
                idx      = rng.choice(n_avail, size=N_eff, replace=False)
                X_sample = X_pca[idx]

                var_sample  = np.var(X_sample, axis=0)
                gt_err_norm = float(np.linalg.norm(var_sample - var_gt)) / gt_mag
                delta_rsd   = _terminal_delta_rsd(X_sample)

                raw_records.append(dict(
                    route=route_id,
                    draw=k,
                    N=N_eff,
                    delta_rsd=delta_rsd,
                    gt_err_norm=gt_err_norm,
                ))

    df_raw = pd.DataFrame(raw_records)
    raw_csv = CALIBRATION_OUT_DIR / "phase_b_raw_bootstrap.csv"
    df_raw.to_csv(raw_csv, index=False)
    logger.info(f"  Saved raw records ({len(df_raw)} rows): {raw_csv}")

    # ----- Build confidence table -----
    total_draws = len(route_cache) * bootstrap_k
    conf_rows   = []

    for tau in candidate_thresholds:
        conv_errors  = []
        conv_ns      = []
        n_conv_at_n_std = 0
        n_no_conv    = 0

        for route_id in route_cache:
            for k in range(bootstrap_k):
                sub = df_raw[
                    (df_raw["route"] == route_id) & (df_raw["draw"] == k)
                ].sort_values("N")

                converged = sub[sub["delta_rsd"] < tau]
                if converged.empty:
                    n_no_conv += 1
                else:
                    first = converged.iloc[0]
                    conv_errors.append(first["gt_err_norm"])
                    conv_ns.append(int(first["N"]))
                    if first["N"] <= n_standard:
                        n_conv_at_n_std += 1

        n_conv = total_draws - n_no_conv

        if conv_errors:
            median_err = float(np.median(conv_errors))
            p_good     = float(np.mean(np.array(conv_errors) < GT_ERROR_BOUND))
            avg_n      = float(np.mean(conv_ns))
        else:
            median_err, p_good, avg_n = float("nan"), 0.0, float("nan")

        p_conv_at_nstd = n_conv_at_n_std / total_draws

        conf_rows.append(dict(
            threshold          = tau,
            median_gt_error    = round(median_err, 4),
            p_gt_error_lt_5pct = round(p_good, 3),
            avg_n_at_convergence        = round(avg_n, 1),
            p_converge_at_n_standard    = round(p_conv_at_nstd, 3),
            n_draws_converged           = n_conv,
            n_draws_no_converge         = n_no_conv,
        ))

        logger.info(
            f"  τ={tau:.3f} | median_err={median_err:.4f} "
            f"| P(err<5%)={p_good:.1%} "
            f"| avg_N={avg_n:.0f} "
            f"| P(conv@N_STD)={p_conv_at_nstd:.1%} "
            f"| converged={n_conv}/{total_draws}"
        )

    confidence_df = pd.DataFrame(conf_rows)
    conf_csv      = CALIBRATION_OUT_DIR / "phase_b_confidence_table.csv"
    confidence_df.to_csv(conf_csv, index=False)
    logger.info(f"  Saved confidence table: {conf_csv}")

    # Auto-select: smallest tau where P(GT error < 5%) >= CONFIDENCE_TARGET
    eligible = confidence_df[confidence_df["p_gt_error_lt_5pct"] >= CONFIDENCE_TARGET]
    if not eligible.empty:
        selected_tau = float(eligible["threshold"].min())
        logger.info(
            f"\n  ✓  Auto-selected DELTA_CV_THRESHOLD = {selected_tau}  "
            f"(P(GT error < {int(GT_ERROR_BOUND*100)}%) ≥ {CONFIDENCE_TARGET:.0%})"
        )
    else:
        selected_tau = float(candidate_thresholds[-1])
        logger.warning(
            f"  No threshold reached P(GT error < {int(GT_ERROR_BOUND*100)}%) "
            f"≥ {CONFIDENCE_TARGET:.0%}.  Using tightest candidate: {selected_tau}. "
            f"Consider tightening GT_ERROR_BOUND or reducing CONFIDENCE_TARGET."
        )

    return confidence_df, selected_tau


# ---------------------------------------------------------------------------
# Phase C — Requery Rate Simulation
# ---------------------------------------------------------------------------

def run_phase_c(
    route_cache: dict,
    n_standard: int,
    candidate_thresholds: list = DEFAULT_CANDIDATE_THRESHOLDS,
    bootstrap_k: int = DEFAULT_BOOTSTRAP_K,
) -> pd.DataFrame:
    """
    Simulates the Step 3 resampling loop on all calibration routes.

    For each (threshold, route, bootstrap draw):
    - Start with N_STANDARD flights → compute ΔRSD
    - If ΔRSD ≥ threshold: expand sample by STABILITY_RESAMPLE_MULTIPLIER, retry
    - Up to STABILITY_MAX_RESAMPLE_ROUNDS times; record convergence round

    Note: uses the oracle PCA coordinates already in route_cache (i.e., PCA is
    not re-fitted per draw).  This is a simplification; in production, PCA is
    re-fitted on the expanded cohort.  The requery-rate direction of bias is
    slightly optimistic (a re-fitted PCA on a larger sample is more stable, so
    this simulation may predict marginally fewer resamples than reality).

    Returns
    -------
    requery_df : pd.DataFrame (one row per candidate threshold)
    """
    logger.info("\n%s\nPHASE C  —  Requery Rate Simulation\n%s", "="*60, "="*60)
    CALIBRATION_OUT_DIR.mkdir(parents=True, exist_ok=True)

    total_draws = len(route_cache) * bootstrap_k
    requery_rows = []

    for tau in candidate_thresholds:
        round_counts  = {r: 0 for r in range(STABILITY_MAX_RESAMPLE_ROUNDS + 1)}
        round_counts["forced"] = 0
        total_trino_calls = 0

        for route_id, data in route_cache.items():
            X_pca   = data["X_pca"]
            n_avail = len(X_pca)
            rng = np.random.default_rng(
                seed=(abs(hash(route_id)) + abs(hash(tau))) % 2**32
            )

            for _ in range(bootstrap_k):
                converged_round = None

                for rnd in range(STABILITY_MAX_RESAMPLE_ROUNDS + 1):
                    n_query = min(
                        int(n_standard * (STABILITY_RESAMPLE_MULTIPLIER ** rnd)),
                        n_avail,
                    )
                    total_trino_calls += 1

                    idx      = rng.choice(n_avail, size=n_query, replace=False)
                    X_sample = X_pca[idx]
                    delta_rsd = _terminal_delta_rsd(X_sample)

                    if delta_rsd < tau:
                        converged_round = rnd
                        break

                if converged_round is not None:
                    round_counts[converged_round] += 1
                else:
                    round_counts["forced"] += 1

        expected_calls = total_trino_calls / total_draws if total_draws > 0 else 0

        row = {"threshold": tau}
        for rnd in range(STABILITY_MAX_RESAMPLE_ROUNDS + 1):
            pct = round_counts[rnd] / total_draws
            row[f"converge_round_{rnd}_pct"] = round(pct, 3)
        row["forced_pct"]                       = round(round_counts["forced"] / total_draws, 3)
        row["expected_trino_calls_per_route"]   = round(expected_calls, 3)

        logger.info(
            f"  τ={tau:.3f} | " +
            " | ".join(
                f"R{r}={round_counts[r]/total_draws:.1%}"
                for r in range(STABILITY_MAX_RESAMPLE_ROUNDS + 1)
            ) +
            f" | forced={round_counts['forced']/total_draws:.1%}"
            f" | E[calls]={expected_calls:.2f}"
        )

        requery_rows.append(row)

    requery_df = pd.DataFrame(requery_rows)
    out_csv    = CALIBRATION_OUT_DIR / "phase_c_requery_table.csv"
    requery_df.to_csv(out_csv, index=False)
    logger.info(f"  Saved requery table: {out_csv}")

    return requery_df


# ---------------------------------------------------------------------------
# Phase D — CPU Timing Profile
# ---------------------------------------------------------------------------

def run_phase_d(registry_df: pd.DataFrame, d_pca: int) -> pd.DataFrame:
    """
    Times each CPU-bound computation phase on the calibration routes.

    Trino / parquet-fetch timings are NOT measured here — they come from the
    existing fetch log and will be read by the Step 6 decision script.

    Outputs phase_d_timings.csv with one row per route plus an AVERAGE row.
    """
    logger.info("\n%s\nPHASE D  —  CPU Computation Timing Profile\n%s", "="*60, "="*60)
    CALIBRATION_OUT_DIR.mkdir(parents=True, exist_ok=True)

    timing_rows = []

    for route_id in CALIBRATION_ROUTES:
        logger.info(f"  [{route_id}] timing …")
        row = {"route": route_id}

        # ---- Load + Classify + Normalise ----
        try:
            t0 = time.perf_counter()
            flights = _load_route_flights(route_id, n_target=9_999, registry_df=registry_df)
            if not flights:
                logger.warning(f"  [{route_id}] no flights — skipping.")
                continue
            norm_flights, _ = classify_and_normalize_cohort(flights)
            row["t_classify_ms"] = round((time.perf_counter() - t0) * 1_000, 2)
            row["n_flights"]     = len(norm_flights)
        except Exception as exc:
            logger.error(f"  [{route_id}] classify failed: {exc}")
            continue

        # ---- Vectorise + Z-score ----
        try:
            t0 = time.perf_counter()
            X_raw = vectorize_cohort(norm_flights)
            X_scaled, *_ = normalize_vectors(X_raw)
            row["t_vectorize_ms"] = round((time.perf_counter() - t0) * 1_000, 2)
        except Exception as exc:
            logger.error(f"  [{route_id}] vectorise failed: {exc}")
            continue

        # ---- PCA Fit + Project ----
        try:
            t0 = time.perf_counter()
            pca = PCA(n_components=d_pca, random_state=42)
            pca.fit(X_scaled)
            X_pca = pca.transform(X_scaled)
            row["t_pca_ms"] = round((time.perf_counter() - t0) * 1_000, 2)
        except Exception as exc:
            logger.error(f"  [{route_id}] PCA failed: {exc}")
            continue

        # ---- ΔRSD computation ----
        try:
            t0 = time.perf_counter()
            _terminal_delta_rsd(X_pca)
            row["t_delta_rsd_ms"] = round((time.perf_counter() - t0) * 1_000, 2)
        except Exception as exc:
            logger.error(f"  [{route_id}] ΔRSD failed: {exc}")
            row["t_delta_rsd_ms"] = float("nan")

        # ---- K-Means + Silhouette loop (k = 2 … CLUSTERING_MAX_K) ----
        try:
            n = len(X_pca)
            max_k = min(CLUSTERING_MAX_K, n - 1)
            t0 = time.perf_counter()
            k_ms = []
            for k in range(2, max_k + 1):
                tk     = time.perf_counter()
                km     = KMeans(n_clusters=k, random_state=42, n_init="auto")
                labels = km.fit_predict(X_pca)
                if len(set(labels)) > 1:
                    silhouette_score(X_pca, labels)
                k_ms.append(round((time.perf_counter() - tk) * 1_000, 2))
            row["t_clustering_total_ms"] = round((time.perf_counter() - t0) * 1_000, 2)
            row["t_clustering_per_k_ms"] = round(
                row["t_clustering_total_ms"] / max(len(k_ms), 1), 2
            )
            row["n_k_evaluated"] = len(k_ms)
        except Exception as exc:
            logger.error(f"  [{route_id}] clustering timing failed: {exc}")
            row["t_clustering_total_ms"] = float("nan")
            row["t_clustering_per_k_ms"] = float("nan")
            row["n_k_evaluated"]         = 0

        # ---- Total CPU per route ----
        cpu_phases = [
            row.get("t_classify_ms",  0.0),
            row.get("t_vectorize_ms", 0.0),
            row.get("t_pca_ms",       0.0),
            row.get("t_delta_rsd_ms", 0.0),
            row.get("t_clustering_total_ms", 0.0),
        ]
        row["t_total_cpu_ms"] = round(sum(
            v for v in cpu_phases if not math.isnan(v)
        ), 2)

        timing_rows.append(row)
        logger.info(
            f"  [{route_id}]  classify={row.get('t_classify_ms', '?'):.0f}ms  "
            f"vec={row.get('t_vectorize_ms', '?'):.0f}ms  "
            f"pca={row.get('t_pca_ms', '?'):.0f}ms  "
            f"ΔRsd={row.get('t_delta_rsd_ms', '?'):.0f}ms  "
            f"cluster={row.get('t_clustering_total_ms', '?'):.0f}ms  "
            f"TOTAL={row['t_total_cpu_ms']:.0f}ms"
        )

    if not timing_rows:
        logger.error("Phase D: no timing data collected.")
        return pd.DataFrame()

    timings_df = pd.DataFrame(timing_rows)

    # Append AVERAGE summary row
    num_cols = [c for c in timings_df.columns if c != "route"]
    avg_row  = {"route": "AVERAGE"}
    for c in num_cols:
        try:
            avg_row[c] = round(float(timings_df[c].mean(skipna=True)), 2)
        except Exception:
            avg_row[c] = None
    timings_df = pd.concat([timings_df, pd.DataFrame([avg_row])], ignore_index=True)

    out_csv = CALIBRATION_OUT_DIR / "phase_d_timings.csv"
    timings_df.to_csv(out_csv, index=False)
    logger.info(f"  Saved timing profile: {out_csv}")

    avg_total = avg_row.get("t_total_cpu_ms", "?")
    logger.info(
        f"\n  ✓  Average CPU per route: {avg_total} ms\n"
        f"     (Step 6 reads Trino fetch log → computes ratio → Option A vs B decision)"
    )

    return timings_df


# ---------------------------------------------------------------------------
# Config writer
# ---------------------------------------------------------------------------

def write_config(d_pca: int, n_standard: int, delta_cv_threshold: float) -> None:
    """
    Patches the three sentinel values in ``src/common/config.py`` with the
    calibrated constants.  Idempotent; safe to call multiple times.
    """
    config_path = BASE_DIR / "src" / "common" / "config.py"
    text = config_path.read_text(encoding="utf-8")

    replacements = [
        (r"(D_PCA\s*=\s*)-?\d+",             rf"\g<1>{d_pca}"),
        (r"(N_STANDARD\s*=\s*)-?\d+",         rf"\g<1>{n_standard}"),
        (r"(DELTA_CV_THRESHOLD\s*=\s*)[\d.e+-]+", rf"\g<1>{delta_cv_threshold}"),
    ]
    for pattern, repl in replacements:
        new_text = re.sub(pattern, repl, text)
        if new_text == text:
            logger.warning(f"Pattern did not match in config.py — check manually: {pattern}")
        text = new_text

    config_path.write_text(text, encoding="utf-8")
    logger.info(
        f"  ✓  config.py updated — "
        f"D_PCA={d_pca}  N_STANDARD={n_standard}  "
        f"DELTA_CV_THRESHOLD={delta_cv_threshold}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Calibration Campaign — offline simulation to derive "
            "D_PCA, N_STANDARD, and DELTA_CV_THRESHOLD."
        )
    )
    parser.add_argument(
        "--phases", default="A,B,C,D",
        help="Comma-separated phases to run (default: A,B,C,D).",
    )
    parser.add_argument(
        "--bootstrap-k", type=int, default=DEFAULT_BOOTSTRAP_K,
        help=f"Bootstrap draws per (route, N) pair in Phase B/C (default: {DEFAULT_BOOTSTRAP_K}).",
    )
    parser.add_argument(
        "--candidate-thresholds", default=",".join(str(t) for t in DEFAULT_CANDIDATE_THRESHOLDS),
        help="Comma-separated ΔRSD candidate thresholds for Phase B/C.",
    )
    parser.add_argument(
        "--write-config", action="store_true",
        help="Write calibrated constants to config.py (opt-in; review CSVs first).",
    )
    parser.add_argument(
        "--d-pca", type=int, default=None,
        help=(
            "Override D_PCA directly (skips Phase A). "
            "Route cache is re-projected at this dimensionality. "
            "Example: --d-pca 13"
        ),
    )
    parser.add_argument(
        "--n-standard", type=int, default=None,
        help=(
            "Override N_STANDARD directly. "
            "Defaults to N_STANDARD_FACTOR × --d-pca when --d-pca is provided."
        ),
    )
    parser.add_argument(
        "--out-suffix", type=str, default="",
        help=(
            "Subdirectory suffix for output CSVs so multiple scenarios coexist. "
            "Example: --out-suffix d13  →  data/calibration/d13/*.csv"
        ),
    )
    args = parser.parse_args()

    phases = {p.strip().upper() for p in args.phases.split(",")}
    candidate_thresholds = [float(t) for t in args.candidate_thresholds.split(",")]

    # ---- Load registry once ----
    logger.info("Loading trajectory registry …")
    registry_df = load_trajectory_registry()
    if registry_df.empty:
        logger.error("Trajectory registry is empty or missing. Aborting.")
        sys.exit(1)
    logger.info(f"Registry loaded: {len(registry_df):,} entries.")

    d_pca       = None
    n_standard  = None
    route_cache = None
    selected_tau = None

    # ---- Apply --out-suffix (changes output directory for this run) ----
    global CALIBRATION_OUT_DIR
    if args.out_suffix:
        CALIBRATION_OUT_DIR = BASE_DIR / "data" / "calibration" / args.out_suffix.strip("_")
        logger.info(f"Output directory overridden to: {CALIBRATION_OUT_DIR}")

    # ---- Phase A / d-pca override ----
    if args.d_pca is not None:
        # CLI override takes full precedence — skip Phase A entirely.
        d_pca      = args.d_pca
        n_standard = args.n_standard if args.n_standard is not None else N_STANDARD_FACTOR * d_pca
        logger.info(
            f"D_PCA override from CLI: D_PCA={d_pca}, N_STANDARD={n_standard} "
            f"(N_STANDARD_FACTOR={N_STANDARD_FACTOR})"
        )
        if phases & {"B", "C", "D"}:
            logger.info("Re-projecting route cache at overridden D_PCA …")
            route_cache = {}
            for route_id in CALIBRATION_ROUTES:
                try:
                    route_cache[route_id] = _prepare_route(
                        route_id, registry_df, d_pca=d_pca
                    )
                except RuntimeError as exc:
                    logger.error(f"  Could not load {route_id}: {exc}")
    elif "A" in phases:
        d_pca, n_standard, route_cache, _ = run_phase_a(registry_df)
    else:
        from src.common.config import D_PCA as cfg_d, N_STANDARD as cfg_n
        if cfg_d <= 0 or cfg_n <= 0:
            logger.error(
                "Phase A skipped but D_PCA/N_STANDARD are still sentinel (-1) in config. "
                "Run Phase A first, or pass --d-pca / --n-standard directly."
            )
            sys.exit(1)
        d_pca, n_standard = cfg_d, cfg_n
        logger.info(f"Phase A skipped — using D_PCA={d_pca}, N_STANDARD={n_standard} from config.")
        # Still load route data for downstream phases
        if phases & {"B", "C", "D"}:
            route_cache = {}
            for route_id in CALIBRATION_ROUTES:
                try:
                    route_cache[route_id] = _prepare_route(route_id, registry_df, d_pca=d_pca)
                except RuntimeError as exc:
                    logger.error(f"  Could not load {route_id}: {exc}")

    # ---- Phase B ----
    if "B" in phases:
        if not route_cache:
            logger.error("Phase B: no route data available. Run Phase A first.")
            sys.exit(1)
        _, selected_tau = run_phase_b(
            route_cache, d_pca, n_standard,
            candidate_thresholds, args.bootstrap_k,
        )

    # ---- Phase C ----
    if "C" in phases:
        if not route_cache:
            logger.error("Phase C: no route data available. Run Phase A first.")
            sys.exit(1)
        run_phase_c(route_cache, n_standard, candidate_thresholds, args.bootstrap_k)

    # ---- Phase D ----
    if "D" in phases:
        run_phase_d(registry_df, d_pca)

    # ---- Write config (opt-in) ----
    if args.write_config:
        if d_pca is None or n_standard is None:
            logger.error("Cannot write config: D_PCA / N_STANDARD not determined.")
        elif selected_tau is None:
            logger.error("Cannot write config: DELTA_CV_THRESHOLD not determined — run Phase B.")
        else:
            logger.info("\nCommitting calibrated constants to config.py …")
            write_config(d_pca, n_standard, selected_tau)

    logger.info("\nCalibration campaign complete.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - [CALIB] - %(levelname)s - %(message)s",
    )
    setup_file_logger(log_filename="calibration_campaign.log")
    main()
