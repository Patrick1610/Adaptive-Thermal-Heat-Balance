"""Deterministic full-path Virtual Installation runner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.athb.adapters.broker import (
    BrokerPreflight,
    CommandBroker,
    ContextToken,
    NormalizedIntent,
    PendingCommand,
    intent_fingerprint,
)
from custom_components.athb.adapters.sources import StateValue
from custom_components.athb.adapters.storage import (
    ControlLoadResult,
    ControlStoreState,
    StoredActuator,
    StoredCommand,
    prepare_startup_recovery,
)
from custom_components.athb.calculation import (
    CapturedCriticalLocation,
    CapturedTarget,
    CapturedZoneSnapshot,
    RuntimeCalculation,
    calculate_runtime_snapshot,
)
from custom_components.athb.controller import ZoneController
from custom_components.athb.core.climate import (
    ClimateCapabilitySnapshot,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    TemperatureUnit,
)
from custom_components.athb.core.contracts import (
    AcknowledgementStatus,
    DispatchStatus,
    RootFailure,
    RootSuccess,
    TargetShape,
)
from custom_components.athb.core.history import (
    HistoryQuality,
    OutdoorSample,
    integrate_outdoor_history,
    running_mean,
)
from custom_components.athb.core.locations import CriticalDeltaState, CriticalDeltaUpdate
from custom_components.athb.core.ownership import (
    DataReadiness,
    FeedbackObservation,
    Ownership,
    OwnershipEvent,
    OwnershipState,
    TargetReadiness,
    initial_ownership,
    reduce_ownership,
)

ROOT = Path(__file__).parents[2]
GOLDENS = json.loads(
    (ROOT / "tests/fixtures/reference/vi_numerical_v1.json").read_text(encoding="utf-8")
)["goldens"]
ROOT_NAMES = (
    "lower_comfort",
    "heating_control",
    "thermal_neutral",
    "cooling_control",
    "upper_comfort",
)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    name: str
    passed: bool
    input_summary: dict[str, Any]
    numerical: dict[str, Any]
    policy: dict[str, Any]
    broker: dict[str, Any]
    execution: dict[str, Any]
    failed_assertions: tuple[str, ...] = ()


class CapturedService:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], ContextToken]] = []

    async def async_set_temperature(
        self, payload: dict[str, object], context: ContextToken
    ) -> None:
        self.calls.append((payload, context))


class MemoryPersistence:
    def __init__(self) -> None:
        self.pending: PendingCommand | None = None
        self.events: list[str] = []

    async def async_persist_pending(self, command: PendingCommand) -> bool:
        self.pending = command
        self.events.append("persisted")
        return True

    async def async_mark_dispatched(self, command: PendingCommand) -> bool:
        self.pending = command
        self.events.append("dispatched")
        return True

    async def async_resolve(self, command: PendingCommand, reason: str) -> bool:
        assert self.pending is None or self.pending.command_id == command.command_id
        self.pending = None
        self.events.append(reason)
        return True


def _history(scenario: dict[str, Any]) -> tuple[float | None, str, int]:
    clock = datetime.fromisoformat(scenario["clock"])
    timezone = ZoneInfo(scenario["timezone"])
    local_date = clock.astimezone(timezone).date()
    day_start = datetime.combine(local_date, time.min, timezone).astimezone(UTC)
    earliest = datetime.combine(local_date - timedelta(days=7), time.min, timezone).astimezone(UTC)
    count = scenario["environmental_state"]["history_days"]
    selected_start = datetime.combine(
        local_date - timedelta(days=count), time.min, timezone
    ).astimezone(UTC)
    value = scenario["environmental_state"]["history_value_c"]
    samples = tuple(
        OutdoorSample(selected_start + timedelta(hours=index), value)
        for index in range(int((day_start - selected_start).total_seconds() // 3600) + 1)
        if selected_start + timedelta(hours=index) <= day_start
    )
    summaries = integrate_outdoor_history(
        samples=samples,
        timezone=scenario["timezone"],
        start_utc=earliest,
        end_utc=day_start,
    )
    result = running_mean(summaries=summaries, current_local_date=local_date)
    eligible = result.quality in {HistoryQuality.COMPLETE, HistoryQuality.PARTIAL}
    return result.value_c if eligible else None, result.quality.value, len(summaries)


def _state(entity_id: str, raw: object, unit: str, clock: datetime) -> StateValue | None:
    if raw is None:
        return None
    return StateValue(entity_id, raw, unit, clock, True, {}, None, None)


def _captured(scenario: dict[str, Any], *, history_days: int | None = None) -> CapturedZoneSnapshot:
    config = scenario["zone_configuration"]
    environment = dict(scenario["environmental_state"])
    if history_days is not None:
        environment["history_days"] = history_days
        scenario = {**scenario, "environmental_state": environment}
    target = scenario["target_capabilities"]
    clock = datetime.fromisoformat(scenario["clock"])
    running, quality, _ = _history(scenario)
    capability = ClimateCapabilitySnapshot(
        target["hvac_mode"],
        tuple(target["advertised_modes"]),
        target["supported_features"],
        target["min_temp"],
        target["max_temp"],
        target["step"],
        TemperatureUnit(target["unit"]),
        target["current_temperature"],
        target["current_low"],
        target["current_high"],
        available=target["available"],
        restored=target["restored"],
    )
    options = {
        "met": config["met"],
        "air_speed_m_s": config["air_speed_m_s"],
        "radiant_model": config["radiant_model"],
        "surface_view_factor": config["surface_view_factor"],
        "surface_modelled": config["surface_modelled"],
        "surface_f_rsi": config["surface_f_rsi"],
        "lower_comfort_vote": config["lower_vote"],
        "upper_comfort_vote": config["upper_vote"],
        "eco_intensity": config.get("eco_intensity", "custom"),
        "inactive_heating_temperature": config.get("inactive_heating_temperature", 18.0),
        "inactive_cooling_temperature": config.get("inactive_cooling_temperature", 26.0),
        "minimum_control_temperature": config["user_min_c"],
        "maximum_control_temperature": config["user_max_c"],
        "minimum_range_gap": config["minimum_range_gap_c"],
        "fallback_mode": config["fallback_mode"],
        "fallback_heating_c": config["fallback_heating_c"],
        "fallback_cooling_c": config["fallback_cooling_c"],
        "reject_extrapolation": config["reject_extrapolation"],
        "auto_mapping": target["auto_mapping"],
        f"calibration_{target['target_uuid']}": target["calibration_offset_c"],
    }
    critical = []
    try:
        primary = float(environment["primary_raw"])
    except TypeError, ValueError:
        primary = 0.0
    for item in config["critical_locations"]:
        delta = float(item["delta_c"])
        critical.append(
            CapturedCriticalLocation(
                item["location_id"],
                item["mode"],
                _state(item["entity_id"], primary - delta, "°C", clock),
                CriticalDeltaUpdate(
                    CriticalDeltaState(clock - timedelta(minutes=10), clock, delta, delta, 3),
                    delta,
                    True,
                    (),
                ),
            )
        )
    return CapturedZoneSnapshot(
        clock,
        _state(
            config["primary_entity"], environment["primary_raw"], environment["primary_unit"], clock
        ),
        _state(
            config["rh_entity"] or "sensor.rh", environment["rh_raw"], environment["rh_unit"], clock
        ),
        config["rh_declared"] if config["rh_mode"] == "declared" else None,
        _state(
            config["outdoor_entity"], environment["outdoor_raw"], environment["outdoor_unit"], clock
        ),
        _state("sensor.radiant", environment["radiant_raw"], environment["radiant_unit"], clock),
        running,
        quality,
        config["strategy"],
        "eco" if config["occupancy_state"] == "off" else "comfort",
        options,
        (
            CapturedTarget(
                target["target_uuid"], target["registry_identity"], target["entity_id"], capability
            ),
        ),
        tuple(critical),
        True,
        boost_mode=config["boost_mode"],
    )


def _normalized_intent(
    scenario: dict[str, Any], calculation: RuntimeCalculation, *, now: datetime
) -> NormalizedIntent | None:
    target = calculation.targets[0]
    result = target.result
    if target.mapping is None or target.suppression_reason is not None or result is None:
        return None
    normalized = result.normalized
    if normalized is None:
        return None
    cap = scenario["target_capabilities"]
    common = dict(
        target_entity_id=target.entity_id,
        target_registry_identity=target.registry_identity,
        direction=target.mapping.direction,
        meaningful_delta_ha=0.1,
        feedback_resolution_ha=cap["feedback_resolution"],
        entry_generation=1,
        input_generation=1,
        capability_generation=1,
        ownership_revision=1,
        external_revision=0,
        lease_owner="zone",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        explicit_transition=True,
    )
    if isinstance(normalized, NormalizedRangeTarget):
        return NormalizedIntent(
            shape=TargetShape.RANGE,
            temperature_ha=None,
            target_temp_low_ha=normalized.heating.normalized_ha,
            target_temp_high_ha=normalized.cooling.normalized_ha,
            normalized_room_c=None,
            normalized_low_room_c=normalized.heating.normalized_room_c,
            normalized_high_room_c=normalized.cooling.normalized_room_c,
            continuous_bounded_room_c=None,
            continuous_bounded_low_room_c=normalized.heating.bounded_c,
            continuous_bounded_high_room_c=normalized.cooling.bounded_c,
            step_ha=normalized.heating.grid.step_ha,
            **common,
        )
    assert isinstance(normalized, NormalizedScalarTarget)
    return NormalizedIntent(
        shape=TargetShape.SCALAR,
        temperature_ha=normalized.normalized_ha,
        target_temp_low_ha=None,
        target_temp_high_ha=None,
        normalized_room_c=normalized.normalized_room_c,
        normalized_low_room_c=None,
        normalized_high_room_c=None,
        continuous_bounded_room_c=normalized.bounded_c,
        continuous_bounded_low_room_c=None,
        continuous_bounded_high_room_c=None,
        step_ha=normalized.grid.step_ha,
        **common,
    )


async def _broker_run(
    scenario: dict[str, Any], calculation: RuntimeCalculation, *, stable: bool = False
) -> tuple[list[dict[str, object]], str, str, str]:
    now = datetime.fromisoformat(scenario["clock"])
    service = CapturedService()
    persistence = MemoryPersistence()
    target_identity = scenario["target_capabilities"]["registry_identity"]
    ownership_state = _scenario_ownership(scenario, calculation, now=now)

    def preflight(_identity: str) -> BrokerPreflight:
        return BrokerPreflight(
            target_identity,
            1,
            1,
            1,
            ownership_state.revision,
            ownership_state.ownership,
            ownership_state.target_readiness is TargetReadiness.AVAILABLE_SUPPORTED,
            ownership_state.data_readiness
            in {DataReadiness.READY, DataReadiness.DEGRADED_READY, DataReadiness.FALLBACK_READY},
            "zone",
        )

    counter = iter(range(1, 20))
    broker = CommandBroker(
        service=service,
        persistence=persistence,
        preflight=preflight,
        command_id_factory=lambda: f"cmd-{next(counter)}",
        context_factory=lambda: ContextToken(f"ctx-{next(counter)}", object()),
    )
    intent = _normalized_intent(scenario, calculation, now=now)
    if intent is None:
        return (
            [],
            calculation.targets[0].suppression_reason or "no_executable_target",
            "not_applicable",
            ownership_state.ownership.value,
        )
    intent = replace(
        intent,
        ownership_revision=ownership_state.revision,
        external_revision=ownership_state.external_revision,
    )
    outcome = await broker.async_submit(intent, now=now)
    if outcome.dispatch_status is DispatchStatus.DISPATCHED and persistence.pending is not None:
        pending = persistence.pending
        feedback = FeedbackObservation(
            target_identity,
            intent_fingerprint(intent),
            pending.context.context_id,
            None,
            1,
            1,
            1,
            ownership_state.revision,
            ownership_state.external_revision,
        )
        acknowledged = await broker.async_feedback(feedback, now=now)
        outcome = acknowledged
    if stable:
        service.calls.clear()
        stable_now = now + timedelta(minutes=30)
        stable_intent = _normalized_intent(scenario, calculation, now=stable_now)
        assert stable_intent is not None
        stable_intent = replace(
            stable_intent,
            ownership_revision=ownership_state.revision,
            external_revision=ownership_state.external_revision,
        )
        outcome = await broker.async_submit(stable_intent, now=stable_now)
    return (
        [payload for payload, _context in service.calls],
        outcome.reason,
        outcome.acknowledgement_status.value,
        ownership_state.ownership.value,
    )


def _scenario_ownership(
    scenario: dict[str, Any], calculation: RuntimeCalculation, *, now: datetime
) -> OwnershipState:
    """Reach the scenario's live state through the production ownership reducer."""

    identity = scenario["target_capabilities"]["registry_identity"]
    state = reduce_ownership(
        initial_ownership(identity),
        OwnershipEvent.ENABLE,
        now=now,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
        data_readiness=DataReadiness.READY,
    ).state
    state = reduce_ownership(
        state,
        OwnershipEvent.RECONCILED,
        now=now,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
        data_readiness=DataReadiness.READY,
    ).state
    target = calculation.targets[0]
    capability = target.capability
    readiness = (
        TargetReadiness.UNAVAILABLE
        if not capability.available
        else TargetReadiness.SUSPENDED_MODE
        if capability.hvac_mode in {"off", "auto"} and target.mapping is None
        else TargetReadiness.INCOMPATIBLE
        if target.mapping is None
        else TargetReadiness.AVAILABLE_SUPPORTED
    )
    if readiness is not TargetReadiness.AVAILABLE_SUPPORTED:
        state = reduce_ownership(
            state,
            OwnershipEvent.EXTERNAL_HVAC_MODE,
            now=now,
            target_readiness=readiness,
        ).state
    result = target.result
    data = (
        DataReadiness.HOLD_LAST_GOOD
        if calculation.hold_condition is not None and (result is None or result.normalized is None)
        else DataReadiness.INVALID
        if result is None or result.normalized is None
        else DataReadiness.FALLBACK_READY
        if result.policy is not None and result.policy.fallback
        else DataReadiness.DEGRADED_READY
    )
    state = replace(state, target_readiness=readiness, data_readiness=data)
    if scenario["case"] == "manual_override":
        state = reduce_ownership(state, OwnershipEvent.EXTERNAL_TARGET, now=now).state
    return state


