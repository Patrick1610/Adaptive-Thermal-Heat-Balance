"""Pure ATHB product policy over immutable raw sensation roots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .contracts import (
    ActuationDirection,
    BoostMode,
    ControlProfile,
    CriticalEligibilityMode,
    EcoIntensity,
    RootResult,
    RootSet,
    RootSuccess,
)

MAX_CRITICAL_INFLUENCE_C = 2.0
DEFAULT_MINIMUM_RANGE_GAP_C = 1.0
DEFAULT_ECO_SETBACK_C = 2.0
WORKDAY_ECO_SETBACK_C = 4.0
DEFAULT_BOOST_DELTA_C = 1.0
DEFAULT_BOOST_DURATION = timedelta(minutes=60)
OCCUPANCY_UNKNOWN_HOLD = timedelta(minutes=30)
SLEW_RATE_C_PER_SECOND = 0.5 / 600.0
DEFAULT_USER_MIN_C = 18.0
DEFAULT_USER_MAX_C = 26.0


class OccupancyState(StrEnum):
    ABSENT = "absent"
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    selected: ControlProfile
    resolved: ControlProfile
    resolved_at: datetime
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CriticalDemand:
    location_id: str
    mode: CriticalEligibilityMode
    heating_control: RootResult
    cooling_control: RootResult
    control_eligible: bool = True


@dataclass(frozen=True, slots=True)
class DirectionalContribution:
    governing_location: str
    requested_c: float
    applied_c: float


@dataclass(frozen=True, slots=True)
class CriticalTargets:
    heating_c: float | None
    cooling_c: float | None
    heating_contribution: DirectionalContribution | None
    cooling_contribution: DirectionalContribution | None
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyTargets:
    heating_c: float | None
    cooling_c: float | None
    pre_profile_heating_c: float | None
    pre_profile_cooling_c: float | None
    pre_slew_heating_c: float | None
    pre_slew_cooling_c: float | None
    profile: ControlProfile
    fallback: bool
    governing_heating: str
    governing_cooling: str
    heating_contribution: DirectionalContribution | None
    cooling_contribution: DirectionalContribution | None
    limitations: tuple[str, ...]
    boost_mode: BoostMode = BoostMode.OFF
    boost_phase: str = "off"
    rapid_boost_reached: bool = False
    boost_target_heating_c: float | None = None
    boost_target_cooling_c: float | None = None
    occupied_heating_c: float | None = None
    occupied_cooling_c: float | None = None
    unoccupied_heating_c: float | None = None
    unoccupied_cooling_c: float | None = None


@dataclass(frozen=True, slots=True)
class PolicyFailure:
    reason: str
    limitations: tuple[str, ...] = ()


type PolicyResult = PolicyTargets | PolicyFailure


@dataclass(frozen=True, slots=True)
class ActuatorBoundResult:
    requested_room_c: float
    calibrated_c: float
    bounded_c: float
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpposingTarget:
    """Known intended or observed opposing actuator in room coordinates."""

    target_identity: str
    direction: ActuationDirection
    owned: bool
    available: bool
    intended_room_c: float | None
    observed_room_c: float | None


@dataclass(frozen=True, slots=True)
class CoordinationResult:
    eligible: bool
    reason: str | None


def resolve_profile(
    *,
    selected: ControlProfile,
    occupancy: OccupancyState,
    now: datetime,
    previous: ProfileResolution | None = None,
) -> ProfileResolution:
    """Resolve auto occupancy without implementing a scheduling engine."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if selected is not ControlProfile.AUTO:
        return ProfileResolution(selected, selected, now, ())
    if occupancy is OccupancyState.ON:
        return ProfileResolution(selected, ControlProfile.COMFORT, now, ())
    if occupancy is OccupancyState.OFF:
        return ProfileResolution(selected, ControlProfile.ECO, now, ())
    if occupancy is OccupancyState.ABSENT:
        return ProfileResolution(selected, ControlProfile.COMFORT, now, ())
    if (
        previous is not None
        and previous.resolved in {ControlProfile.COMFORT, ControlProfile.ECO}
        and timedelta(0) <= now - previous.resolved_at <= OCCUPANCY_UNKNOWN_HOLD
    ):
        return ProfileResolution(selected, previous.resolved, previous.resolved_at, ())
    return ProfileResolution(
        selected,
        ControlProfile.COMFORT,
        now,
        ("occupancy_unknown",),
    )


