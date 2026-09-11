"""Tests for immutable Phase 1 and reserved later-phase contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.athb.core.contracts import (
    AUTOMATIC_CLOTHING,
    AcknowledgementStatus,
    ApplicabilityReason,
    AthbInputs,
    AthbSuccess,
    AutomaticClothing,
    ComfortStrategy,
    CommandOutcome,
    ControlProfile,
    DispatchStatus,
    EnvironmentalSnapshot,
    FixedClothing,
    HeatBalanceSuccess,
    LocationResult,
    NormalizedIntent,
    NumericalFailure,
    NumericalFailureCode,
    NumericalStatus,
    Observation,
    ObservationValidity,
    PolicyDecision,
    Provenance,
    RootFailure,
    RootFailureCode,
    RootName,
    RootSet,
    RootSuccess,
    TargetShape,
    ZoneSnapshot,
)


def _root_set() -> RootSet:
    successes = [
        RootSuccess(name, vote, 20.0 + index, 20.0 + index, 0.0, 1e-10, 12)
        for index, (name, vote) in enumerate(
            (
                (RootName.LOWER_COMFORT, -0.5),
                (RootName.HEATING_CONTROL, -0.25),
                (RootName.THERMAL_NEUTRAL, 0.0),
                (RootName.COOLING_CONTROL, 0.25),
                (RootName.UPPER_COMFORT, 0.5),
            )
        )
    ]
    return RootSet(*successes)


def test_contract_enums_have_stable_wire_values() -> None:
    assert tuple(Provenance) == (
        Provenance.MEASURED,
        Provenance.DECLARED,
        Provenance.ESTIMATED,
    )
    assert [reason.value for reason in ApplicabilityReason] == [
        "limited_evidence",
        "extrapolated",
    ]
    assert [name.value for name in RootName] == [
        "lower_comfort",
        "heating_control",
        "thermal_neutral",
        "cooling_control",
        "upper_comfort",
    ]
    assert {failure.value for failure in RootFailureCode} == {
        "above_search_domain",
        "below_search_domain",
        "evaluation_budget_exceeded",
        "heat_balance_non_convergence",
        "iteration_limit",
        "moisture_limited_no_solution",
        "multiple_brackets",
        "no_bracket",
        "non_finite",
        "non_monotonic",
        "outside_engineering_domain",
    }
    assert [strategy.value for strategy in ComfortStrategy] == [
        "efficient",
        "balanced",
        "comfort",
    ]
    assert [profile.value for profile in ControlProfile] == [
        "auto",
        "comfort",
        "eco",
        "boost",
    ]
    assert [status.value for status in NumericalStatus] == ["success", "failure"]


def test_automatic_clothing_is_an_explicit_immutable_tag() -> None:
    assert isinstance(AUTOMATIC_CLOTHING, AutomaticClothing)
    assert AUTOMATIC_CLOTHING.kind == "automatic"
    assert AUTOMATIC_CLOTHING is not False
    with pytest.raises(FrozenInstanceError):
        AUTOMATIC_CLOTHING.kind = "fixed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        AutomaticClothing(kind="fixed")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        FixedClothing(0.7, kind="automatic")  # type: ignore[call-arg]


def test_all_contract_records_are_frozen_and_slotted() -> None:
    contract_types = (
        AutomaticClothing,
        FixedClothing,
        AthbInputs,
        NumericalFailure,
        HeatBalanceSuccess,
        AthbSuccess,
        RootSuccess,
        RootFailure,
        RootSet,
        Observation,
        EnvironmentalSnapshot,
        LocationResult,
        PolicyDecision,
        NormalizedIntent,
        CommandOutcome,
        ZoneSnapshot,
    )

    for contract_type in contract_types:
        assert is_dataclass(contract_type)
        assert contract_type.__dataclass_params__.frozen
        assert hasattr(contract_type, "__slots__")


def test_named_roots_retain_stable_semantic_order_and_typed_failure() -> None:
    roots = _root_set()
    assert [field.name for field in fields(RootSet)] == [
        "lower_comfort",
        "heating_control",
        "thermal_neutral",
        "cooling_control",
        "upper_comfort",
    ]
    assert [
        root.name
        for root in (
            roots.lower_comfort,
            roots.heating_control,
            roots.thermal_neutral,
            roots.cooling_control,
            roots.upper_comfort,
        )
    ] == list(RootName)

    failure = RootFailure(
        RootName.HEATING_CONTROL,
        -0.25,
        RootFailureCode.MOISTURE_LIMITED_NO_SOLUTION,
        257,
        "candidate humidity exceeds saturation",
    )
    partial = RootSet(
        roots.lower_comfort,
        failure,
        roots.thermal_neutral,
        roots.cooling_control,
        roots.upper_comfort,
    )
    assert partial.heating_control.failure is RootFailureCode.MOISTURE_LIMITED_NO_SOLUTION
    assert not hasattr(partial.heating_control, "mapped_room_temperature_c")


def test_reserved_later_phase_contracts_are_data_only_and_composable() -> None:
    now = datetime(2026, 9, 11, 6, 36, 5, tzinfo=UTC)
    observation = Observation(
        source_identity="sensor.room_temperature",
        value=20.0,
        unit="degC",
        observed_at=now,
        received_at=now,
        provenance=Provenance.MEASURED,
        validity=ObservationValidity.VALID,
    )
    environment = EnvironmentalSnapshot(
        generation=3,
        primary=(observation,),
        critical_locations=(),
        radiant_model="uniform",
        moisture_states=("measured",),
        activity_met=1.1,
        clothing=AUTOMATIC_CLOTHING,
        air_speed_m_s=0.1,
        running_mean_c=5.0,
        history_quality="complete_history",
        input_expiries=(now + timedelta(minutes=5),),
    )
    result = AthbSuccess(-0.17, -0.17, 1.08, 0.77, 0.13, -20.25, ())
    roots = _root_set()
    location = LocationResult("primary", result, roots, (), ("heating", "cooling"))
    policy = PolicyDecision(
        3,
        ComfortStrategy.BALANCED,
        0.5,
        -0.25,
        0.25,
        ControlProfile.AUTO,
        ("primary",),
        roots,
        (19.36, 23.48),
        (19.36, 23.48),
        (),
    )
    intent = NormalizedIntent(
        "climate.study",
        TargetShape.RANGE,
        "degC",
        None,
        (19.5, 23.0),
        4,
        2,
        now + timedelta(minutes=1),
    )
    outcome = CommandOutcome(
        "command-1",
        DispatchStatus.DISPATCHED,
        AcknowledgementStatus.PENDING,
        "awaiting_feedback",
    )
    snapshot = ZoneSnapshot(8, (location,), policy, (outcome.reason,), ("ready",))

    assert environment.primary == (observation,)
    assert intent.range_target == (19.5, 23.0)
    assert snapshot.policy is policy
    with pytest.raises(FrozenInstanceError):
        observation.value = 21.0  # type: ignore[misc]


def test_numerical_failure_has_no_numeric_success_payload() -> None:
    failure = NumericalFailure(
        NumericalFailureCode.NON_FINITE,
        "tdb_c",
        "tdb_c must be finite",
    )

    assert failure.numerical_status is NumericalStatus.FAILURE
    assert not hasattr(failure, "sensation_vote")


def test_numerical_status_is_fixed_by_concrete_result_type() -> None:
    status_fields = {
        contract_type: next(
            field for field in fields(contract_type) if field.name == "numerical_status"
        )
        for contract_type in (NumericalFailure, HeatBalanceSuccess, AthbSuccess)
    }

    assert all(not status_field.init for status_field in status_fields.values())
    with pytest.raises(TypeError):
        NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            None,
            "failure",
            numerical_status=NumericalStatus.SUCCESS,
        )  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        HeatBalanceSuccess(
            0.0,
            0.0,
            0.0,
            0,
            "forced",
            numerical_status=NumericalStatus.FAILURE,
        )  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AthbSuccess(
            0.0,
            0.0,
            1.0,
            1.0,
            0.1,
            0.0,
            (),
            numerical_status=NumericalStatus.FAILURE,
        )  # type: ignore[call-arg]