def _assert_goldens(scenario: dict[str, Any], calculation: RuntimeCalculation) -> None:
    expected = scenario["expected_numerical"]
    key = expected["golden_key"]
    if key is None:
        assert calculation.targets[0].result is None
        return
    golden = GOLDENS[key]
    result = calculation.targets[0].result
    assert result is not None
    assert result.current is not None
    assert result.current.sensation_vote == pytest.approx(golden["current"], abs=1e-6)
    assert result.roots is not None
    expected_statuses = expected["root_status"]
    for name, expected_root in zip(ROOT_NAMES, golden["roots"], strict=True):
        root = getattr(result.roots, name)
        if expected_root is None:
            assert isinstance(root, RootFailure)
            assert root.failure.value == expected_statuses[name]
        else:
            assert isinstance(root, RootSuccess)
            assert expected_statuses[name] == "success"
            assert root.mapped_room_temperature_c == pytest.approx(expected_root, abs=0.01)


async def run_scenario(scenario: dict[str, Any]) -> ScenarioResult:
    """Run one expanded fixture and return report-ready evidence."""

    calculation = calculate_runtime_snapshot(_captured(scenario))
    _assert_goldens(scenario, calculation)
    case = scenario["case"]
    if case == "cooling_strategies":
        calls = []
        for strategy in ("balanced", "efficient", "comfort"):
            variant = json.loads(json.dumps(scenario))
            variant["zone_configuration"]["strategy"] = strategy
            variant_calculation = calculate_runtime_snapshot(_captured(variant))
            variant_calls, _reason, _ack, _ending = await _broker_run(variant, variant_calculation)
            calls.extend(variant_calls)
        ending_ownership = "owned"
        reason, acknowledgement = "own_context_match", "acknowledged"
    else:
        calls, reason, acknowledgement, ending_ownership = await _broker_run(
            scenario, calculation, stable=case == "stable_ack"
        )
    if case in {"restart_recovery", "obsolete_ack", "shared_history", "multi_actuator_conflict"}:
        calls, reason, acknowledgement, ending_ownership = await _special_case(
            case, scenario, calculation
        )
    expected = scenario["expected_broker"]
    assert calls == expected["service_payloads"]
    if expected["suppression_reason"] is not None:
        assert reason == expected["suppression_reason"]
    if expected["acknowledgement"] != "not_applicable" or not calls:
        assert acknowledgement == expected["acknowledgement"]
    assert ending_ownership == expected["ending_ownership"]
    result = calculation.targets[0].result
    policy = result.policy if result is not None else None
    expected_policy = scenario["expected_policy"]
    assert (policy.fallback if policy is not None else False) == expected_policy["fallback"]
    assert (policy.governing_heating if policy is not None else None) == expected_policy[
        "governing_heating"
    ]
    assert (policy.governing_cooling if policy is not None else None) == expected_policy[
        "governing_cooling"
    ]
    assert (list(policy.limitations) if policy is not None else []) == expected_policy[
        "limitations"
    ]
    if result is not None:
        assert result.evaluation_count <= scenario["expected_execution"]["evaluation_budget"]
    if case == "event_storm":
        await _event_storm_qualification()
    variants = await _mandatory_variants(case, scenario, calculation)
    numerical = {
        "golden_key": scenario["expected_numerical"]["golden_key"],
        "current_sensation": (
            result.current.sensation_vote
            if result is not None and hasattr(result.current, "sensation_vote")
            else None
        ),
        "evaluation_count": result.evaluation_count if result is not None else 0,
        "roots": {
            name: (
                {"status": "unavailable", "value_c": None}
                if result is None or result.roots is None
                else {
                    "status": (
                        "success"
                        if isinstance(root := getattr(result.roots, name), RootSuccess)
                        else root.failure.value
                    ),
                    "value_c": (
                        root.mapped_room_temperature_c if isinstance(root, RootSuccess) else None
                    ),
                }
            )
            for name in ROOT_NAMES
        },
    }
    return ScenarioResult(
        scenario["scenario_id"],
        scenario["name"],
        True,
        {
            "primary": scenario["environmental_state"]["primary_raw"],
            "rh": scenario["environmental_state"]["rh_raw"],
            "rh_provenance": calculation.relative_humidity_provenance,
            "history_quality": calculation.history_quality,
            "running_mean_c": calculation.running_mean_c,
            "provenance": dict(calculation.provenance),
            "radiant_model": scenario["zone_configuration"]["radiant_model"],
        },
        numerical,
        {
            "strategy": scenario["zone_configuration"]["strategy"],
            "occupancy_state": scenario["zone_configuration"]["occupancy_state"],
            "boost_mode": policy.boost_mode.value if policy is not None else "off",
            "boost_phase": policy.boost_phase if policy is not None else "off",
            "fallback": policy.fallback if policy is not None else False,
            "governing_heating": policy.governing_heating if policy is not None else None,
            "governing_cooling": policy.governing_cooling if policy is not None else None,
            "limitations": list(policy.limitations) if policy is not None else [],
            "requested_heating_c": policy.heating_c if policy is not None else None,
            "requested_cooling_c": policy.cooling_c if policy is not None else None,
            "normalized": (
                asdict(result.normalized)
                if result is not None and result.normalized is not None
                else None
            ),
        },
        {
            "calls": calls,
            "reason": reason,
            "acknowledgement": acknowledgement,
            "ending_ownership": ending_ownership,
        },
        {
            **scenario["expected_execution"],
            "evaluation_budget": 3000,
            "settled": True,
            "variants": variants,
        },
    )


