"""Calendar weighting, DST, sharing, bootstrap, and history-storage tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.athb.core.history import (
    DAILY_COVERAGE_THRESHOLD,
    BootstrapResult,
    DailyHistoryIntegrator,
    DailySummary,
    HistoryQuality,
    OutdoorSample,
    OutdoorSourceKey,
    OutdoorSourceRegistry,
    bootstrap_day_windows,
    bootstrap_history,
    collection_policy_fingerprint,
    integrate_outdoor_history,
    load_history_state,
    retain_recent_summaries,
    running_mean,
    serialize_history_state,
)

TZ = ZoneInfo("Europe/Amsterdam")


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, TZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, TZ).astimezone(UTC)
    return start, end


def _summary(day: date, value: float, coverage: float = 1.0) -> DailySummary:
    start, end = _bounds(day)
    length = (end - start).total_seconds()
    covered = length * coverage
    return DailySummary(day, start, end, value * covered, covered, 24, length - covered, "measured")


def test_piecewise_constant_integration_stops_at_hold_and_tracks_continuous_gap() -> None:
    day = date(2026, 9, 10)
    start, end = _bounds(day)
    samples = (
        OutdoorSample(start, 10.0),
        OutdoorSample(start + timedelta(hours=1), 20.0),
        OutdoorSample(start + timedelta(hours=4), None, valid=False),
    )
    summaries = integrate_outdoor_history(
        samples=samples, timezone="Europe/Amsterdam", start_utc=start, end_utc=end
    )
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.covered_seconds == 3 * 3600
    assert summary.integral_c_seconds == 10 * 3600 + 20 * 2 * 3600
    assert summary.largest_uncovered_gap_seconds == 21 * 3600
    assert not summary.eligible


@pytest.mark.parametrize(
    ("day", "hours"),
    [(date(2026, 3, 29), 23), (date(2026, 2, 1), 24), (date(2026, 10, 25), 25)],
)
def test_local_calendar_days_have_real_dst_length(day: date, hours: int) -> None:
    start, end = _bounds(day)
    samples = tuple(OutdoorSample(start + timedelta(hours=index), 5.0) for index in range(hours))
    summary = integrate_outdoor_history(
        samples=samples, timezone="Europe/Amsterdam", start_utc=start, end_utc=end
    )[0]
    assert summary.day_length_seconds == hours * 3600
    assert summary.coverage_fraction == 1.0
    assert summary.mean_c == 5.0


def test_explicit_invalid_event_ends_hold_immediately() -> None:
    day = date(2026, 9, 10)
    start, end = _bounds(day)
    summary = integrate_outdoor_history(
        samples=(
            OutdoorSample(start, 5.0),
            OutdoorSample(start + timedelta(minutes=30), None, valid=False),
        ),
        timezone="Europe/Amsterdam",
        start_utc=start,
        end_utc=end,
    )[0]
    assert summary.covered_seconds == 1800
    assert summary.largest_uncovered_gap_seconds == summary.day_length_seconds - 1800


def test_synthetic_recorder_start_state_is_not_counted_as_observation() -> None:
    day = date(2026, 9, 10)
    start, end = _bounds(day)
    integrator = DailyHistoryIntegrator(timezone="Europe/Amsterdam", start_utc=start)
    integrator.add_sample(OutdoorSample(start, 5.0, synthetic_start=True))
    integrator.add_sample(OutdoorSample(start + timedelta(hours=1), 5.0))
    integrator.advance_to(end)
    assert integrator.summaries[0].observations == 1
    assert integrator.last_valid_observation is not None


def test_coverage_threshold_is_exactly_ninety_percent() -> None:
    eligible = _summary(date(2026, 9, 10), 5.0, DAILY_COVERAGE_THRESHOLD)
    ineligible = _summary(date(2026, 9, 9), 5.0, DAILY_COVERAGE_THRESHOLD - 1e-6)
    assert eligible.eligible
    assert not ineligible.eligible


def test_running_mean_uses_calendar_age_without_compressing_missing_days() -> None:
    today = date(2026, 9, 11)
    summaries = (
        _summary(today - timedelta(days=1), 10.0),
        _summary(today - timedelta(days=3), 30.0),
        _summary(today - timedelta(days=7), 70.0),
    )
    result = running_mean(summaries=summaries, current_local_date=today, alpha=0.8)
    expected = (10.0 + 0.8**2 * 30.0 + 0.8**6 * 70.0) / (1.0 + 0.8**2 + 0.8**6)
    assert result.value_c == pytest.approx(expected)
    assert result.quality is HistoryQuality.DIAGNOSTIC
    assert result.most_recent_eligible_date == today - timedelta(days=1)


def test_complete_partial_diagnostic_and_unavailable_history_quality() -> None:
    today = date(2026, 9, 11)
    complete = tuple(_summary(today - timedelta(days=index), float(index)) for index in range(1, 8))
    assert (
        running_mean(summaries=complete, current_local_date=today).quality
        is HistoryQuality.COMPLETE
    )
    partial = complete[:3]
    partial_result = running_mean(summaries=partial, current_local_date=today)
    assert partial_result.quality is HistoryQuality.PARTIAL
    assert partial_result.represented_weight_fraction >= 0.6
    diagnostic = running_mean(summaries=complete[-2:], current_local_date=today)
    assert diagnostic.quality is HistoryQuality.DIAGNOSTIC
    unavailable = running_mean(summaries=(), current_local_date=today)
    assert unavailable.quality is HistoryQuality.UNAVAILABLE
    assert unavailable.value_c is None


@pytest.mark.parametrize("alpha", [0.59, 0.91, float("nan"), True])
def test_invalid_alpha_is_rejected(alpha: float) -> None:
    with pytest.raises(ValueError, match="alpha"):
        running_mean(summaries=(), current_local_date=date(2026, 9, 11), alpha=alpha)


def test_retention_is_sorted_unique_and_bounded_to_35_days() -> None:
    today = date(2026, 9, 11)
    summaries = tuple(_summary(today - timedelta(days=index), 5.0) for index in range(40))
    retained = retain_recent_summaries(summaries)
    assert len(retained) == 35
    assert retained == tuple(sorted(retained, key=lambda item: item.local_date))
    with pytest.raises(ValueError, match="duplicate"):
        retain_recent_summaries((retained[0], retained[0]))
    with pytest.raises(ValueError, match="positive"):
        retain_recent_summaries((), maximum=0)


def test_shared_registry_uses_one_collector_and_bootstrap_for_forty_zones() -> None:
    registry = OutdoorSourceRegistry()
    key = OutdoorSourceKey(
        "registry-outdoor",
        None,
        "Europe/Amsterdam",
        collection_policy_fingerprint(maximum_hold_seconds=7200, coverage_threshold=0.9),
    )
    sources = [
        registry.acquire(key=key, source_identity="sensor.outdoor", source_generation=1)
        for _ in range(40)
    ]
    assert registry.source_count == 1
    assert sources[0].listener_references == 40
    assert sources[0].begin_bootstrap()
    assert not sources[1].begin_bootstrap()
    renamed = registry.acquire(
        key=key, source_identity="sensor.outdoor_renamed", source_generation=1
    )
    assert renamed is sources[0]
    assert renamed.source_identity == "sensor.outdoor_renamed"
    for _ in range(41):
        assert registry.release(key)
    assert registry.source_count == 0
    assert not registry.release(key)


def test_history_storage_round_trip_and_corruption_preservation() -> None:
    day = date(2026, 9, 10)
    summary = _summary(day, 5.0)
    current = _summary(day + timedelta(days=1), 6.0, 0.5)
    start, _end = _bounds(day)
    sample = OutdoorSample(start, 5.0)
    payload = serialize_history_state(
        source_uuid="registry-outdoor",
        source_identity="sensor.outdoor",
        source_generation=3,
        timezone="Europe/Amsterdam",
        policy_fingerprint="fingerprint",
        summaries=(summary,),
        current_day_accumulator=current,
        last_valid_observation=sample,
        last_integrated_utc=start + timedelta(hours=1),
        storage_generation=7,
    )
    loaded = load_history_state(payload)
    assert loaded.reasons == ()
    assert loaded.summaries == (summary,)
    assert loaded.current_day_accumulator == current
    assert loaded.last_valid_observation == sample
    assert loaded.storage_generation == 7
    corrupt = dict(payload)
    corrupt["schema_version"] = 99
    failed = load_history_state(corrupt)
    assert failed.summaries == ()
    assert failed.reasons == ("corrupt_history_storage",)
    assert failed.corrupt_payload is corrupt


def test_duplicate_and_impossible_persisted_summaries_fail_closed() -> None:
    summary = _summary(date(2026, 9, 10), 5.0)
    payload = serialize_history_state(
        source_uuid="source",
        source_identity="sensor.outdoor",
        source_generation=1,
        timezone="Europe/Amsterdam",
        policy_fingerprint="fingerprint",
        summaries=(summary,),
        storage_generation=1,
    )
    payload["daily_summaries"].append(payload["daily_summaries"][0])
    assert load_history_state(payload).reasons == ("corrupt_history_storage",)
    payload["daily_summaries"][0]["covered_seconds"] = 999999
    assert load_history_state(payload).reasons == ("corrupt_history_storage",)
    assert load_history_state("broken").corrupt_payload == "broken"


def test_bootstrap_generation_deadline_range_and_recorder_absence_are_bounded() -> None:
    day = date(2026, 9, 10)
    start, end = _bounds(day)
    records = (OutdoorSample(start, 5.0),)
    common = dict(
        records=records,
        timezone="Europe/Amsterdam",
        start_utc=start,
        end_utc=end,
        expected_source_generation=1,
        current_source_generation=1,
        completed_at=end,
        deadline_utc=end + timedelta(seconds=1),
    )
    result = bootstrap_history(**common)
    assert result == BootstrapResult(result.summaries, True, "bootstrap_complete")
    assert bootstrap_history(**{**common, "records": None}).reason == "recorder_unavailable"
    assert bootstrap_history(**{**common, "current_source_generation": 2}).reason == (
        "stale_source_generation"
    )
    assert bootstrap_history(**{**common, "completed_at": end + timedelta(seconds=2)}).reason == (
        "bootstrap_deadline_exceeded"
    )
    assert (
        bootstrap_history(**{**common, "end_utc": start + timedelta(days=9)}).reason
        == "bootstrap_range_exceeded"
    )


def test_bootstrap_queries_are_sequential_local_days_with_two_hour_lookback() -> None:
    windows = bootstrap_day_windows(
        current_local_date=date(2026, 3, 30), timezone="Europe/Amsterdam"
    )
    assert len(windows) == 8
    assert windows[0][1] - windows[0][0] in {
        timedelta(hours=25),
        timedelta(hours=26),
        timedelta(hours=27),
    }
    assert all(windows[index][1] == windows[index + 1][0] for index in range(7))


def test_history_rejects_unordered_and_nonfinite_records() -> None:
    day = date(2026, 9, 10)
    start, end = _bounds(day)
    with pytest.raises(ValueError, match="ordered"):
        integrate_outdoor_history(
            samples=(OutdoorSample(start + timedelta(hours=1), 5.0), OutdoorSample(start, 5.0)),
            timezone="Europe/Amsterdam",
            start_utc=start,
            end_utc=end,
        )
    with pytest.raises(ValueError, match="finite"):
        integrate_outdoor_history(
            samples=(OutdoorSample(start, float("nan")),),
            timezone="Europe/Amsterdam",
            start_utc=start,
            end_utc=end,
        )
