"""
models/ps_cocip.py — Slot 4: Physics Model Factory

Instantiates and returns the PSFlight + Cocip model pair for a given
model_config_id string flag.

Architecture:
- _get_<variant>_params(): returns physics-only param dicts (no copy_source,
  no aircraft_performance). One function per model variant.
- get_model(): the public completion function. Accepts copy_source as an
  execution-path flag from worker.py, injects it into both model param dicts,
  and performs final instantiation.

Cocip is instantiated WITHOUT aircraft_performance. When PSFlight.eval() is
run first, all required performance columns are pre-computed in the Flight.
This has been empirically verified (bit-identical EF_total, 734 flights).

get_model() is the only public symbol.
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


# ---------------------------------------------------------------------------
# Private model builders
# ---------------------------------------------------------------------------

def _get_kerosene_params(
    max_age_hours: int,
) -> Tuple[dict, dict]:
    """Return (ps_params, cocip_params) for the standard kerosene configuration.

    These are physics-only parameter dicts. They contain no copy_source flag
    and no aircraft_performance reference. The caller (get_model) injects
    copy_source and performs final instantiation.
    """
    ps_params: dict = {
        "fill_low_altitude_with_isa_temperature": True,
        "fill_low_altitude_with_zero_wind": False,
        "correct_fuel_flow": False,
        "n_iter": 5,
        "downselect_met": False,
    }

    cocip_params: dict = {
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

    return ps_params, cocip_params


def _get_kerosene_lowmem_params(
    max_age_hours: int,
) -> Tuple[dict, dict]:
    """Return (ps_params, cocip_params) for low-memory CoCiP mode.

    Identical to _get_kerosene_params but sets preprocess_lowmem=True unconditionally.
    preprocess_lowmem reduces peak RAM during CoCiP met interpolation by processing
    in smaller chunks rather than materializing all pressure-level variables at once.
    """
    ps_params, cocip_params = _get_kerosene_params(max_age_hours)
    cocip_params["preprocess_lowmem"] = True
    return ps_params, cocip_params


_BUILDERS: dict = {
    "kerosene":        _get_kerosene_params,
    "kerosene_lowmem": _get_kerosene_lowmem_params,
}


def get_model(
    model_config_id: str,
    met: MetDataset,
    rad: MetDataset,
    max_age_hours: int,
    copy_source: bool = True,
) -> Tuple[PSFlight, Cocip]:
    """Return an initialised (PSFlight, Cocip) pair for the given model config.

    Parameters
    ----------
    model_config_id : str
        Fuel/model configuration identifier (e.g. ``'kerosene'``, ``'kerosene_lowmem'``).
    met : MetDataset
        Pressure-level meteorological dataset (ERA5).
    rad : MetDataset
        Surface-level radiation dataset (ERA5).
    max_age_hours : int
        Maximum contrail segment age in hours.
    copy_source : bool, optional
        Whether models should deep-copy their input Flight before eval
        (default True). Set False only for the sequential single-Flight
        path where the Flight object is discarded after writing to the lake.
        Must be True for Fleet/vectorized eval.

    Returns
    -------
    Tuple[PSFlight, Cocip]
        Initialised model pair ready for ``ps_model.eval()`` +
        ``cocip_model.eval()``.

    Raises
    ------
    NotImplementedError
        If ``model_config_id`` is not supported.

    Notes
    -----
    ``Cocip`` is instantiated without ``aircraft_performance``. When
    ``PSFlight.eval()`` is run first, all required performance columns
    (fuel flow, thrust, nvpm_ei_n) are already present in the Flight.
    Empirically verified: omitting ``aircraft_performance`` produces
    bit-identical EF_total results across 734 flights.

    **Expansion:** Add new ``_get_<variant>_params`` functions and wire
    them in here for SAF variants.
    """
    if model_config_id not in _BUILDERS:
        raise NotImplementedError(
            f"model_config_id='{model_config_id}' is not supported. "
            f"Supported: {list(_BUILDERS.keys())}."
        )
    ps_params, cocip_params = _BUILDERS[model_config_id](max_age_hours)
    ps_model = PSFlight(
        met=met,
        params={**ps_params, "copy_source": copy_source},
    )
    cocip_model = Cocip(
        met=met,
        rad=rad,
        params={**cocip_params, "copy_source": copy_source},
    )
    return ps_model, cocip_model
