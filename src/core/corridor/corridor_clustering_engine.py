from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

@dataclass
class ClusteringResult:
    """
    Pure data class containing the output of a clustering run.
    Stores all representations of the feature space for easy downstream analysis and plotting.
    """
    k: int                      # Optimal number of clusters (1 if no multi-cluster passes)
    route_class: int            # Assigned route class: 1=Single, 2=Binary, 3=Multi-Track, 4=Chaos
    silhouette_score: float     # Silhouette score of the selected clustering (np.nan if k=1)
    labels: np.ndarray          # Cluster assignment labels for each flight, shape (N,)
    medoid_indices: list[int]   # Index of the chosen medoid flight for each cluster, length k
    X_raw: np.ndarray           # Raw feature matrix, shape (N, 300)
    X_scaled: np.ndarray        # Z-score normalized feature matrix, shape (N, 300)
    X_pca: np.ndarray           # PCA projected feature matrix, shape (N, D_PCA)


def build_feature_matrix(flight_dfs: list[pd.DataFrame]) -> np.ndarray:
    """
    Resamples a list of trajectories to exactly 100 time-uniform points and stacks
    their coordinates into a single 300-dimension feature matrix [lat*100, lon*100, alt*100].

    Parameters
    ----------
    flight_dfs : list[pd.DataFrame]
        DataFrames representing smoothed flight trajectories. Must contain
        'latitude', 'longitude', 'altitude', and 'time' columns.

    Returns
    -------
    np.ndarray
        Feature matrix of shape (N, 300), dtype float64.
    """
    if not flight_dfs:
        raise ValueError("Cannot build feature matrix from empty list of flight DataFrames.")

    vectors = []
    for i, df in enumerate(flight_dfs):
        if df.empty or len(df) < 2:
            raise ValueError(f"Flight at index {i} has insufficient rows ({len(df)}) for resampling.")

        # Sort by time to ensure monotonic interpolation
        df_sorted = df.sort_values(by="time")
        lats = df_sorted["latitude"].values.astype(float)
        lons = df_sorted["longitude"].values.astype(float)
        alts = df_sorted["altitude"].values.astype(float)

        # Convert time to relative elapsed seconds
        times = pd.to_datetime(df_sorted["time"])
        ts = (times - times.iloc[0]).dt.total_seconds().values.astype(float)

        if len(ts) > 1 and ts[-1] > 0:
            t_norm = ts / ts[-1]
        else:
            t_norm = np.linspace(0.0, 1.0, len(ts))

        # Uniform interpolation grid
        target = np.linspace(0.0, 1.0, 100)
        lats_r = np.interp(target, t_norm, lats)
        lons_r = np.interp(target, t_norm, lons)
        alts_r = np.interp(target, t_norm, alts)

        # Flat 300-dimensional vector
        vectors.append(np.concatenate([lats_r, lons_r, alts_r]))

    return np.vstack(vectors)


