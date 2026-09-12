"""Immutable contracts shared by the ATHB core and later integration layers."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from enum import StrEnum
from typing import Literal


class Provenance(StrEnum):
    """Origin of an observation or derived value."""

    MEASURED = "measured"
    DECLARED = "declared"
    ESTIMATED = "estimated"


class ObservationValidity(StrEnum):
    """Validation state of an observation."""

    VALID = "valid"
    INVALID = "invalid"
    STALE = "stale"


class ApplicabilityReason(StrEnum):
    """Scientific applicability labels for successful evaluations."""

    LIMITED_EVIDENCE = "limited_evidence"
    EXTRAPOLATED = "extrapolated"


class NumericalStatus(StrEnum):
    """Top-level numerical outcome state."""

    SUCCESS = "success"
    FAILURE = "failure"


class NumericalFailureCode(StrEnum):
    """Typed failures emitted before or during a forward evaluation."""

    BOOLEAN_INPUT = "boolean_input"
    NON_NUMERIC = "non_numeric"
    NON_FINITE = "non_finite"
    OUTSIDE_ENGINEERING_DOMAIN = "outside_engineering_domain"
    HEAT_BALANCE_NON_CONVERGENCE = "heat_balance_non_convergence"
    EVALUATION_BUDGET_EXCEEDED = "evaluation_budget_exceeded"


class RootName(StrEnum):
    """Stable semantic root order."""

    LOWER_COMFORT = "lower_comfort"
    HEATING_CONTROL = "heating_control"
    THERMAL_NEUTRAL = "thermal_neutral"
    COOLING_CONTROL = "cooling_control"
    UPPER_COMFORT = "upper_comfort"


class RootFailureCode(StrEnum):
    """Typed inverse-result failures reserved by the numerical contract."""

    BELOW_SEARCH_DOMAIN = "below_search_domain"
    ABOVE_SEARCH_DOMAIN = "above_search_domain"
    MOISTURE_LIMITED_NO_SOLUTION = "moisture_limited_no_solution"
    NO_BRACKET = "no_bracket"
    MULTIPLE_BRACKETS = "multiple_brackets"
    NON_MONOTONIC = "non_monotonic"
    NON_FINITE = "non_finite"
    HEAT_BALANCE_NON_CONVERGENCE = "heat_balance_non_convergence"
    ITERATION_LIMIT = "iteration_limit"
    EVALUATION_BUDGET_EXCEEDED = "evaluation_budget_exceeded"
    OUTSIDE_ENGINEERING_DOMAIN = "outside_engineering_domain"


class MoistureFailureCode(StrEnum):
    """Typed failures from the physical moisture transformation."""

    BOOLEAN_INPUT = "boolean_input"
    NON_NUMERIC = "non_numeric"
    NON_FINITE = "non_finite"
    OUTSIDE_PSYCHROMETRIC_DOMAIN = "outside_psychrometric_domain"
    INVALID_RELATIVE_HUMIDITY = "invalid_relative_humidity"
    BELOW_PSYCHROMETRIC_DOMAIN = "below_psychrometric_domain"
    MOISTURE_LIMITED_NO_SOLUTION = "moisture_limited_no_solution"
    INVALID_TOTAL_PRESSURE = "invalid_total_pressure"


class DewPointStatus(StrEnum):
    """Outcome label for the dew/frost-point inverse."""

    SOLVED = "solved"
    DRY_LIMIT = "dry_limit"


class RadiantFailureCode(StrEnum):
    """Typed failures from radiant calculations."""

    BOOLEAN_INPUT = "boolean_input"
    NON_NUMERIC = "non_numeric"
    NON_FINITE = "non_finite"
    OUTSIDE_SOURCE_DOMAIN = "outside_source_domain"
    INVALID_GLOBE_CONFIGURATION = "invalid_globe_configuration"
    INVALID_VIEW_FACTOR = "invalid_view_factor"
    TOO_MANY_SURFACES = "too_many_surfaces"
    MISSING_SURFACE_DATA = "missing_surface_data"
    NONPOSITIVE_RADICAND = "nonpositive_radicand"


class ComfortStrategy(StrEnum):
    """Fixed product comfort levels, ordered from efficiency to comfort."""

    ECO = "eco"
    EFFICIENT = "efficient"
    BALANCED = "balanced"
    COMFORT = "comfort"
    NEAR_NEUTRAL = "near_neutral"


class BoostMode(StrEnum):
    """Independent temporary Boost delivery modes."""

    OFF = "off"
    ADAPTIVE = "adaptive"
    RAPID = "rapid"


class ActuationDirection(StrEnum):
    """Directional roots required by a configured actuator shape."""

    HEATING_ONLY = "heating_only"
    COOLING_ONLY = "cooling_only"
    RANGED = "ranged"


class CriticalEligibilityMode(StrEnum):
    """Declared command eligibility for a critical local-air location."""

    MONITORING = "monitoring"
    HEATING = "heating"
    COOLING = "cooling"
    BOTH = "both"


class ControlProfile(StrEnum):
    """Internal resolved occupancy policy plus legacy wire values."""

    AUTO = "auto"
    COMFORT = "comfort"
    ECO = "eco"
    BOOST = "boost"


class EcoIntensity(StrEnum):
    """Post-solve widening applied while the resolved profile is Eco."""

    MILD = "mild"
    WORKDAY = "workday"
    DEEP = "deep"
    CUSTOM = "custom"


class TargetShape(StrEnum):
    """Shape of a normalized climate intent."""

    SCALAR = "scalar"
    RANGE = "range"


class DispatchStatus(StrEnum):
    """Command dispatch state."""

    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class AcknowledgementStatus(StrEnum):
    """Command acknowledgement state."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    INFERRED_ACKNOWLEDGED = "inferred_acknowledged"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AutomaticClothing:
    """Explicit automatic-clothing tag; never a false-like number."""

    kind: Literal["automatic"] = dataclass_field(default="automatic", init=False)