def resolve_occupancy_profile(
    *,
    occupancy: OccupancyState,
    now: datetime,
    previous: ProfileResolution | None = None,
) -> ProfileResolution:
    """Resolve occupancy to the internal setback state without a public profile."""

    return resolve_profile(
        selected=ControlProfile.AUTO,
        occupancy=occupancy,
        now=now,
        previous=previous,
    )


def _success(root: RootResult) -> float | None:
    return root.mapped_room_temperature_c if isinstance(root, RootSuccess) else None


def _select_heating(demands: tuple[CriticalDemand, ...], baseline: float) -> tuple[str, float]:
    eligible = [
        (item.location_id, value)
        for item in demands
        if item.control_eligible
        and item.mode in {CriticalEligibilityMode.HEATING, CriticalEligibilityMode.BOTH}
        and (value := _success(item.heating_control)) is not None
        and value > baseline
    ]
    return (
        min(eligible, key=lambda item: (-item[1], item[0])) if eligible else ("primary", baseline)
    )


def _select_cooling(demands: tuple[CriticalDemand, ...], baseline: float) -> tuple[str, float]:
    eligible = [
        (item.location_id, value)
        for item in demands
        if item.control_eligible
        and item.mode in {CriticalEligibilityMode.COOLING, CriticalEligibilityMode.BOTH}
        and (value := _success(item.cooling_control)) is not None
        and value < baseline
    ]
    return min(eligible, key=lambda item: (item[1], item[0])) if eligible else ("primary", baseline)


def apply_critical_demands(
    *,
    roots: RootSet,
    demands: tuple[CriticalDemand, ...],
    direction: ActuationDirection,
    minimum_range_gap_c: float = DEFAULT_MINIMUM_RANGE_GAP_C,
) -> CriticalTargets | PolicyFailure:
    """Select worst directional demand, cap once, and coordinate a range."""

    heating_baseline = _success(roots.heating_control)
    cooling_baseline = _success(roots.cooling_control)
    needs_heating = direction in {ActuationDirection.HEATING_ONLY, ActuationDirection.RANGED}
    needs_cooling = direction in {ActuationDirection.COOLING_ONLY, ActuationDirection.RANGED}
    if needs_heating and heating_baseline is None:
        return PolicyFailure("missing_required_root:heating_control")
    if needs_cooling and cooling_baseline is None:
        return PolicyFailure("missing_required_root:cooling_control")
    gap = max(DEFAULT_MINIMUM_RANGE_GAP_C, minimum_range_gap_c)
    if (
        direction is ActuationDirection.RANGED
        and heating_baseline is not None
        and cooling_baseline is not None
        and cooling_baseline - heating_baseline < gap
    ):
        return PolicyFailure("control_band_too_narrow")

    limitations: list[str] = []
    heating = heating_baseline if needs_heating else None
    cooling = cooling_baseline if needs_cooling else None
    heating_contribution = None
    cooling_contribution = None
    lower = _success(roots.lower_comfort)
    upper = _success(roots.upper_comfort)
    outer_available = lower is not None and upper is not None and upper > lower

    if heating is not None:
        location_id, requested = _select_heating(demands, heating)
        if location_id != "primary":
            if not outer_available:
                limitations.append("critical_constraints_unavailable")
            else:
                assert upper is not None
                assert lower is not None
                edge_guard = min(0.25, (upper - lower) / 4.0)
                cap = max(heating, min(heating + MAX_CRITICAL_INFLUENCE_C, upper - edge_guard))
                if cap == heating and requested > heating:
                    limitations.append("critical_constraints_inconsistent")
                applied_target = min(max(heating, requested), cap)
                heating_contribution = DirectionalContribution(
                    location_id, requested - heating, applied_target - heating
                )
                heating = applied_target
                if applied_target < requested:
                    limitations.append("critical_demand_limited")

    if cooling is not None:
        location_id, requested = _select_cooling(demands, cooling)
        if location_id != "primary":
            if not outer_available:
                limitations.append("critical_constraints_unavailable")
            else:
                assert upper is not None
                assert lower is not None
                edge_guard = min(0.25, (upper - lower) / 4.0)
                floor = min(cooling, max(cooling - MAX_CRITICAL_INFLUENCE_C, lower + edge_guard))
                if floor == cooling and requested < cooling:
                    limitations.append("critical_constraints_inconsistent")
                applied_target = max(min(cooling, requested), floor)
                cooling_contribution = DirectionalContribution(
                    location_id, requested - cooling, applied_target - cooling
                )
                cooling = applied_target
                if applied_target > requested:
                    limitations.append("critical_demand_limited")

    if (
        direction is ActuationDirection.RANGED
        and heating is not None
        and cooling is not None
        and cooling - heating < gap
    ):
        limitations.append("critical_locations_conflict")
        heating = heating_baseline
        cooling = cooling_baseline
        heating_contribution = None
        cooling_contribution = None
    return CriticalTargets(
        heating,
        cooling,
        heating_contribution,
        cooling_contribution,
        tuple(dict.fromkeys(limitations)),
    )