async def _mandatory_variants(
    case: str, scenario: dict[str, Any], calculation: RuntimeCalculation
) -> tuple[str, ...]:
    """Exercise every mandatory scenario variant beyond the fixture's named baseline."""

    if case == "invalid_primary":
        for _name, value in (
            ("missing", None),
            ("unavailable", "unavailable"),
            ("nonfinite", float("inf")),
            ("boolean", True),
            ("malformed", "20 degrees"),
        ):
            variant = json.loads(json.dumps(scenario))
            variant["environmental_state"]["primary_raw"] = value
            result = calculate_runtime_snapshot(_captured(variant))
            assert result.targets[0].result is None
            assert result.targets[0].suppression_reason == "primary_temperature_invalid"
            after_hold = calculate_runtime_snapshot(
                replace(_captured(variant), failure_hold_elapsed=True)
            )
            assert after_hold.targets[0].result is None
            assert (await _broker_run(variant, result))[0] == []
        stale = replace(
            _captured(scenario),
            primary=replace(
                _captured(scenario).primary,
                raw_state="20.0",
                observed_at=datetime.fromisoformat(scenario["clock"]) - timedelta(minutes=31),
            ),
        )
        stale_result = calculate_runtime_snapshot(stale)
        assert stale_result.targets[0].result is None
        assert stale_result.targets[0].suppression_reason == "primary_temperature_stale"
        return ("missing", "unavailable", "nonfinite", "boolean", "malformed", "stale")

    if case == "invalid_rh":
        for _name, value in (
            ("missing", None),
            ("unavailable", "unavailable"),
            ("nonfinite", float("inf")),
            ("boolean", True),
            ("malformed", "fifty"),
        ):
            variant = json.loads(json.dumps(scenario))
            variant["environmental_state"]["rh_raw"] = value
            result = calculate_runtime_snapshot(_captured(variant))
            assert result.targets[0].result is None
            assert result.targets[0].suppression_reason == "primary_rh_invalid"
            after_hold = calculate_runtime_snapshot(
                replace(_captured(variant), failure_hold_elapsed=True)
            )
            assert after_hold.targets[0].result is None
            assert (await _broker_run(variant, result))[0] == []
        stale_snapshot = _captured(scenario)
        assert stale_snapshot.relative_humidity is not None
        stale_result = calculate_runtime_snapshot(
            replace(
                stale_snapshot,
                relative_humidity=replace(
                    stale_snapshot.relative_humidity,
                    raw_state="50.0",
                    observed_at=datetime.fromisoformat(scenario["clock"]) - timedelta(minutes=31),
                ),
            )
        )
        assert stale_result.targets[0].result is None
        assert stale_result.targets[0].suppression_reason == "primary_rh_stale"
        return ("missing", "unavailable", "nonfinite", "boolean", "malformed", "stale")

    if case == "history_variants":
        partial = calculation
        insufficient = calculate_runtime_snapshot(_captured(scenario, history_days=2))
        assert partial.targets[0].result is not None
        assert partial.targets[0].result.policy is not None
        assert not partial.targets[0].result.policy.fallback
        assert insufficient.targets[0].result is not None
        assert insufficient.targets[0].result.policy is not None
        assert insufficient.targets[0].result.policy.fallback
        assert (await _broker_run(scenario, insufficient))[0] == [
            {"entity_id": "climate.living_room", "temperature": 18.0}
        ]
        return ("three_day_adaptive", "two_day_fixed_fallback")

    if case == "winter_variants":
        rejected_fixture = json.loads(json.dumps(scenario))
        rejected_fixture["zone_configuration"]["reject_extrapolation"] = True
        rejected = calculate_runtime_snapshot(_captured(rejected_fixture))
        assert rejected.targets[0].result is not None
        assert rejected.targets[0].result.policy is None
        assert rejected.targets[0].suppression_reason == "extrapolation_rejected"
        assert (await _broker_run(rejected_fixture, rejected))[0] == []
        after_hold = calculate_runtime_snapshot(
            replace(_captured(rejected_fixture), failure_hold_elapsed=True)
        )
        assert after_hold.targets[0].result is not None
        assert after_hold.targets[0].result.policy is not None
        assert after_hold.targets[0].result.policy.fallback
        assert (await _broker_run(rejected_fixture, after_hold))[0] == [
            {"entity_id": "climate.living_room", "temperature": 18.0}
        ]
        return (
            "adaptive_extrapolation_allowed",
            "rejected_immediate_hold_no_write",
            "rejected_after_15m_fixed_fallback_heat_18",
        )

    if case == "humid_cooling":
        ranged = json.loads(json.dumps(scenario))
        ranged["target_capabilities"].update(
            {
                "hvac_mode": "heat_cool",
                "advertised_modes": ["off", "heat_cool"],
                "supported_features": 2,
                "current_low": 18.0,
                "current_high": 28.0,
            }
        )
        ranged_result = calculate_runtime_snapshot(_captured(ranged))
        assert ranged_result.targets[0].suppression_reason == (
            "missing_required_root:heating_control"
        )
        assert (await _broker_run(ranged, ranged_result))[0] == []
        return ("cooling_directional_root", "ranged_missing_heating_endpoint")

    if case == "coarse_grid":
        expected = {
            ("heat", 2.0): {"entity_id": "climate.living_room", "temperature": 20.0},
            ("cool", 2.0): {"entity_id": "climate.living_room", "temperature": 22.0},
            ("heat_cool", 2.0): {
                "entity_id": "climate.living_room",
                "target_temp_low": 20.0,
                "target_temp_high": 22.0,
            },
        }
        for (mode, step), payload in expected.items():
            variant = _capability_variant(scenario, mode=mode, step=step)
            result = calculate_runtime_snapshot(_captured(variant))
            assert (await _broker_run(variant, result))[0] == [payload]
        infeasible = _capability_variant(scenario, mode="heat_cool", step=4.0)
        infeasible_result = calculate_runtime_snapshot(_captured(infeasible))
        assert infeasible_result.targets[0].suppression_reason == "no_legal_range"
        assert (await _broker_run(infeasible, infeasible_result))[0] == []
        return ("heat_step_2", "cool_step_2", "range_step_2", "range_step_4_infeasible")

    if case == "acknowledgements":
        inferred = await _contextless_acknowledgement(scenario, calculation)
        assert inferred is AcknowledgementStatus.INFERRED_ACKNOWLEDGED
        return ("own_context_before_return", "contextless_exact_pending")

    if case == "restart_recovery":
        _restart_variants()
        return ("clean_recompute", "unclean_reconcile", "explicit_resume_new_identity")

    if case == "multi_actuator_conflict":
        await _multi_actuator_normal_variant(scenario)
        return ("normal_two_actuator", "manual_opposing_conflict")

    if case == "shared_history":
        await _forty_zone_shared_source_qualification(scenario)
        return ("two_zone_commands", "forty_zone_eight_location_shared_source")

    if case == "event_storm":
        return ("ten_thousand_events", "latest_generation_only")

    return ()


