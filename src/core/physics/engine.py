"""
engine.py — Pure Parallelisation Layer

Provides two things only:
  1. crop_met_dataset()  — spatial ERA5 window slicing (unchanged from old engine)
  2. run_parallel()      — ThreadPoolExecutor dispatcher that yields
                           List[WorkerResult] per completed batch

All model instantiation, flight loading, and batch construction logic has been
moved to worker.py and the slots/. This file is a pure coordinator.

ThreadPoolExecutor is used (not ProcessPoolExecutor) so that all threads share
the parent process's MetDataset in memory. ProcessPool would require each worker
to hold its own ERA5 copy — that is OOM territory for typical dataset sizes.
"""

import gc
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Iterator, List

from pycontrails import MetDataset

from src.common.config import WEATHER_PADDING
from src.data_manager.schemas import BatchOutput

logger = logging.getLogger(__name__)


def crop_met_dataset(
    met: MetDataset,
    bbox: list,
    pad: float = WEATHER_PADDING,
) -> MetDataset:
    """Spatially crop an xarray-backed MetDataset to a bounding box [W, S, E, N].

    Handles descending ERA5 latitude coordinates. Applies ``pad`` degrees of
    buffer on all sides before slicing.

    Parameters
    ----------
    met : MetDataset
        Full-resolution ERA5 dataset.
    bbox : list[float]
        ``[west, south, east, north]`` in decimal degrees.
    pad : float, optional
        Padding in degrees added to each edge (default ``WEATHER_PADDING``).

    Returns
    -------
    MetDataset
        Spatially sliced dataset.
    """
    ds = met.data
    west, south, east, north = bbox

    lat_name = "latitude" if "latitude" in ds.coords else "lat"
    lon_name = "longitude" if "longitude" in ds.coords else "lon"

    orig_lats = ds[lat_name].values
    orig_lons = ds[lon_name].values
    logger.info(
        "Original weather grid bounds: lat=[%.2f, %.2f] (n=%d), lon=[%.2f, %.2f] (n=%d).",
        orig_lats.min(), orig_lats.max(), orig_lats.shape[0],
        orig_lons.min(), orig_lons.max(), orig_lons.shape[0],
    )

    west  = max(-180.0, west  - pad)
    east  = min( 180.0, east  + pad)
    south = max( -90.0, south - pad)
    north = min(  90.0, north + pad)

    lat_coords        = ds[lat_name].values
    is_lat_descending = len(lat_coords) > 1 and lat_coords[0] > lat_coords[-1]
    lat_slice = slice(north, south) if is_lat_descending else slice(south, north)
    lon_slice = slice(west, east)

    logger.info("Slicing MetDataset: lat=%s, lon=%s.", lat_slice, lon_slice)
    ds_cropped   = ds.sel({lat_name: lat_slice, lon_name: lon_slice})
    cropped_lats = ds_cropped[lat_name].values
    cropped_lons = ds_cropped[lon_name].values
    logger.info(
        "Cropped weather grid bounds: lat=[%.2f, %.2f] (n=%d), lon=[%.2f, %.2f] (n=%d).",
        cropped_lats.min(), cropped_lats.max(), cropped_lats.shape[0],
        cropped_lons.min(), cropped_lons.max(), cropped_lons.shape[0],
    )
    return MetDataset(ds_cropped)


def run_parallel(
    batches: Iterable[List],
    worker_fn: Callable[[List], BatchOutput],
    max_workers: int = 4,
) -> Iterator[BatchOutput]:
    """Execute pre-formed SimTask batches in parallel and yield results as they complete.

    Parameters
    ----------
    batches : Iterable[List[SimTask]]
        Pre-partitioned batches produced by Slot 2. Each inner list is one
        ``(dep, arr, cluster_id)`` group.
    worker_fn : Callable[[List[SimTask]], BatchOutput]
        Pre-bound callable — typically ``functools.partial(run_batch,
        corridors_map=..., model_config_id=..., sim_mode=...,
        lake_path=..., met=..., rad=..., max_age_hours=...)``.
        Engine knows nothing about its arguments beyond the batch.
    max_workers : int, optional
        Thread pool size (default 4). Even ``max_workers=1`` uses the
        executor so code paths are identical regardless of concurrency level.

    Yields
    ------
    BatchOutput
        Raw results from one completed batch, in completion order (not submission
        order). On batch exception: logs ERROR and yields empty ``BatchOutput``
        so the orchestrator loop can continue.

    Notes
    -----
    ``gc.collect()`` is called after each batch result is yielded to promptly
    release Flight object memory before the next batch starts.
    """
    batch_list = list(batches)
    if not batch_list:
        logger.warning("run_parallel called with no batches — nothing to do.")
        return

    logger.info(
        "Submitting %d batches to ThreadPoolExecutor (max_workers=%d).",
        len(batch_list), max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(worker_fn, batch): i
            for i, batch in enumerate(batch_list)
        }

        for future in as_completed(future_to_idx):
            batch_idx = future_to_idx[future]
            try:
                results: BatchOutput = future.result()
                logger.info(
                    "Batch %d/%d complete — %d successful, %d failed.",
                    batch_idx + 1, len(batch_list),
                    len(results.successful), len(results.failed),
                )
                yield results
            except Exception as exc:
                logger.error(
                    "Batch %d/%d raised an exception — yielding empty BatchOutput: %s",
                    batch_idx + 1, len(batch_list), exc,
                )
                yield BatchOutput(successful=[], failed=[])
            finally:
                # Prompt GC to release Flight objects from the completed batch
                gc.collect()