@dataclass(frozen=True, slots=True)
class _PolicyTransform:
    heating_c: float | None
    cooling_c: float | None
    limitations: tuple[str, ...]
    boost_phase: str
    rapid_boost_reached: bool
    boost_target_heating_c: float | None
    boost_target_cooling_c: float | None


def _policy_transform(
    *,
    heating: float | None,
    cooling: float | None,
    profile: ControlProfile,
    boost_mode: BoostMode,
    direction: ActuationDirection,
    current_air_temperature_c: float,
    rapid_boost_reached: bool,
    neutral_heating_c: float | None,
    neutral_cooling_c: float | None,
    eco_intensity: EcoIntensity,
    minimum_range_gap_c: float,
    eco_heating_setback_c: float,
    eco_cooling_setback_c: float,
    inactive_heating_c: float,
    inactive_cooling_c: float,
    rapid_heating_c: float,
    rapid_cooling_c: float,
    boost_delta_c: float,
) -> _PolicyTransform:
    limitations: list[str] = []
    if boost_mode is BoostMode.OFF and profile is ControlProfile.ECO:
        if eco_intensity is EcoIntensity.DEEP:
            return _PolicyTransform(
                inactive_heating_c if heating is not None else None,
                inactive_cooling_c if cooling is not None else None,
                ("occupancy_setback", "setback_max"),
                "off",
                False,
                None,
                None,
            )
        heating_setback = (
            DEFAULT_ECO_SETBACK_C
            if eco_intensity is EcoIntensity.MILD
            else WORKDAY_ECO_SETBACK_C
            if eco_intensity is EcoIntensity.WORKDAY
            else eco_heating_setback_c
        )
        cooling_setback = (
            DEFAULT_ECO_SETBACK_C
            if eco_intensity is EcoIntensity.MILD
            else WORKDAY_ECO_SETBACK_C
            if eco_intensity is EcoIntensity.WORKDAY
            else eco_cooling_setback_c
        )
        return _PolicyTransform(
            heating - heating_setback if heating is not None else None,
            cooling + cooling_setback if cooling is not None else None,
            ("occupancy_setback",),
            "off",
            False,
            None,
            None,
        )
    if boost_mode is BoostMode.OFF:
        return _PolicyTransform(heating, cooling, (), "off", False, None, None)

    boost_heating = heating
    boost_cooling = cooling
    if heating is not None and cooling is not None:
        available = max(0.0, (cooling - heating - minimum_range_gap_c) / 2.0)
        applied = min(max(0.0, boost_delta_c), available)
        if applied < boost_delta_c:
            limitations.append("boost_limited")
        boost_heating = heating + applied
        boost_cooling = cooling - applied
    else:
        if heating is not None:
            boost_heating = heating + boost_delta_c
            if neutral_heating_c is not None:
                boost_heating = min(boost_heating, neutral_heating_c)
            else:
                limitations.append("boost_neutral_unavailable")
            if boost_heating < heating + boost_delta_c:
                limitations.append("boost_limited")
        if cooling is not None:
            boost_cooling = cooling - boost_delta_c
            if neutral_cooling_c is not None:
                boost_cooling = max(boost_cooling, neutral_cooling_c)
            else:
                limitations.append("boost_neutral_unavailable")
            if boost_cooling > cooling - boost_delta_c:
                limitations.append("boost_limited")

    if boost_mode is BoostMode.ADAPTIVE:
        return _PolicyTransform(
            boost_heating,
            boost_cooling,
            tuple(dict.fromkeys(limitations)),
            "adaptive",
            False,
            boost_heating,
            boost_cooling,
        )
    if direction is ActuationDirection.RANGED:
        limitations.append("rapid_boost_range_adaptive")
        return _PolicyTransform(
            boost_heating,
            boost_cooling,
            tuple(dict.fromkeys(limitations)),
            "adaptive",
            False,
            boost_heating,
            boost_cooling,
        )

    reached = rapid_boost_reached
    if direction is ActuationDirection.HEATING_ONLY and boost_heating is not None:
        reached = reached or current_air_temperature_c >= boost_heating
        return _PolicyTransform(
            boost_heating if reached else rapid_heating_c,
            None,
            tuple(dict.fromkeys(limitations)),
            "hold" if reached else "rapid",
            reached,
            boost_heating,
            None,
        )
    if direction is ActuationDirection.COOLING_ONLY and boost_cooling is not None:
        reached = reached or current_air_temperature_c <= boost_cooling
        return _PolicyTransform(
            None,
            boost_cooling if reached else rapid_cooling_c,
            tuple(dict.fromkeys(limitations)),
            "hold" if reached else "rapid",
            reached,
            None,
            boost_cooling,
        )
    return _PolicyTransform(
        boost_heating,
        boost_cooling,
        tuple(dict.fromkeys((*limitations, "rapid_boost_unavailable"))),
        "adaptive",
        False,
        boost_heating,
        boost_cooling,
    )


