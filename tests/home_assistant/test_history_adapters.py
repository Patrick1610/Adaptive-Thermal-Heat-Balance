"""Recorder and shared outdoor-history adapter contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from homeassistant.core import State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.athb.adapters import outdoor_history as outdoor_history_adapter
from custom_components.athb.adapters import recorder as recorder_adapter
from custom_components.athb.adapters.outdoor_history import (
    OutdoorHistoryCollector,
    OutdoorHistoryManager,
    unavailable_history,
)
from custom_components.athb.adapters.recorder import HomeAssistantRecorderHistoryReader
from custom_components.athb.core.history import HistoryQuality, OutdoorSample

NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)


class MemoryStore:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload
        self.saves: list[dict[str, object]] = []

    async def async_load(self) -> dict[str, object] | None:
        return self.payload

    async def async_save(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.saves.append(payload)


class FailingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def async_load(self) -> dict[str, object] | None:
        if self.fail:
            raise OSError("simulated history read failure")
        return await super().async_load()

    async def async_save(self, payload: dict[str, object]) -> None:
        if self.fail:
            raise OSError("simulated history write failure")
        await super().async_save(payload)


class HistoryReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_window(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[OutdoorSample, ...]:
        self.calls += 1
        count = int((end_utc - start_utc).total_seconds() // 3600)
        return tuple(
            OutdoorSample(start_utc + timedelta(hours=index), 5.0) for index in range(count + 1)
        )


async def test_collector_bootstraps_persists_integrates_and_manager_reference_counts(
    hass: Any,
) -> None:
    reader = HistoryReader()
    collector = OutdoorHistoryCollector(hass, "sensor.outdoor", "Europe/Amsterdam", reader)
    store = MemoryStore()
    collector._store = cast(Any, store)
    await collector.async_start(now=NOW, current=OutdoorSample(NOW, 6.0))
    assert reader.calls == 8
    assert collector.bootstrap_count == 1
    assert collector.summaries
    assert store.saves
    assert collector.result(now=NOW).quality in {
        HistoryQuality.COMPLETE,
        HistoryQuality.PARTIAL,
    }
    running_mean_before_current_day_update = collector.result(now=NOW).value_c
    collector.add_sample(OutdoorSample(NOW + timedelta(hours=1), 7.0), now=NOW + timedelta(hours=1))
    assert collector.result(now=NOW + timedelta(hours=1)).value_c == (
        running_mean_before_current_day_update
    )
    observations = collector.integrator.current_summary.observations
    collector.add_sample(OutdoorSample(NOW + timedelta(hours=1), 7.0), now=NOW + timedelta(hours=1))
    assert collector.integrator.current_summary.observations == observations
    collector.add_sample(OutdoorSample(NOW - timedelta(days=20), 7.0), now=NOW)

    manager = OutdoorHistoryManager()
    first, created = manager.acquire(
        hass=hass,
        source_identity="sensor.outdoor",
        timezone="Europe/Amsterdam",
        reader=reader,
    )
    second, created_again = manager.acquire(
        hass=hass,
        source_identity="sensor.outdoor",
        timezone="Europe/Amsterdam",
        reader=None,
    )
    assert created
    assert not created_again
    assert first is second
    assert first.references == 2
    assert not manager.release("missing", "Europe/Amsterdam")
    assert manager.release("sensor.outdoor", "Europe/Amsterdam")
    assert manager.source_count == 1
    assert manager.release("sensor.outdoor", "Europe/Amsterdam")
    assert manager.source_count == 0
    assert unavailable_history().quality is HistoryQuality.UNAVAILABLE


async def test_corrupt_history_payload_is_preserved_and_rebuilt(hass: Any) -> None:
    collector = OutdoorHistoryCollector(hass, "sensor.bad", "UTC", None)
    store = MemoryStore({"schema_version": 999})
    collector._store = cast(Any, store)
    await collector.async_start(now=NOW, current=None)
    assert collector.corrupt_payload == {"schema_version": 999}
    assert collector.integrator is not None
    assert collector.summaries == ()


async def test_history_storage_failure_keeps_memory_active_and_can_recover(hass: Any) -> None:
    collector = OutdoorHistoryCollector(hass, "sensor.outdoor", "UTC", None, references=1)
    store = FailingStore()
    collector._store = cast(Any, store)

    await collector.async_start(now=NOW, current=OutdoorSample(NOW, 5.0))

    assert collector.storage_fault
    assert "history_storage_unavailable" in collector.result(now=NOW).reasons
    store.fail = False
    await collector.async_save()
    assert not collector.storage_fault
    assert "history_storage_unavailable" not in collector.result(now=NOW).reasons
    await collector.async_close()


async def test_collector_replays_recorder_downtime_across_a_day_boundary(hass: Any) -> None:
    first_store = MemoryStore()
    first = OutdoorHistoryCollector(hass, "sensor.outdoor", "UTC", None)
    first._store = cast(Any, first_store)
    initial = NOW.replace(hour=22)
    await first.async_start(now=initial, current=OutdoorSample(initial, 5.0))
    first.add_sample(
        OutdoorSample(initial + timedelta(hours=1), 5.0),
        now=initial + timedelta(hours=1),
    )
    await first.async_save()

    reader = HistoryReader()
    restarted = OutdoorHistoryCollector(hass, "sensor.outdoor", "UTC", reader)
    restarted._store = cast(Any, first_store)
    restart_time = initial + timedelta(hours=12)
    await restarted.async_start(
        now=restart_time,
        current=OutdoorSample(restart_time, 6.0),
    )

    assert reader.calls == 1
    assert restarted.bootstrap_count == 0
    assert restarted.integrator is not None
    assert restarted.integrator.cursor == restart_time
    assert any(summary.local_date == initial.date() for summary in restarted.summaries)


async def test_dirty_collector_persists_after_five_minutes_without_polling(hass: Any) -> None:
    collector = OutdoorHistoryCollector(hass, "sensor.outdoor", "UTC", None, references=1)
    store = MemoryStore()
    collector._store = cast(Any, store)
    await collector.async_start(now=NOW, current=OutdoorSample(NOW, 5.0))
    initial_saves = len(store.saves)

    collector.add_sample(
        OutdoorSample(NOW + timedelta(minutes=1), 6.0),
        now=NOW + timedelta(minutes=1),
    )
    assert collector._save_cancel is not None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()

    assert len(store.saves) == initial_saves + 1
    assert collector._saved_generation == collector._dirty_generation
    await collector.async_close()


async def test_manager_uses_one_live_listener_for_multiple_zone_subscribers(hass: Any) -> None:
    manager = OutdoorHistoryManager()
    notifications: list[str] = []
    first, created = manager.acquire(
        hass=hass,
        source_identity="registry:outdoor",
        timezone="UTC",
        reader=None,
        entity_id="sensor.outdoor",
        subscriber=lambda: notifications.append("first"),
    )
    second, created_again = manager.acquire(
        hass=hass,
        source_identity="registry:outdoor",
        timezone="UTC",
        reader=None,
        entity_id="sensor.outdoor",
        subscriber=lambda: notifications.append("second"),
    )
    store = MemoryStore()
    first._store = cast(Any, store)
    await first.async_start(now=NOW, current=OutdoorSample(NOW, 5.0))

    assert created
    assert not created_again
    assert first is second
    assert first.references == 2
    assert first._listener_cancel is not None
    hass.states.async_set("sensor.outdoor", "6", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()

    assert sorted(notifications) == ["first", "second"]
    assert manager.release("registry:outdoor", "UTC")
    assert manager.release("registry:outdoor", "UTC")
    await first.async_close()


async def test_collector_bounded_reader_and_pre_start_paths(hass: Any, monkeypatch: Any) -> None:
    collector = OutdoorHistoryCollector(hass, "sensor.outdoor", "UTC", None)
    collector.add_sample(OutdoorSample(NOW, 5.0), now=NOW)
    assert not collector._collect_completed_summaries()
    assert await collector._read_recorder_window(start_utc=NOW, end_utc=NOW) == ()

    reader = HistoryReader()
    collector.reader = reader
    monkeypatch.setattr(outdoor_history_adapter, "MAX_BOOTSTRAP_RECORDS", 1)
    assert (
        await collector._read_recorder_window(start_utc=NOW, end_utc=NOW + timedelta(hours=2)) == ()
    )


async def test_manager_updates_live_entity_and_unsubscribes_one_zone(hass: Any) -> None:
    manager = OutdoorHistoryManager()
    notifications: list[str] = []

    def first_subscriber() -> None:
        notifications.append("first")

    def second_subscriber() -> None:
        notifications.append("second")

    collector, _created = manager.acquire(
        hass=hass,
        source_identity="registry:outdoor",
        timezone="UTC",
        reader=None,
        entity_id="sensor.outdoor",
        subscriber=first_subscriber,
    )
    collector._store = cast(Any, MemoryStore())
    await collector.async_start(now=NOW, current=OutdoorSample(NOW, 5.0))
    original_cancel = collector._listener_cancel
    same, created_again = manager.acquire(
        hass=hass,
        source_identity="registry:outdoor",
        timezone="UTC",
        reader=None,
        entity_id="sensor.renamed_outdoor",
        subscriber=second_subscriber,
    )

    assert same is collector
    assert not created_again
    assert collector.entity_id == "sensor.renamed_outdoor"
    assert collector._listener_cancel is not original_cancel
    collector.update_entity_id("sensor.renamed_outdoor")
    assert manager.release("registry:outdoor", "UTC", first_subscriber)

    hass.states.async_set("sensor.renamed_outdoor", "6", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    hass.states.async_remove("sensor.renamed_outdoor")
    await hass.async_block_till_done()

    assert notifications == ["second", "second"]
    assert manager.release("registry:outdoor", "UTC", second_subscriber)
    await collector.async_close()


async def test_recorder_absence_and_exported_query_conversion(monkeypatch: Any) -> None:
    absent_hass = SimpleNamespace(config=SimpleNamespace(components=set()))
    absent = HomeAssistantRecorderHistoryReader(cast(Any, absent_hass), "sensor.outdoor")
    assert await absent.read_window(start_utc=NOW, end_utc=NOW + timedelta(hours=1)) == ()

    valid = State(
        "sensor.outdoor",
        "50",
        {"unit_of_measurement": "°F"},
        last_changed=NOW + timedelta(minutes=20),
        last_updated=NOW + timedelta(minutes=20),
    )
    invalid = State(
        "sensor.outdoor",
        "unknown",
        {"unit_of_measurement": "°C"},
        last_changed=NOW + timedelta(minutes=10),
        last_updated=NOW + timedelta(minutes=10),
    )
    synthetic_start = State(
        "sensor.outdoor",
        "5",
        {"unit_of_measurement": "°C"},
        last_changed=NOW - timedelta(hours=1),
        last_updated=NOW - timedelta(hours=1),
    )

    class Recorder:
        async def async_add_executor_job(self, query: Any) -> Any:
            return query()

    monkeypatch.setattr(recorder_adapter, "get_instance", lambda _hass: Recorder())
    monkeypatch.setattr(
        recorder_adapter,
        "get_significant_states",
        lambda *_args, **_kwargs: {"sensor.outdoor": [valid, object(), invalid, synthetic_start]},
    )
    hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
    reader = HomeAssistantRecorderHistoryReader(cast(Any, hass), "sensor.outdoor")
    samples = await reader.read_window(start_utc=NOW, end_utc=NOW + timedelta(hours=1))
    assert [sample.observed_at for sample in samples] == sorted(
        sample.observed_at for sample in samples
    )
    assert samples[0].observed_at == NOW
    assert samples[0].synthetic_start
    assert samples[1].value_c is None
    assert not samples[1].valid
    assert samples[2].value_c == 10.0
    assert samples[2].valid
