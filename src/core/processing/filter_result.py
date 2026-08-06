from __future__ import annotations
import dataclasses
import logging
import pandas as pd
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_METRIC_FIELDS = [
    "metric_max_horiz_speed_kt",
    "metric_max_vert_speed_fpm",
    "metric_max_coord_horiz_speed_kt",
    "metric_max_coord_vert_speed_fpm",
    "metric_max_acceleration_mps2",
    "metric_dep_horiz_dist_m",
    "metric_dep_vert_dist_m",
    "metric_arr_horiz_dist_m",
    "metric_arr_vert_dist_m",
]

@dataclass
class FilterResult:
    flight_id: str
    file_path: str  # absolute path to _clean_si.parquet

    # Scalar feature metrics extracted from trajectory
    metric_max_horiz_speed_kt: Optional[float] = None
    metric_max_vert_speed_fpm: Optional[float] = None
    metric_max_coord_horiz_speed_kt: Optional[float] = None
    metric_max_coord_vert_speed_fpm: Optional[float] = None
    metric_max_acceleration_mps2: Optional[float] = None
    metric_dep_horiz_dist_m: Optional[float] = None
    metric_dep_vert_dist_m: Optional[float] = None
    metric_arr_horiz_dist_m: Optional[float] = None
    metric_arr_vert_dist_m: Optional[float] = None

    def __post_init__(self) -> None:
        """Pre-check: sanitize metric fields at construction time."""
        for field in _METRIC_FIELDS:
            val = getattr(self, field)
            if val is not None and pd.isna(val):
                setattr(self, field, pd.NA)
            elif val is not None:
                try:
                    setattr(self, field, float(val))
                except (ValueError, TypeError):
                    setattr(self, field, pd.NA)

    def as_dict(self) -> dict[str, object]:
        """Post-check: sanitize metrics before export, then return flat dict."""
        for field in _METRIC_FIELDS:
            val = getattr(self, field)
            if val is not None and pd.isna(val):
                continue
            if val is not None and not isinstance(val, float):
                logger.warning(
                    f"Flight {self.flight_id}: post-check detected non-float/non-NA "
                    f"value in field '{field}' ({val!r}). Sanitizing to pd.NA."
                )
                setattr(self, field, pd.NA)
        return dataclasses.asdict(self)
