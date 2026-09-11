"""Building-specific steady-state surface estimates and diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .contracts import ModelledSurfaceTemperature, Provenance
from .psychrometrics import (
    MAX_PSYCHROMETRIC_TEMPERATURE_C,
    MIN_PSYCHROMETRIC_TEMPERATURE_C,
    MoistureFailure,
    MoistureState,
    saturation_vapor_pressure_pa,
)

MIN_CALIBRATION_DELTA_K = 5.0
DEFAULT_HIGH_SURFACE_RH_THRESHOLD_PCT = 80.0
SURFACE_INVERSE_TOLERANCE_C = 0.001
SURFACE_INVERSE_ITERATIONS = 64


class SurfaceFailureCode(StrEnum):
    """Typed failures for surface calibration and diagnostics."""

    INVALID_INPUT = "invalid_input"
    INSUFFICIENT_CALIBRATION_DELTA = "insufficient_calibration_delta"
    SURFACE_OUTSIDE_CALIBRATION_RANGE = "surface_outside_calibration_range"
    INVALID_CALIBRATION_FACTOR = "invalid_calibration_factor"
    INVALID_THRESHOLD = "invalid_threshold"
    OUTSIDE_PSYCHROMETRIC_DOMAIN = "outside_psychrometric_domain"


@dataclass(frozen=True, slots=True)
class SurfaceFailure:
    code: SurfaceFailureCode
    field: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class SurfaceCalibration:
    f_rsi: float


@dataclass(frozen=True, slots=True)
class SurfaceEstimate:
    temperature: ModelledSurfaceTemperature
    f_rsi: float
    reasons: tuple[str, ...] = ("steady_state_surface_estimate",)


@dataclass(frozen=True, slots=True)
class SurfaceHumidityDiagnostic:
    displayed_relative_humidity_pct: float
    uncapped_relative_humidity_pct: float
    high_surface_humidity: bool
    predicted_saturation: bool
    threshold_pct: float
    provenance: Provenance
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SurfaceThresholdTemperature:
    temperature_c: float
    threshold_pct: float
    provenance: Provenance


type SurfaceCalibrationResult = SurfaceCalibration | SurfaceFailure
type SurfaceEstimateResult = SurfaceEstimate | SurfaceFailure
type SurfaceHumidityResult = SurfaceHumidityDiagnostic | SurfaceFailure
type SurfaceThresholdResult = SurfaceThresholdTemperature | SurfaceFailure


def _number(value: object, field: str) -> float | SurfaceFailure:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return SurfaceFailure(SurfaceFailureCode.INVALID_INPUT, field, f"{field} must be numeric")
    try:
        result = float(value)
    except OverflowError:
        return SurfaceFailure(SurfaceFailureCode.INVALID_INPUT, field, f"{field} must be finite")
    if not math.isfinite(result):
        return SurfaceFailure(SurfaceFailureCode.INVALID_INPUT, field, f"{field} must be finite")
    return result


def calibrate_surface_factor(
    *,
    indoor_temperature_c: float,
    outdoor_temperature_c: float,
    measured_surface_temperature_c: float,
) -> SurfaceCalibrationResult:
    """Calculate f_Rsi only from a valid user-entered calibration measurement."""

    indoor = _number(indoor_temperature_c, "indoor_temperature_c")
    if isinstance(indoor, SurfaceFailure):
        return indoor
    outdoor = _number(outdoor_temperature_c, "outdoor_temperature_c")
    if isinstance(outdoor, SurfaceFailure):
        return outdoor
    surface = _number(measured_surface_temperature_c, "measured_surface_temperature_c")
    if isinstance(surface, SurfaceFailure):
        return surface
    difference = indoor - outdoor
    if abs(difference) < MIN_CALIBRATION_DELTA_K:
        return SurfaceFailure(
            SurfaceFailureCode.INSUFFICIENT_CALIBRATION_DELTA,
            None,
            "indoor/outdoor calibration difference must be at least 5 K",
        )
    if not min(indoor, outdoor) <= surface <= max(indoor, outdoor):
        return SurfaceFailure(
            SurfaceFailureCode.SURFACE_OUTSIDE_CALIBRATION_RANGE,
            "measured_surface_temperature_c",
            "surface calibration temperature must lie between indoor and outdoor",
        )
    factor = (surface - outdoor) / difference
    if not 0.0 < factor <= 1.0:
        return SurfaceFailure(
            SurfaceFailureCode.INVALID_CALIBRATION_FACTOR,
            "f_rsi",
            "calculated f_Rsi must satisfy 0 < f_Rsi <= 1",
        )
    return SurfaceCalibration(factor)


def estimate_surface_temperature(
    *,
    indoor_temperature_c: float,
    current_outdoor_temperature_c: float,
    f_rsi: float,
) -> SurfaceEstimateResult:
    """Estimate one current surface from current outdoor temperature, never running mean."""

    indoor = _number(indoor_temperature_c, "indoor_temperature_c")
    if isinstance(indoor, SurfaceFailure):
        return indoor
    outdoor = _number(current_outdoor_temperature_c, "current_outdoor_temperature_c")
    if isinstance(outdoor, SurfaceFailure):
        return outdoor
    factor = _number(f_rsi, "f_rsi")
    if isinstance(factor, SurfaceFailure):
        return factor
    if not 0.0 < factor <= 1.0:
        return SurfaceFailure(
            SurfaceFailureCode.INVALID_CALIBRATION_FACTOR,
            "f_rsi",
            "f_Rsi must satisfy 0 < f_Rsi <= 1",
        )
    temperature = outdoor + factor * (indoor - outdoor)
    if not math.isfinite(temperature):
        return SurfaceFailure(
            SurfaceFailureCode.INVALID_INPUT,
            None,
            "surface estimate was not finite",
        )
    return SurfaceEstimate(ModelledSurfaceTemperature(temperature), factor)


def surface_humidity_diagnostic(
    *,
    moisture: MoistureState,
    surface_temperature_c: float,
    high_threshold_pct: float = DEFAULT_HIGH_SURFACE_RH_THRESHOLD_PCT,
) -> SurfaceHumidityResult:
    """Calculate finite uncapped surface RH while capping only the display value."""

    threshold = _number(high_threshold_pct, "high_threshold_pct")
    if isinstance(threshold, SurfaceFailure):
        return threshold
    if not 0.0 < threshold <= 100.0:
        return SurfaceFailure(
            SurfaceFailureCode.INVALID_THRESHOLD,
            "high_threshold_pct",
            "surface RH threshold must satisfy 0 < threshold <= 100",
        )
    saturation = saturation_vapor_pressure_pa(surface_temperature_c)
    if isinstance(saturation, MoistureFailure):
        return SurfaceFailure(
            SurfaceFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN,
            saturation.field,
            saturation.detail,
        )
    vapor_pressure = _number(moisture.vapor_pressure_pa, "vapor_pressure_pa")
    if isinstance(vapor_pressure, SurfaceFailure):
        return vapor_pressure
    if vapor_pressure < 0.0:
        return SurfaceFailure(
            SurfaceFailureCode.INVALID_INPUT,
            "vapor_pressure_pa",
            "vapor pressure must not be negative",
        )
    uncapped = 100.0 * vapor_pressure / saturation
    if not math.isfinite(uncapped):
        return SurfaceFailure(
            SurfaceFailureCode.INVALID_INPUT,
            None,
            "surface relative humidity was not finite",
        )
    saturation_flag = uncapped >= 100.0
    reasons = ["surface_humidity_diagnostic_only"]
    if uncapped >= threshold:
        reasons.append("high_surface_humidity")
    if saturation_flag:
        reasons.append("predicted_surface_saturation")
    return SurfaceHumidityDiagnostic(
        min(100.0, uncapped),
        uncapped,
        uncapped >= threshold,
        saturation_flag,
        threshold,
        moisture.provenance,
        tuple(reasons),
    )


def surface_temperature_for_rh_threshold(
    *,
    moisture: MoistureState,
    threshold_pct: float,
) -> SurfaceThresholdResult:
    """Invert the same saturation equation for a selected surface-RH threshold."""

    threshold = _number(threshold_pct, "threshold_pct")
    if isinstance(threshold, SurfaceFailure):
        return threshold
    if not 0.0 < threshold <= 100.0:
        return SurfaceFailure(
            SurfaceFailureCode.INVALID_THRESHOLD,
            "threshold_pct",
            "surface RH threshold must satisfy 0 < threshold <= 100",
        )
    vapor_pressure = _number(moisture.vapor_pressure_pa, "vapor_pressure_pa")
    if isinstance(vapor_pressure, SurfaceFailure):
        return vapor_pressure
    if vapor_pressure <= 0.0:
        return SurfaceFailure(
            SurfaceFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN,
            "vapor_pressure_pa",
            "zero moisture has no finite surface threshold temperature",
        )
    target_saturation = vapor_pressure * 100.0 / threshold
    low = MIN_PSYCHROMETRIC_TEMPERATURE_C
    high = MAX_PSYCHROMETRIC_TEMPERATURE_C
    low_pressure = saturation_vapor_pressure_pa(low)
    high_pressure = saturation_vapor_pressure_pa(high)
    if (
        isinstance(low_pressure, MoistureFailure)
        or isinstance(high_pressure, MoistureFailure)
        or target_saturation < low_pressure
        or target_saturation > high_pressure
    ):
        return SurfaceFailure(
            SurfaceFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN,
            "threshold_pct",
            "surface threshold temperature lies outside the psychrometric domain",
        )
    for _ in range(SURFACE_INVERSE_ITERATIONS):
        if high - low <= SURFACE_INVERSE_TOLERANCE_C:
            break
        midpoint = (low + high) / 2.0
        pressure = saturation_vapor_pressure_pa(midpoint)
        if isinstance(pressure, MoistureFailure):
            return SurfaceFailure(
                SurfaceFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN,
                pressure.field,
                pressure.detail,
            )
        if pressure < target_saturation:
            low = midpoint
        else:
            high = midpoint
    return SurfaceThresholdTemperature((low + high) / 2.0, threshold, moisture.provenance)
