"""Critical local-air assembly, filtering, warm-up, and mapped roots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .contracts import (
    Clothing,
    CriticalEligibilityMode,
    DeclaredRelativeHumidity,
    MeasuredAirTemperature,
    MeasuredRelativeHumidity,
    RelativeHumiditySource,
    RootSet,
    RootSuccess,
)
from .inverse import (
    EvaluationBudget,
    InverseLocationContext,
    RadiantModel,
    StrategyVotes,
    solve_five_roots,
)
from .psychrometrics import (
    MoistureFailure,
    MoistureState,
    candidate_relative_humidity,
    moisture_state,
)
from .radiant import DirectRadiantModel, UniformRadiantModel

FILTER_TIME_CONSTANT_SECONDS = 600.0
ELIGIBILITY_WARMUP_SECONDS = 600.0
MINIMUM_WARMUP_REPORTS = 3
RAW_DELTA_OUTLIER_C = 6.0
MAX_EFFECTIVE_DELTA_C = 3.0
MAX_CRITICAL_LOCATIONS = 8


@dataclass(frozen=True, slots=True)
class CriticalDeltaState:
    """Persistent filter state for one eligibility window."""

    window_started_at: datetime | None = None
    last_report_at: datetime | None = None
    filtered_delta_c: float | None = None
    raw_delta_c: float | None = None
    report_count: int = 0


@dataclass(frozen=True, slots=True)
class CriticalDeltaUpdate:
    """Filtered delta and explicit command eligibility diagnostics."""

    state: CriticalDeltaState
    effective_delta_c: float | None
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CriticalLocationFailure:
    """A location cannot be assembled without inventing physical inputs."""

    location_id: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class PreparedCriticalLocation:
    """One eligible or monitoring critical location ready for evaluation."""

    location_id: str
    mode: CriticalEligibilityMode
    context: InverseLocationContext
    filtered_delta_c: float
    effective_delta_c: float
    control_eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CriticalLocationSolution:
    """Mapped five-root result using the primary location's exact votes."""

    location_id: str
    mode: CriticalEligibilityMode
    roots: RootSet
    effective_delta_c: float
    control_eligible: bool
    reasons: tuple[str, ...]


