"""
slots/slot5_evaluator.py — Slot 5: Batch Result Evaluation

Classifies completed worker results into succeeded / failed / still_todo and
returns a structured EvalResult. The orchestrator logs from this object and
feeds still_todo back into the daily queue.

Execution Modes:
- O1 (Waterfall 1 Baseline):
  Partitions WorkerResult list by status. still_todo is always empty.
- O2 (Variational Optimization Pass):
  Reserved for second pass. Raises NotImplementedError.

Selection Pattern:
  The public entry point is evaluate(). Internally it calls get_evaluator(sim_mode)
  to dispatch to the correct implementation. Callers never reference evaluate_o1
  or evaluate_o2 directly — only the sim_mode string flag is passed down.
"""

import logging
from typing import Callable, List

from src.data_manager.schemas import EvalResult, SimTask, WorkerResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private evaluator implementations
# ---------------------------------------------------------------------------

def _evaluate_o1(
    results: List[WorkerResult],
    step_size: float,
    min_safe_fl: float,
) -> EvalResult:
    """Partition results into succeeded/failed; still_todo always empty for O1."""
    succeeded = [r for r in results if r.status == "success"]
    failed    = [r for r in results if r.status == "fail"]

    logger.info(
        "Slot 5 (O1): batch evaluated — %d succeeded, %d failed.",
        len(succeeded), len(failed),
    )
    return EvalResult(succeeded=succeeded, failed=failed, still_todo=[])


def _evaluate_o2(
    results: List[WorkerResult],
    step_size: float,
    min_safe_fl: float,
) -> EvalResult:
    """Stub O2 evaluator — generates step-down SimTasks for still_todo.

    # TODO: second pass — implement O2 variational evaluation.
    # Logic: for each succeeded result with EF > 0, call
    # compute_stepdown_task() from utils.py and add to still_todo.
    """
    raise NotImplementedError(
        "O2 evaluator is reserved for second pass. Run O1 first."
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_evaluator(
    sim_mode: str,
) -> Callable[[List[WorkerResult], float, float], EvalResult]:
    """Return the correct evaluator function for the given sim_mode flag.

    Parameters
    ----------
    sim_mode : str
        Execution mode: ``'O1'`` or ``'O2'``.

    Returns
    -------
    Callable
        A function with signature
        ``(results: List[WorkerResult], step_size: float, min_safe_fl: float) -> EvalResult``.

    Raises
    ------
    ValueError
        If sim_mode is not ``'O1'`` or ``'O2'``.
    """
    _registry: dict = {
        "O1": _evaluate_o1,
        "O2": _evaluate_o2,
    }
    if sim_mode not in _registry:
        raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'O1' or 'O2'.")
    return _registry[sim_mode]


# ---------------------------------------------------------------------------
# Public entry point — the only symbol callers import
# ---------------------------------------------------------------------------

def evaluate(
    results: List[WorkerResult],
    sim_mode: str,
    step_size: float = 1000.0,
    min_safe_fl: float = 280.0,
) -> EvalResult:
    """Evaluate a completed batch of worker results and return a structured EvalResult.

    Parameters
    ----------
    results : List[WorkerResult]
        Results from one completed engine batch.
    sim_mode : str
        Execution mode string flag: ``'O1'`` or ``'O2'``.
    step_size : float, optional
        FL decrement per step-down (default 1000.0). Unused in O1.
    min_safe_fl : float, optional
        Minimum safe FL in feet (default 280.0). Unused in O1.

    Returns
    -------
    EvalResult
        succeeded, failed, and still_todo (empty for O1; step-down tasks for O2).
    """
    evaluator = get_evaluator(sim_mode)
    return evaluator(results, step_size, min_safe_fl)