@dataclass(frozen=True, slots=True)
class FixedClothing:
    """Explicit fixed-clothing tag."""

    clo: float
    kind: Literal["fixed"] = dataclass_field(default="fixed", init=False)


type Clothing = AutomaticClothing | FixedClothing
AUTOMATIC_CLOTHING = AutomaticClothing()


@dataclass(frozen=True, slots=True)
class AthbInputs:
    """Complete scalar input vector for one forward ATHB evaluation."""

    tdb_c: float
    tr_c: float
    relative_air_speed_m_s: float
    rh_pct: float
    met: float
    running_mean_c: float
    clothing: Clothing


@dataclass(frozen=True, slots=True)
class NumericalFailure:
    """Typed forward-evaluation failure with no fabricated numeric payload."""

    code: NumericalFailureCode
    field: str | None
    detail: str
    numerical_status: Literal[NumericalStatus.FAILURE] = dataclass_field(
        default=NumericalStatus.FAILURE,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class HeatBalanceSuccess:
    """Successful scalar heat-load calculation."""

    thermal_load_w_m2: float
    clothing_surface_temperature_c: float
    convective_heat_transfer_w_m2_k: float
    clothing_iteration_updates: int
    convection_mode: str
    numerical_status: Literal[NumericalStatus.SUCCESS] = dataclass_field(
        default=NumericalStatus.SUCCESS,
        init=False,
    )


type HeatBalanceResult = HeatBalanceSuccess | NumericalFailure


@dataclass(frozen=True, slots=True)
class AthbSuccess:
    """Successful unrounded and public ATHB result."""

    sensation_vote: float
    public_sensation_vote: float
    adapted_met: float
    effective_clo: float
    relative_air_speed_m_s: float
    thermal_load_w_m2: float
    applicability_reasons: tuple[ApplicabilityReason, ...]
    numerical_status: Literal[NumericalStatus.SUCCESS] = dataclass_field(
        default=NumericalStatus.SUCCESS,
        init=False,
    )


type AthbResult = AthbSuccess | NumericalFailure


@dataclass(frozen=True, slots=True)
class RootSuccess:
    """One solved semantic root in room and local coordinates."""

    name: RootName
    requested_vote: float
    mapped_room_temperature_c: float
    local_candidate_temperature_c: float
    residual: float
    bracket_width_c: float
    evaluation_count: int
    applicability_reasons: tuple[ApplicabilityReason, ...] = ()


@dataclass(frozen=True, slots=True)
class RootFailure:
    """One attempted semantic root that has no numeric substitute."""

    name: RootName
    requested_vote: float
    failure: RootFailureCode
    evaluation_count: int
    detail: str


type RootResult = RootSuccess | RootFailure


@dataclass(frozen=True, slots=True)
class RootSet:
    """All five semantic roots in stable contract order."""

    lower_comfort: RootResult
    heating_control: RootResult
    thermal_neutral: RootResult
    cooling_control: RootResult
    upper_comfort: RootResult


@dataclass(frozen=True, slots=True)
class Observation:
    """Validated or rejected source observation with explicit provenance."""

    source_identity: str
    value: float | None
    unit: str
    observed_at: datetime | None
    received_at: datetime | None
    provenance: Provenance
    validity: ObservationValidity
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MeasuredAirTemperature:
    """A measured air temperature; never interchangeable with a surface."""

    value_c: float
    provenance: Literal[Provenance.MEASURED] = dataclass_field(
        default=Provenance.MEASURED, init=False
    )


@dataclass(frozen=True, slots=True)
class MeasuredRelativeHumidity:
    """A measured starting relative-humidity state."""

    value_pct: float
    provenance: Literal[Provenance.MEASURED] = dataclass_field(
        default=Provenance.MEASURED, init=False
    )


@dataclass(frozen=True, slots=True)
class DeclaredRelativeHumidity:
    """An explicit RH declaration, not a fabricated measurement."""

    value_pct: float
    provenance: Literal[Provenance.DECLARED] = dataclass_field(
        default=Provenance.DECLARED, init=False
    )


type RelativeHumiditySource = MeasuredRelativeHumidity | DeclaredRelativeHumidity


@dataclass(frozen=True, slots=True)
class MeasuredMeanRadiantTemperature:
    """A direct measurement of mean radiant temperature."""

    value_c: float
    provenance: Literal[Provenance.MEASURED] = dataclass_field(
        default=Provenance.MEASURED, init=False
    )


@dataclass(frozen=True, slots=True)
class MeasuredGlobeTemperature:
    """A globe-temperature observation used to derive MRT."""

    value_c: float
    provenance: Literal[Provenance.MEASURED] = dataclass_field(
        default=Provenance.MEASURED, init=False
    )


@dataclass(frozen=True, slots=True)
class MeasuredSurfaceTemperature:
    """A measured surface temperature."""

    value_c: float | None
    provenance: Literal[Provenance.MEASURED] = dataclass_field(
        default=Provenance.MEASURED, init=False
    )


@dataclass(frozen=True, slots=True)
class ModelledSurfaceTemperature:
    """A modelled surface estimate, physically distinct from air and MRT."""

    value_c: float | None
    provenance: Literal[Provenance.ESTIMATED] = dataclass_field(
        default=Provenance.ESTIMATED, init=False
    )


@dataclass(frozen=True, slots=True)
class DerivedMeanRadiantTemperature:
    """An MRT value derived from other physical observations."""

    value_c: float
    reasons: tuple[str, ...]
    provenance: Literal[Provenance.ESTIMATED] = dataclass_field(
        default=Provenance.ESTIMATED, init=False
    )


@dataclass(frozen=True, slots=True)
class CriticalAirLocation:
    """Explicitly typed local-air source for later location policy."""

    location_id: str
    air_temperature: MeasuredAirTemperature


@dataclass(frozen=True, slots=True)
class EnvironmentalSnapshot:
    """Coherent immutable environmental input snapshot for later phases."""

    generation: int
    primary: tuple[Observation, ...]
    critical_locations: tuple[tuple[Observation, ...], ...]
    radiant_model: str
    moisture_states: tuple[str, ...]
    activity_met: float
    clothing: Clothing
    air_speed_m_s: float
    running_mean_c: float
    history_quality: str
    input_expiries: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class LocationResult:
    """Data-only numerical result for one primary or critical location."""

    location_id: str
    current_vote: AthbResult
    roots: RootSet
    applicability: tuple[ApplicabilityReason, ...]
    eligible_for: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Data-only policy output; Phase 1 implements no policy algorithm."""

    snapshot_generation: int
    comfort_strategy: ComfortStrategy
    inward_fraction: float
    heating_control_vote: float
    cooling_control_vote: float
    profile: ControlProfile
    governing_locations: tuple[str, ...]
    primary_five_roots: RootSet
    critical_adjusted_targets_c: tuple[float | None, float | None]
    transformed_targets_c: tuple[float | None, float | None]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalizedIntent:
    """Data-only normalized actuator intent for later broker phases."""

    target_identity: str
    shape: TargetShape
    ha_unit: str
    scalar_target: float | None
    range_target: tuple[float, float] | None
    capability_generation: int
    ownership_revision: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Data-only dispatch and acknowledgement result."""

    command_id: str
    dispatch_status: DispatchStatus
    acknowledgement_status: AcknowledgementStatus
    reason: str


@dataclass(frozen=True, slots=True)
class ZoneSnapshot:
    """Coherent zone publication contract for later orchestration."""

    sequence: int
    calculation: tuple[LocationResult, ...]
    policy: PolicyDecision | None
    actuator_states: tuple[str, ...]
    quality: tuple[str, ...]
