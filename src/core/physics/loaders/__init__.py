# src/core/physics/loaders/__init__.py
"""
loaders/ — Trajectory Loader Factory

get_loader(sim_mode) returns the correct load() callable for the given
execution mode. Callers (worker.py) only interact with this factory;
they never import cluster_loader or stepdown_loader directly.
"""

from typing import Callable, Dict, Optional, Tuple
from pathlib import Path

from src.core.physics.loaders import cluster_loader
from src.data_manager.schemas import SimTask
from pycontrails import Flight


def get_loader(sim_mode: str) -> Callable[[SimTask, Dict[Tuple[str, int], Path]], Optional[Flight]]:
    """Return the trajectory loader callable for the given sim_mode flag.

    Parameters
    ----------
    sim_mode : str
        Execution mode: ``'O1'`` or ``'O2'``.

    Returns
    -------
    Callable
        A function with signature
        ``(task: SimTask, corridors_map: Dict[Tuple[str, int], Path]) -> Optional[Flight]``.

    Raises
    ------
    ValueError
        If sim_mode is not ``'O1'`` or ``'O2'``.
    NotImplementedError
        If sim_mode is ``'O2'`` (reserved for second pass).
    """
    if sim_mode == "O1":
        return cluster_loader.load

    if sim_mode == "O2":
        # TODO: second pass — wire in stepdown_loader.load here.
        raise NotImplementedError(
            "O2 stepdown loader is reserved for second pass. Run O1 first."
        )

    raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'O1' or 'O2'.")
