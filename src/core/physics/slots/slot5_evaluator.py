"""
slots/slot5_evaluator.py — Slot 5: Batch Result Evaluation

Owns all verdict construction for a completed worker batch. Receives raw
BatchOutput from worker.run_batch and is the single source of truth for what
"success" and "fail" mean in the pipeline.

Responsibilities
----------------
1. _classify_results(): Universally (sim_mode-independent) constructs WorkerResult
   objects from raw (task, Flight) pairs. Extracts EF, actual_fl, detects
   missing-from-output failures. Never called directly by orchestrator.

2. Mode-specific evaluators: Determine still_todo only. Success/fail partitioning
   is already done by _classify_results before mode dispatch. Adding a new sim_mode
   means adding one _evaluate_<mode> function here — zero changes to worker.py.

Execution Modes
---------------
- standard:
  still_todo is always empty.
- variational:
  FL sanity check applied to succeeded results; step-down SimTasks emitted
  into still_todo for re-dispatch by the orchestrator.

Selection Pattern
-----------------
  The public entry point is evaluate(). Internally it calls get_evaluator(sim_mode)
  to dispatch to the correct implementation. Callers never reference
  _evaluate_standard or _evaluate_variational directly.
"""

import logging
from typing import Callable, Dict, List, Tuple

import numpy as np
from pycontrails import Flight

from src.common.config import MIN_SAFE_FL
from src.core.physics.slots.slot2_batcher import compute_stepdown_task
from src.data_manager.schemas import BatchOutput, EvalResult, SimTask, WorkerResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (moved from worker.py — Slot 5 owns EF/FL extraction)
# ---------------------------------------------------------------------------

def _extract_ef(flight: Flight) -> float:
    """Extract total Energy Forcing (J) from a simulated Flight."""
    if "ef" not in flight.data:
        return 0.0
    return float(np.nansum(flight["ef"]))


def _extract_actual_fl(flight: Flight) -> float:
    """Extract max cruise FL from flight trajectory altitude."""
    if "altitude" in flight.data and len(flight.data["altitude"]) > 0:
        max_alt_m = float(np.nanmax(flight["altitude"]))
        return round(max_alt_m / (100 * 0.3048), 1)
    return 0.0


# ---------------------------------------------------------------------------
# Universal classifier — sim_mode independent
# ---------------------------------------------------------------------------

def _classify_results(
    batch_output: BatchOutput,
    model_config_id: str,
) -> Tuple[List[WorkerResult], List[WorkerResult]]:
    """Classify a BatchOutput into (succeeded, failed) WorkerResult lists.

    This is the single source of truth for what "success" and "fail" mean.
    It is sim_mode-independent — the verdict on whether a flight physically
    completed does not change per mode.

    Parameters
    ----------
    batch_output : BatchOutput
        Raw output from worker.run_batch.
    model_config_id : str
        Propagated to each WorkerResult for lake-query consistency.

    Returns
    -------
    succeeded : List[WorkerResult]
        Flights where PSFlight + CoCiP both completed and appeared in output.
    failed : List[WorkerResult]
        Flights that failed at any stage (load, PSFlight, CoCiP, missing from output).
    """
    succeeded: List[WorkerResult] = []
    failed: List[WorkerResult] = []

    for task, flight in batch_output.successful:
        succeeded.append(WorkerResult(
            sim_fid=task.to_sim_fid(),
            ef=_extract_ef(flight),
            fl=task.fl,
            model_config_id=model_config_id,
            status="success",
            actual_fl=_extract_actual_fl(flight),
        ))

    for task, _reason in batch_output.failed:
        failed.append(WorkerResult(
            sim_fid=task.to_sim_fid(),
            ef=0.0,
            fl=task.fl,
            model_config_id=model_config_id,
            status="fail",
            actual_fl=None,
        ))

    return succeeded, failed


# ---------------------------------------------------------------------------
# Private evaluators — sim_mode dependent (still_todo only)
# ---------------------------------------------------------------------------

def _evaluate_standard(
    succeeded: List[WorkerResult],
    failed: List[WorkerResult],
    task_by_fid: Dict[str, SimTask],
    step_size: float,
    min_safe_fl: float,
) -> EvalResult:
    """Standard mode: still_todo always empty."""
    logger.info(
        "Slot 5 (standard): batch evaluated — %d succeeded, %d failed.",
        len(succeeded), len(failed),
    )
    return EvalResult(succeeded=succeeded, failed=failed, still_todo=[])


