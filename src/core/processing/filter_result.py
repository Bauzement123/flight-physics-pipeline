from __future__ import annotations
import dataclasses
import logging
import pandas as pd
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_PASS_FIELDS = [
    "velocity_pass",
    "coordinate_velocity_pass",
    "acceleration_pass",
    "distance_pass",
]

@dataclass
class FilterResult:
    flight_id: str
    file_path: str  # absolute path to _clean_si.parquet

    # velocity check
    velocity_pass: Optional[bool] = None
    velocity_reject_reason: Optional[str] = None

    # coordinate velocity check
    coordinate_velocity_pass: Optional[bool] = None
    coordinate_velocity_reject_reason: Optional[str] = None

    # acceleration check
    acceleration_pass: Optional[bool] = None
    acceleration_reject_reason: Optional[str] = None

    # distance check (airport proximity)
    distance_pass: Optional[bool] = None
    distance_reject_reason: Optional[str] = None

    def __post_init__(self) -> None:
        """Pre-check: sanitize filter pass fields at construction time."""
        for field in _PASS_FIELDS:
            val = getattr(self, field)
            if val is not True and val is not False:
                setattr(self, field, pd.NA)

    def as_dict(self) -> dict[str, object]:
        """Post-check: sanitize filter pass fields before export, then return flat dict."""
        for field in _PASS_FIELDS:
            val = getattr(self, field)
            if val is not True and val is not False and val is not pd.NA:
                logger.warning(
                    f"Flight {self.flight_id}: post-check detected non-boolean/non-NA "
                    f"value in field '{field}' ({val!r}). Sanitizing to pd.NA."
                )
                setattr(self, field, pd.NA)
        return dataclasses.asdict(self)
