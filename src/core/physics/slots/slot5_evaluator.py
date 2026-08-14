"""
slots/slot5_evaluator.py — Slot 5: Batch Result Evaluation

Classifies completed worker results into succeeded / failed / still_todo and
returns a structured EvalResult. The orchestrator logs from this object and
feeds still_todo back into the daily queue.

Execution Modes:
- standard (Waterfall 1 Baseline):
  Partitions WorkerResult list by status. still_todo is always empty.
- variational (Variational Optimization Pass):
  For each succeeded result with EF > 0, emits a step-down SimTask into
  still_todo. The orchestrator re-dispatches these into the next iteration.

Selection Pattern:
  The public entry point is evaluate(). Internally it calls get_evaluator(sim_mode)
  to dispatch to the correct implementation. Callers never reference
  _evaluate_standard or _evaluate_variational directly.
"""

import logging
from typing import Callable, Dict, List

from src.common.config import MIN_SAFE_FL
from src.core.physics.slots.slot2_batcher import compute_stepdown_task
from src.data_manager.schemas import EvalResult, SimTask, WorkerResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private evaluator implementations
# ---------------------------------------------------------------------------

def _evaluate_standard(
    results: List[WorkerResult],
    task_by_fid: Dict[str, SimTask],
    step_size: float,
    min_safe_fl: float,
) -> EvalResult:
    """Partition results into succeeded/failed; still_todo always empty for standard mode."""
    succeeded = [r for r in results if r.status == "success"]
    failed    = [r for r in results if r.status == "fail"]

    logger.info(
        "Slot 5 (standard): batch evaluated — %d succeeded, %d failed.",
        len(succeeded), len(failed),
    )
    return EvalResult(succeeded=succeeded, failed=failed, still_todo=[])


def _evaluate_variational(
    results: List[WorkerResult],
    task_by_fid: Dict[str, SimTask],
    step_size: float,
    min_safe_fl: float,
) -> EvalResult:
    """Evaluate batch results for the variational step-down campaign.

    For each succeeded result with EF > 0 and FL still above the operational
    floor, a step-down SimTask is generated and placed into still_todo.
    The orchestrator feeds still_todo back into the daily dispatch loop.

    Parameters
    ----------
    results : List[WorkerResult]
        Completed worker results for this batch.
    task_by_fid : Dict[str, SimTask]
        Mapping of SIM_FID → original SimTask for this batch, used to
        reconstruct step-down tasks with all original fields intact.
    step_size : float
        FL decrement per step-down iteration (feet).
    min_safe_fl : float
        Operational floor — no step-down is emitted below this FL.

    Returns
    -------
    EvalResult
        succeeded, failed, and still_todo (step-down SimTasks for next iteration).
    """
    succeeded  = [r for r in results if r.status == "success"]
    failed     = [r for r in results if r.status == "fail"]
    still_todo: List[SimTask] = []

    for result in succeeded:
        task = task_by_fid.get(result.sim_fid)
        if task is None:
            logger.warning(
                "Slot 5 (variational): no matching task for sim_fid=%s — skipping step-down.",
                result.sim_fid,
            )
            continue
        next_task = compute_stepdown_task(task, result.ef, step_size, min_safe_fl)
        if next_task is not None:
            still_todo.append(next_task)

    logger.info(
        "Slot 5 (variational): %d succeeded, %d failed → %d step-down task(s) re-queued.",
        len(succeeded), len(failed), len(still_todo),
    )
    return EvalResult(succeeded=succeeded, failed=failed, still_todo=still_todo)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_evaluator(
    sim_mode: str,
) -> Callable[..., EvalResult]:
    """Return the correct evaluator function for the given sim_mode flag.

    Parameters
    ----------
    sim_mode : str
        Execution mode: ``'standard'`` or ``'variational'``.

    Returns
    -------
    Callable
        Signature: ``(results, task_by_fid, step_size, min_safe_fl) -> EvalResult``.

    Raises
    ------
    ValueError
        If sim_mode is not ``'standard'`` or ``'variational'``.
    """
    _registry: dict = {
        "standard": _evaluate_standard,
        "variational": _evaluate_variational,
    }
    if sim_mode not in _registry:
        raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'standard' or 'variational'.")
    return _registry[sim_mode]


# ---------------------------------------------------------------------------
# Public entry point — the only symbol callers import
# ---------------------------------------------------------------------------

def evaluate(
    results: List[WorkerResult],
    task_by_fid: Dict[str, SimTask],
    sim_mode: str,
    step_size: float = 10.0,
    min_safe_fl: float = MIN_SAFE_FL,
) -> EvalResult:
    """Evaluate a completed batch of worker results and return a structured EvalResult.

    Parameters
    ----------
    results : List[WorkerResult]
        Results from one completed engine batch.
    task_by_fid : Dict[str, SimTask]
        Mapping of SIM_FID → SimTask for all tasks in this batch.
        Required by the variational evaluator to reconstruct step-down tasks.
        Pass an empty dict for standard mode (unused).
    sim_mode : str
        Execution mode string flag: ``'standard'`` or ``'variational'``.
    step_size : float, optional
        FL decrement per step-down (default 1000.0). Unused in standard mode.
    min_safe_fl : float, optional
        Minimum safe FL in feet (default 280.0). Unused in standard mode.

    Returns
    -------
    EvalResult
        succeeded, failed, and still_todo (empty for standard; step-down tasks for variational).
    """
    evaluator = get_evaluator(sim_mode)
    return evaluator(results, task_by_fid, step_size, min_safe_fl)