def _evaluate_variational(
    succeeded: List[WorkerResult],
    failed: List[WorkerResult],
    task_by_fid: Dict[str, SimTask],
    step_size: float,
    min_safe_fl: float,
) -> EvalResult:
    """Evaluate batch results for the variational step-down campaign.

    Applies an FL sanity check to each succeeded result: if actual_fl deviates
    from task.fl by more than 1.5 FL, the result is demoted to failed.
    For results that pass, emits a step-down SimTask into still_todo if EF > 0
    and FL is still above the operational floor.

    Parameters
    ----------
    succeeded : List[WorkerResult]
        Pre-classified successful results from _classify_results.
    failed : List[WorkerResult]
        Pre-classified failed results from _classify_results.
    task_by_fid : Dict[str, SimTask]
        SIM_FID → original SimTask, used for FL sanity check and step-down.
    step_size : float
        FL decrement per step-down iteration (feet).
    min_safe_fl : float
        Operational floor — no step-down emitted below this FL.
    """
    clean_succeeded: List[WorkerResult] = []
    all_failed: List[WorkerResult] = list(failed)
    still_todo: List[SimTask] = []

    for result in succeeded:
        task = task_by_fid.get(result.sim_fid)
        if task is None:
            logger.warning(
                "Slot 5 (variational): no matching task for sim_fid=%s — skipping step-down.",
                result.sim_fid,
            )
            clean_succeeded.append(result)
            continue

        # FL Sanity Check: actual simulated FL must be within 1.5 FL of task target
        if result.actual_fl is not None and abs(result.actual_fl - task.fl) > 1.5:
            logger.error(
                "Slot 5 (variational) FL SANITY CHECK FAILED for %s: "
                "actual_fl=%.1f != task.fl=%.1f (tolerance=1.5 FL). Marking as failed.",
                result.sim_fid, result.actual_fl, task.fl,
            )
            result.status = "fail"
            all_failed.append(result)
            continue

        clean_succeeded.append(result)
        next_task = compute_stepdown_task(task, result.ef, step_size, min_safe_fl)
        if next_task is not None:
            still_todo.append(next_task)

    logger.info(
        "Slot 5 (variational): %d succeeded, %d failed → %d step-down task(s) re-queued.",
        len(clean_succeeded), len(all_failed), len(still_todo),
    )
    return EvalResult(succeeded=clean_succeeded, failed=all_failed, still_todo=still_todo)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_evaluator(
    sim_mode: str,
) -> Callable[..., EvalResult]:
    """Return the correct evaluator function for the given sim_mode.

    Parameters
    ----------
    sim_mode : str
        Execution mode: ``'standard'`` or ``'variational'``.

    Returns
    -------
    Callable
        Signature: ``(succeeded, failed, task_by_fid, step_size, min_safe_fl) -> EvalResult``.

    Raises
    ------
    ValueError
        If sim_mode is not ``'standard'`` or ``'variational'``.
    """
    _registry: dict = {
        "standard":    _evaluate_standard,
        "variational": _evaluate_variational,
    }
    if sim_mode not in _registry:
        raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'standard' or 'variational'.")
    return _registry[sim_mode]


# ---------------------------------------------------------------------------
# Public entry point — the only symbol callers import
# ---------------------------------------------------------------------------

def evaluate(
    batch_output: BatchOutput,
    task_by_fid: Dict[str, SimTask],
    sim_mode: str,
    model_config_id: str,
    step_size: float = 10.0,
    min_safe_fl: float = MIN_SAFE_FL,
) -> EvalResult:
    """Evaluate a completed batch of worker results and return a structured EvalResult.

    Parameters
    ----------
    batch_output : BatchOutput
        Raw output from worker.run_batch — raw (task, Flight) pairs.
    task_by_fid : Dict[str, SimTask]
        Mapping of SIM_FID → SimTask for all tasks in this batch.
        Required by the variational evaluator for FL sanity check and step-down.
        Pass an empty dict for standard mode (unused).
    sim_mode : str
        Execution mode string flag: ``'standard'`` or ``'variational'``.
    model_config_id : str
        Propagated to each WorkerResult for Delta Lake query consistency.
    step_size : float, optional
        FL decrement per step-down (default 10.0). Unused in standard mode.
    min_safe_fl : float, optional
        Minimum safe FL in feet (default MIN_SAFE_FL). Unused in standard mode.

    Returns
    -------
    EvalResult
        succeeded, failed, and still_todo (empty for standard; step-down tasks for variational).
    """
    # Step 1: Universal classification — constructs all WorkerResult objects
    succeeded, failed = _classify_results(batch_output, model_config_id)

    # Step 2: Mode-specific dispatch — determines still_todo only
    evaluator = get_evaluator(sim_mode)
    return evaluator(succeeded, failed, task_by_fid, step_size, min_safe_fl)
