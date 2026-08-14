"""
models/ps_cocip.py — Slot 4: Physics Model Factory

Instantiates and returns the PSFlight + Cocip model pair for a given
model_config_id string flag. First-pass implementation supports only
'kerosene'. SAF variants are reserved for second-pass expansion.

get_model() is the only public symbol. Callers (worker.py) never
reference _build_kerosene directly.

Extracted from: engine.py create_simulation_models() L72-116
"""

import logging
from typing import Tuple

import numpy as np
import pandas as pd
from pycontrails import MetDataset
from pycontrails.models.cocip import Cocip
from pycontrails.models.humidity_scaling import ConstantHumidityScaling
from pycontrails.models.ps_model import PSFlight

logger = logging.getLogger(__name__)


def get_model(
    model_config_id: str,
    met: MetDataset,
    rad: MetDataset,
    max_age_hours: int,
    low_mem: bool = False,
) -> Tuple[PSFlight, Cocip]:
    """Return an initialised (PSFlight, Cocip) pair for the given model config.

    Parameters
    ----------
    model_config_id : str
        Fuel/model configuration identifier. Currently only ``'kerosene'``
        is supported. All other values raise ``NotImplementedError``.
    met : MetDataset
        Pressure-level meteorological dataset (ERA5).
    rad : MetDataset
        Surface-level radiation dataset (ERA5).
    max_age_hours : int
        Maximum contrail segment age in hours.
    low_mem : bool, optional
        Enable low-memory CoCiP preprocessing (default False).

    Returns
    -------
    Tuple[PSFlight, Cocip]
        Initialised model pair ready for ``ps_model.eval()`` +
        ``cocip_model.eval()``.

    Raises
    ------
    NotImplementedError
        If ``model_config_id`` is not ``'kerosene'``.

    Notes
    -----
    **Second pass / Expansion:** SAF variants (``'saf20'``, ``'saf50'``,
    etc.) are pinned. Add new ``_build_<variant>`` functions and wire them
    into this factory when fuel-blend model configs are ready.
    """
    if model_config_id == "kerosene":
        return _build_kerosene(met, rad, max_age_hours, low_mem)

    # TODO: second pass — add SAF variant branches here.
    raise NotImplementedError(
        f"model_config_id='{model_config_id}' is not supported in first pass. "
        "Only 'kerosene' is available. SAF variants reserved for second pass."
    )


# ---------------------------------------------------------------------------
# Private model builders
# ---------------------------------------------------------------------------

def _build_kerosene(
    met: MetDataset,
    rad: MetDataset,
    max_age_hours: int,
    low_mem: bool,
) -> Tuple[PSFlight, Cocip]:
    """Instantiate PSFlight + Cocip for standard kerosene fuel.

    Extracted verbatim from engine.py create_simulation_models() L81-116.
    """
    ps_model = PSFlight(
        met=met,
        params={
            "fill_low_altitude_with_isa_temperature": True,
            "fill_low_altitude_with_zero_wind": False,
            "correct_fuel_flow": False,
            "n_iter": 5,
        },
    )

    cocip_params = {
        "process_emissions": True,
        "verbose_outputs": False,
        "humidity_scaling": ConstantHumidityScaling(rhi_adj=0.97),
        "max_age": pd.Timedelta(hours=max_age_hours),
        "dt_integration": np.timedelta64(30, "m"),
        "dz_m": 200.0,
        "effective_vertical_resolution": 2000.0,
        "filter_sac": True,
        "filter_initially_persistent": True,
        "min_altitude_m": 5000.0,
        "max_altitude_m": 13000.0,
        "max_seg_length_m": 40000.0,
    }

    if low_mem:
        cocip_params["preprocess_lowmem"] = True

    cocip_model = Cocip(
        met=met,
        rad=rad,
        params=cocip_params,
        aircraft_performance=ps_model,
    )

    return ps_model, cocip_model