def _slew(value: float | None, previous: float | None, elapsed_seconds: float) -> float | None:
    if value is None or previous is None:
        return value
    allowance = max(0.0, elapsed_seconds) * SLEW_RATE_C_PER_SECOND
    return min(max(value, previous - allowance), previous + allowance)


def build_adaptive_policy(
    *,
    roots: RootSet,
    critical_demands: tuple[CriticalDemand, ...],
    direction: ActuationDirection,
    profile: ControlProfile,
    boost_mode: BoostMode = BoostMode.OFF,
    current_air_temperature_c: float = 20.0,
    rapid_boost_reached: bool = False,
    eco_intensity: EcoIntensity = EcoIntensity.CUSTOM,
    minimum_range_gap_c: float = DEFAULT_MINIMUM_RANGE_GAP_C,
    eco_heating_setback_c: float = DEFAULT_ECO_SETBACK_C,
    eco_cooling_setback_c: float = DEFAULT_ECO_SETBACK_C,
    inactive_heating_c: float = DEFAULT_USER_MIN_C,
    inactive_cooling_c: float = DEFAULT_USER_MAX_C,
    boost_delta_c: float = DEFAULT_BOOST_DELTA_C,
    previous_requested: tuple[float | None, float | None] = (None, None),
    elapsed_since_previous_seconds: float = 0.0,
    explicit_transition: bool = False,
) -> PolicyResult:
    """Apply critical, profile, and ordinary environmental-slew policy in order."""

    numeric = (
        minimum_range_gap_c,
        eco_heating_setback_c,
        eco_cooling_setback_c,
        inactive_heating_c,
        inactive_cooling_c,
        boost_delta_c,
        current_air_temperature_c,
        elapsed_since_previous_seconds,
    )
    if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in numeric):
        return PolicyFailure("invalid_policy_configuration")
    if (
        minimum_range_gap_c < 1.0
        or eco_heating_setback_c < 0.0
        or eco_cooling_setback_c < 0.0
        or boost_delta_c < 0.0
        or inactive_heating_c >= inactive_cooling_c
    ):
        return PolicyFailure("invalid_policy_configuration")
    critical = apply_critical_demands(
        roots=roots,
        demands=critical_demands,
        direction=direction,
        minimum_range_gap_c=minimum_range_gap_c,
    )
    if isinstance(critical, PolicyFailure):
        return critical
    neutral = _success(roots.thermal_neutral)
    transformed = _policy_transform(
        heating=critical.heating_c,
        cooling=critical.cooling_c,
        profile=profile,
        boost_mode=boost_mode,
        direction=direction,
        current_air_temperature_c=current_air_temperature_c,
        rapid_boost_reached=rapid_boost_reached,
        neutral_heating_c=neutral,
        neutral_cooling_c=neutral,
        eco_intensity=eco_intensity,
        minimum_range_gap_c=minimum_range_gap_c,
        eco_heating_setback_c=eco_heating_setback_c,
        eco_cooling_setback_c=eco_cooling_setback_c,
        inactive_heating_c=inactive_heating_c
        + (
            critical.heating_contribution.applied_c
            if critical.heating_contribution is not None
            else 0.0
        ),
        inactive_cooling_c=inactive_cooling_c
        + (
            critical.cooling_contribution.applied_c
            if critical.cooling_contribution is not None
            else 0.0
        ),
        rapid_heating_c=inactive_cooling_c,
        rapid_cooling_c=inactive_heating_c,
        boost_delta_c=boost_delta_c,
    )

    def occupancy_reference(reference_profile: ControlProfile) -> _PolicyTransform:
        return _policy_transform(
            heating=critical.heating_c,
            cooling=critical.cooling_c,
            profile=reference_profile,
            boost_mode=BoostMode.OFF,
            direction=direction,
            current_air_temperature_c=current_air_temperature_c,
            rapid_boost_reached=False,
            neutral_heating_c=neutral,
            neutral_cooling_c=neutral,
            eco_intensity=eco_intensity,
            minimum_range_gap_c=minimum_range_gap_c,
            eco_heating_setback_c=eco_heating_setback_c,
            eco_cooling_setback_c=eco_cooling_setback_c,
            inactive_heating_c=inactive_heating_c
            + (
                critical.heating_contribution.applied_c
                if critical.heating_contribution is not None
                else 0.0
            ),
            inactive_cooling_c=inactive_cooling_c
            + (
                critical.cooling_contribution.applied_c
                if critical.cooling_contribution is not None
                else 0.0
            ),
            rapid_heating_c=inactive_cooling_c,
            rapid_cooling_c=inactive_heating_c,
            boost_delta_c=boost_delta_c,
        )

    occupied = occupancy_reference(ControlProfile.COMFORT)
    unoccupied = occupancy_reference(ControlProfile.ECO)
    if explicit_transition:
        requested_heating = transformed.heating_c
        requested_cooling = transformed.cooling_c
    else:
        boost_reached_now = (
            boost_mode is BoostMode.RAPID
            and transformed.rapid_boost_reached
            and not rapid_boost_reached
        )
        requested_heating = _slew(
            transformed.heating_c, previous_requested[0], elapsed_since_previous_seconds
        )
        requested_cooling = _slew(
            transformed.cooling_c, previous_requested[1], elapsed_since_previous_seconds
        )
        if boost_reached_now:
            requested_heating = transformed.heating_c
            requested_cooling = transformed.cooling_c
    limitations = tuple(dict.fromkeys((*critical.limitations, *transformed.limitations)))
    return PolicyTargets(
        requested_heating,
        requested_cooling,
        critical.heating_c,
        critical.cooling_c,
        transformed.heating_c,
        transformed.cooling_c,
        profile,
        False,
        (
            critical.heating_contribution.governing_location
            if critical.heating_contribution is not None
            else "primary"
        ),
        (
            critical.cooling_contribution.governing_location
            if critical.cooling_contribution is not None
            else "primary"
        ),
        critical.heating_contribution,
        critical.cooling_contribution,
        limitations,
        boost_mode,
        transformed.boost_phase,
        transformed.rapid_boost_reached,
        transformed.boost_target_heating_c,
        transformed.boost_target_cooling_c,
        occupied.heating_c,
        occupied.cooling_c,
        unoccupied.heating_c,
        unoccupied.cooling_c,
    )


