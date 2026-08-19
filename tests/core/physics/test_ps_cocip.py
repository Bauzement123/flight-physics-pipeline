"""Unit tests for ps_cocip.get_model factory."""
from unittest.mock import MagicMock
import pytest
from src.core.physics.models.ps_cocip import get_model
from pycontrails import MetDataset
from pycontrails.models.ps_model import PSFlight
from pycontrails.models.cocip import Cocip


def _mock_met():
    met = MagicMock(spec=MetDataset)
    met.data = {"tau_cirrus": MagicMock(), "air_temperature": MagicMock()}
    return met


def test_kerosene_builds_without_error():
    """get_model('kerosene') returns a (PSFlight, Cocip) pair."""
    ps, cocip = get_model("kerosene", _mock_met(), _mock_met(), max_age_hours=48)
    assert isinstance(ps, PSFlight)
    assert isinstance(cocip, Cocip)


def test_kerosene_lowmem_sets_preprocess_flag():
    """get_model('kerosene_lowmem') includes preprocess_lowmem=True in Cocip params."""
    ps, cocip = get_model("kerosene_lowmem", _mock_met(), _mock_met(), max_age_hours=48)
    assert isinstance(ps, PSFlight)
    assert isinstance(cocip, Cocip)
    assert cocip.params.get("preprocess_lowmem") is True


def test_unknown_model_config_id_raises():
    """get_model with unsupported model_config_id raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        get_model("saf_blend", _mock_met(), _mock_met(), max_age_hours=48)
