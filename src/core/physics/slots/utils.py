"""
slots/utils.py — Shared slot-level pure helper functions.

All functions here are pure (no I/O, no side effects) and may be imported
by any slot module. Utilities accumulate here rather than in scattered
single-function files.
"""

import dataclasses
from typing import Optional

from src.common.config import MIN_SAFE_FL
from src.data_manager.schemas import SimTask


def compute_stepdown_task(
    task: SimTask,
    ef: float,
    step_size: float = 10.0,
    min_safe_fl: float = MIN_SAFE_FL,
) -> Optional[SimTask]:
    """Return a new step-down SimTask if conditions are met, else None.

    Both Slot 2 (initial variational campaign setup) and Slot 5 (dynamic loop
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
        ``task.fl - step_size >= min_safe_fl``, otherwise ``None``.
    """
    if ef <= 0:
        return None                         # already suppressed or neutral

    next_fl = task.fl - step_size
    if next_fl < min_safe_fl:
        return None                         # at or below operational floor

    return dataclasses.replace(task, fl=next_fl)