def _capability_variant(scenario: dict[str, Any], *, mode: str, step: float) -> dict[str, Any]:
    variant = json.loads(json.dumps(scenario))
    ranged = mode == "heat_cool"
    variant["target_capabilities"].update(
        {
            "hvac_mode": mode,
            "advertised_modes": ["off", mode],
            "supported_features": 2 if ranged else 1,
            "step": step,
            "current_low": 18.0 if ranged else None,
            "current_high": 26.0 if ranged else None,
        }
    )
    return variant


async def _contextless_acknowledgement(
    scenario: dict[str, Any], calculation: RuntimeCalculation
) -> AcknowledgementStatus:
    now = datetime.fromisoformat(scenario["clock"])
    service = CapturedService()
    persistence = MemoryPersistence()
    identity = scenario["target_capabilities"]["registry_identity"]
    broker = CommandBroker(
        service=service,
        persistence=persistence,
        preflight=lambda _identity: BrokerPreflight(
            identity, 1, 1, 1, 1, Ownership.OWNED, True, True, "zone"
        ),
        command_id_factory=lambda: "contextless-command",
        context_factory=lambda: ContextToken("contextless-context", object()),
    )
    intent = _normalized_intent(scenario, calculation, now=now)
    assert intent is not None
    dispatched = await broker.async_submit(intent, now=now)
    assert dispatched.dispatch_status is DispatchStatus.DISPATCHED
    outcome = await broker.async_feedback(
        FeedbackObservation(
            identity,
            intent_fingerprint(intent),
            None,
            None,
            1,
            1,
            1,
            1,
            0,
        ),
        now=now + timedelta(seconds=1),
    )
    assert len(service.calls) == 1
    assert persistence.pending is None
    return outcome.acknowledgement_status


