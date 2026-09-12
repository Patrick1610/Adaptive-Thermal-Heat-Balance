"""Pure climate capability, unit, and inward-grid compatibility contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .contracts import ActuationDirection, TargetShape

TARGET_TEMPERATURE = 1
TARGET_TEMPERATURE_RANGE = 2
GRID_TOLERANCE_HA = 1e-9
MAX_GRID_POINTS = 2_000


class TemperatureUnit(StrEnum):
    """Temperature units accepted by the Home Assistant service boundary."""

    CELSIUS = "°C"
    FAHRENHEIT = "°F"


class StepUnit(StrEnum):
    """Declared unit for a configured grid-step override."""

    HA = "ha_unit"
    CELSIUS = "°C"
    FAHRENHEIT = "°F"


class AutoMapping(StrEnum):
    """Explicit temperature semantics for an adjustable HA auto mode."""

    UNMAPPED = "unmapped"
    HEATING = "heating"
    COOLING = "cooling"
    RANGE = "range"
    BIDIRECTIONAL_SCALAR = "bidirectional_scalar"


@dataclass(frozen=True, slots=True)
class ClimateCapabilitySnapshot:
    """Public climate state needed by ATHB without private entity access."""

    hvac_mode: str | None
    advertised_hvac_modes: tuple[str, ...]
    supported_features: int
    min_temp_ha: float | None
    max_temp_ha: float | None
    target_temp_step_ha: float | None
    temperature_unit: TemperatureUnit
    scalar_target_ha: float | None = None
    target_temp_low_ha: float | None = None
    target_temp_high_ha: float | None = None
    preset_mode: str | None = None
    available: bool = True
    restored: bool = False
    setpoint_feedback_observable: bool = True


@dataclass(frozen=True, slots=True)
class CapabilityMapping:
    """Supported target shape and direction for the current HVAC mode."""

    direction: ActuationDirection
    shape: TargetShape


@dataclass(frozen=True, slots=True)
class ClimateFailure:
    """Typed suppression or configuration failure at the climate boundary."""

    reason: str
    detail: str = ""


type CapabilityResult = CapabilityMapping | ClimateFailure


@dataclass(frozen=True, slots=True)
class GridOptions:
    """Per-actuator grid configuration in public HA coordinates."""

    user_min_c: float
    user_max_c: float
    calibration_offset_c: float = 0.0
    step_override: float | None = None
    step_unit: StepUnit = StepUnit.HA
    grid_origin_override_ha: float | None = None
    minimum_range_gap_c: float = 1.0


@dataclass(frozen=True, slots=True)
class GridDefinition:
    """Finite canonical target grid after intersecting all bounds."""

    origin_ha: float
    step_ha: float
    lower_index: int
    upper_index: int
    lower_bound_ha: float
    upper_bound_ha: float
    assumed_step: bool

    @property
    def point_count(self) -> int:
        return self.upper_index - self.lower_index + 1


@dataclass(frozen=True, slots=True)
class NormalizedScalarTarget:
    """One directionally normalized target with both coordinate systems."""

    direction: ActuationDirection
    requested_room_c: float
    calibrated_c: float
    bounded_c: float
    normalized_ha: float
    normalized_actuator_c: float
    normalized_room_c: float
    limitations: tuple[str, ...]
    grid: GridDefinition


@dataclass(frozen=True, slots=True)
class NormalizedRangeTarget:
    """Atomic inward-normalized range."""

    requested_heating_room_c: float
    requested_cooling_room_c: float
    heating: NormalizedScalarTarget
    cooling: NormalizedScalarTarget
    limitations: tuple[str, ...]


type NormalizedTarget = NormalizedScalarTarget | NormalizedRangeTarget | ClimateFailure


def celsius_to_ha(value_c: float, unit: TemperatureUnit) -> float:
    """Convert an absolute Celsius temperature to the configured HA unit."""

    return value_c if unit is TemperatureUnit.CELSIUS else value_c * 9.0 / 5.0 + 32.0


def ha_to_celsius(value_ha: float, unit: TemperatureUnit) -> float:
    """Convert an absolute HA temperature to Celsius."""

    return value_ha if unit is TemperatureUnit.CELSIUS else (value_ha - 32.0) * 5.0 / 9.0


def celsius_delta_to_unit(value_c: float, unit: TemperatureUnit) -> float:
    """Convert a temperature difference without applying an absolute offset."""

    return value_c if unit is TemperatureUnit.CELSIUS else value_c * 9.0 / 5.0


def unit_delta_to_ha(value: float, *, declared_unit: StepUnit, ha_unit: TemperatureUnit) -> float:
    """Convert a configured step difference into the HA service unit."""

    if declared_unit is StepUnit.HA:
        return value
    value_c = value if declared_unit is StepUnit.CELSIUS else value * 5.0 / 9.0
    return celsius_delta_to_unit(value_c, ha_unit)


def resolve_capability(
    snapshot: ClimateCapabilitySnapshot,
    *,
    auto_mapping: AutoMapping = AutoMapping.UNMAPPED,
) -> CapabilityResult:
    """Apply the complete current-mode capability matrix."""

    if not snapshot.available or snapshot.hvac_mode in {None, "unknown", "unavailable"}:
        return ClimateFailure("target_unavailable")
    if snapshot.restored:
        return ClimateFailure("restored_target_state")
    if not snapshot.setpoint_feedback_observable:
        return ClimateFailure("setpoint_feedback_unavailable")
    mode = snapshot.hvac_mode
    scalar = bool(snapshot.supported_features & TARGET_TEMPERATURE)
    ranged = bool(snapshot.supported_features & TARGET_TEMPERATURE_RANGE)
    if mode == "off":
        return ClimateFailure("hvac_off")
    if mode == "heat":
        return (
            CapabilityMapping(ActuationDirection.HEATING_ONLY, TargetShape.SCALAR)
            if scalar
            else ClimateFailure("unsupported_hvac_mode", "heat requires scalar target support")
        )
    if mode == "cool":
        return (
            CapabilityMapping(ActuationDirection.COOLING_ONLY, TargetShape.SCALAR)
            if scalar
            else ClimateFailure("unsupported_hvac_mode", "cool requires scalar target support")
        )
    if mode == "heat_cool":
        return (
            CapabilityMapping(ActuationDirection.RANGED, TargetShape.RANGE)
            if ranged
            else ClimateFailure("unsupported_hvac_mode", "heat_cool requires range support")
        )
    if mode == "auto":
        if auto_mapping is AutoMapping.UNMAPPED:
            auto_mapping = infer_auto_mapping(snapshot)
        if auto_mapping is AutoMapping.HEATING and scalar:
            return CapabilityMapping(ActuationDirection.HEATING_ONLY, TargetShape.SCALAR)
        if auto_mapping is AutoMapping.COOLING and scalar:
            return CapabilityMapping(ActuationDirection.COOLING_ONLY, TargetShape.SCALAR)
        if auto_mapping is AutoMapping.RANGE and ranged:
            return CapabilityMapping(ActuationDirection.RANGED, TargetShape.RANGE)
        return ClimateFailure("unsupported_auto_mapping")
    return ClimateFailure("unsupported_hvac_mode")


def infer_auto_mapping(snapshot: ClimateCapabilitySnapshot) -> AutoMapping:
    """Infer only unambiguous Auto target semantics from public capabilities."""

    if snapshot.hvac_mode != "auto":
        return AutoMapping.UNMAPPED
    scalar = bool(snapshot.supported_features & TARGET_TEMPERATURE)
    ranged = bool(snapshot.supported_features & TARGET_TEMPERATURE_RANGE)
    if ranged:
        return AutoMapping.RANGE
    if not scalar:
        return AutoMapping.UNMAPPED
    modes = set(snapshot.advertised_hvac_modes)
    has_heat = "heat" in modes
    has_cool = "cool" in modes
    if has_heat and not has_cool:
        return AutoMapping.HEATING
    if has_cool and not has_heat:
        return AutoMapping.COOLING
    return AutoMapping.UNMAPPED


def _finite(value: float | None) -> bool:
    return value is not None and not isinstance(value, bool) and math.isfinite(float(value))


def build_grid(
    snapshot: ClimateCapabilitySnapshot, options: GridOptions
) -> GridDefinition | ClimateFailure:
    """Build the finite legal grid from public device and user bounds."""

    required = (
        snapshot.min_temp_ha,
        snapshot.max_temp_ha,
        options.user_min_c,
        options.user_max_c,
        options.calibration_offset_c,
        options.minimum_range_gap_c,
    )
    if any(not _finite(value) for value in required):
        return ClimateFailure("invalid_grid_configuration")
    assert snapshot.min_temp_ha is not None
    assert snapshot.max_temp_ha is not None
    if (
        snapshot.min_temp_ha >= snapshot.max_temp_ha
        or options.user_min_c >= options.user_max_c
        or not -3.0 <= options.calibration_offset_c <= 3.0
        or options.minimum_range_gap_c < 1.0
    ):
        return ClimateFailure("invalid_grid_configuration")
    if options.step_override is not None:
        if not _finite(options.step_override) or options.step_override <= 0.0:
            return ClimateFailure("invalid_grid_configuration")
        step = unit_delta_to_ha(
            options.step_override,
            declared_unit=options.step_unit,
            ha_unit=snapshot.temperature_unit,
        )
        assumed = False
    elif snapshot.target_temp_step_ha is not None:
        if not _finite(snapshot.target_temp_step_ha) or snapshot.target_temp_step_ha <= 0.0:
            return ClimateFailure("invalid_grid_configuration")
        step = snapshot.target_temp_step_ha
        assumed = False
    else:
        step = 0.5 if snapshot.temperature_unit is TemperatureUnit.CELSIUS else 1.0
        assumed = True
    origin = (
        options.grid_origin_override_ha
        if options.grid_origin_override_ha is not None
        else snapshot.min_temp_ha
    )
    if not _finite(origin) or not math.isfinite(step) or step <= 0.0:
        return ClimateFailure("invalid_grid_configuration")
    lower = max(snapshot.min_temp_ha, celsius_to_ha(options.user_min_c, snapshot.temperature_unit))
    upper = min(snapshot.max_temp_ha, celsius_to_ha(options.user_max_c, snapshot.temperature_unit))
    if lower > upper + GRID_TOLERANCE_HA:
        return ClimateFailure("no_legal_target_grid")
    lower_index = math.ceil((lower - origin) / step - GRID_TOLERANCE_HA)
    upper_index = math.floor((upper - origin) / step + GRID_TOLERANCE_HA)
    if lower_index > upper_index:
        return ClimateFailure("no_legal_target_grid")
    grid = GridDefinition(
        float(origin), float(step), lower_index, upper_index, lower, upper, assumed
    )
    if grid.point_count > MAX_GRID_POINTS:
        return ClimateFailure("grid_too_fine")
    return grid


def _canonical_grid_value(grid: GridDefinition, index: int) -> float:
    return grid.origin_ha + index * grid.step_ha


def _normalize_scalar_with_grid(
    *,
    requested_room_c: float,
    direction: ActuationDirection,
    snapshot: ClimateCapabilitySnapshot,
    options: GridOptions,
    grid: GridDefinition,
) -> NormalizedScalarTarget | ClimateFailure:
    if direction is ActuationDirection.RANGED or not _finite(requested_room_c):
        return ClimateFailure("invalid_target_request")
    assert snapshot.min_temp_ha is not None
    assert snapshot.max_temp_ha is not None
    calibrated = requested_room_c + options.calibration_offset_c
    user_bounded = min(max(calibrated, options.user_min_c), options.user_max_c)
    limitations: list[str] = []
    if user_bounded != calibrated:
        limitations.append("user_bound_applied")
    device_min_c = ha_to_celsius(snapshot.min_temp_ha, snapshot.temperature_unit)
    device_max_c = ha_to_celsius(snapshot.max_temp_ha, snapshot.temperature_unit)
    bounded = min(max(user_bounded, device_min_c), device_max_c)
    if bounded != user_bounded:
        limitations.append("device_bound_applied")
    requested_ha = celsius_to_ha(bounded, snapshot.temperature_unit)
    position = (requested_ha - grid.origin_ha) / grid.step_ha
    if direction is ActuationDirection.HEATING_ONLY:
        index = math.ceil(position - GRID_TOLERANCE_HA)
    else:
        index = math.floor(position + GRID_TOLERANCE_HA)
    if index < grid.lower_index or index > grid.upper_index:
        return ClimateFailure("no_legal_inward_target")
    normalized_ha = _canonical_grid_value(grid, index)
    if (
        normalized_ha < grid.lower_bound_ha - GRID_TOLERANCE_HA
        or normalized_ha > grid.upper_bound_ha + GRID_TOLERANCE_HA
    ):
        return ClimateFailure("no_legal_inward_target")
    normalized_c = ha_to_celsius(normalized_ha, snapshot.temperature_unit)
    if abs(normalized_ha - requested_ha) > GRID_TOLERANCE_HA:
        limitations.append("grid_inward_adjustment")
    if grid.assumed_step:
        limitations.append("assumed_target_step")
    return NormalizedScalarTarget(
        direction,
        requested_room_c,
        calibrated,
        bounded,
        normalized_ha,
        normalized_c,
        normalized_c - options.calibration_offset_c,
        tuple(limitations),
        grid,
    )


def normalize_scalar_target(
    *,
    requested_room_c: float,
    direction: ActuationDirection,
    snapshot: ClimateCapabilitySnapshot,
    options: GridOptions,
) -> NormalizedScalarTarget | ClimateFailure:
    """Clamp then normalize one heating or cooling target inward."""

    grid = build_grid(snapshot, options)
    if isinstance(grid, ClimateFailure):
        return grid
    return _normalize_scalar_with_grid(
        requested_room_c=requested_room_c,
        direction=direction,
        snapshot=snapshot,
        options=options,
        grid=grid,
    )


def normalize_range_target(
    *,
    requested_heating_room_c: float,
    requested_cooling_room_c: float,
    snapshot: ClimateCapabilitySnapshot,
    options: GridOptions,
) -> NormalizedRangeTarget | ClimateFailure:
    """Normalize an atomic range inward and preserve its minimum gap."""

    grid = build_grid(snapshot, options)
    if isinstance(grid, ClimateFailure):
        return grid
    heating = _normalize_scalar_with_grid(
        requested_room_c=requested_heating_room_c,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=snapshot,
        options=options,
        grid=grid,
    )
    cooling = _normalize_scalar_with_grid(
        requested_room_c=requested_cooling_room_c,
        direction=ActuationDirection.COOLING_ONLY,
        snapshot=snapshot,
        options=options,
        grid=grid,
    )
    if isinstance(heating, ClimateFailure) or isinstance(cooling, ClimateFailure):
        return ClimateFailure("no_legal_range")
    if cooling.normalized_actuator_c - heating.normalized_actuator_c + 1e-12 < max(
        1.0, options.minimum_range_gap_c
    ):
        return ClimateFailure("no_legal_range")
    limitations = tuple(dict.fromkeys((*heating.limitations, *cooling.limitations)))
    return NormalizedRangeTarget(
        requested_heating_room_c,
        requested_cooling_room_c,
        heating,
        cooling,
        limitations,
    )
