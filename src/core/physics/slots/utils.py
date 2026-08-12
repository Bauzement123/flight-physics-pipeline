"""
slots/utils.py — Shared slot-level pure helper functions.

All functions here are pure (no I/O, no side effects) and may be imported
by any slot module. Utilities accumulate here rather than in scattered
single-function files.
"""

from typing import Optional

from src.data_manager.schemas import SimTask


def compute_stepdown_task(
    task: SimTask,
    ef: float,
    step_size: float,
    min_safe_fl: float,
) -> Optional[SimTask]:
    """Return a new step-down SimTask if conditions are met, else None.

    Both Slot 2 (initial O2 campaign setup) and Slot 5 (dynamic loop
    injection) call this function. It is the single source of truth for
    the step-down decision.

    Parameters
    ----------
    task : SimTask
        The completed or candidate task whose FL we want to reduce.
    ef : float
        Environmental Forcing result (J) for this task. Positive means
        contrail warming — a step-down should be attempted.
    step_size : float
        FL reduction per step-down iteration (in feet).
    min_safe_fl : float
        Minimum FL below which no further step-down is attempted.

    Returns
    -------
    Optional[SimTask]
        New SimTask at ``task.fl - step_size`` if ``ef > 0`` and
        ``task.fl > min_safe_fl``, otherwise ``None``.

    # TODO: second pass — implement variational logic here.
    # This stub is a placeholder so both Slot 2 and Slot 5 can import
    # a real callable. The actual decision logic will be completed once
    # the O1 (Waterfall 1) baseline run is verified end-to-end.
    """
    raise NotImplementedError(
        "compute_stepdown_task is reserved for the variational (O2) second pass. "
        "Do not call this function in O1 (Waterfall 1) runs."
    )
