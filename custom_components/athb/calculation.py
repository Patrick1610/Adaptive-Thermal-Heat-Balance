"""Immutable Home Assistant snapshot to production ATHB calculation adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .adapters.sources import (
    StateValue,
    configured_freshness,
    valid_value,
    validate_state_value,
)
from .core.athb_engine import relative_air_speed
from .core.climate import (
    CapabilityMapping,
    ClimateCapabilitySnapshot,
    ClimateFailure,
    GridOptions,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    ha_to_celsius,
    resolve_capability,
)
from .core.contracts import (
    AUTOMATIC_CLOTHING,
    ActuationDirection,
    AthbSuccess,
    BoostMode,
    ComfortStrategy,
    ControlProfile,
    CriticalEligibilityMode,
    DeclaredRelativeHumidity,
    EcoIntensity,
    FixedClothing,
    MeasuredAirTemperature,
    MeasuredGlobeTemperature,
    MeasuredMeanRadiantTemperature,
    MeasuredRelativeHumidity,
    MeasuredSurfaceTemperature,
    RootSuccess,
    TargetShape,
)
from .core.inverse import RadiantModel
from .core.locations import (
    CriticalDeltaUpdate,
    CriticalLocationFailure,
    prepare_critical_location,
)
from .core.pipeline import ZoneCalculationInput, ZoneCalculationResult, calculate_zone
from .core.policy import OpposingTarget, check_cross_actuator_coordination
from .core.psychrometrics import MoistureFailure, moisture_state
from .core.radiant import (
    DirectRadiantModel,
    GlobeRadiantModel,
    RadiantFailure,
    SurfaceCompositeRadiantModel,
    SurfaceContribution,
    UniformRadiantModel,
)
from .core.sources import SourceKind, SourceState
from .core.surface import (
    SurfaceFailure,
    estimate_surface_temperature,
    surface_humidity_diagnostic,
)


@dataclass(frozen=True, slots=True)
class CapturedTarget:
    target_uuid: str
    registry_identity: str
    entity_id: str
    capability: ClimateCapabilitySnapshot


@dataclass(frozen=True, slots=True)
class CapturedCriticalLocation:
    location_id: str
    mode: str
    state: StateValue | None
    delta: CriticalDeltaUpdate


@dataclass(frozen=True, slots=True)
class CapturedZoneSnapshot:
    now: datetime
    primary: StateValue | None
    relative_humidity: StateValue | None
    declared_relative_humidity_pct: float | None
    outdoor: StateValue | None
    optional_radiant: StateValue | None
    running_mean_c: float | None
    history_quality: str
    strategy: str
    profile: str
    options: dict[str, Any]
    targets: tuple[CapturedTarget, ...]
    critical_locations: tuple[CapturedCriticalLocation, ...] = ()
    explicit_transition: bool = False
    previous_requested: tuple[float | None, float | None] = (None, None)
    elapsed_since_previous_seconds: float = 0.0
    context_reasons: tuple[str, ...] = ()
    source_states: tuple[tuple[str, SourceState], ...] = ()
    air_speed: StateValue | None = None
    failure_hold_elapsed: bool = False
    boost_mode: str = "off"
    rapid_boost_reached: bool = False


@dataclass(frozen=True, slots=True)
class TargetCalculation:
    target_uuid: str
    registry_identity: str
    entity_id: str
    capability: ClimateCapabilitySnapshot
    mapping: CapabilityMapping | None
    result: ZoneCalculationResult | None
    suppression_reason: str | None


@dataclass(frozen=True, slots=True)
class RuntimeCalculation:
    primary_value_c: float | None
    relative_humidity_pct: float | None
    relative_humidity_provenance: str
    outdoor_value_c: float | None
    running_mean_c: float | None
    history_quality: str
    targets: tuple[TargetCalculation, ...]
    quality_reasons: tuple[str, ...]
    suppression_reason: str | None
    explicit_transition: bool
    surface_temperature_c: float | None = None
    surface_relative_humidity_pct: float | None = None
    surface_saturation: bool | None = None
    source_states: tuple[tuple[str, SourceState], ...] = ()
    hold_condition: str | None = None
    provenance: tuple[tuple[str, str], ...] = ()


def _finite_option(options: dict[str, Any], name: str, default: float) -> float:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _measured_failure_reason(
    prefix: str,
    *,
    value: float | None,
    reasons: tuple[str, ...],
    recovering: bool,
) -> str | None:
    if value is None:
        return f"{prefix}_stale" if "source_stale" in reasons else f"{prefix}_invalid"
    if recovering:
        return f"{prefix}_recovering"
    return None


def _radiant_model(
    snapshot: CapturedZoneSnapshot,
    *,
    air_c: float,
    outdoor_c: float | None,
    ambient_speed_m_s: float,
) -> tuple[RadiantModel, tuple[str, ...], float | None]:
    mode = str(snapshot.options.get("radiant_model", "uniform"))
    source = snapshot.optional_radiant
    if mode == "surface" and bool(snapshot.options.get("surface_modelled", False)):
        f_rsi = snapshot.options.get("surface_f_rsi")
        if outdoor_c is not None and not isinstance(f_rsi, bool) and isinstance(f_rsi, int | float):
            estimate = estimate_surface_temperature(
                indoor_temperature_c=air_c,
                current_outdoor_temperature_c=outdoor_c,
                f_rsi=float(f_rsi),
            )
            if not isinstance(estimate, SurfaceFailure):
                return (
                    SurfaceCompositeRadiantModel(
                        (
                            SurfaceContribution(
                                estimate.temperature,
                                _finite_option(snapshot.options, "surface_view_factor", 0.25),
                            ),
                        )
                    ),
                    ("modelled_surface",),
                    estimate.temperature.value_c,
                )
        return (
            UniformRadiantModel(),
            (
                "estimated_uniform_radiant_environment",
                "radiant_fallback_invalid_surface_model",
            ),
            None,
        )
    kind = {
        "direct_mrt": SourceKind.DIRECT_MRT,
        "globe": SourceKind.GLOBE,
        "surface": SourceKind.SURFACE,
        "mold_indicator": SourceKind.SURFACE,
    }.get(mode)
    if kind is None or source is None:
        return (
            UniformRadiantModel(),
            tuple(
                reason
                for reason in (
                    "estimated_uniform_radiant_environment",
                    "radiant_fallback_missing_source" if mode != "uniform" else None,
                )
                if reason is not None
            ),
            None,
        )
    observation, _ = validate_state_value(
        source,
        kind=kind,
        now=snapshot.now,
        freshness=configured_freshness(snapshot.options, kind),
    )
    value = valid_value(observation)
    if value is None:
        return (
            UniformRadiantModel(),
            (
                "estimated_uniform_radiant_environment",
                "radiant_fallback_missing_source",
            ),
            None,
        )
    if mode == "mold_indicator":
        return (
            UniformRadiantModel(),
            (
                "estimated_uniform_radiant_environment",
                "mold_indicator_surface_diagnostic",
            ),
            value,
        )
    if mode == "direct_mrt":
        return DirectRadiantModel(MeasuredMeanRadiantTemperature(value)), (), None
    if mode == "globe":
        model = GlobeRadiantModel.from_observations(
            globe=MeasuredGlobeTemperature(value),
            air_temperature_c=air_c,
            ambient_air_speed_m_s=ambient_speed_m_s,
            diameter_m=_finite_option(snapshot.options, "globe_diameter_m", 0.15),
            emissivity=_finite_option(snapshot.options, "globe_emissivity", 0.95),
        )
        if isinstance(model, RadiantFailure):
            return (
                UniformRadiantModel(),
                (
                    "estimated_uniform_radiant_environment",
                    "radiant_fallback_invalid_globe",
                ),
                None,
            )
        return model, (), None
    surface = MeasuredSurfaceTemperature(value)
    return (
        SurfaceCompositeRadiantModel(
            (
                SurfaceContribution(
                    surface,
                    _finite_option(snapshot.options, "surface_view_factor", 0.25),
                ),
            )
        ),
        (),
        value,
    )


def calculate_runtime_snapshot(snapshot: CapturedZoneSnapshot) -> RuntimeCalculation:
    """Validate one coherent snapshot and calculate every supported target."""

    prior_states = dict(snapshot.source_states)
    updated_states: dict[str, SourceState] = {}
    primary, updated_states["primary"] = validate_state_value(
        snapshot.primary,
        kind=SourceKind.PRIMARY_AIR,
        now=snapshot.now,
        prior=prior_states.get("primary"),
        freshness=configured_freshness(snapshot.options, SourceKind.PRIMARY_AIR),
    )
    outdoor, updated_states["outdoor"] = validate_state_value(
        snapshot.outdoor,
        kind=SourceKind.OUTDOOR,
        now=snapshot.now,
        prior=prior_states.get("outdoor"),
    )
    air_c = valid_value(primary)
    outdoor_c = valid_value(outdoor)
    if snapshot.options.get("air_speed_mode", "fixed") == "measured":
        speed_observation, updated_states["air_speed"] = validate_state_value(
            snapshot.air_speed,
            kind=SourceKind.AIR_SPEED,
            now=snapshot.now,
            prior=prior_states.get("air_speed"),
            freshness=configured_freshness(snapshot.options, SourceKind.AIR_SPEED),
        )
        ambient_speed = valid_value(speed_observation)
        speed_reason = _measured_failure_reason(
            "air_speed",
            value=ambient_speed,
            reasons=speed_observation.reasons,
            recovering=updated_states["air_speed"].recovering,
        )
    else:
        ambient_speed = _finite_option(snapshot.options, "air_speed_m_s", 0.1)
        speed_reason = None
    rh_value: float | None
    if snapshot.declared_relative_humidity_pct is not None:
        rh_value = snapshot.declared_relative_humidity_pct
        rh_provenance = "declared"
        rh_reasons: tuple[str, ...] = () if 0.0 <= rh_value <= 100.0 else ("invalid_declaration",)
    else:
        rh, updated_states["rh"] = validate_state_value(
            snapshot.relative_humidity,
            kind=SourceKind.RELATIVE_HUMIDITY,
            now=snapshot.now,
            prior=prior_states.get("rh"),
            freshness=configured_freshness(snapshot.options, SourceKind.RELATIVE_HUMIDITY),
        )
        rh_value = valid_value(rh)
        rh_provenance = "measured"
        rh_reasons = rh.reasons
    mandatory_reason = _measured_failure_reason(
        "primary_temperature",
        value=air_c,
        reasons=primary.reasons,
        recovering=updated_states["primary"].recovering,
    )
    if mandatory_reason is None:
        mandatory_reason = (
            "primary_rh_invalid"
            if snapshot.declared_relative_humidity_pct is not None and rh_reasons
            else _measured_failure_reason(
                "primary_rh",
                value=rh_value,
                reasons=rh_reasons,
                recovering="rh" in updated_states and updated_states["rh"].recovering,
            )
        )
    fallback_for_speed = speed_reason is not None and snapshot.failure_hold_elapsed
    base_provenance = (
        ("primary", "measured"),
        ("rh", rh_provenance),
        ("outdoor", "measured"),
        ("met", "declared"),
        (
            "air_speed",
            "measured"
            if snapshot.options.get("air_speed_mode", "fixed") == "measured"
            else "declared",
        ),
        (
            "clothing",
            "declared"
            if snapshot.options.get("clothing_mode", "automatic") == "fixed"
            else "estimated",
        ),
    )
    configured_radiant_provenance = (
        "estimated"
        if snapshot.options.get("radiant_model", "uniform") in {"uniform", "mold_indicator"}
        or snapshot.optional_radiant is None
        or bool(snapshot.options.get("surface_modelled", False))
        else "measured"
    )
    if mandatory_reason is not None or (speed_reason is not None and not fallback_for_speed):
        failure_reason = mandatory_reason or speed_reason
        assert failure_reason is not None
        targets = tuple(
            TargetCalculation(
                item.target_uuid,
                item.registry_identity,
                item.entity_id,
                item.capability,
                None,
                None,
                failure_reason,
            )
            for item in snapshot.targets
        )
        return RuntimeCalculation(
            air_c,
            rh_value,
            rh_provenance,
            outdoor_c,
            snapshot.running_mean_c,
            snapshot.history_quality,
            targets,
            tuple(
                dict.fromkeys(
                    (*primary.reasons, *rh_reasons, *((speed_reason,) if speed_reason else ()))
                )
            ),
            failure_reason,
            snapshot.explicit_transition,
            source_states=tuple(sorted(updated_states.items())),
            hold_condition=failure_reason,
            provenance=(*base_provenance, ("radiant", configured_radiant_provenance)),
        )
    assert air_c is not None
    assert rh_value is not None
    radiant: RadiantModel
    radiant_reasons: tuple[str, ...]
    surface_temperature_c: float | None
    if fallback_for_speed:
        radiant, radiant_reasons, surface_temperature_c = (
            UniformRadiantModel(),
            ("air_speed_invalid_fixed_fallback",),
            None,
        )
    else:
        assert ambient_speed is not None
        radiant, radiant_reasons, surface_temperature_c = _radiant_model(
            snapshot,
            air_c=air_c,
            outdoor_c=outdoor_c,
            ambient_speed_m_s=ambient_speed,
        )
    radiant_provenance = (
        "estimated"
        if fallback_for_speed
        or str(snapshot.options.get("radiant_model", "uniform")) in {"uniform", "mold_indicator"}
        or any(reason.startswith("radiant_fallback") for reason in radiant_reasons)
        or "modelled_surface" in radiant_reasons
        else "measured"
    )
    strategy = ComfortStrategy(snapshot.strategy)
    profile = ControlProfile(snapshot.profile)
    boost_mode = BoostMode(snapshot.boost_mode)
    critical = []
    met = _finite_option(snapshot.options, "met", 1.1)
    clothing = (
        FixedClothing(_finite_option(snapshot.options, "fixed_clothing_clo", 0.7))
        if snapshot.options.get("clothing_mode", "automatic") == "fixed"
        else AUTOMATIC_CLOTHING
    )
    effective_speed = None if ambient_speed is None else relative_air_speed(ambient_speed, met)
    primary_moisture = moisture_state(
        air_c,
        # Pipeline supplies the authoritative measured/declared tag again.
        DeclaredRelativeHumidity(rh_value)
        if rh_provenance == "declared"
        else MeasuredRelativeHumidity(rh_value),
    )
    surface_rh = (
        surface_humidity_diagnostic(
            moisture=primary_moisture,
            surface_temperature_c=surface_temperature_c,
            high_threshold_pct=_finite_option(snapshot.options, "surface_rh_threshold_pct", 80.0),
        )
        if not isinstance(primary_moisture, MoistureFailure) and surface_temperature_c is not None
        else None
    )
    if (
        not isinstance(primary_moisture, MoistureFailure)
        and isinstance(effective_speed, float)
        and snapshot.running_mean_c is not None
    ):
        for item in snapshot.critical_locations:
            state_key = f"critical:{item.location_id}"
            local, updated_states[state_key] = validate_state_value(
                item.state,
                kind=SourceKind.LOCAL_AIR,
                now=snapshot.now,
                prior=prior_states.get(state_key),
                freshness=configured_freshness(snapshot.options, SourceKind.LOCAL_AIR),
            )
            local_c = valid_value(local)
            if local_c is None or updated_states[state_key].recovering:
                continue
            prepared = prepare_critical_location(
                location_id=item.location_id,
                mode=CriticalEligibilityMode(item.mode),
                delta=item.delta,
                current_room_air_temperature_c=air_c,
                local_air_temperature=MeasuredAirTemperature(local_c),
                primary_moisture=primary_moisture,
                zone_radiant=radiant,
                relative_air_speed_m_s=effective_speed,
                met=met,
                running_mean_c=snapshot.running_mean_c,
                clothing=clothing,
            )
            if not isinstance(prepared, CriticalLocationFailure):
                critical.append(prepared)
    resolved_mappings: dict[str, CapabilityMapping] = {}
    for target in snapshot.targets:
        resolved = resolve_capability(target.capability)
        if isinstance(resolved, CapabilityMapping):
            resolved_mappings[target.target_uuid] = resolved
    mapped_directions = {resolved.direction for resolved in resolved_mappings.values()}
    mixed_scalar_rapid = boost_mode is BoostMode.RAPID and {
        ActuationDirection.HEATING_ONLY,
        ActuationDirection.COOLING_ONLY,
    }.issubset(mapped_directions)
    effective_boost_mode = BoostMode.ADAPTIVE if mixed_scalar_rapid else boost_mode
    target_results: list[TargetCalculation] = []
    for target in snapshot.targets:
        control_mapping = resolved_mappings.get(target.target_uuid)
        capability_result: CapabilityMapping | ClimateFailure
        if control_mapping is None:
            capability_result = resolve_capability(target.capability)
        else:
            capability_result = control_mapping
        capability_reason = (
            capability_result.reason if isinstance(capability_result, ClimateFailure) else None
        )
        calculation_mapping: CapabilityMapping
        if isinstance(capability_result, ClimateFailure):
            calculation_mapping = CapabilityMapping(
                ActuationDirection.RANGED
                if target.capability.supported_features & 2
                else ActuationDirection.COOLING_ONLY
                if target.capability.hvac_mode == "cool"
                else ActuationDirection.HEATING_ONLY,
                TargetShape.RANGE
                if target.capability.supported_features & 2
                else TargetShape.SCALAR,
            )
        else:
            calculation_mapping = capability_result
        grid = GridOptions(
            _finite_option(snapshot.options, "minimum_control_temperature", 18.0),
            _finite_option(snapshot.options, "maximum_control_temperature", 26.0),
            _finite_option(snapshot.options, f"calibration_{target.target_uuid}", 0.0),
            minimum_range_gap_c=_finite_option(snapshot.options, "minimum_range_gap", 1.0),
        )
        result = calculate_zone(
            ZoneCalculationInput(
                air_c,
                rh_value,
                rh_provenance == "declared",
                snapshot.running_mean_c,
                strategy,
                calculation_mapping.direction,
                profile,
                target.capability,
                grid,
                met=_finite_option(snapshot.options, "met", 1.1),
                air_speed_m_s=ambient_speed,
                clothing=clothing,
                radiant=radiant,
                critical_locations=tuple(critical),
                lower_comfort_vote=_finite_option(snapshot.options, "lower_comfort_vote", -0.5),
                upper_comfort_vote=_finite_option(snapshot.options, "upper_comfort_vote", 0.5),
                fallback_heating_c=_finite_option(snapshot.options, "fallback_heating_c", 18.0),
                fallback_cooling_c=_finite_option(snapshot.options, "fallback_cooling_c", 26.0),
                fallback_no_write=str(snapshot.options.get("fallback_mode", "fixed")) == "no_write",
                reject_extrapolation=bool(snapshot.options.get("reject_extrapolation", False)),
                eco_heating_setback_c=_finite_option(
                    snapshot.options, "eco_heating_setback_c", 2.0
                ),
                eco_cooling_setback_c=_finite_option(
                    snapshot.options, "eco_cooling_setback_c", 2.0
                ),
                eco_intensity=EcoIntensity(str(snapshot.options.get("eco_intensity", "custom"))),
                inactive_heating_c=grid.user_min_c,
                inactive_cooling_c=grid.user_max_c,
                boost_delta_c=_finite_option(snapshot.options, "boost_delta_c", 1.0),
                boost_mode=effective_boost_mode,
                rapid_boost_reached=snapshot.rapid_boost_reached,
                previous_requested=snapshot.previous_requested,
                elapsed_since_previous_seconds=snapshot.elapsed_since_previous_seconds,
                explicit_transition=snapshot.explicit_transition,
                failure_hold_elapsed=snapshot.failure_hold_elapsed,
                fixed_fallback_reason="air_speed_invalid" if fallback_for_speed else None,
            )
        )
        target_results.append(
            TargetCalculation(
                target.target_uuid,
                target.registry_identity,
                target.entity_id,
                target.capability,
                control_mapping,
                result,
                capability_reason or result.suppression_reason,
            )
        )
    coordinated: list[TargetCalculation] = []
    for target_result in target_results:
        calculated = target_result.result
        active_mapping = target_result.mapping
        normalized = calculated.normalized if calculated is not None else None
        if active_mapping is None or not isinstance(normalized, NormalizedScalarTarget):
            coordinated.append(target_result)
            continue
        opposing: list[OpposingTarget] = []
        for other in target_results:
            if other is target_result or other.mapping is None:
                continue
            other_normalized = other.result.normalized if other.result is not None else None
            intended = (
                other_normalized.normalized_room_c
                if isinstance(other_normalized, NormalizedScalarTarget)
                else None
            )
            observed = (
                ha_to_celsius(
                    other.capability.scalar_target_ha,
                    other.capability.temperature_unit,
                )
                if other.capability.scalar_target_ha is not None
                else None
            )
            opposing.append(
                OpposingTarget(
                    other.registry_identity,
                    other.mapping.direction,
                    intended is not None,
                    other.capability.available,
                    intended,
                    observed,
                )
            )
        coordination = check_cross_actuator_coordination(
            direction=active_mapping.direction,
            proposed_room_c=normalized.normalized_room_c,
            opposing_targets=tuple(opposing),
            minimum_range_gap_c=_finite_option(snapshot.options, "minimum_range_gap", 1.0),
        )
        coordinated.append(
            target_result
            if coordination.eligible
            else replace(target_result, suppression_reason=coordination.reason)
        )
    target_results = coordinated
    first_result = next((item.result for item in target_results if item.result is not None), None)
    current = first_result.current if first_result is not None else None
    scientific_reasons = (
        tuple(reason.value for reason in current.applicability_reasons)
        if isinstance(current, AthbSuccess)
        else ()
    )
    suppression = next(
        (item.suppression_reason for item in target_results if item.suppression_reason), None
    )
    hold_condition = next(
        (
            item.result.hold_condition
            for item in target_results
            if item.result is not None and item.result.hold_condition is not None
        ),
        "air_speed_invalid" if fallback_for_speed else None,
    )
    return RuntimeCalculation(
        air_c,
        rh_value,
        rh_provenance,
        outdoor_c,
        snapshot.running_mean_c,
        snapshot.history_quality,
        tuple(target_results),
        tuple(
            dict.fromkeys(
                (
                    *snapshot.context_reasons,
                    *radiant_reasons,
                    *scientific_reasons,
                    *(("rapid_boost_mixed_adaptive",) if mixed_scalar_rapid else ()),
                )
            )
        ),
        suppression,
        snapshot.explicit_transition,
        surface_temperature_c,
        (
            surface_rh.displayed_relative_humidity_pct
            if surface_rh is not None and not isinstance(surface_rh, SurfaceFailure)
            else None
        ),
        (
            surface_rh.predicted_saturation
            if surface_rh is not None and not isinstance(surface_rh, SurfaceFailure)
            else None
        ),
        tuple(sorted(updated_states.items())),
        hold_condition,
        (*base_provenance, ("radiant", radiant_provenance)),
    )


def result_values(result: RuntimeCalculation) -> dict[str, Any]:
    """Project a calculation into bounded entity-facing values."""

    numerical = next((item.result for item in result.targets if item.result is not None), None)
    roots = numerical.roots if numerical is not None else None

    def root_value(name: str) -> float | None:
        root = getattr(roots, name, None)
        return root.mapped_room_temperature_c if isinstance(root, RootSuccess) else None

    effective: dict[str, dict[str, float]] = {}
    effective_details: dict[str, dict[str, str | bool | float | None]] = {}
    for target in result.targets:
        calculation = target.result
        if calculation is None or calculation.normalized is None:
            continue
        normalized = calculation.normalized
        fallback = bool(calculation.policy and calculation.policy.fallback)
        effective_details[target.target_uuid] = {
            "mode": "fallback" if fallback else "adaptive",
            "reason": calculation.hold_condition or target.suppression_reason,
            "fallback": fallback,
            "boost_mode": calculation.policy.boost_mode.value if calculation.policy else "off",
            "boost_phase": calculation.policy.boost_phase if calculation.policy else "off",
            "boost_target_heating": (
                calculation.policy.boost_target_heating_c if calculation.policy else None
            ),
            "boost_target_cooling": (
                calculation.policy.boost_target_cooling_c if calculation.policy else None
            ),
        }
        if isinstance(normalized, NormalizedScalarTarget):
            effective[target.target_uuid] = {"temperature": normalized.normalized_actuator_c}
        elif isinstance(normalized, NormalizedRangeTarget):
            effective[target.target_uuid] = {
                "target_low": normalized.heating.normalized_actuator_c,
                "target_high": normalized.cooling.normalized_actuator_c,
            }
    current = numerical.current if numerical is not None else None
    sensation = current.public_sensation_vote if isinstance(current, AthbSuccess) else None
    comfort_status = (
        "unknown"
        if sensation is None
        else "cold"
        if sensation < -0.5
        else "warm"
        if sensation > 0.5
        else "comfortable"
    )
    return {
        "thermal_sensation": sensation,
        "heating_control_target": root_value("heating_control"),
        "thermal_neutral": root_value("thermal_neutral"),
        "cooling_control_target": root_value("cooling_control"),
        "comfort_status": comfort_status,
        "input_status": result.hold_condition or "ready",
        "control_status": "suppressed" if result.suppression_reason else "ready",
        "outdoor_running_mean": result.running_mean_c,
        "effective_targets": effective,
        "effective_target_details": effective_details,
        "quality_reasons": result.quality_reasons,
        "suppression_reason": result.suppression_reason,
        "rh_provenance": result.relative_humidity_provenance,
        "surface_temperature": result.surface_temperature_c,
        "surface_relative_humidity": result.surface_relative_humidity_pct,
        "surface_saturation": result.surface_saturation,
        "provenance": dict(result.provenance),
        "calculation": result,
    }
