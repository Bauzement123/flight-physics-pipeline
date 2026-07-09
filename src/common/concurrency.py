"""Concurrency helpers for multiprocessing workers and numeric thread limits."""

import os


def set_numeric_thread_env(threads_per_worker: int) -> None:
    """Restrict BLAS/OpenMP/NumExpr C-library thread counts for the current process."""
    thread_count = str(threads_per_worker)
    for var in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ):
        os.environ[var] = thread_count


def limit_numeric_threads(threads_per_worker: int) -> None:
    """Apply environment and threadpoolctl numeric thread limits when available."""
    set_numeric_thread_env(threads_per_worker)
    try:
        from threadpoolctl import threadpool_limits
        threadpool_limits(threads_per_worker)
    except ImportError:
        pass
