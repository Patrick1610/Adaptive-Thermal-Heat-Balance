"""Typed, event-driven per-zone Home Assistant runtime."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ENTITY_MATCH_ALL, EVENT_CALL_SERVICE
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
from homeassistant.util import dt as dt_util

from .adapters.broker import (
    ACKNOWLEDGEMENT_DEADLINE,
    HARD_COMMAND_INTERVAL,
    ORDINARY_COMMAND_INTERVAL,
    BrokerPreflight,
    CommandBroker,
    ContextToken,
    NormalizedIntent,
    TargetLeaseRegistry,
)
from .adapters.climate import HomeAssistantClimateService, capability_from_state
from .adapters.outdoor_history import (
    OutdoorHistoryCollector,
    OutdoorHistoryManager,
    unavailable_history,
)
from .adapters.recorder import HomeAssistantRecorderHistoryReader
from .adapters.sources import StateValue, configured_freshness, snapshot_state
from .adapters.storage import (
    LAST_VALID_OUTPUT_KEYS,
    HomeAssistantControlStorageBackend,
    ZoneCommandPersistence,
)
from .calculation import (
    CapturedCriticalLocation,
    CapturedTarget,
    CapturedZoneSnapshot,
    RuntimeCalculation,
    TargetCalculation,
    calculate_runtime_snapshot,
    result_values,
)
from .const import (
    CONF_BOOST_MODE,
    CONF_COMFORT_STRATEGY,
    CONF_CONTROL_ENABLED,
    CONF_ECO_INTENSITY,
    CONF_MOLD_INDICATOR_ENTITY,
    DEFAULT_BOOST_MODE,
    DEFAULT_STRATEGY,
    DOMAIN,
    MOLD_INDICATOR_CRITICAL_TEMP_ATTRIBUTE,
)
from .controller import ZoneController
from .core.climate import (
    CapabilityMapping,
    ClimateFailure,
    GridOptions,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    ha_to_celsius,
    normalize_range_target,
    normalize_scalar_target,
    resolve_capability,
)
from .core.contracts import (
    AcknowledgementStatus,
    ActuationDirection,
    BoostMode,
    DispatchStatus,
    TargetShape,
)
from .core.history import HistoryQuality, OutdoorSample
from .core.locations import CriticalDeltaState, update_critical_delta
from .core.ownership import (
    DataReadiness,
    FeedbackObservation,
    Ownership,
    OwnershipEvent,
    OwnershipState,
    TargetFingerprint,
    TargetReadiness,
    initial_ownership,
    reduce_ownership,
)
from .core.policy import (
    OccupancyState,
    OpposingTarget,
    ProfileResolution,
    check_cross_actuator_coordination,
    resolve_occupancy_profile,
)
from .core.sources import SourceKind, SourceState, convert_source_value
from .core.trace import DecisionTraceRing
from .repairs import RepairManager, TransitionLogger

_LOGGER = logging.getLogger(__name__)
STALE_SAFETY_DELAY = timedelta(hours=1)
_HELD_VALUE_KEYS = LAST_VALID_OUTPUT_KEYS


@dataclass(slots=True)
class ZoneRuntime:
    hass: HomeAssistant
    entry: ConfigEntry[ZoneRuntime]
    zone_uuid: str
    strategy: str
    boost_mode: str
    control_enabled: bool
    eco_intensity: str = "custom"
    configuration_generation: int = 1
    runtime_generation: int = 1
    input_generation: int = 1
    source_generation: int = 1
    values: dict[str, Any] = field(default_factory=dict)
    listeners: list[Callable[[], None]] = field(default_factory=list)
    update_callbacks: set[Callable[[], None]] = field(default_factory=set)
    controller: ZoneController[CapturedZoneSnapshot, RuntimeCalculation] | None = None
    debounce_cancel: Callable[[], None] | None = None
    debounce_started: float | None = None
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    ownership: dict[str, OwnershipState] = field(default_factory=dict)
    capability_generations: dict[str, int] = field(default_factory=dict)
    last_target_fingerprints: dict[str, TargetFingerprint] = field(default_factory=dict)
    critical_delta_states: dict[str, CriticalDeltaState] = field(default_factory=dict)
    history_collector: OutdoorHistoryCollector | None = None
    history_source_identity: str | None = None
    persistence: ZoneCommandPersistence | None = None
    broker: CommandBroker | None = None
    explicit_transition: bool = True
    trace_ring: DecisionTraceRing = field(default_factory=DecisionTraceRing)
    repair_manager: RepairManager | None = None
    transition_logger: TransitionLogger = field(default_factory=lambda: TransitionLogger(_LOGGER))
    timers: dict[str, Callable[[], None]] = field(default_factory=dict)
    profile_resolution: ProfileResolution | None = None
    rapid_boost_reached: bool = False
    previous_requested: tuple[float | None, float | None] = (None, None)
    previous_requested_at: datetime | None = None
    source_states: dict[str, SourceState] = field(default_factory=dict)
    repair_condition_started: dict[str, datetime] = field(default_factory=dict)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    failure_hold_reason: str | None = None
    failure_hold_elapsed: bool = False
    last_valid_values: dict[str, Any] = field(default_factory=dict)
    last_valid_at: datetime | None = None
    last_valid_persistence_payload: str | None = None
    stale_safety_applied: set[str] = field(default_factory=set)

    async def async_start(self) -> None:
        """Acquire recovery state, shared history, listeners and initial calculation."""

        targets = tuple(self.entry.data.get("targets", ()))
        identities = tuple(str(target["registry_identity"]) for target in targets)
        self.ownership = {identity: initial_ownership(identity) for identity in identities}
        self.capability_generations = dict.fromkeys(identities, 1)
        self.persistence = ZoneCommandPersistence(
            HomeAssistantControlStorageBackend(self.hass, self.zone_uuid),
            run_id=str(uuid4()),
            configuration_fingerprint=self._configuration_fingerprint(),
            strategy=self.strategy,
            target_identities=identities,
            control_enabled=self.control_enabled,
            boost_mode=self.boost_mode,
        )
        self.repair_manager = RepairManager(self.hass, self.entry.entry_id)
        if not await self.persistence.async_start():
            self.publish(
                {"control_status": "storage_fault", "suppression_reason": "storage_io_failed"}
            )
            self.repair_manager.update("corrupt_control_storage", True)
        if self.persistence.state is not None:
            stored_values = getattr(self.persistence.state, "last_valid_values_json", None)
            stored_at = getattr(self.persistence.state, "last_valid_at", None)
            if stored_values is not None and stored_at is not None:
                restored = json.loads(stored_values)
                if isinstance(restored, dict):
                    self.last_valid_values = restored
                    self.last_valid_at = datetime.fromisoformat(stored_at)
                    self.last_valid_persistence_payload = stored_values
        if self.boost_mode != "off" and self.persistence.state is not None:
            stored_expiry = self.persistence.state.boost_expiry_utc
            expiry = datetime.fromisoformat(stored_expiry) if stored_expiry is not None else None
            if expiry is None or expiry <= dt_util.utcnow():
                self.boost_mode = BoostMode.OFF.value
                self.rapid_boost_reached = False
                self.hass.config_entries.async_update_entry(
                    self.entry, options={**self.entry.options, CONF_BOOST_MODE: self.boost_mode}
                )
            else:
                self.rapid_boost_reached = bool(
                    getattr(self.persistence.state, "rapid_boost_reached", False)
                )
                self._schedule_boost_expiry(expiry)
        self.broker = CommandBroker(
            service=HomeAssistantClimateService(self.hass),
            persistence=self.persistence,
            preflight=self._broker_preflight,
            command_id_factory=lambda: str(uuid4()),
            context_factory=self._context_token,
        )
        if self.control_enabled:
            await self._async_acquire_target_leases()
            if self.persistence.requires_resume:
                for identity in identities:
                    self._transition(identity, OwnershipEvent.UNCLEAN_RESTART)
        await self._async_start_history()
        self.controller = ZoneController(
            executor=self.hass.async_add_executor_job,
            calculate=calculate_runtime_snapshot,
            publish=self._publish_calculation,
            global_semaphore=get_global_semaphore(self.hass),
        )

        tracked = self._tracked_entity_ids()
        if tracked:
            self.listeners.append(
                async_track_state_change_event(self.hass, tracked, self._handle_state_event)
            )
        reported_sources = self._reported_source_entity_ids()
        if reported_sources:
            self.listeners.append(
                async_track_state_report_event(
                    self.hass,
                    reported_sources,
                    self._handle_source_report_event,
                )
            )
        reported_targets = self._reported_target_entity_ids()
        if reported_targets:
            self.listeners.append(
                async_track_state_report_event(
                    self.hass,
                    reported_targets,
                    self._handle_target_report_event,
                )
            )
        self.listeners.append(self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._service_event))
        self.listeners.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._entity_registry_event
            )
        )
        self._initialize_target_fingerprints()
        self.async_request_snapshot()

    async def _async_start_history(self) -> None:
        outdoor_id = str(self.entry.data.get("outdoor_source", ""))
        if not outdoor_id:
            return
        registry_entry = er.async_get(self.hass).async_get(outdoor_id)
        self.history_source_identity = (
            f"registry:{registry_entry.id}"
            if registry_entry is not None
            else f"unregistered:{outdoor_id}:{self.source_generation}"
        )
        collector, created = get_history_manager(self.hass).acquire(
            hass=self.hass,
            source_identity=self.history_source_identity,
            timezone=str(self.hass.config.time_zone),
            reader=HomeAssistantRecorderHistoryReader(self.hass, outdoor_id),
            entity_id=outdoor_id,
            subscriber=self._history_updated,
        )
        self.history_collector = collector
        if created:
            now = dt_util.utcnow()
            await collector.async_start(now=now, current=self._outdoor_sample(now))

    def _configuration_fingerprint(self) -> str:
        payload = json.dumps(
            {"data": self.entry.data, "options": self.entry.options},
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return sha256(payload.encode()).hexdigest()

    def _tracked_entity_ids(self) -> set[str]:
        ids = self._reported_source_entity_ids()
        ids.add(str(self.entry.options.get("occupancy_entity", "")))
        ids.update(str(target["entity_id"]) for target in self.entry.data.get("targets", ()))
        ids.discard("")
        return ids

    def _reported_source_entity_ids(self) -> set[str]:
        """Return measured inputs whose unchanged reports renew freshness."""

        ids = {
            str(self.entry.data.get("primary_temperature", "")),
            str(self.entry.data.get("rh_entity", "")),
            str(self.entry.options.get("air_speed_entity", "")),
        }
        for name in ("mrt_entity", "globe_temperature_entity", "surface_temperature_entity"):
            ids.add(str(self.entry.options.get(name, "")))
        ids.add(str(self.entry.options.get(CONF_MOLD_INDICATOR_ENTITY, "")))
        for item in self.entry.options.get("critical_locations", ()):
            if isinstance(item, dict):
                ids.add(str(item.get("entity_id", "")))
        ids.discard("")
        return ids

    def _reported_target_entity_ids(self) -> set[str]:
        """Return climate targets whose unchanged reports may acknowledge a command."""

        return {
            str(target["entity_id"])
            for target in self.entry.data.get("targets", ())
            if target.get("entity_id")
        }

    @callback
    def _handle_state_event(self, event: Event[Any]) -> None:
        entity_id = str(event.data.get("entity_id", ""))
        old_state = cast(State | None, event.data.get("old_state"))
        new_state = cast(State | None, event.data.get("new_state"))
        target = next(
            (item for item in self.entry.data.get("targets", ()) if item["entity_id"] == entity_id),
            None,
        )
        if target is not None:
            self._create_task(self._async_handle_target_state(target, old_state, new_state))
        else:
            self.input_generation += 1
            self._update_critical_delta(entity_id, new_state)
        self.schedule_environmental_snapshot()

    @callback
    def _handle_source_report_event(self, event: Event[Any]) -> None:
        """Recalculate when a measured source reports an unchanged value."""

        entity_id = str(event.data.get("entity_id", ""))
        new_state = cast(State | None, event.data.get("new_state"))
        self.input_generation += 1
        self._update_critical_delta(entity_id, new_state)
        self.schedule_environmental_snapshot()

    @callback
    def _handle_target_report_event(self, event: Event[Any]) -> None:
        """Use an unchanged target report only to acknowledge a pending command."""

        entity_id = str(event.data.get("entity_id", ""))
        target = next(
            (item for item in self.entry.data.get("targets", ()) if item["entity_id"] == entity_id),
            None,
        )
        if target is None or self.broker is None:
            return
        identity = str(target["registry_identity"])
        pending, _queued = self.broker.state_counts(identity)
        if not pending:
            return
        self._create_task(
            self._async_handle_target_report(
                target,
                cast(State | None, event.data.get("new_state")),
                event.context.id,
                event.context.parent_id,
            )
        )

    @callback
    def _history_updated(self) -> None:
        self.input_generation += 1
        self.schedule_environmental_snapshot()

    @callback
    def _service_event(self, event: Event[Any]) -> None:
        if event.data.get("domain") != "climate" or event.data.get("service") != "set_temperature":
            return
        data = event.data.get("service_data", {})
        if not isinstance(data, dict):
            return
        if data.get("entity_id") == ENTITY_MATCH_ALL:
            requested = {str(target["entity_id"]) for target in self.entry.data.get("targets", ())}
        else:
            selected = async_extract_referenced_entity_ids(self.hass, TargetSelection(data))
            requested = selected.referenced | selected.indirectly_referenced
        for target in self.entry.data.get("targets", ()):
            if target["entity_id"] not in requested:
                continue
            identity = str(target["registry_identity"])
            own_context = None
            if self.persistence is not None and self.persistence.state is not None:
                stored = next(
                    (
                        item
                        for item in self.persistence.state.actuators
                        if item.target_identity == identity
                    ),
                    None,
                )
                own_context = stored.last_command_context_id if stored is not None else None
            if event.context.id != own_context:
                self._transition(identity, OwnershipEvent.EXTERNAL_TARGET)

    @callback
    def _entity_registry_event(self, event: Event[Any]) -> None:
        if self.repair_manager is None:
            return
        affected = {
            str(event.data.get("entity_id", "")),
            str(event.data.get("old_entity_id", "")),
        }
        configured_ids = {
            *self._tracked_entity_ids(),
            str(self.entry.data.get("outdoor_source", "")),
        }
        if affected & configured_ids and event.data.get("action") == "remove":
            self.repair_manager.update("removed_source_or_target", True)
            return
        old_entity_id = str(event.data.get("old_entity_id", ""))
        entity_id = str(event.data.get("entity_id", ""))
        if (
            event.data.get("action") != "update"
            or not old_entity_id
            or not entity_id
            or old_entity_id == entity_id
        ):
            return
        data = dict(self.entry.data)
        options = dict(self.entry.options)
        changed = False
        for key in ("primary_temperature", "outdoor_source", "rh_entity"):
            if data.get(key) == old_entity_id:
                data[key] = entity_id
                changed = True
        targets = []
        for target in data.get("targets", ()):
            updated = dict(target)
            if updated.get("entity_id") == old_entity_id:
                updated["entity_id"] = entity_id
                changed = True
            targets.append(updated)
        if targets:
            data["targets"] = targets
        for key in (
            "occupancy_entity",
            "air_speed_entity",
            "mrt_entity",
            "globe_temperature_entity",
            "surface_temperature_entity",
            CONF_MOLD_INDICATOR_ENTITY,
        ):
            if options.get(key) == old_entity_id:
                options[key] = entity_id
                changed = True
        critical = []
        for location in options.get("critical_locations", ()):
            updated = dict(location) if isinstance(location, dict) else location
            if isinstance(updated, dict) and updated.get("entity_id") == old_entity_id:
                updated["entity_id"] = entity_id
                changed = True
            critical.append(updated)
        if critical:
            options["critical_locations"] = critical
        if not changed:
            return
        self.hass.config_entries.async_update_entry(self.entry, data=data, options=options)
        if self.history_collector is not None and data.get("outdoor_source") == entity_id:
            self.history_collector.update_entity_id(entity_id)
        self.listeners.append(
            async_track_state_change_event(self.hass, {entity_id}, self._handle_state_event)
        )
        if entity_id in self._reported_source_entity_ids():
            self.listeners.append(
                async_track_state_report_event(
                    self.hass,
                    {entity_id},
                    self._handle_source_report_event,
                )
            )
        if entity_id in self._reported_target_entity_ids():
            self.listeners.append(
                async_track_state_report_event(
                    self.hass,
                    {entity_id},
                    self._handle_target_report_event,
                )
            )
        self.input_generation += 1
        self.schedule_environmental_snapshot()

    def _update_critical_delta(self, entity_id: str, state: State | None) -> None:
        primary_state = self.hass.states.get(str(self.entry.data.get("primary_temperature", "")))
        primary = self._state_temperature_c(primary_state, SourceKind.PRIMARY_AIR)
        local = self._state_temperature_c(state, SourceKind.LOCAL_AIR)
        if primary is None or local is None or state is None:
            return
        for item in self.entry.options.get("critical_locations", ()):
            if not isinstance(item, dict) or item.get("entity_id") != entity_id:
                continue
            location_id = str(item.get("location_id", entity_id))
            update = update_critical_delta(
                self.critical_delta_states.get(location_id, CriticalDeltaState()),
                primary_air_temperature_c=primary,
                local_air_temperature_c=local,
                observed_at=getattr(state, "last_reported", None) or state.last_updated,
            )
            self.critical_delta_states[location_id] = update.state

    async def _async_handle_target_state(
        self, target: dict[str, str], old: State | None, new: State | None
    ) -> None:
        identity = str(target["registry_identity"])
        old_capability = capability_from_state(old)
        capability = capability_from_state(new)
        availability_changed = old_capability.available != capability.available
        if availability_changed:
            self.capability_generations[identity] += 1
            self._transition(
                identity,
                (
                    OwnershipEvent.TARGET_RETURNED
                    if capability.available
                    else OwnershipEvent.TARGET_UNAVAILABLE
                ),
                target_readiness=self._target_readiness(capability),
            )
        elif old is not None and old_capability.hvac_mode != capability.hvac_mode:
            self.capability_generations[identity] += 1
            self._transition(
                identity,
                OwnershipEvent.EXTERNAL_HVAC_MODE,
                target_readiness=self._target_readiness(capability),
            )
        fingerprint = self._fingerprint(capability)
        prior = self.last_target_fingerprints.get(identity)
        self.last_target_fingerprints[identity] = fingerprint
        if prior == fingerprint or self.broker is None:
            return
        if availability_changed and not self.broker.state_counts(identity)[0]:
            return
        await self._async_process_target_feedback(
            identity,
            fingerprint,
            new.context.id if new is not None else None,
            new.context.parent_id if new is not None else None,
        )

    async def _async_handle_target_report(
        self,
        target: dict[str, str],
        new: State | None,
        context_id: str | None,
        parent_context_id: str | None,
    ) -> None:
        """Acknowledge a pending command from a same-value climate report."""

        identity = str(target["registry_identity"])
        if self.broker is None or not self.broker.state_counts(identity)[0]:
            return
        capability = capability_from_state(new)
        if not capability.available:
            return
        await self._async_process_target_feedback(
            identity,
            self._fingerprint(capability),
            context_id,
            parent_context_id,
        )

    async def _async_process_target_feedback(
        self,
        identity: str,
        fingerprint: TargetFingerprint,
        context_id: str | None,
        parent_context_id: str | None,
    ) -> None:
        """Classify one target observation and apply its broker outcome."""

        if self.broker is None:
            return
        state = self.ownership[identity]
        feedback = FeedbackObservation(
            identity,
            fingerprint,
            context_id,
            parent_context_id,
            self.configuration_generation,
            self.input_generation,
            self.capability_generations[identity],
            state.revision,
            state.external_revision,
        )
        outcome = await self.broker.async_feedback(feedback, now=dt_util.utcnow())
        if (
            outcome.acknowledgement_status
            in {
                AcknowledgementStatus.ACKNOWLEDGED,
                AcknowledgementStatus.INFERRED_ACKNOWLEDGED,
                AcknowledgementStatus.REJECTED,
            }
            and (cancel := self.timers.pop(f"ack:{identity}", None)) is not None
        ):
            cancel()
        if outcome.reason == "external_temperature_target":
            self._transition(identity, OwnershipEvent.EXTERNAL_TARGET)
        elif outcome.acknowledgement_status is AcknowledgementStatus.REJECTED:
            self.rejection_counts[identity] = self.rejection_counts.get(identity, 0) + 1
            if self.repair_manager is not None:
                self.repair_manager.update(
                    "persistent_target_rejection", self.rejection_counts[identity] >= 3
                )
            self._transition(identity, OwnershipEvent.COMMAND_REJECTED)
        elif outcome.acknowledgement_status in {
            AcknowledgementStatus.ACKNOWLEDGED,
            AcknowledgementStatus.INFERRED_ACKNOWLEDGED,
        }:
            self.rejection_counts[identity] = 0
            if self.repair_manager is not None:
                self.repair_manager.update("persistent_target_rejection", False)
            self._create_task(self._async_drain_queued(identity))

    @callback
    def schedule_environmental_snapshot(self) -> None:
        now = self.hass.loop.time()
        if self.debounce_started is None:
            self.debounce_started = now
        if self.debounce_cancel is not None:
            self.debounce_cancel()
        delay = max(0.0, min(2.0, self.debounce_started + 10.0 - now))

        @callback
        def fire(_now: Any) -> None:
            self.debounce_cancel = None
            self.debounce_started = None
            self.async_request_snapshot()

        self.debounce_cancel = async_call_later(self.hass, delay, fire)

    @callback
    def async_request_snapshot(self) -> None:
        if self.controller is None:
            return
        now = dt_util.utcnow()
        history = (
            self.history_collector.result(
                now=now, alpha=float(self.entry.options.get("running_mean_alpha", 0.8))
            )
            if self.history_collector is not None
            else unavailable_history()
        )
        running_mean = (
            history.value_c
            if history.quality in {HistoryQuality.COMPLETE, HistoryQuality.PARTIAL}
            else None
        )
        radiant_id = next(
            (
                str(self.entry.options.get(name))
                for name in (
                    "mrt_entity",
                    "globe_temperature_entity",
                    "surface_temperature_entity",
                    CONF_MOLD_INDICATOR_ENTITY,
                )
                if self.entry.options.get(name)
            ),
            "",
        )
        primary_state = self.hass.states.get(str(self.entry.data.get("primary_temperature", "")))
        critical = self._capture_critical_locations(primary_state, now)
        targets = tuple(
            CapturedTarget(
                str(target["target_uuid"]),
                str(target["registry_identity"]),
                str(target["entity_id"]),
                capability_from_state(self.hass.states.get(str(target["entity_id"]))),
            )
            for target in self.entry.data.get("targets", ())
        )
        snapshot = CapturedZoneSnapshot(
            now,
            self._snapshot_state(primary_state),
            self._snapshot_state(self.hass.states.get(str(self.entry.data.get("rh_entity", "")))),
            (
                float(self.entry.data["rh_declared"])
                if self.entry.data.get("rh_mode") == "declared"
                else None
            ),
            self._snapshot_state(
                self.hass.states.get(str(self.entry.data.get("outdoor_source", "")))
            ),
            (
                self._snapshot_mold_indicator(self.hass.states.get(radiant_id))
                if self.entry.options.get("radiant_model") == "mold_indicator"
                else self._snapshot_state(self.hass.states.get(radiant_id))
            ),
            running_mean,
            history.quality.value,
            self.strategy,
            (profile_resolution := self._resolve_profile(now)).resolved.value,
            {**self.entry.options, CONF_ECO_INTENSITY: self.eco_intensity},
            targets,
            critical,
            self.explicit_transition,
            self.previous_requested,
            (
                0.0
                if self.previous_requested_at is None
                else max(0.0, (now - self.previous_requested_at).total_seconds())
            ),
            profile_resolution.reasons,
            tuple(self.source_states.items()),
            self._snapshot_state(
                self.hass.states.get(str(self.entry.options.get("air_speed_entity", "")))
            ),
            self.failure_hold_elapsed,
            self.boost_mode,
            self.rapid_boost_reached,
        )
        self.values["source_states"] = {
            entity_id: (
                None
                if (state := self.hass.states.get(entity_id)) is None
                else {
                    "state": state.state,
                    "unit": state.attributes.get("unit_of_measurement"),
                    "last_reported": (
                        getattr(state, "last_reported", None) or state.last_updated
                    ).isoformat(),
                }
            )
            for entity_id in (
                str(self.entry.data.get("primary_temperature", "")),
                str(self.entry.data.get("outdoor_source", "")),
                str(self.entry.data.get("rh_entity", "")),
            )
            if entity_id
        }
        self.values["strategy"] = self.strategy
        self.values["comfort_level"] = self.strategy
        self.values["boost_mode"] = self.boost_mode
        self.values["eco_intensity"] = self.eco_intensity
        self.values["occupancy_status"] = profile_resolution.resolved.value
        self.values["setback_active"] = profile_resolution.resolved.value == "eco"
        self.values["configuration_generation"] = self.configuration_generation
        self._schedule_freshness_expiries(now, radiant_id)
        self.explicit_transition = False
        self.controller.request(snapshot)

    def _capture_critical_locations(
        self, primary_state: State | None, now: datetime
    ) -> tuple[CapturedCriticalLocation, ...]:
        primary = self._state_temperature_c(primary_state, SourceKind.PRIMARY_AIR)
        captured = []
        for item in self.entry.options.get("critical_locations", ()):
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id", ""))
            location_id = str(item.get("location_id", entity_id))
            local_state = self.hass.states.get(entity_id)
            local = self._state_temperature_c(local_state, SourceKind.LOCAL_AIR)
            update = update_critical_delta(
                self.critical_delta_states.get(location_id, CriticalDeltaState()),
                primary_air_temperature_c=primary or 0.0,
                local_air_temperature_c=local or 0.0,
                observed_at=(
                    (getattr(local_state, "last_reported", None) or local_state.last_updated)
                    if local_state is not None
                    else now
                ),
                valid=primary is not None and local is not None,
            )
            captured.append(
                CapturedCriticalLocation(
                    location_id,
                    str(item.get("mode", "monitoring")),
                    self._snapshot_state(local_state),
                    update,
                )
            )
        return tuple(captured)

    def _snapshot_state(self, state: State | None) -> StateValue | None:
        if state is None:
            return None
        registry_entry = er.async_get(self.hass).async_get(state.entity_id)
        return snapshot_state(
            state,
            registry_identity=(registry_entry.id if registry_entry is not None else None),
            source_generation=self.source_generation,
        )

    def _snapshot_mold_indicator(self, state: State | None) -> StateValue | None:
        """Capture the Mold Indicator critical-point attribute as estimated surface data."""

        if state is None:
            return None
        registry_entry = er.async_get(self.hass).async_get(state.entity_id)
        captured = snapshot_state(
            state,
            attributes=(MOLD_INDICATOR_CRITICAL_TEMP_ATTRIBUTE,),
            registry_identity=(registry_entry.id if registry_entry is not None else None),
            source_generation=self.source_generation,
        )
        assert captured is not None
        return StateValue(
            captured.entity_id,
            captured.attributes.get(MOLD_INDICATOR_CRITICAL_TEMP_ATTRIBUTE),
            str(self.hass.config.units.temperature_unit),
            captured.observed_at,
            captured.available,
            captured.attributes,
            captured.context_id,
            captured.parent_context_id,
            captured.registry_identity,
            captured.source_generation,
        )

    @staticmethod
    def _state_temperature_c(state: State | None, kind: SourceKind) -> float | None:
        if state is None:
            return None
        converted = convert_source_value(
            kind,
            state.state,
            str(state.attributes.get("unit_of_measurement", "")),
        )
        return converted[0] if converted is not None else None

    @callback
    def _publish_calculation(self, generation: int, result: RuntimeCalculation) -> None:
        self.source_states = dict(result.source_states)
        self._update_failure_hold(result.hold_condition)
        self._update_observability_conditions(result)
        calculation_payload = asdict(result)
        calculation_payload.pop("source_states", None)
        self.trace_ring.add(
            generation=generation,
            payload={
                "strategy": self.strategy,
                "boost_mode": self.boost_mode,
                "history_quality": result.history_quality,
                "running_mean_c": result.running_mean_c,
                "relative_humidity_provenance": result.relative_humidity_provenance,
                "quality_reasons": result.quality_reasons,
                "calculation": calculation_payload,
                "ownership": {
                    identity: asdict(state) for identity, state in self.ownership.items()
                },
            },
        )
        self._reconcile_targets(result)
        self._schedule_stale_safety(result)
        values = {**self.values, **self._observable_values(result)}
        values["calculation_generation"] = generation
        values["control_enabled"] = self.control_enabled
        values["ownership"] = {
            identity: state.ownership.value for identity, state in self.ownership.items()
        }
        values["data_readiness"] = {
            identity: state.data_readiness.value for identity, state in self.ownership.items()
        }
        values["target_readiness"] = {
            identity: state.target_readiness.value for identity, state in self.ownership.items()
        }
        values["control_status"] = self._control_status()
        values["control_eligible"] = (
            self.control_enabled
            and bool(result.targets)
            and all(
                self.ownership[target.registry_identity].ownership is Ownership.OWNED
                and target.mapping is not None
                and target.result is not None
                and target.result.normalized is not None
                and target.suppression_reason is None
                for target in result.targets
            )
        )
        self.publish(values)
        self._create_task(self._async_apply_calculation(result))

        numerical = next((item.result for item in result.targets if item.result is not None), None)
        if numerical is not None and numerical.policy is not None:
            if (
                self.boost_mode == BoostMode.RAPID.value
                and numerical.policy.rapid_boost_reached
                and not self.rapid_boost_reached
            ):
                self.rapid_boost_reached = True
                self._schedule_runtime_persistence()
            self.previous_requested = (
                numerical.policy.heating_c,
                numerical.policy.cooling_c,
            )
            self.previous_requested_at = dt_util.utcnow()

    def _observable_values(self, result: RuntimeCalculation) -> dict[str, Any]:
        """Keep the last valid output visible while clearly labelling held data."""

        projected = result_values(result)
        primary = dict(result.source_states).get("primary")
        accepted = primary.last_accepted if primary is not None else None
        primary_fresh = (
            result.primary_value_c is not None
            and primary is not None
            and not primary.recovering
            and accepted is not None
            and accepted.observed_at is not None
        )
        if primary_fresh and projected.get("thermal_sensation") is not None:
            assert accepted is not None
            self.last_valid_values = {
                key: deepcopy(projected[key])
                for key in _HELD_VALUE_KEYS
                if key in projected and projected[key] is not None and projected[key] != "unknown"
            }
            self.last_valid_at = accepted.observed_at
            self._schedule_last_valid_persistence()
        elif result.hold_condition is not None and self.last_valid_values:
            for key, value in self.last_valid_values.items():
                projected_value = projected.get(key)
                if (
                    projected_value is None
                    or projected_value == "unknown"
                    or (
                        key in {"effective_targets", "effective_target_details"}
                        and not projected_value
                    )
                ):
                    projected[key] = deepcopy(value)
            details = projected.get("effective_target_details", {})
            if isinstance(details, dict):
                projected["effective_target_details"] = {
                    target_uuid: {
                        **detail,
                        "mode": "stale_hold",
                        "reason": result.hold_condition,
                        "stale": True,
                    }
                    for target_uuid, detail in details.items()
                    if isinstance(detail, dict)
                }
            if self.stale_safety_applied:
                projected["effective_targets"] = deepcopy(
                    self.values.get("effective_targets", projected.get("effective_targets", {}))
                )
                projected["effective_target_details"] = deepcopy(
                    self.values.get(
                        "effective_target_details",
                        projected.get("effective_target_details", {}),
                    )
                )
        now = dt_util.utcnow()
        projected["data_quality"] = (
            "current"
            if primary_fresh and projected.get("thermal_sensation") is not None
            else "stale"
            if self.last_valid_values and result.hold_condition is not None
            else "unavailable"
        )
        projected["last_valid_at"] = (
            self.last_valid_at.isoformat() if self.last_valid_at is not None else None
        )
        projected["data_age_minutes"] = (
            max(0.0, (now - self.last_valid_at).total_seconds() / 60.0)
            if self.last_valid_at is not None
            else None
        )
        projected["stale_safety_active"] = bool(self.stale_safety_applied)
        return projected

    def _schedule_last_valid_persistence(self) -> None:
        """Persist changed last-valid display values without blocking publication."""

        if self.persistence is None or self.last_valid_at is None:
            return
        payload = json.dumps(
            self.last_valid_values,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if payload == self.last_valid_persistence_payload:
            return
        self.last_valid_persistence_payload = payload
        self._create_task(self._async_persist_last_valid_output(payload, self.last_valid_at))

    async def _async_persist_last_valid_output(self, payload: str, observed_at: datetime) -> None:
        persistence = self.persistence
        if persistence is None:
            return
        saved = await persistence.async_update_last_valid_output(
            values_json=payload,
            observed_at=observed_at.isoformat(),
        )
        if not saved and self.last_valid_persistence_payload == payload:
            self.last_valid_persistence_payload = None

    def _schedule_stale_safety(self, result: RuntimeCalculation) -> None:
        """Arm one bounded de-escalation after one hour of stale primary data."""

        if not self.control_enabled:
            if (cancel := self.timers.pop("stale_safety", None)) is not None:
                cancel()
            self.stale_safety_applied.clear()
            return
        if not (
            result.hold_condition is not None
            and result.hold_condition.startswith("primary_temperature_")
        ):
            if (cancel := self.timers.pop("stale_safety", None)) is not None:
                cancel()
            if any(
                target.result is not None and target.result.normalized is not None
                for target in result.targets
            ):
                self.stale_safety_applied.clear()
            return
        reference = self._primary_safety_reference(result)
        if reference is None:
            return
        _value, observed_at = reference
        pending = {
            str(target["registry_identity"]) for target in self.entry.data.get("targets", ())
        } - self.stale_safety_applied
        if not pending:
            return
        now = dt_util.utcnow()
        delay = max(
            0.0,
            (observed_at + STALE_SAFETY_DELAY - now).total_seconds(),
        )
        self._replace_timer(
            "stale_safety",
            delay,
            lambda: self._create_task(self._async_apply_stale_safety()),
        )

    async def _async_apply_stale_safety(self) -> None:
        """Withdraw stale heating/cooling demand using configured fallback targets."""

        if not self.control_enabled or self.broker is None:
            return
        reference = self._primary_safety_reference()
        now = dt_util.utcnow()
        if reference is None or now - reference[1] < STALE_SAFETY_DELAY:
            return
        last_primary_c, observed_at = reference
        outcomes: dict[str, str] = {}
        for configured in self.entry.data.get("targets", ()):
            identity = str(configured["registry_identity"])
            if identity in self.stale_safety_applied:
                continue
            entity_id = str(configured["entity_id"])
            capability = capability_from_state(self.hass.states.get(entity_id))
            mapping = resolve_capability(capability)
            if not isinstance(mapping, CapabilityMapping):
                outcomes[str(configured["target_uuid"])] = "stale_safety_target_not_ready"
                continue
            normalized = self._stale_safety_target(
                str(configured["target_uuid"]),
                capability,
                mapping,
                last_primary_c,
            )
            if normalized is None:
                outcomes[str(configured["target_uuid"])] = "stale_safety_not_required"
                continue
            target = TargetCalculation(
                str(configured["target_uuid"]),
                identity,
                entity_id,
                capability,
                mapping,
                None,
                None,
            )
            intent = self._intent_from_result(
                target,
                normalized,
                mapping,
                now,
                explicit_transition=True,
                safety_deescalation=True,
            )
            outcome = await self.broker.async_submit(intent, now=now)
            outcomes[target.target_uuid] = outcome.reason
            accepted_outcome = (
                outcome.dispatch_status is DispatchStatus.DISPATCHED
                or outcome.reason
                in {
                    "command_pending",
                    "command_interval",
                    "target_unchanged",
                }
            )
            if accepted_outcome:
                self.stale_safety_applied.add(identity)
                self._publish_stale_safety_target(target.target_uuid, normalized)
            self._schedule_broker_deadline(
                identity,
                acknowledgement=outcome.acknowledgement_status
                in {AcknowledgementStatus.PENDING, AcknowledgementStatus.UNKNOWN},
                queued=outcome.reason == "command_interval",
                explicit_transition=True,
            )
            if outcome.reason == "command_outcome_unknown":
                self._transition(identity, OwnershipEvent.COMMAND_UNKNOWN)
        values = {
            **self.values,
            "command_outcomes": outcomes,
            "stale_safety_active": bool(self.stale_safety_applied),
            "control_eligible": False,
        }
        if self.stale_safety_applied:
            values["control_status"] = "stale_safety"
        self.publish(values)
        self.trace_ring.add(
            generation=int(self.values.get("calculation_generation", 0)),
            payload={
                "event": "stale_safety",
                "command_outcomes": outcomes,
                "last_valid_primary_temperature_c": last_primary_c,
                "last_valid_primary_at": observed_at.isoformat(),
            },
        )

    def _primary_safety_reference(
        self, result: RuntimeCalculation | None = None
    ) -> tuple[float, datetime] | None:
        """Return measured stale evidence without promoting it to a fresh observation."""

        source_states = dict(result.source_states) if result is not None else self.source_states
        primary = source_states.get("primary")
        accepted = primary.last_accepted if primary is not None else None
        candidates: list[tuple[float, datetime]] = []
        if accepted is not None and accepted.value is not None and accepted.observed_at is not None:
            candidates.append((float(accepted.value), accepted.observed_at))
        entity_id = str(self.entry.data.get("primary_temperature", ""))
        state = self.hass.states.get(entity_id) if entity_id else None
        value = self._state_temperature_c(state, SourceKind.PRIMARY_AIR)
        observed_at = (
            getattr(state, "last_reported", None) or state.last_updated
            if state is not None
            else None
        )
        if value is not None and observed_at is not None:
            candidates.append((value, observed_at))
        return max(candidates, key=lambda item: item[1]) if candidates else None

    def _stale_safety_target(
        self,
        target_uuid: str,
        capability: Any,
        mapping: CapabilityMapping,
        last_primary_c: float,
    ) -> NormalizedScalarTarget | NormalizedRangeTarget | None:
        """Return only a fallback request that reduces the observed demand."""

        calibration = float(self.entry.options.get(f"calibration_{target_uuid}", 0.0))
        grid = GridOptions(
            float(self.entry.options.get("minimum_control_temperature", 18.0)),
            float(self.entry.options.get("maximum_control_temperature", 26.0)),
            calibration,
            minimum_range_gap_c=float(self.entry.options.get("minimum_range_gap", 1.0)),
        )
        if mapping.direction is ActuationDirection.RANGED:
            if capability.target_temp_low_ha is None or capability.target_temp_high_ha is None:
                return None
            current_low = (
                ha_to_celsius(capability.target_temp_low_ha, capability.temperature_unit)
                - calibration
            )
            current_high = (
                ha_to_celsius(capability.target_temp_high_ha, capability.temperature_unit)
                - calibration
            )
            demand = last_primary_c < current_low or last_primary_c > current_high
            normalized = normalize_range_target(
                requested_heating_room_c=float(self.entry.options.get("fallback_heating_c", 18.0)),
                requested_cooling_room_c=float(self.entry.options.get("fallback_cooling_c", 26.0)),
                snapshot=capability,
                options=grid,
            )
            if isinstance(normalized, ClimateFailure):
                return None
            deescalates = (
                normalized.heating.normalized_room_c <= current_low
                and normalized.cooling.normalized_room_c >= current_high
                and (
                    normalized.heating.normalized_room_c < current_low
                    or normalized.cooling.normalized_room_c > current_high
                )
            )
            return normalized if demand and deescalates else None
        if capability.scalar_target_ha is None:
            return None
        current = (
            ha_to_celsius(capability.scalar_target_ha, capability.temperature_unit) - calibration
        )
        fallback = (
            float(self.entry.options.get("fallback_heating_c", 18.0))
            if mapping.direction is ActuationDirection.HEATING_ONLY
            else float(self.entry.options.get("fallback_cooling_c", 26.0))
        )
        scalar_normalized = normalize_scalar_target(
            requested_room_c=fallback,
            direction=mapping.direction,
            snapshot=capability,
            options=grid,
        )
        if isinstance(scalar_normalized, ClimateFailure):
            return None
        if mapping.direction is ActuationDirection.HEATING_ONLY:
            return (
                scalar_normalized
                if current > last_primary_c and scalar_normalized.normalized_room_c < current
                else None
            )
        return (
            scalar_normalized
            if current < last_primary_c and scalar_normalized.normalized_room_c > current
            else None
        )

    def _publish_stale_safety_target(
        self,
        target_uuid: str,
        normalized: NormalizedScalarTarget | NormalizedRangeTarget,
    ) -> None:
        effective = deepcopy(self.values.get("effective_targets", {}))
        details = deepcopy(self.values.get("effective_target_details", {}))
        if isinstance(normalized, NormalizedScalarTarget):
            effective[target_uuid] = {"temperature": normalized.normalized_actuator_c}
        else:
            effective[target_uuid] = {
                "target_low": normalized.heating.normalized_actuator_c,
                "target_high": normalized.cooling.normalized_actuator_c,
            }
        details[target_uuid] = {
            **details.get(target_uuid, {}),
            "mode": "stale_safety",
            "reason": "primary_temperature_stale",
            "fallback": True,
            "stale": True,
            "safety_deescalation": True,
        }
        self.values["effective_targets"] = effective
        self.values["effective_target_details"] = details

    def _control_status(self) -> str:
        if not self.control_enabled:
            return Ownership.DISABLED.value
        if self.stale_safety_applied:
            return "stale_safety"
        ownerships = {state.ownership for state in self.ownership.values()}
        for ownership in (
            Ownership.COMMAND_FAULT,
            Ownership.MANUAL_OVERRIDE,
            Ownership.RECONCILING,
        ):
            if ownership in ownerships:
                return ownership.value
        readiness = {state.target_readiness for state in self.ownership.values()}
        for target_state in (
            TargetReadiness.UNAVAILABLE,
            TargetReadiness.INCOMPATIBLE,
            TargetReadiness.SUSPENDED_MODE,
        ):
            if target_state in readiness:
                return target_state.value
        data_states = {state.data_readiness for state in self.ownership.values()}
        for data_state in (
            DataReadiness.INVALID,
            DataReadiness.HOLD_LAST_GOOD,
            DataReadiness.FALLBACK_READY,
            DataReadiness.DEGRADED_READY,
            DataReadiness.READY,
        ):
            if data_state in data_states:
                return data_state.value
        return "unknown"

    def _update_observability_conditions(self, result: RuntimeCalculation) -> None:
        history_missing = result.history_quality in {
            HistoryQuality.DIAGNOSTIC.value,
            HistoryQuality.UNAVAILABLE.value,
        }
        mandatory_missing = bool(result.targets) and all(
            target.result is None for target in result.targets
        )
        self._update_delayed_repair("missing_history_24h", history_missing, timedelta(hours=24))
        self._update_delayed_repair(
            "mandatory_input_unavailable_1h", mandatory_missing, timedelta(hours=1)
        )
        fallback = any(
            target.result is not None
            and target.result.policy is not None
            and target.result.policy.fallback
            for target in result.targets
        )
        self.transition_logger.update(
            "fallback",
            fallback,
            active_message="ATHB entered fixed fallback",
            recovery_message="ATHB recovered from fixed fallback",
        )
        self.transition_logger.update(
            "mandatory_input",
            mandatory_missing,
            active_message="ATHB mandatory input is unavailable",
            recovery_message="ATHB mandatory input recovered",
        )

    def _update_failure_hold(self, condition: str | None) -> None:
        """Track the normative 15-minute write-free failure interval."""

        if condition is None:
            self.failure_hold_reason = None
            self.failure_hold_elapsed = False
            if (cancel := self.timers.pop("failure_hold", None)) is not None:
                cancel()
            return
        if condition == self.failure_hold_reason:
            return
        self.failure_hold_reason = condition
        self.failure_hold_elapsed = False

        def elapse() -> None:
            if self.failure_hold_reason != condition:
                return
            self.failure_hold_elapsed = True
            self.input_generation += 1
            self.async_request_snapshot()

        self._replace_timer("failure_hold", timedelta(minutes=15).total_seconds(), elapse)

    def _update_delayed_repair(self, repair_type: str, active: bool, delay: timedelta) -> None:
        timer_key = f"repair:{repair_type}"
        if not active:
            self.repair_condition_started.pop(repair_type, None)
            if (cancel := self.timers.pop(timer_key, None)) is not None:
                cancel()
            if self.repair_manager is not None:
                self.repair_manager.update(repair_type, False)
            return
        if repair_type in self.repair_condition_started:
            return
        self.repair_condition_started[repair_type] = dt_util.utcnow()

        def activate() -> None:
            if repair_type in self.repair_condition_started and self.repair_manager is not None:
                self.repair_manager.update(repair_type, True)

        self._replace_timer(timer_key, delay.total_seconds(), activate)

    def _schedule_freshness_expiries(self, now: datetime, radiant_id: str) -> None:
        sources: list[tuple[str, str, SourceKind]] = [
            (
                "primary",
                str(self.entry.data.get("primary_temperature", "")),
                SourceKind.PRIMARY_AIR,
            ),
            (
                "rh",
                str(self.entry.data.get("rh_entity", "")),
                SourceKind.RELATIVE_HUMIDITY,
            ),
            ("outdoor", str(self.entry.data.get("outdoor_source", "")), SourceKind.OUTDOOR),
            ("radiant", radiant_id, self._radiant_source_kind()),
            (
                "air_speed",
                str(self.entry.options.get("air_speed_entity", "")),
                SourceKind.AIR_SPEED,
            ),
        ]
        sources.extend(
            (
                f"critical:{item.get('location_id', item.get('entity_id', index))!s}",
                str(item.get("entity_id", "")),
                SourceKind.LOCAL_AIR,
            )
            for index, item in enumerate(self.entry.options.get("critical_locations", ()))
            if isinstance(item, dict)
        )
        for key, entity_id, kind in sources:
            timer_key = f"freshness:{key}"
            if (cancel := self.timers.pop(timer_key, None)) is not None:
                cancel()
            state = self.hass.states.get(entity_id) if entity_id else None
            observed = (
                getattr(state, "last_reported", None) or state.last_updated
                if state is not None
                else None
            )
            if observed is None:
                continue
            deadline = observed + configured_freshness(self.entry.options, kind)
            if deadline <= now:
                continue

            @callback
            def expire(_now: Any, *, source_key: str = timer_key) -> None:
                self.timers.pop(source_key, None)
                self.input_generation += 1
                self.async_request_snapshot()

            self.timers[timer_key] = async_track_point_in_utc_time(self.hass, expire, deadline)

    def _radiant_source_kind(self) -> SourceKind:
        return {
            "direct_mrt": SourceKind.DIRECT_MRT,
            "globe": SourceKind.GLOBE,
            "surface": SourceKind.SURFACE,
            "mold_indicator": SourceKind.SURFACE,
        }.get(str(self.entry.options.get("radiant_model", "uniform")), SourceKind.DIRECT_MRT)

    def _reconcile_targets(self, result: RuntimeCalculation) -> None:
        incompatible = False
        for target in result.targets:
            state = self.ownership[target.registry_identity]
            readiness = self._target_readiness(target.capability, target.mapping)
            incompatible = incompatible or readiness is TargetReadiness.INCOMPATIBLE
            calculation = target.result
            if calculation is None or calculation.normalized is None:
                data = (
                    DataReadiness.HOLD_LAST_GOOD
                    if result.hold_condition is not None and not self.failure_hold_elapsed
                    else DataReadiness.INVALID
                )
            elif calculation.policy is not None and calculation.policy.fallback:
                data = DataReadiness.FALLBACK_READY
            else:
                data = DataReadiness.DEGRADED_READY
            self.ownership[target.registry_identity] = OwnershipState(
                state.target_identity,
                state.ownership,
                data,
                readiness,
                state.revision,
                state.external_revision,
                state.override_reason,
                state.override_expiry,
                state.resume_required,
            )
            state = self.ownership[target.registry_identity]
            if (
                self.control_enabled
                and state.ownership is Ownership.RECONCILING
                and not state.resume_required
                and readiness is TargetReadiness.AVAILABLE_SUPPORTED
                and data is not DataReadiness.INVALID
            ):
                self._transition(
                    target.registry_identity,
                    OwnershipEvent.RECONCILED,
                    target_readiness=readiness,
                    data_readiness=data,
                )
        if self.repair_manager is not None:
            self.repair_manager.update("incompatible_auto_mapping", incompatible)

    async def _async_apply_calculation(self, result: RuntimeCalculation) -> None:
        if not self.control_enabled or self.broker is None:
            return
        outcomes: dict[str, str] = {}
        now = dt_util.utcnow()
        for target in result.targets:
            calculation = target.result
            mapping = target.mapping
            if (
                calculation is None
                or calculation.normalized is None
                or mapping is None
                or target.suppression_reason is not None
            ):
                outcomes[target.target_uuid] = target.suppression_reason or "no_executable_target"
                continue
            if (
                coordination_reason := self._runtime_coordination_reason(target, result)
            ) is not None:
                outcomes[target.target_uuid] = coordination_reason
                continue
            intent = self._intent_from_result(
                target,
                calculation.normalized,
                mapping,
                now,
                explicit_transition=result.explicit_transition,
            )
            outcome = await self.broker.async_submit(intent, now=now)
            outcomes[target.target_uuid] = outcome.reason
            self._schedule_broker_deadline(
                target.registry_identity,
                acknowledgement=outcome.acknowledgement_status
                in {AcknowledgementStatus.PENDING, AcknowledgementStatus.UNKNOWN},
                queued=outcome.reason == "command_interval",
                explicit_transition=result.explicit_transition,
            )
            if outcome.reason == "command_outcome_unknown":
                self._transition(target.registry_identity, OwnershipEvent.COMMAND_UNKNOWN)
        self.publish({**self.values, "command_outcomes": outcomes})
        self.trace_ring.add(
            generation=int(self.values.get("calculation_generation", 0)),
            payload={
                "strategy": self.strategy,
                "boost_mode": self.boost_mode,
                "command_outcomes": outcomes,
                "suppression_reason": result.suppression_reason,
                "ownership": {
                    identity: asdict(state) for identity, state in self.ownership.items()
                },
            },
        )

    def _runtime_coordination_reason(
        self, target: TargetCalculation, result: RuntimeCalculation
    ) -> str | None:
        calculated = target.result
        if (
            target.mapping is None
            or calculated is None
            or not isinstance(calculated.normalized, NormalizedScalarTarget)
        ):
            return None
        opposing: list[OpposingTarget] = []
        for other in result.targets:
            if other is target or other.mapping is None:
                continue
            other_state = self.ownership[other.registry_identity]
            other_calculated = other.result
            other_normalized = other_calculated.normalized if other_calculated is not None else None
            intended = (
                other_normalized.normalized_room_c
                if other_state.ownership is Ownership.OWNED
                and isinstance(other_normalized, NormalizedScalarTarget)
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
            direction=target.mapping.direction,
            proposed_room_c=calculated.normalized.normalized_room_c,
            opposing_targets=tuple(opposing),
            minimum_range_gap_c=float(self.entry.options.get("minimum_range_gap", 1.0)),
        )
        return coordination.reason if not coordination.eligible else None

    def _intent_from_result(
        self,
        target: TargetCalculation,
        normalized: NormalizedScalarTarget | NormalizedRangeTarget,
        mapping: CapabilityMapping,
        now: datetime,
        *,
        explicit_transition: bool,
        safety_deescalation: bool = False,
    ) -> NormalizedIntent:
        state = self.ownership[target.registry_identity]
        common = (
            target.entity_id,
            target.registry_identity,
            mapping.direction,
            float(self.entry.options.get("minimum_meaningful_change", 0.1)),
            float(self.entry.options.get("feedback_resolution", 0.01)),
            self.configuration_generation,
            self.input_generation,
            self.capability_generations[target.registry_identity],
            state.revision,
            state.external_revision,
            self.entry.entry_id,
            now,
            now + timedelta(minutes=5),
            explicit_transition,
            safety_deescalation,
        )
        if isinstance(normalized, NormalizedRangeTarget):
            return NormalizedIntent(
                common[0],
                common[1],
                TargetShape.RANGE,
                common[2],
                None,
                normalized.heating.normalized_ha,
                normalized.cooling.normalized_ha,
                None,
                normalized.heating.normalized_room_c,
                normalized.cooling.normalized_room_c,
                None,
                normalized.heating.bounded_c,
                normalized.cooling.bounded_c,
                normalized.heating.grid.step_ha,
                *common[3:],
            )
        return NormalizedIntent(
            common[0],
            common[1],
            TargetShape.SCALAR,
            common[2],
            normalized.normalized_ha,
            None,
            None,
            normalized.normalized_room_c,
            None,
            None,
            normalized.bounded_c,
            None,
            None,
            normalized.grid.step_ha,
            *common[3:],
        )

    def _broker_preflight(self, identity: str) -> BrokerPreflight:
        state = self.ownership[identity]
        return BrokerPreflight(
            identity,
            self.configuration_generation,
            self.input_generation,
            self.capability_generations[identity],
            state.revision,
            state.ownership,
            state.target_readiness is TargetReadiness.AVAILABLE_SUPPORTED,
            state.data_readiness
            in {DataReadiness.READY, DataReadiness.DEGRADED_READY, DataReadiness.FALLBACK_READY},
            get_lease_registry(self.hass).owner(identity),
            True,
        )

    def _context_token(self) -> ContextToken:
        context = Context()
        return ContextToken(context.id, context)

    @staticmethod
    def _fingerprint(capability: Any) -> TargetFingerprint:
        shape = (
            TargetShape.RANGE
            if capability.target_temp_low_ha is not None
            or capability.target_temp_high_ha is not None
            else TargetShape.SCALAR
        )
        return TargetFingerprint(
            shape,
            capability.scalar_target_ha,
            capability.target_temp_low_ha,
            capability.target_temp_high_ha,
        )

    def _initialize_target_fingerprints(self) -> None:
        for target in self.entry.data.get("targets", ()):
            self.last_target_fingerprints[str(target["registry_identity"])] = self._fingerprint(
                capability_from_state(self.hass.states.get(str(target["entity_id"])))
            )

    @staticmethod
    def _target_readiness(
        capability: Any, mapping: CapabilityMapping | None = None
    ) -> TargetReadiness:
        if not capability.available:
            return TargetReadiness.UNAVAILABLE
        if capability.hvac_mode in {"off", "auto"} and mapping is None:
            return TargetReadiness.SUSPENDED_MODE
        if mapping is None and isinstance(resolve_capability(capability), ClimateFailure):
            return TargetReadiness.INCOMPATIBLE
        return TargetReadiness.AVAILABLE_SUPPORTED

    def _transition(
        self,
        identity: str,
        event: OwnershipEvent,
        *,
        target_readiness: TargetReadiness | None = None,
        data_readiness: DataReadiness | None = None,
        now: datetime | None = None,
    ) -> None:
        current = self.ownership.setdefault(identity, initial_ownership(identity))
        transition = reduce_ownership(
            current,
            event,
            now=now or dt_util.utcnow(),
            target_readiness=target_readiness,
            data_readiness=data_readiness,
            override_duration=timedelta(
                minutes=float(self.entry.options.get("manual_override_minutes", 120.0))
            ),
        )
        self.ownership[identity] = transition.state
        if transition.invalidate_intents and self.broker is not None:
            self.broker.invalidate(identity)
        if transition.state.ownership is Ownership.MANUAL_OVERRIDE:
            self._schedule_override_expiry(identity, transition.state.override_expiry)
        elif (timer := self.timers.pop(f"override:{identity}", None)) is not None:
            timer()
        self._schedule_ownership_persistence(identity)

    def _schedule_ownership_persistence(self, identity: str) -> None:
        if self.persistence is None or not hasattr(self.hass, "async_create_task"):
            return
        state = self.ownership[identity]
        self._create_task(
            self.persistence.async_update_actuator(
                identity,
                ownership=state.ownership.value,
                ownership_revision=state.revision,
                external_revision=state.external_revision,
                override_reason=state.override_reason,
                override_expiry=(
                    state.override_expiry.isoformat() if state.override_expiry is not None else None
                ),
                resume_required=state.resume_required,
            )
        )

    def _schedule_runtime_persistence(self) -> None:
        if self.persistence is None or not hasattr(self.hass, "async_create_task"):
            return
        boost_expiry = self._timer_expiry("boost")
        self._create_task(
            self.persistence.async_update_runtime(
                control_enabled=self.control_enabled,
                boost_mode=self.boost_mode,
                rapid_boost_reached=self.rapid_boost_reached,
                boost_expiry_utc=(boost_expiry.isoformat() if boost_expiry is not None else None),
            )
        )

    def _timer_expiry(self, key: str) -> datetime | None:
        return cast(datetime | None, self.values.get(f"{key}_expiry"))

    def _resolve_profile(self, now: datetime) -> ProfileResolution:
        occupancy_id = str(self.entry.options.get("occupancy_entity", ""))
        state = self.hass.states.get(occupancy_id) if occupancy_id else None
        occupancy = (
            OccupancyState.ABSENT
            if not occupancy_id
            else OccupancyState.ON
            if state is not None and state.state == "on"
            else OccupancyState.OFF
            if state is not None and state.state == "off"
            else OccupancyState.UNKNOWN
        )
        resolution = resolve_occupancy_profile(
            occupancy=occupancy,
            now=now,
            previous=self.profile_resolution,
        )
        self.profile_resolution = resolution
        return resolution

    def _schedule_override_expiry(self, identity: str, expiry: datetime | None) -> None:
        key = f"override:{identity}"
        if (old := self.timers.pop(key, None)) is not None:
            old()
        if expiry is None:
            return

        @callback
        def expire(_now: Any) -> None:
            self.timers.pop(key, None)
            self._transition(identity, OwnershipEvent.OVERRIDE_EXPIRED, now=_now)
            self.explicit_transition = True
            self.async_request_snapshot()

        self.timers[key] = async_track_point_in_utc_time(self.hass, expire, expiry)

    def _schedule_broker_deadline(
        self,
        identity: str,
        *,
        acknowledgement: bool,
        queued: bool,
        explicit_transition: bool,
    ) -> None:
        if acknowledgement:
            self._replace_timer(
                f"ack:{identity}",
                ACKNOWLEDGEMENT_DEADLINE.total_seconds(),
                lambda: self._create_task(self._async_acknowledgement_timeout(identity)),
            )
        if queued:
            interval = HARD_COMMAND_INTERVAL if explicit_transition else ORDINARY_COMMAND_INTERVAL
            self._replace_timer(
                f"queue:{identity}",
                interval.total_seconds(),
                lambda: self._create_task(self._async_drain_queued(identity)),
            )

    def _replace_timer(self, key: str, delay: float, action: Callable[[], None]) -> None:
        if (old := self.timers.pop(key, None)) is not None:
            old()
        if not hasattr(self.hass, "loop"):
            return

        @callback
        def fire(_now: Any) -> None:
            self.timers.pop(key, None)
            action()

        self.timers[key] = async_call_later(self.hass, delay, fire)

    async def _async_acknowledgement_timeout(self, identity: str) -> None:
        if self.broker is None:
            return
        outcome = await self.broker.async_acknowledgement_timeout(identity, now=dt_util.utcnow())
        if outcome is not None:
            self._transition(identity, OwnershipEvent.COMMAND_UNKNOWN)
            self.publish({**self.values, "last_command_outcome": outcome.reason})

    async def _async_drain_queued(self, identity: str) -> None:
        if self.broker is None:
            return
        outcome = await self.broker.async_drain_queued(identity, now=dt_util.utcnow())
        if outcome is not None:
            self._schedule_broker_deadline(
                identity,
                acknowledgement=(outcome.acknowledgement_status is AcknowledgementStatus.PENDING),
                queued=outcome.reason == "command_interval",
                explicit_transition=False,
            )

    def _outdoor_sample(self, now: datetime, state: State | None = None) -> OutdoorSample | None:
        outdoor = state or self.hass.states.get(str(self.entry.data.get("outdoor_source", "")))
        if outdoor is None:
            return None
        converted = convert_source_value(
            SourceKind.OUTDOOR,
            outdoor.state,
            str(outdoor.attributes.get("unit_of_measurement", "")),
        )
        value = converted[0] if converted is not None else None
        observed = getattr(outdoor, "last_reported", None) or outdoor.last_updated or now
        return OutdoorSample(observed.astimezone(UTC), value, value is not None)

    @callback
    def subscribe(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self.update_callbacks.add(update_callback)

        @callback
        def remove() -> None:
            self.update_callbacks.discard(update_callback)

        return remove

    @callback
    def publish(self, values: dict[str, Any]) -> None:
        self.values = values
        for update_callback in tuple(self.update_callbacks):
            update_callback()

    async def async_set_strategy(self, strategy: str) -> None:
        if strategy == self.strategy:
            return
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_COMFORT_STRATEGY: strategy}
        )
        self.strategy = strategy
        self.configuration_generation += 1
        self.explicit_transition = True
        if self.controller is not None:
            self.controller.invalidate()
            if self.debounce_cancel is not None:
                self.debounce_cancel()
                self.debounce_cancel = None
                self.debounce_started = None
            if self.broker is not None:
                for identity in self.ownership:
                    self.broker.invalidate(identity)
            self.async_request_snapshot()
        self.publish({**self.values, "strategy": strategy, "reason": "strategy_changed"})
        self._schedule_runtime_persistence()

    async def async_set_boost_mode(self, boost_mode: str) -> None:
        BoostMode(boost_mode)
        same_active_mode = boost_mode == self.boost_mode and boost_mode != BoostMode.OFF.value
        if boost_mode == self.boost_mode and not same_active_mode:
            return
        if boost_mode == BoostMode.OFF.value:
            if (timer := self.timers.pop("boost", None)) is not None:
                timer()
            self.values["boost_expiry"] = None
        else:
            self._schedule_boost_expiry()
        self.boost_mode = boost_mode
        self.rapid_boost_reached = False
        self.explicit_transition = True
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_BOOST_MODE: boost_mode}
        )
        self._schedule_runtime_persistence()
        self.async_request_snapshot()

    async def async_set_eco_intensity(self, intensity: str) -> None:
        if intensity == self.eco_intensity:
            return
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_ECO_INTENSITY: intensity}
        )
        self.eco_intensity = intensity
        self.configuration_generation += 1
        self.explicit_transition = True
        if self.controller is not None:
            self.controller.invalidate()
            if self.debounce_cancel is not None:
                self.debounce_cancel()
                self.debounce_cancel = None
                self.debounce_started = None
            if self.broker is not None:
                for identity in self.ownership:
                    self.broker.invalidate(identity)
            self.async_request_snapshot()
        self.publish({**self.values, "eco_intensity": intensity, "reason": "eco_intensity_changed"})

    def _schedule_boost_expiry(self, expiry: datetime | None = None) -> None:
        expiry = expiry or (
            dt_util.utcnow()
            + timedelta(minutes=float(self.entry.options.get("boost_duration_minutes", 60.0)))
        )
        self.values["boost_expiry"] = expiry

        @callback
        def expire(_now: Any) -> None:
            self.timers.pop("boost", None)
            self._create_task(self.async_set_boost_mode(BoostMode.OFF.value))

        if (old := self.timers.pop("boost", None)) is not None:
            old()
        if hasattr(self.hass, "loop"):
            self.timers["boost"] = async_track_point_in_utc_time(self.hass, expire, expiry)

    async def _async_acquire_target_leases(self) -> None:
        acquired: list[str] = []
        for target in self.entry.data.get("targets", ()):
            identity = str(target["registry_identity"])
            self.capability_generations.setdefault(identity, 1)
            if not get_lease_registry(self.hass).acquire(identity, self.entry.entry_id):
                for acquired_identity in acquired:
                    get_lease_registry(self.hass).release(acquired_identity, self.entry.entry_id)
                if self.repair_manager is not None:
                    self.repair_manager.update("duplicate_target_ownership", True)
                raise ValueError("target_already_controlled")
            acquired.append(identity)
        for identity in acquired:
            self._transition(identity, OwnershipEvent.ENABLE)

    async def async_set_control_enabled(self, enabled: bool) -> None:
        if enabled == self.control_enabled:
            return
        if enabled:
            await self._async_acquire_target_leases()
        else:
            for target in self.entry.data.get("targets", ()):
                identity = str(target["registry_identity"])
                get_lease_registry(self.hass).release(identity, self.entry.entry_id)
                self._transition(identity, OwnershipEvent.DISABLE)
        self.control_enabled = enabled
        self.explicit_transition = True
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_CONTROL_ENABLED: enabled}
        )
        self.publish({**self.values, "control_enabled": enabled})
        self._schedule_runtime_persistence()
        self.async_request_snapshot()

    async def async_resume(self) -> None:
        if self.persistence is not None:
            self.persistence.requires_resume = False
        for identity in self.ownership:
            if self.broker is not None:
                await self.broker.async_resume_target(identity)
            if (timer := self.timers.pop(f"ack:{identity}", None)) is not None:
                timer()
            if (timer := self.timers.pop(f"queue:{identity}", None)) is not None:
                timer()
            self._transition(identity, OwnershipEvent.RESUME)
        self.explicit_transition = True
        self.publish({**self.values, "resume_requested": True})
        self._schedule_runtime_persistence()
        self.async_request_snapshot()

    def _create_task(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = self.hass.async_create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def async_unload(self) -> None:
        self.runtime_generation += 1
        if self.broker is not None:
            self.broker.close_gate()
        if self.debounce_cancel is not None:
            self.debounce_cancel()
            self.debounce_cancel = None
        for cancel in self.timers.values():
            cancel()
        self.timers.clear()
        if self.controller is not None:
            await self.controller.async_stop()
        for unsubscribe in self.listeners:
            unsubscribe()
        self.listeners.clear()
        if self.background_tasks:
            _done, pending = await asyncio.wait(self.background_tasks, timeout=15)
            for task in pending:
                task.cancel()
        self.update_callbacks.clear()
        for target in self.entry.data.get("targets", ()):
            get_lease_registry(self.hass).release(
                str(target["registry_identity"]), self.entry.entry_id
            )
        if self.history_collector is not None:
            collector = self.history_collector
            get_history_manager(self.hass).release(
                self.history_source_identity or str(self.entry.data.get("outdoor_source", "")),
                str(self.hass.config.time_zone),
                self._history_updated,
            )
            if collector.references <= 0:
                await collector.async_close()
        if self.persistence is not None:
            await self.persistence.async_mark_clean(now=dt_util.utcnow())


type AthbConfigEntry = ConfigEntry[ZoneRuntime]


def get_global_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if (semaphore := domain_data.get("calculation_semaphore")) is None:
        semaphore = asyncio.Semaphore(2)
        domain_data["calculation_semaphore"] = semaphore
    return cast(asyncio.Semaphore, semaphore)


def get_lease_registry(hass: HomeAssistant) -> TargetLeaseRegistry:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if (leases := domain_data.get("target_leases")) is None:
        leases = TargetLeaseRegistry()
        domain_data["target_leases"] = leases
    return cast(TargetLeaseRegistry, leases)


def get_history_manager(hass: HomeAssistant) -> OutdoorHistoryManager:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if (manager := domain_data.get("outdoor_history")) is None:
        manager = OutdoorHistoryManager()
        domain_data["outdoor_history"] = manager
    return cast(OutdoorHistoryManager, manager)


def runtime_from_entry(hass: HomeAssistant, entry: AthbConfigEntry) -> ZoneRuntime:
    return ZoneRuntime(
        hass,
        entry,
        str(entry.data["zone_uuid"]),
        str(entry.options.get(CONF_COMFORT_STRATEGY, DEFAULT_STRATEGY)),
        str(entry.options.get(CONF_BOOST_MODE, DEFAULT_BOOST_MODE)),
        bool(entry.options.get(CONF_CONTROL_ENABLED, False)),
        str(entry.options.get(CONF_ECO_INTENSITY, "custom")),
    )
