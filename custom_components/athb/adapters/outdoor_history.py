"""Shared persisted outdoor-history collection for Home Assistant runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..core.history import (
    HISTORY_SCHEMA_VERSION,
    MAX_BOOTSTRAP_RECORDS,
    DailyHistoryIntegrator,
    DailySummary,
    HistoryQuality,
    OutdoorSample,
    RunningMeanResult,
    bootstrap_history,
    collection_policy_fingerprint,
    load_history_state,
    retain_recent_summaries,
    running_mean,
    serialize_history_state,
)
from ..core.sources import SourceKind, convert_source_value


class HistoryReader(Protocol):
    async def read_window(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[OutdoorSample, ...]: ...


@dataclass(slots=True)
class OutdoorHistoryCollector:
    """One physical source lineage shared by all referencing zones."""

    hass: HomeAssistant
    source_identity: str
    timezone: str
    reader: HistoryReader | None
    entity_id: str = ""
    references: int = 0
    bootstrap_count: int = 0
    storage_generation: int = 0
    summaries: tuple[DailySummary, ...] = ()
    integrator: DailyHistoryIntegrator | None = None
    corrupt_payload: object | None = None
    storage_fault: bool = False
    _store: Store[dict[str, object]] = field(init=False, repr=False)
    _emitted_summaries: int = field(default=0, init=False, repr=False)
    _dirty_generation: int = field(default=0, init=False, repr=False)
    _saved_generation: int = field(default=0, init=False, repr=False)
    _save_cancel: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _save_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _save_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _last_live_sample: tuple[datetime, float | None, bool] | None = field(
        default=None, init=False, repr=False
    )
    _subscribers: set[Callable[[], None]] = field(default_factory=set, init=False, repr=False)
    _listener_cancel: Callable[[], None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        digest = sha256(f"{self.source_identity}|{self.timezone}".encode()).hexdigest()[:24]
        self._store = Store(
            self.hass,
            HISTORY_SCHEMA_VERSION,
            f"athb.outdoor.{digest}",
            atomic_writes=True,
        )

    async def async_start(self, *, now: datetime, current: OutdoorSample | None) -> None:
        try:
            payload = await self._store.async_load()
        except Exception:
            payload = None
            self.storage_fault = True
        loaded = load_history_state(payload) if payload is not None else None
        self.corrupt_payload = loaded.corrupt_payload if loaded is not None else None
        if loaded is not None and not loaded.reasons:
            self.summaries = loaded.summaries
            self.storage_generation = loaded.storage_generation or 0
        local_now = now.astimezone(ZoneInfo(self.timezone))
        today_start = datetime.combine(
            local_now.date(), time.min, ZoneInfo(self.timezone)
        ).astimezone(UTC)
        restorable_accumulator = loaded
        if not (
            restorable_accumulator is not None
            and not restorable_accumulator.reasons
            and restorable_accumulator.current_day_accumulator is not None
            and restorable_accumulator.last_integrated_utc is not None
            and restorable_accumulator.last_integrated_utc <= now
        ):
            restorable_accumulator = None
        recorder_records: tuple[OutdoorSample, ...] = ()
        if self.reader is not None:
            recorder_start = (
                restorable_accumulator.last_integrated_utc
                if restorable_accumulator is not None
                else today_start - timedelta(days=7, hours=2)
                if not self.summaries
                else today_start
            )
            assert recorder_start is not None
            recorder_records = await self._read_recorder_window(
                start_utc=recorder_start, end_utc=now
            )
        if not self.summaries and self.reader is not None and restorable_accumulator is None:
            self.bootstrap_count += 1
            start = today_start - timedelta(days=7, hours=2)
            bootstrap = bootstrap_history(
                records=tuple(
                    record for record in recorder_records if record.observed_at <= today_start
                ),
                timezone=self.timezone,
                start_utc=start,
                end_utc=today_start,
                expected_source_generation=1,
                current_source_generation=1,
                completed_at=now,
                deadline_utc=now + timedelta(seconds=30),
            )
            if bootstrap.accepted:
                self.summaries = retain_recent_summaries(bootstrap.summaries)
        if restorable_accumulator is not None:
            assert restorable_accumulator.current_day_accumulator is not None
            assert restorable_accumulator.last_integrated_utc is not None
            self.integrator = DailyHistoryIntegrator.restore(
                timezone=self.timezone,
                accumulator=restorable_accumulator.current_day_accumulator,
                last_valid_observation=restorable_accumulator.last_valid_observation,
                last_integrated_utc=restorable_accumulator.last_integrated_utc,
            )
        else:
            self.integrator = DailyHistoryIntegrator(timezone=self.timezone, start_utc=today_start)
        replay_start = self.integrator.cursor
        for record in recorder_records:
            if replay_start <= record.observed_at <= now:
                self.integrator.add_sample(record)
        if current is not None and current.observed_at > self.integrator.cursor:
            self.integrator.add_sample(current)
        self.integrator.advance_to(now)
        self._collect_completed_summaries()
        await self.async_save()
        self._ensure_listener()

    def _ensure_listener(self) -> None:
        if self._listener_cancel is not None or not self.entity_id or self.references <= 0:
            return

        @callback
        def state_event(event: Event[Any]) -> None:
            state = cast(State | None, event.data.get("new_state"))
            now = dt_util.utcnow()
            if state is None:
                sample = OutdoorSample(now, None, False)
            else:
                converted = convert_source_value(
                    SourceKind.OUTDOOR,
                    state.state,
                    str(state.attributes.get("unit_of_measurement", "")),
                )
                observed = getattr(state, "last_reported", None) or state.last_updated
                sample = OutdoorSample(
                    observed.astimezone(UTC),
                    converted[0] if converted is not None else None,
                    converted is not None,
                )
            self.add_sample(sample, now=now)
            for subscriber in tuple(self._subscribers):
                subscriber()

        self._listener_cancel = async_track_state_change_event(
            self.hass, {self.entity_id}, state_event
        )

    def update_entity_id(self, entity_id: str) -> None:
        if entity_id == self.entity_id:
            return
        if self._listener_cancel is not None:
            self._listener_cancel()
            self._listener_cancel = None
        self.entity_id = entity_id
        self._ensure_listener()

    def subscribe(self, subscriber: Callable[[], None]) -> None:
        self._subscribers.add(subscriber)

    async def _read_recorder_window(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[OutdoorSample, ...]:
        """Read bounded day-sized Recorder chunks under one 30-second deadline."""

        if self.reader is None or end_utc <= start_utc:
            return ()
        records: list[OutdoorSample] = []
        cursor = start_utc
        try:
            async with asyncio.timeout(30):
                while cursor < end_utc:
                    chunk_end = min(cursor + timedelta(days=1), end_utc)
                    records.extend(
                        await self.reader.read_window(start_utc=cursor, end_utc=chunk_end)
                    )
                    if len(records) > MAX_BOOTSTRAP_RECORDS:
                        return ()
                    cursor = chunk_end
        except TimeoutError:
            return ()
        unique = {
            (sample.observed_at, sample.value_c, sample.valid, sample.provenance): sample
            for sample in records
            if start_utc <= sample.observed_at <= end_utc
        }
        return tuple(sorted(unique.values(), key=lambda sample: sample.observed_at))

    def add_sample(self, sample: OutdoorSample, *, now: datetime) -> None:
        if self.integrator is None:
            return
        signature = (sample.observed_at, sample.value_c, sample.valid)
        if signature == self._last_live_sample:
            return
        try:
            self.integrator.add_sample(sample)
            self.integrator.advance_to(now)
        except ValueError:
            return
        self._last_live_sample = signature
        rolled_over = self._collect_completed_summaries()
        self._dirty_generation += 1
        if rolled_over:
            self._start_save_task()
        else:
            self._schedule_save()

    def _collect_completed_summaries(self) -> bool:
        if self.integrator is None:
            return False
        completed = self.integrator.summaries[self._emitted_summaries :]
        if not completed:
            return False
        self.summaries = retain_recent_summaries((*self.summaries, *completed))
        self._emitted_summaries = len(self.integrator.summaries)
        return True

    def _schedule_save(self) -> None:
        if self._save_cancel is not None or self.references <= 0:
            return

        @callback
        def fire(_now: object) -> None:
            self._save_cancel = None
            self._start_save_task()

        self._save_cancel = async_call_later(self.hass, timedelta(minutes=5), fire)

    def _start_save_task(self) -> None:
        if self._save_task is not None and not self._save_task.done():
            return
        task = self.hass.async_create_task(self.async_save())
        self._save_task = task
        task.add_done_callback(self._save_finished)

    def _save_finished(self, task: asyncio.Task[None]) -> None:
        if self._save_task is task:
            self._save_task = None

    def result(self, *, now: datetime, alpha: float = 0.8) -> RunningMeanResult:
        result = running_mean(
            summaries=self.summaries,
            current_local_date=now.astimezone(ZoneInfo(self.timezone)).date(),
            alpha=alpha,
        )
        return (
            replace(
                result,
                reasons=tuple(dict.fromkeys((*result.reasons, "history_storage_unavailable"))),
            )
            if self.storage_fault
            else result
        )

    async def async_save(self) -> None:
        if self._save_cancel is not None:
            self._save_cancel()
            self._save_cancel = None
        async with self._save_lock:
            saving_generation = self._dirty_generation
            self.storage_generation += 1
            current = self.integrator.current_summary if self.integrator is not None else None
            last = self.integrator.last_valid_observation if self.integrator is not None else None
            integrated = self.integrator.cursor if self.integrator is not None else None
            payload = serialize_history_state(
                source_uuid=sha256(self.source_identity.encode()).hexdigest(),
                source_identity=self.source_identity,
                source_generation=1,
                timezone=self.timezone,
                policy_fingerprint=collection_policy_fingerprint(
                    maximum_hold_seconds=7200,
                    coverage_threshold=0.9,
                ),
                summaries=self.summaries,
                current_day_accumulator=current,
                last_valid_observation=last,
                last_integrated_utc=integrated,
                storage_generation=self.storage_generation,
            )
            try:
                await self._store.async_save(payload)
            except Exception:
                self.storage_fault = True
                self._saved_generation = saving_generation - 1
            else:
                self.storage_fault = False
                self._saved_generation = saving_generation
        if self._dirty_generation != self._saved_generation:
            self._schedule_save()

    async def async_close(self) -> None:
        """Flush the shared collector and cancel all pending persistence work."""

        await self.async_save()
        task = self._save_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            await task
        if self._save_cancel is not None:
            self._save_cancel()
            self._save_cancel = None
        if self._listener_cancel is not None:
            self._listener_cancel()
            self._listener_cancel = None
        self._subscribers.clear()


class OutdoorHistoryManager:
    """Reference-counted domain registry with one collector per source/timezone."""

    def __init__(self) -> None:
        self._collectors: dict[tuple[str, str], OutdoorHistoryCollector] = {}

    def acquire(
        self,
        *,
        hass: HomeAssistant,
        source_identity: str,
        timezone: str,
        reader: HistoryReader | None,
        entity_id: str | None = None,
        subscriber: Callable[[], None] | None = None,
    ) -> tuple[OutdoorHistoryCollector, bool]:
        key = (source_identity, timezone)
        collector = self._collectors.get(key)
        created = collector is None
        if collector is None:
            collector = OutdoorHistoryCollector(
                hass, source_identity, timezone, reader, entity_id or source_identity
            )
            self._collectors[key] = collector
        elif entity_id is not None:
            collector.update_entity_id(entity_id)
        if subscriber is not None:
            collector.subscribe(subscriber)
        collector.references += 1
        return collector, created

    def release(
        self,
        source_identity: str,
        timezone: str,
        subscriber: Callable[[], None] | None = None,
    ) -> bool:
        key = (source_identity, timezone)
        collector = self._collectors.get(key)
        if collector is None:
            return False
        if subscriber is not None:
            collector._subscribers.discard(subscriber)
        collector.references -= 1
        if collector.references <= 0:
            if collector._save_cancel is not None:
                collector._save_cancel()
                collector._save_cancel = None
            del self._collectors[key]
        return True

    @property
    def source_count(self) -> int:
        return len(self._collectors)


def unavailable_history() -> RunningMeanResult:
    return RunningMeanResult(
        None, HistoryQuality.UNAVAILABLE, 0, 0.0, None, ("running_mean_unavailable",)
    )