def build_fixed_fallback(
    *,
    direction: ActuationDirection,
    primary_air_valid: bool,
    primary_rh_valid: bool,
    fallback_policy_no_write: bool = False,
    heating_c: float = DEFAULT_USER_MIN_C,
    cooling_c: float = DEFAULT_USER_MAX_C,
    minimum_range_gap_c: float = DEFAULT_MINIMUM_RANGE_GAP_C,
) -> PolicyResult:
    """Build a separate fixed fallback without an ATHB result or profile transform."""

    if fallback_policy_no_write:
        return PolicyFailure("fallback_no_write")
    if not primary_air_valid:
        return PolicyFailure("primary_air_invalid")
    if not primary_rh_valid:
        return PolicyFailure("primary_rh_invalid")
    if not all(math.isfinite(value) for value in (heating_c, cooling_c, minimum_range_gap_c)):
        return PolicyFailure("invalid_fallback_configuration")
    if direction is ActuationDirection.RANGED and cooling_c - heating_c < max(
        1.0, minimum_range_gap_c
    ):
        return PolicyFailure("control_band_too_narrow")
    heating = heating_c if direction is not ActuationDirection.COOLING_ONLY else None
    cooling = cooling_c if direction is not ActuationDirection.HEATING_ONLY else None
    return PolicyTargets(
        heating,
        cooling,
        heating,
        cooling,
        heating,
        cooling,
        ControlProfile.COMFORT,
        True,
        "fallback",
        "fallback",
        None,
        None,
        ("fixed_fallback",),
        BoostMode.OFF,
        "fallback",
        occupied_heating_c=heating,
        occupied_cooling_c=cooling,
        unoccupied_heating_c=heating,
        unoccupied_cooling_c=cooling,
    )


