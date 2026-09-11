"""Pure constant-vapor-pressure psychrometric transformations.

The saturation equations are the SI ASHRAE formulations used by PsychroLib.
This module intentionally remains separate from the frozen ATHB heat-balance
kernel, whose historical vapor-pressure approximation is part of its versioned
numerical identity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    DeclaredRelativeHumidity,
    DewPointStatus,
    MeasuredRelativeHumidity,
    MoistureFailureCode,
    Provenance,
)

MIN_PSYCHROMETRIC_TEMPERATURE_C = -100.0
MAX_PSYCHROMETRIC_TEMPERATURE_C = 200.0
TRIPLE_POINT_WATER_C = 0.01
SATURATION_OVERSHOOT_TOLERANCE_PCT = 1e-6
DEW_POINT_TOLERANCE_C = 0.001
MAX_DEW_POINT_ITERATIONS = 64


@dataclass(frozen=True, slots=True)
class MoistureFailure:
    """A physical moisture failure with no substituted value."""

    code: MoistureFailureCode
    field: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class MoistureState:
    """Starting moisture state preserved throughout an inverse calculation."""

    starting_air_temperature_c: float
    starting_relative_humidity_pct: float
    vapor_pressure_pa: float
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class CandidateHumidity:
    """Candidate RH derived without changing the starting vapor pressure."""

    relative_humidity_pct: float
    vapor_pressure_pa: float
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class DewPointResult:
    """Dew/frost point or the explicit zero-moisture dry limit."""

    temperature_c: float | None
    status: DewPointStatus


@dataclass(frozen=True, slots=True)
class HumidityRatio:
    """Optional diagnostic humidity ratio."""

    kg_water_per_kg_dry_air: float
    provenance: Provenance


type SaturationPressureResult = float | MoistureFailure
type MoistureStateResult = MoistureState | MoistureFailure
type CandidateHumidityResult = CandidateHumidity | MoistureFailure
type DewPointCalculationResult = DewPointResult | MoistureFailure
type HumidityRatioResult = HumidityRatio | MoistureFailure


def _number(value: object, field: str) -> float | MoistureFailure:
    if isinstance(value, bool):
        return MoistureFailure(MoistureFailureCode.BOOLEAN_INPUT, field, f"{field} is Boolean")
    if not isinstance(value, (int, float)):
        return MoistureFailure(MoistureFailureCode.NON_NUMERIC, field, f"{field} must be numeric")
    try:
        numeric = float(value)
    except OverflowError:
        return MoistureFailure(MoistureFailureCode.NON_FINITE, field, f"{field} must be finite")
    if not math.isfinite(numeric):
        return MoistureFailure(MoistureFailureCode.NON_FINITE, field, f"{field} must be finite")
    return numeric


def saturation_vapor_pressure_pa(temperature_c: float) -> SaturationPressureResult:
    """Return saturation vapor pressure in Pa across the water/ice branches."""

    temperature = _number(temperature_c, "temperature_c")
    if isinstance(temperature, MoistureFailure):
        return temperature
    if not MIN_PSYCHROMETRIC_TEMPERATURE_C <= temperature <= MAX_PSYCHROMETRIC_TEMPERATURE_C:
        return MoistureFailure(
            MoistureFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN,
            "temperature_c",
            f"temperature_c must be within [{MIN_PSYCHROMETRIC_TEMPERATURE_C}, "
            f"{MAX_PSYCHROMETRIC_TEMPERATURE_C}] degrees Celsius",
        )

    kelvin = temperature + 273.15
    if temperature > TRIPLE_POINT_WATER_C:
        log_pressure = (
            -5800.2206 / kelvin
            + 1.3914993
            - 0.048640239 * kelvin
            + 4.1764768e-5 * kelvin**2
            - 1.4452093e-8 * kelvin**3
            + 6.5459673 * math.log(kelvin)
        )
    else:
        log_pressure = (
            -5674.5359 / kelvin
            + 6.3925247
            - 0.009677843 * kelvin
            + 6.2215701e-7 * kelvin**2
            + 2.0747825e-9 * kelvin**3
            - 9.484024e-13 * kelvin**4
            + 4.1635019 * math.log(kelvin)
        )
    try:
        pressure = math.exp(log_pressure)
    except OverflowError:
        return MoistureFailure(
            MoistureFailureCode.NON_FINITE, "temperature_c", "saturation pressure overflowed"
        )
    if not math.isfinite(pressure) or pressure <= 0.0:
        return MoistureFailure(
            MoistureFailureCode.NON_FINITE,
            "temperature_c",
            "saturation pressure was not finite and positive",
        )
    return pressure


def moisture_state(
    air_temperature_c: float,
    relative_humidity: object,
) -> MoistureStateResult:
    """Build the one starting moisture state for measured or declared RH."""

    temperature = _number(air_temperature_c, "air_temperature_c")
    if isinstance(temperature, MoistureFailure):
        return temperature
    if not isinstance(relative_humidity, (MeasuredRelativeHumidity, DeclaredRelativeHumidity)):
        return MoistureFailure(
            MoistureFailureCode.NON_NUMERIC,
            "relative_humidity",
            "relative humidity requires an explicit measured or declared source tag",
        )
    rh = _number(relative_humidity.value_pct, "relative_humidity_pct")
    if isinstance(rh, MoistureFailure):
        return rh
    if not 0.0 <= rh <= 100.0:
        return MoistureFailure(
            MoistureFailureCode.INVALID_RELATIVE_HUMIDITY,
            "relative_humidity_pct",
            "relative humidity must be within [0, 100] percent",
        )
    saturation = saturation_vapor_pressure_pa(temperature)
    if isinstance(saturation, MoistureFailure):
        return saturation
    vapor_pressure = rh / 100.0 * saturation
    return MoistureState(temperature, rh, vapor_pressure, relative_humidity.provenance)


def candidate_relative_humidity(
    state: MoistureState,
    candidate_air_temperature_c: float,
) -> CandidateHumidityResult:
    """Transform RH at constant vapor pressure, rejecting material saturation."""

    vapor_pressure = _number(state.vapor_pressure_pa, "vapor_pressure_pa")
    if isinstance(vapor_pressure, MoistureFailure):
        return vapor_pressure
    if vapor_pressure < 0.0:
        return MoistureFailure(
            MoistureFailureCode.INVALID_RELATIVE_HUMIDITY,
            "vapor_pressure_pa",
            "vapor pressure must not be negative",
        )
    saturation = saturation_vapor_pressure_pa(candidate_air_temperature_c)
    if isinstance(saturation, MoistureFailure):
        return saturation
    candidate = 100.0 * vapor_pressure / saturation
    if not math.isfinite(candidate):
        return MoistureFailure(
            MoistureFailureCode.NON_FINITE,
            "candidate_relative_humidity_pct",
            "candidate relative humidity was not finite",
        )
    if candidate > 100.0 + SATURATION_OVERSHOOT_TOLERANCE_PCT:
        return MoistureFailure(
            MoistureFailureCode.MOISTURE_LIMITED_NO_SOLUTION,
            "candidate_air_temperature_c",
            "candidate temperature crosses the constant-moisture saturation boundary",
        )
    if candidate > 100.0:
        candidate = 100.0
    return CandidateHumidity(candidate, vapor_pressure, state.provenance)


def dew_or_frost_point(state: MoistureState) -> DewPointCalculationResult:
    """Invert the same saturation equation below the current dry-bulb value."""

    vapor_pressure = _number(state.vapor_pressure_pa, "vapor_pressure_pa")
    if isinstance(vapor_pressure, MoistureFailure):
        return vapor_pressure
    if vapor_pressure < 0.0:
        return MoistureFailure(
            MoistureFailureCode.INVALID_RELATIVE_HUMIDITY,
            "vapor_pressure_pa",
            "vapor pressure must not be negative",
        )
    if vapor_pressure == 0.0:
        return DewPointResult(None, DewPointStatus.DRY_LIMIT)
    low_pressure = saturation_vapor_pressure_pa(MIN_PSYCHROMETRIC_TEMPERATURE_C)
    if isinstance(low_pressure, MoistureFailure):  # defensive, constant is in-domain
        return low_pressure
    if vapor_pressure < low_pressure:
        return MoistureFailure(
            MoistureFailureCode.BELOW_PSYCHROMETRIC_DOMAIN,
            "vapor_pressure_pa",
            "dew/frost point is below -100 degrees Celsius",
        )
    high_value = _number(state.starting_air_temperature_c, "starting_air_temperature_c")
    if isinstance(high_value, MoistureFailure):
        return high_value
    high = high_value
    high_pressure = saturation_vapor_pressure_pa(high)
    if isinstance(high_pressure, MoistureFailure):
        return high_pressure
    if vapor_pressure > high_pressure * (1.0 + SATURATION_OVERSHOOT_TOLERANCE_PCT / 100.0):
        return MoistureFailure(
            MoistureFailureCode.MOISTURE_LIMITED_NO_SOLUTION,
            "vapor_pressure_pa",
            "starting state is supersaturated",
        )

    low = MIN_PSYCHROMETRIC_TEMPERATURE_C
    for _ in range(MAX_DEW_POINT_ITERATIONS):
        if high - low <= DEW_POINT_TOLERANCE_C:
            return DewPointResult((low + high) / 2.0, DewPointStatus.SOLVED)
        midpoint = (low + high) / 2.0
        midpoint_pressure = saturation_vapor_pressure_pa(midpoint)
        if isinstance(midpoint_pressure, MoistureFailure):
            return midpoint_pressure
        if midpoint_pressure < vapor_pressure:
            low = midpoint
        else:
            high = midpoint
    return DewPointResult((low + high) / 2.0, DewPointStatus.SOLVED)


def humidity_ratio(
    state: MoistureState,
    total_pressure_pa: float,
) -> HumidityRatioResult:
    """Return optional humidity-ratio diagnostics without inventing pressure."""

    pressure = _number(total_pressure_pa, "total_pressure_pa")
    if isinstance(pressure, MoistureFailure):
        return pressure
    vapor_pressure = _number(state.vapor_pressure_pa, "vapor_pressure_pa")
    if isinstance(vapor_pressure, MoistureFailure):
        return vapor_pressure
    if vapor_pressure < 0.0 or pressure <= vapor_pressure:
        return MoistureFailure(
            MoistureFailureCode.INVALID_TOTAL_PRESSURE,
            "total_pressure_pa",
            "total pressure must be greater than vapor pressure",
        )
    ratio = 0.621945 * vapor_pressure / (pressure - vapor_pressure)
    if not math.isfinite(ratio) or ratio < 0.0:
        return MoistureFailure(
            MoistureFailureCode.INVALID_TOTAL_PRESSURE,
            "total_pressure_pa",
            "humidity ratio could not be calculated",
        )
    return HumidityRatio(ratio, state.provenance)