type CriticalMoistureResult = MoistureState | MoistureFailure
type PreparedCriticalResult = PreparedCriticalLocation | CriticalLocationFailure


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _timestamp_is_usable(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def update_critical_delta(
    state: CriticalDeltaState,
    *,
    primary_air_temperature_c: float,
    local_air_temperature_c: float,
    observed_at: datetime,
    valid: bool = True,
    fresh: bool = True,
) -> CriticalDeltaUpdate:
    """Advance the signed exponential delta filter or reset its warm-up."""

    primary = _finite(primary_air_temperature_c)
    local = _finite(local_air_temperature_c)
    if not valid or primary is None or local is None or not _timestamp_is_usable(observed_at):
        return CriticalDeltaUpdate(
            CriticalDeltaState(),
            None,
            False,
            ("critical_observation_invalid",),
        )
    if not fresh:
        return CriticalDeltaUpdate(
            CriticalDeltaState(),
            None,
            False,
            ("critical_observation_stale",),
        )
    raw_delta = primary - local
    if abs(raw_delta) > RAW_DELTA_OUTLIER_C:
        return CriticalDeltaUpdate(
            CriticalDeltaState(raw_delta_c=raw_delta),
            None,
            False,
            ("critical_delta_outlier",),
        )

    if (
        state.window_started_at is None
        or state.last_report_at is None
        or state.filtered_delta_c is None
    ):
        next_state = CriticalDeltaState(
            window_started_at=observed_at,
            last_report_at=observed_at,
            filtered_delta_c=raw_delta,
            raw_delta_c=raw_delta,
            report_count=1,
        )
    else:
        elapsed = (observed_at - state.last_report_at).total_seconds()
        if elapsed <= 0.0:
            effective = max(
                -MAX_EFFECTIVE_DELTA_C,
                min(MAX_EFFECTIVE_DELTA_C, state.filtered_delta_c),
            )
            return CriticalDeltaUpdate(
                state,
                effective,
                False,
                ("critical_report_not_newer",),
            )
        alpha = 1.0 - math.exp(-elapsed / FILTER_TIME_CONSTANT_SECONDS)
        filtered = state.filtered_delta_c + alpha * (raw_delta - state.filtered_delta_c)
        next_state = CriticalDeltaState(
            window_started_at=state.window_started_at,
            last_report_at=observed_at,
            filtered_delta_c=filtered,
            raw_delta_c=raw_delta,
            report_count=state.report_count + 1,
        )

    assert next_state.window_started_at is not None
    assert next_state.filtered_delta_c is not None
    warmup_elapsed = (observed_at - next_state.window_started_at).total_seconds()
    eligible = (
        warmup_elapsed >= ELIGIBILITY_WARMUP_SECONDS
        and next_state.report_count >= MINIMUM_WARMUP_REPORTS
    )
    effective = max(
        -MAX_EFFECTIVE_DELTA_C,
        min(MAX_EFFECTIVE_DELTA_C, next_state.filtered_delta_c),
    )
    reasons: list[str] = []
    if effective != next_state.filtered_delta_c:
        reasons.append("critical_delta_bounded")
    if not eligible:
        reasons.append("critical_location_warming_up")
    return CriticalDeltaUpdate(next_state, effective, eligible, tuple(reasons))


def critical_moisture_state(
    *,
    primary_moisture: MoistureState,
    local_air_temperature: MeasuredAirTemperature,
    local_relative_humidity: RelativeHumiditySource | None = None,
) -> CriticalMoistureResult:
    """Use local RH when supplied, otherwise preserve primary vapor pressure."""

    if local_relative_humidity is not None:
        return moisture_state(local_air_temperature.value_c, local_relative_humidity)
    candidate = candidate_relative_humidity(primary_moisture, local_air_temperature.value_c)
    if isinstance(candidate, MoistureFailure):
        return candidate
    return MoistureState(
        starting_air_temperature_c=local_air_temperature.value_c,
        starting_relative_humidity_pct=candidate.relative_humidity_pct,
        vapor_pressure_pa=primary_moisture.vapor_pressure_pa,
        provenance=primary_moisture.provenance,
    )


def prepare_critical_location(
    *,
    location_id: str,
    mode: CriticalEligibilityMode,
    delta: CriticalDeltaUpdate,
    current_room_air_temperature_c: float,
    local_air_temperature: MeasuredAirTemperature,
    primary_moisture: MoistureState,
    zone_radiant: RadiantModel,
    relative_air_speed_m_s: float,
    met: float,
    running_mean_c: float,
    clothing: Clothing,
    local_relative_humidity: MeasuredRelativeHumidity | DeclaredRelativeHumidity | None = None,
    local_radiant: DirectRadiantModel | None = None,
) -> PreparedCriticalResult:
    """Assemble one critical context without conflating air, surface, or MRT."""

    if not location_id:
        return CriticalLocationFailure(location_id, "invalid_location_id", "location ID is empty")
    if delta.effective_delta_c is None or delta.state.filtered_delta_c is None:
        return CriticalLocationFailure(
            location_id,
            delta.reasons[0] if delta.reasons else "critical_location_unavailable",
            "critical location has no usable filtered delta",
        )
    local_moisture = critical_moisture_state(
        primary_moisture=primary_moisture,
        local_air_temperature=local_air_temperature,
        local_relative_humidity=local_relative_humidity,
    )
    if isinstance(local_moisture, MoistureFailure):
        return CriticalLocationFailure(
            location_id,
            local_moisture.code.value,
            local_moisture.detail,
        )
    if local_radiant is not None:
        radiant: RadiantModel = local_radiant
        shared_zone_field = False
        reasons = (*delta.reasons, "local_direct_mrt")
    elif isinstance(zone_radiant, UniformRadiantModel):
        radiant = UniformRadiantModel()
        shared_zone_field = False
        reasons = (*delta.reasons, "local_uniform_mrt")
    else:
        radiant = zone_radiant
        shared_zone_field = True
        reasons = (*delta.reasons, "shared_zone_radiant_field")
    if not delta.eligible and mode is not CriticalEligibilityMode.MONITORING:
        reasons = (*reasons, "control_ineligible_during_warmup")
    return PreparedCriticalLocation(
        location_id,
        mode,
        InverseLocationContext(
            current_room_air_temperature_c=current_room_air_temperature_c,
            current_local_air_temperature_c=local_air_temperature.value_c,
            moisture=local_moisture,
            radiant=radiant,
            relative_air_speed_m_s=relative_air_speed_m_s,
            met=met,
            running_mean_c=running_mean_c,
            clothing=clothing,
            local_delta_c=delta.effective_delta_c,
            radiant_uses_room_coordinate=shared_zone_field,
        ),
        delta.state.filtered_delta_c,
        delta.effective_delta_c,
        delta.eligible and mode is not CriticalEligibilityMode.MONITORING,
        reasons,
    )


def solve_critical_location(
    location: PreparedCriticalLocation,
    votes: StrategyVotes,
    *,
    budget: EvaluationBudget,
) -> CriticalLocationSolution:
    """Solve the primary strategy's exact votes in room coordinates."""

    roots = solve_five_roots(location.context, votes, budget=budget)
    complete = all(
        isinstance(root, RootSuccess)
        for root in (
            roots.lower_comfort,
            roots.heating_control,
            roots.thermal_neutral,
            roots.cooling_control,
            roots.upper_comfort,
        )
    )
    reasons = location.reasons
    if not complete:
        reasons = (*reasons, "critical_roots_incomplete")
    return CriticalLocationSolution(
        location.location_id,
        location.mode,
        roots,
        location.effective_delta_c,
        location.control_eligible and complete,
        reasons,
    )


def validate_critical_location_count(count: int) -> str | None:
    """Return a typed configuration reason when the maximum is exceeded."""

    if count < 0 or count > MAX_CRITICAL_LOCATIONS:
        return "too_many_critical_locations"
    return None