def apply_calibration_and_user_bounds(
    *, requested_room_c: float, calibration_offset_c: float, user_min_c: float, user_max_c: float
) -> ActuatorBoundResult | PolicyFailure:
    """Move to actuator coordinates, then apply explicit user bounds."""

    values = (requested_room_c, calibration_offset_c, user_min_c, user_max_c)
    if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
        return PolicyFailure("invalid_bound_configuration")
    if not -3.0 <= calibration_offset_c <= 3.0 or user_min_c >= user_max_c:
        return PolicyFailure("invalid_bound_configuration")
    calibrated = requested_room_c + calibration_offset_c
    bounded = min(max(calibrated, user_min_c), user_max_c)
    limitations = ("user_bound_applied",) if bounded != calibrated else ()
    return ActuatorBoundResult(requested_room_c, calibrated, bounded, limitations)


def boost_expiry(
    *, selected_at: datetime, duration: timedelta = DEFAULT_BOOST_DURATION
) -> datetime:
    """Return explicit boost expiry; callers persist it without restart extension."""

    if selected_at.tzinfo is None or selected_at.utcoffset() is None or duration <= timedelta(0):
        raise ValueError("boost selection and duration must be valid")
    return selected_at + duration


def check_cross_actuator_coordination(
    *,
    direction: ActuationDirection,
    proposed_room_c: float,
    opposing_targets: tuple[OpposingTarget, ...],
    minimum_range_gap_c: float = DEFAULT_MINIMUM_RANGE_GAP_C,
) -> CoordinationResult:
    """Fail closed when an opposing target cannot preserve the common room gap."""

    values = (proposed_room_c, minimum_range_gap_c)
    if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
        return CoordinationResult(False, "invalid_coordination_configuration")
    if direction is ActuationDirection.RANGED or minimum_range_gap_c < 1.0:
        return CoordinationResult(False, "invalid_coordination_configuration")
    opposite = (
        ActuationDirection.COOLING_ONLY
        if direction is ActuationDirection.HEATING_ONLY
        else ActuationDirection.HEATING_ONLY
    )
    for target in sorted(opposing_targets, key=lambda item: item.target_identity):
        if target.direction is not opposite:
            continue
        established = (
            target.intended_room_c if target.owned and target.available else target.observed_room_c
        )
        if established is None or isinstance(established, bool) or not math.isfinite(established):
            return CoordinationResult(False, "opposing_target_unknown")
        if direction is ActuationDirection.HEATING_ONLY:
            conflict = established - proposed_room_c < minimum_range_gap_c
        else:
            conflict = proposed_room_c - established < minimum_range_gap_c
        if conflict:
            return CoordinationResult(False, "cross_actuator_conflict")
    return CoordinationResult(True, None)


def cooling_dewpoint_eligible(
    *, normalized_room_c: float, dewpoint_constraint_c: float
) -> CoordinationResult:
    """Apply fallback dew-point protection after final room normalization."""

    values = (normalized_room_c, dewpoint_constraint_c)
    if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
        return CoordinationResult(False, "invalid_dewpoint_constraint")
    if normalized_room_c < dewpoint_constraint_c:
        return CoordinationResult(False, "fallback_below_dewpoint")
    return CoordinationResult(True, None)
