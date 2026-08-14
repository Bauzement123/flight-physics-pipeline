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


def get_loader(
    sim_mode: str = "standard",
    fuel: str = "kerosene",
    cap_altitude: bool = False,
) -> Callable[[SimTask, Dict[Tuple[str, int], Any]], Optional[Flight]]:
    """Return a trajectory loader callable configured for fuel type and altitude capping.

    Parameters
    ----------
    sim_mode : str
        Execution mode: ``'standard'`` or ``'variational'``.
    fuel : str
        Fuel type: ``'kerosene'`` (default) or ``'hydrogen'``.
    cap_altitude : bool
        If True, clamp trajectory altitude to task.fl ceiling (default False).

    Returns
    -------
    Callable
        A pre-bound function with signature
        ``(task: SimTask, corridors_map: Dict[Tuple[str, int], Any]) -> Optional[Flight]``.
    """
    if sim_mode not in ("standard", "variational"):
        raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'standard' or 'variational'.")

    use_hydrogen = (fuel == "hydrogen")
    return partial(
        cluster_loader.load,
        use_hydrogen=use_hydrogen,
        cap_altitude=cap_altitude,
    )