def _restart_variants() -> None:
    actuator = StoredActuator(
        "registry-climate-living-room",
        "owned",
        1,
        0,
        None,
        None,
        False,
        "acknowledged:19.5",
        "old-command",
        "hash",
        "ctx-old",
        None,
    )
    clean = prepare_startup_recovery(
        ControlLoadResult(
            ControlStoreState(1, "old-run", True, "fingerprint", "balanced", (actuator,)),
            None,
        ),
        run_id="clean-run",
        configuration_fingerprint="fingerprint",
        strategy="balanced",
    )
    assert not clean.requires_resume
    assert clean.reason == "clean_restart"
    assert clean.state.actuators[0].pending_command is None

    unresolved = replace(
        actuator,
        pending_command=StoredCommand(
            "old-command",
            actuator.target_identity,
            "hash",
            "ctx-old",
            1,
            1,
            1,
            1,
            "2026-09-10T09:00:00+00:00",
            "2026-09-10T09:05:00+00:00",
            True,
        ),
    )
    unclean = prepare_startup_recovery(
        ControlLoadResult(
            ControlStoreState(1, "old-run", False, "fingerprint", "balanced", (unresolved,)),
            None,
        ),
        run_id="unclean-run",
        configuration_fingerprint="fingerprint",
        strategy="balanced",
    )
    assert unclean.requires_resume
    assert unclean.reason == "unresolved_command"
    assert unclean.state.actuators[0].pending_command is None

    resumed = replace(
        unclean.state,
        run_id="resume-run",
        actuators=(
            replace(
                unclean.state.actuators[0],
                ownership="owned",
                resume_required=False,
                last_command_id="new-command",
            ),
        ),
    )
    assert resumed.actuators[0].last_command_id != unresolved.last_command_id
    assert not resumed.actuators[0].resume_required


