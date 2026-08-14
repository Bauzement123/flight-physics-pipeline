# src/core/physics/loaders/__init__.py
"""
loaders/ — Trajectory Loader Factory

get_loader(sim_mode, fuel, cap_altitude) returns a pre-bound load() callable
configured for fuel type and altitude capping.
"""

from functools import partial
from typing import Any, Callable, Dict, Optional, Tuple
from pathlib import Path

from src.core.physics.loaders import cluster_loader
from src.data_manager.schemas import SimTask
from pycontrails import Flight


VALID_STEP_DOWN_METHODS = {"cap"}


def get_loader(
    sim_mode: str = "standard",
    fuel: str = "kerosene",
    step_down_method: Optional[str] = None,
) -> Callable[[SimTask, Dict[Tuple[str, int], Any]], Optional[Flight]]:
    """Return a trajectory loader callable configured for fuel type and step-down method.

    Parameters
    ----------
    sim_mode : str
        Execution mode: ``'standard'`` or ``'variational'``.
    fuel : str
        Fuel type: ``'kerosene'`` (default) or ``'hydrogen'``.
    step_down_method : str, optional
        Step-down altitude method for variational mode (e.g. ``'cap'``).
        Must be None for ``'standard'`` mode, and non-None for ``'variational'`` mode.

    Returns
    -------
    Callable
        A pre-bound function with signature
        ``(task: SimTask, corridors_map: Dict[Tuple[str, int], Any]) -> Optional[Flight]``.
    """
    if sim_mode not in ("standard", "variational"):
        raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'standard' or 'variational'.")

    # Slot 3 Guard: mutual exclusion of mode and step_down_method
    if sim_mode == "variational" and step_down_method is None:
        raise ValueError(
            "sim_mode='variational' requires a step_down_method. "
            "Pass --step-down-method cap (or another valid method) at the CLI."
        )
    if sim_mode == "standard" and step_down_method is not None:
        raise ValueError(
            f"step_down_method='{step_down_method}' is only valid with "
            "sim_mode='variational'. Do not pass --step-down-method for standard runs."
        )
    if step_down_method is not None and step_down_method not in VALID_STEP_DOWN_METHODS:
        raise ValueError(
            f"Unknown step_down_method '{step_down_method}'. "
            f"Valid methods: {sorted(VALID_STEP_DOWN_METHODS)}"
        )

    use_hydrogen = (fuel == "hydrogen")
    cap_altitude = (step_down_method == "cap")
    return partial(
        cluster_loader.load,
        use_hydrogen=use_hydrogen,
        cap_altitude=cap_altitude,
    )