def z_score_normalize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Standardize the features of X by removing the mean and scaling to unit variance.
    If a column has zero standard deviation, it is filled with zeros.

    Parameters
    ----------
    X : np.ndarray
        Feature matrix of shape (N, D).

    Returns
    -------
    X_scaled : np.ndarray
        Standardized feature matrix of shape (N, D).
    mean_vec : np.ndarray
        Mean vector of shape (D,).
    std_vec : np.ndarray
        Standard deviation vector of shape (D,).
    """
    mean_vec = np.mean(X, axis=0)
    std_vec = np.std(X, axis=0)

    # Avoid division by zero for invariant columns (e.g. static altitude segments)
    std_safe = np.where(std_vec == 0.0, 1.0, std_vec)
    X_scaled = (X - mean_vec) / std_safe
    X_scaled[:, std_vec == 0.0] = 0.0

    return X_scaled, mean_vec, std_vec


def pca_project(X_scaled: np.ndarray, n_components: int) -> tuple[np.ndarray, PCA]:
    """
    Applies Principal Component Analysis (PCA) to the Z-scored matrix.
    Caps n_components at N - 1 to satisfy scikit-learn constraints for small sample sizes.

    Parameters
    ----------
    X_scaled : np.ndarray
        Standardized feature matrix of shape (N, D).
    n_components : int
        Target number of PCA components.

    Returns
    -------
    X_pca : np.ndarray
        PCA-projected matrix of shape (N, effective_n).
    pca_model : PCA
        Fitted sklearn PCA model.
    """
    n_samples = X_scaled.shape[0]
    effective_n = min(n_components, n_samples - 1)
    effective_n = max(1, effective_n)

    pca = PCA(n_components=effective_n, random_state=42)
    X_pca = pca.fit_transform(X_scaled)
    return X_pca, pca


def silhouette_sweep(
    X: np.ndarray,
    k_range: range | list[int],
    threshold: float,
) -> tuple[int, float, np.ndarray]:
    """
    Evaluates KMeans clustering models for values of k.
    Selects the k with the highest silhouette score that meets or exceeds the threshold.
    If no k passes the threshold, returns k=1 with label 0 for all elements.

    Parameters
    ----------
    X : np.ndarray
        PCA-projected feature matrix of shape (N, D_PCA).
    k_range : range or list[int]
        Range of clusters to evaluate (e.g., range(2, 5)).
    threshold : float
        Minimum silhouette score required to accept k > 1.

    Returns
    -------
    best_k : int
        Optimal number of clusters (1 if no candidate passes the threshold).
    best_score : float
        Silhouette score of the best clustering (np.nan if best_k=1).
    best_labels : np.ndarray
        Cluster assignments of shape (N,).
    """
    best_k = 1
    best_score = float("-inf")
    best_labels = np.zeros(X.shape[0], dtype=int)
    n_samples = X.shape[0]

    for k in k_range:
        if k >= n_samples:
            continue

        kmeans = KMeans(n_clusters=k, random_state=42, n_init="auto")
        labels = kmeans.fit_predict(X)

        score = silhouette_score(X, labels, random_state=42)
        if score >= threshold and score > best_score:
            best_score = score
            best_k = k
            best_labels = labels

    final_score = best_score if best_k > 1 else np.nan
    return best_k, final_score, best_labels


def select_medoids(X_pca: np.ndarray, labels: np.ndarray, k: int) -> list[int]:
    """
    Identifies the index of the representative flight (medoid) for each cluster.
    The medoid is defined as the flight closest to the cluster's centroid in PCA space.

    Parameters
    ----------
    X_pca : np.ndarray
        PCA-projected matrix of shape (N, D_PCA).
    labels : np.ndarray
        Cluster assignments of shape (N,).
    k : int
        Number of clusters.

    Returns
    -------
    list[int]
        List of flight indices in the original cohort representing the medoids, length k.
    """
    medoid_indices = []
    for c in range(k):
        cluster_mask = (labels == c)
        cluster_indices = np.where(cluster_mask)[0]
        if len(cluster_indices) == 0:
            continue

        X_cluster = X_pca[cluster_mask]
        centroid = np.mean(X_cluster, axis=0)

        # Compute Euclidean distance to the centroid
        distances = np.linalg.norm(X_cluster - centroid, axis=1)
        best_local_idx = np.argmin(distances)
        medoid_indices.append(int(cluster_indices[best_local_idx]))

    return medoid_indices


def _assign_route_class(k: int, X_pca: np.ndarray, chaos_threshold: float) -> int:
    """
    Categorizes the route shape based on cluster counts and coordinate variance.
    1 = Single (k=1, low variance)
    2 = Binary (k=2)
    3 = Multi-Track (k>=3)
    4 = Chaos (k=1, high variance)
    """
    if k == 1:
        # Sum of variances across all PCA dimensions
        total_variance = np.sum(np.var(X_pca, axis=0))
        if total_variance > chaos_threshold:
            return 4  # Chaos
        return 1      # Single
    elif k == 2:
        return 2      # Binary
    else:
        return 3      # Multi-Track


def run_clustering(
    flight_dfs: list[pd.DataFrame],
    n_pca_components: int = 13,
    k_max: int = 4,
    silhouette_threshold: float = 0.35,
    chaos_variance_threshold: float = 200.0,
) -> ClusteringResult:
    """
    Stateless, high-level entry point to run the full corridor clustering math.

    Parameters
    ----------
    flight_dfs : list[pd.DataFrame]
        List of flight DataFrames to cluster.
    n_pca_components : int
        Number of PCA dimensions.
    k_max : int
        Capped maximum number of clusters.
    silhouette_threshold : float
        Silhouette score threshold to select k > 1.
    chaos_variance_threshold : float
        PCA variance threshold to classify a route as Chaos.

    Returns
    -------
    ClusteringResult
        Object containing optimal k, route class, silhouette score, assignments,
        medoid indices, and all feature space representations.
    """
    if not flight_dfs:
        raise ValueError("Cannot run clustering on empty list of flight DataFrames.")

    # 1. Vectorize
    X_raw = build_feature_matrix(flight_dfs)

    # 2. Z-Score Normalize
    X_scaled, _, _ = z_score_normalize(X_raw)

    n_samples = len(flight_dfs)

    # 3. Handle small cohort edge cases
    if n_samples < 3:
        # Force k=1
        X_pca, _ = pca_project(X_scaled, n_pca_components)
        best_k = 1
        best_score = np.nan
        labels = np.zeros(n_samples, dtype=int)
        medoid_indices = [0]
        route_class = 1
        return ClusteringResult(
            k=best_k,
            route_class=route_class,
            silhouette_score=best_score,
            labels=labels,
            medoid_indices=medoid_indices,
            X_raw=X_raw,
            X_scaled=X_scaled,
            X_pca=X_pca,
        )

    # 4. Apply PCA projection
    X_pca, _ = pca_project(X_scaled, n_pca_components)

    # 5. Evaluate k in range [2, min(k_max, n_samples - 1)]
    max_k_eval = min(k_max, n_samples - 1)
    if max_k_eval >= 2:
        k_range = range(2, max_k_eval + 1)
        best_k, best_score, labels = silhouette_sweep(X_pca, k_range, silhouette_threshold)
    else:
        best_k = 1
        best_score = np.nan
        labels = np.zeros(n_samples, dtype=int)

    # 6. Assign route class and select medoids
    route_class = _assign_route_class(best_k, X_pca, chaos_variance_threshold)
    medoid_indices = select_medoids(X_pca, labels, best_k)

    return ClusteringResult(
        k=best_k,
        route_class=route_class,
        silhouette_score=best_score,
        labels=labels,
        medoid_indices=medoid_indices,
        X_raw=X_raw,
        X_scaled=X_scaled,
        X_pca=X_pca,
    )
