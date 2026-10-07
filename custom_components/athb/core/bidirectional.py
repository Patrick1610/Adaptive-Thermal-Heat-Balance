"""Continuous scalar setpoint selection, independent of device commands."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .contracts import ActuationDirection


@dataclass(frozen=True, slots=True)
class ScalarSelection:
    branch: Literal["heating", "neutral", "cooling"]
    requested_c: float
    reason: str
    indoor_c: float
    outdoor_c: float
    heating_c: float
    neutral_c: float
    cooling_c: float

    @property
    def policy_direction(self) -> ActuationDirection:
        return (
            ActuationDirection.HEATING_ONLY
            if self.branch == "heating"
            or (self.branch == "neutral" and self.indoor_c <= self.neutral_c)
            else ActuationDirection.COOLING_ONLY
        )


def select_scalar_target(
    *, indoor_c: float, outdoor_c: float, heating_c: float, neutral_c: float, cooling_c: float
) -> ScalarSelection | None:
    """Choose H/N/C using current outside air, with symmetric cabin exceptions."""

    values = (indoor_c, outdoor_c, heating_c, neutral_c, cooling_c)
    if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
        return None
    if not heating_c <= neutral_c <= cooling_c:
        return None
    branch: Literal["heating", "neutral", "cooling"]
    if outdoor_c < heating_c and indoor_c > cooling_c:
        branch, requested, reason = "neutral", neutral_c, "cold_outside_hot_inside"
    elif outdoor_c > cooling_c and indoor_c < heating_c:
        branch, requested, reason = "neutral", neutral_c, "hot_outside_cold_inside"
    elif outdoor_c < neutral_c:
        branch, requested, reason = "heating", heating_c, "outside_below_neutral"
    elif outdoor_c > neutral_c:
        branch, requested, reason = "cooling", cooling_c, "outside_above_neutral"
    else:
        branch, requested, reason = "neutral", neutral_c, "outside_at_neutral"
    return ScalarSelection(branch, requested, reason, *values)