async def _multi_actuator_normal_variant(scenario: dict[str, Any]) -> None:
    heating = json.loads(json.dumps(scenario))
    heating["target_capabilities"].update(
        {
            "entity_id": "climate.heat_a",
            "target_uuid": "heat-a",
            "registry_identity": "registry-heat-a",
            "calibration_offset_c": 0.5,
        }
    )
    heating_result = calculate_runtime_snapshot(_captured(heating))
    heating_calls, _, _, _ = await _broker_run(heating, heating_result)
    assert heating_calls == [{"entity_id": "climate.heat_a", "temperature": 20.0}]

    cooling = _capability_variant(scenario, mode="cool", step=0.5)
    cooling["target_capabilities"].update(
        {
            "entity_id": "climate.cool_b",
            "target_uuid": "cool-b",
            "registry_identity": "registry-cool-b",
            "calibration_offset_c": 0.0,
        }
    )
    cooling_result = calculate_runtime_snapshot(_captured(cooling))
    cooling_calls, _, _, _ = await _broker_run(cooling, cooling_result)
    assert cooling_calls == [{"entity_id": "climate.cool_b", "temperature": 23.0}]
    assert 23.0 - 19.5 >= 1.0


async def _forty_zone_shared_source_qualification(scenario: dict[str, Any]) -> None:
    from custom_components.athb.core.history import (
        OutdoorSourceKey,
        OutdoorSourceRegistry,
        collection_policy_fingerprint,
    )

    registry = OutdoorSourceRegistry()
    key = OutdoorSourceKey(
        "outdoor",
        None,
        scenario["timezone"],
        collection_policy_fingerprint(maximum_hold_seconds=7200, coverage_threshold=0.9),
    )
    listeners = []
    for zone_index in range(40):
        state = registry.acquire(key=key, source_identity="sensor.outdoor", source_generation=1)
        listeners.append(state)
        variant = json.loads(json.dumps(scenario))
        variant["zone_configuration"]["critical_locations"] = [
            {
                "location_id": f"z{zone_index}-l{location_index}",
                "entity_id": f"sensor.z{zone_index}_l{location_index}",
                "mode": "monitoring",
                "delta_c": 0.5,
            }
            for location_index in range(8)
        ]
        result = calculate_runtime_snapshot(_captured(variant))
        assert result.targets[0].result is not None
        assert result.targets[0].result.evaluation_count <= 3000
    assert len({id(item) for item in listeners}) == 1
    assert listeners[0].listener_references == 40
    assert listeners[0].begin_bootstrap()
    assert not listeners[0].begin_bootstrap()
    for _ in range(40):
        assert registry.release(key)
    assert registry.source_count == 0


async def _special_case(
    case: str, scenario: dict[str, Any], calculation: RuntimeCalculation
) -> tuple[list[dict[str, object]], str, str, str]:
    if case == "restart_recovery":
        command = StoredCommand(
            "old",
            "registry-climate-living-room",
            "hash",
            "ctx-old",
            1,
            1,
            1,
            1,
            "2026-09-10T09:00:00+00:00",
            "2026-09-10T09:05:00+00:00",
            True,
        )
        actuator = StoredActuator(
            "registry-climate-living-room",
            "owned",
            1,
            0,
            None,
            None,
            False,
            None,
            "old",
            "hash",
            "ctx-old",
            command,
        )
        loaded = ControlLoadResult(
            ControlStoreState(1, "old-run", False, "fingerprint", "balanced", (actuator,)), None
        )
        recovery = prepare_startup_recovery(
            loaded, run_id="new-run", configuration_fingerprint="fingerprint", strategy="balanced"
        )
        assert recovery.requires_resume
        assert recovery.state.actuators[0].pending_command is None
        assert recovery.reason == "unresolved_command"
        state = reduce_ownership(
            _scenario_ownership(
                scenario, calculation, now=datetime.fromisoformat(scenario["clock"])
            ),
            OwnershipEvent.UNCLEAN_RESTART,
            now=datetime.fromisoformat(scenario["clock"]),
        ).state
        return [], "reconciliation_required", "not_applicable", state.ownership.value
    if case == "obsolete_ack":
        now = datetime.fromisoformat(scenario["clock"])
        service = CapturedService()
        persistence = MemoryPersistence()
        target_identity = scenario["target_capabilities"]["registry_identity"]
        broker = CommandBroker(
            service=service,
            persistence=persistence,
            preflight=lambda _identity: BrokerPreflight(
                target_identity,
                1,
                1,
                1,
                1,
                Ownership.OWNED,
                True,
                True,
                "zone",
            ),
            command_id_factory=lambda: "obsolete-command",
            context_factory=lambda: ContextToken("obsolete-context", object()),
        )
        intent = _normalized_intent(scenario, calculation, now=now)
        assert intent is not None
        dispatched = await broker.async_submit(intent, now=now)
        assert dispatched.dispatch_status is DispatchStatus.DISPATCHED
        obsolete = await broker.async_feedback(
            FeedbackObservation(
                target_identity,
                intent_fingerprint(intent),
                "obsolete-context",
                None,
                0,
                1,
                1,
                1,
                0,
            ),
            now=now + timedelta(seconds=5),
        )
        assert persistence.pending is not None
        assert obsolete.reason == "obsolete_feedback"
        service.calls.clear()
        state = reduce_ownership(
            _scenario_ownership(scenario, calculation, now=now),
            OwnershipEvent.EXTERNAL_TARGET,
            now=now + timedelta(seconds=1),
        ).state
        return (
            [],
            "manual_override",
            obsolete.acknowledgement_status.value,
            state.ownership.value,
        )
    if case == "shared_history":
        from custom_components.athb.core.history import (
            OutdoorSourceKey,
            OutdoorSourceRegistry,
            collection_policy_fingerprint,
        )

        registry = OutdoorSourceRegistry()
        key = OutdoorSourceKey(
            "outdoor",
            None,
            "Europe/Amsterdam",
            collection_policy_fingerprint(maximum_hold_seconds=7200, coverage_threshold=0.9),
        )
        first = registry.acquire(key=key, source_identity="sensor.outdoor", source_generation=1)
        second = registry.acquire(key=key, source_identity="sensor.outdoor", source_generation=1)
        assert first is second
        assert first.listener_references == 2
        assert first.begin_bootstrap()
        assert not second.begin_bootstrap()
        assert registry.release(key)
        assert registry.release(key)
        assert registry.source_count == 0
        first_calls, first_reason, first_ack, first_ownership = await _broker_run(
            scenario, calculation
        )
        second_scenario = json.loads(json.dumps(scenario))
        second_scenario["target_capabilities"].update(
            {
                "target_uuid": "second-target",
                "registry_identity": "registry-second-target",
                "entity_id": "climate.second_zone",
            }
        )
        second_calculation = calculate_runtime_snapshot(_captured(second_scenario))
        second_calls, second_reason, second_ack, second_ownership = await _broker_run(
            second_scenario, second_calculation
        )
        assert first_reason == second_reason == "own_context_match"
        assert first_ack == second_ack == "acknowledged"
        assert first_ownership == second_ownership == "owned"
        return first_calls + second_calls, first_reason, first_ack, first_ownership
    if case == "multi_actuator_conflict":
        from custom_components.athb.core.contracts import ActuationDirection
        from custom_components.athb.core.policy import (
            OpposingTarget,
            check_cross_actuator_coordination,
        )

        result = calculation.targets[0].result
        assert result is not None
        assert isinstance(result.normalized, NormalizedScalarTarget)
        conflict = check_cross_actuator_coordination(
            direction=ActuationDirection.HEATING_ONLY,
            proposed_room_c=result.normalized.normalized_room_c,
            opposing_targets=(
                OpposingTarget("cool", ActuationDirection.COOLING_ONLY, False, True, None, 20.0),
            ),
        )
        assert not conflict.eligible
        ownership = _scenario_ownership(
            scenario, calculation, now=datetime.fromisoformat(scenario["clock"])
        )
        return (
            [],
            conflict.reason or "cross_actuator_conflict",
            "not_applicable",
            ownership.ownership.value,
        )
    raise AssertionError(case)


async def _event_storm_qualification() -> None:
    gate = asyncio.Event()
    published: list[int] = []
    first = True

    async def executor(function):
        nonlocal first
        if first:
            first = False
            await gate.wait()
        return function()

    controller = ZoneController[int, int](
        executor=executor,
        calculate=lambda value: value,
        publish=lambda generation, _value: published.append(generation),
        global_semaphore=asyncio.Semaphore(2),
    )
    controller.request(0)
    await asyncio.sleep(0)
    for value in range(1, 10_000):
        controller.request(value)
    gate.set()
    await controller.async_wait_idle()
    assert published == [10_000]
    assert controller.maximum_pending == 1
    assert controller.stale_jobs == 1
