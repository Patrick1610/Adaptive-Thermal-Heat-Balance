"""Calendar-correct outdoor integration, running mean, sharing, and recovery."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256
from itertools import pairwise
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ALPHA = 0.8
MIN_ALPHA = 0.6
MAX_ALPHA = 0.9
DEFAULT_MAX_HOLD = timedelta(hours=2)
DAILY_COVERAGE_THRESHOLD = 0.9
MAX_SUMMARIES = 35
HISTORY_SCHEMA_VERSION = 1
HISTORY_ALGORITHM_VERSION = 1
MAX_BOOTSTRAP_RECORDS = 100_000
BOOTSTRAP_DEADLINE_SECONDS = 30.0


class HistoryQuality(StrEnum):
    COMPLETE = "complete_history"
    PARTIAL = "partial_history"
    DIAGNOSTIC = "diagnostic_estimate"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OutdoorSample:
    observed_at: datetime
    value_c: float | None
    valid: bool = True
    provenance: str = "measured"
    synthetic_start: bool = False


class RecorderHistoryReader(Protocol):
    """Executor-backed HA Recorder boundary implemented by the integration adapter."""

    async def read_window(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[OutdoorSample, ...]: ...


@dataclass(frozen=True, slots=True)
class DailySummary:
    local_date: date
    day_start_utc: datetime
    day_end_utc: datetime
    integral_c_seconds: float
    covered_seconds: float
    observations: int
    largest_uncovered_gap_seconds: float
    provenance: str

    @property
    def day_length_seconds(self) -> float:
        return (self.day_end_utc - self.day_start_utc).total_seconds()

    @property
    def coverage_fraction(self) -> float:
        return self.covered_seconds / self.day_length_seconds

    @property
    def eligible(self) -> bool:
        return self.coverage_fraction >= DAILY_COVERAGE_THRESHOLD

    @property
    def mean_c(self) -> float | None:
        return (
            self.integral_c_seconds / self.covered_seconds if self.covered_seconds > 0.0 else None
        )


@dataclass(slots=True)
class _Accumulator:
    local_date: date
    start_utc: datetime
    end_utc: datetime
    integral: float = 0.0
    covered: float = 0.0
    observations: int = 0
    largest_gap: float = 0.0
    provenance: str = "measured"
    current_gap: float = 0.0

    def add(self, *, seconds: float, value_c: float | None) -> None:
        if seconds <= 0.0:
            return
        if value_c is None:
            self.current_gap += seconds
            self.largest_gap = max(self.largest_gap, self.current_gap)
        else:
            self.current_gap = 0.0
            self.integral += value_c * seconds
            self.covered += seconds

    def summary(self) -> DailySummary:
        return DailySummary(
            self.local_date,
            self.start_utc,
            self.end_utc,
            self.integral,
            self.covered,
            self.observations,
            self.largest_gap,
            self.provenance,
        )


@dataclass(frozen=True, slots=True)
class RunningMeanResult:
    value_c: float | None
    quality: HistoryQuality
    eligible_days: int
    represented_weight_fraction: float
    most_recent_eligible_date: date | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoryLoadResult:
    summaries: tuple[DailySummary, ...]
    current_day_accumulator: DailySummary | None
    last_valid_observation: OutdoorSample | None
    last_integrated_utc: datetime | None
    source_generation: int | None
    storage_generation: int | None
    reasons: tuple[str, ...]
    corrupt_payload: object | None


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _day_bounds(local_date: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(local_date, time.min, timezone).astimezone(UTC)
    end = datetime.combine(local_date + timedelta(days=1), time.min, timezone).astimezone(UTC)
    return start, end


def bootstrap_day_windows(
    *, current_local_date: date, timezone: str, maximum_hold: timedelta = DEFAULT_MAX_HOLD
) -> tuple[tuple[datetime, datetime], ...]:
    """Return sequential localized day queries plus the required first lookback."""

    zone = ZoneInfo(timezone)
    earliest = current_local_date - timedelta(days=7)
    earliest_start, _ = _day_bounds(earliest, zone)
    today_start, _ = _day_bounds(current_local_date, zone)
    starts = [earliest_start - maximum_hold]
    for offset in range(1, 8):
        day_start, _ = _day_bounds(earliest + timedelta(days=offset), zone)
        starts.append(day_start)
    starts.append(today_start)
    return tuple(pairwise(starts))


class DailyHistoryIntegrator:
    """Piecewise-constant integration split at real localized midnights."""

    def __init__(
        self,
        *,
        timezone: str,
        start_utc: datetime,
        maximum_hold: timedelta = DEFAULT_MAX_HOLD,
    ) -> None:
        if maximum_hold <= timedelta(0):
            raise ValueError("maximum_hold must be positive")
        self.timezone = ZoneInfo(timezone)
        self.maximum_hold = maximum_hold
        self.cursor = _aware_utc(start_utc, "start_utc")
        local_date = self.cursor.astimezone(self.timezone).date()
        start, end = _day_bounds(local_date, self.timezone)
        self._accumulator = _Accumulator(local_date, start, end)
        self._accumulator.add(
            seconds=(self.cursor - start).total_seconds(),
            value_c=None,
        )
        self._last_value: float | None = None
        self._hold_until: datetime | None = None
        self._summaries: list[DailySummary] = []

    @property
    def summaries(self) -> tuple[DailySummary, ...]:
        return tuple(self._summaries)

    @property
    def current_summary(self) -> DailySummary:
        return self._accumulator.summary()

    @property
    def last_valid_observation(self) -> OutdoorSample | None:
        if self._last_value is None or self._hold_until is None:
            return None
        return OutdoorSample(
            self._hold_until - self.maximum_hold,
            self._last_value,
            provenance=self._accumulator.provenance,
        )

    def _advance_segment(self, end: datetime) -> None:
        while self.cursor < end:
            boundary = min(end, self._accumulator.end_utc)
            covered_end = boundary
            if self._last_value is None or self._hold_until is None:
                covered_end = self.cursor
            else:
                covered_end = max(self.cursor, min(boundary, self._hold_until))
            if covered_end > self.cursor:
                self._accumulator.add(
                    seconds=(covered_end - self.cursor).total_seconds(),
                    value_c=self._last_value,
                )
            if boundary > covered_end:
                self._accumulator.add(
                    seconds=(boundary - covered_end).total_seconds(),
                    value_c=None,
                )
            self.cursor = boundary
            if self.cursor == self._accumulator.end_utc:
                self._summaries.append(self._accumulator.summary())
                next_date = self._accumulator.local_date + timedelta(days=1)
                start, next_end = _day_bounds(next_date, self.timezone)
                self._accumulator = _Accumulator(next_date, start, next_end)

    def add_sample(self, sample: OutdoorSample) -> None:
        timestamp = _aware_utc(sample.observed_at, "observed_at")
        if timestamp < self.cursor:
            raise ValueError("samples must be ordered by timestamp")
        self._advance_segment(timestamp)
        if sample.valid and sample.value_c is not None:
            if isinstance(sample.value_c, bool) or not math.isfinite(float(sample.value_c)):
                raise ValueError("valid outdoor samples must be finite and non-Boolean")
            self._last_value = float(sample.value_c)
            self._hold_until = timestamp + self.maximum_hold
            if not sample.synthetic_start:
                self._accumulator.observations += 1
            self._accumulator.provenance = sample.provenance
        else:
            self._last_value = None
            self._hold_until = None

    def advance_to(self, end_utc: datetime) -> None:
        end = _aware_utc(end_utc, "end_utc")
        if end < self.cursor:
            raise ValueError("cannot move history integration backwards")
        self._advance_segment(end)


def integrate_outdoor_history(
    *,
    samples: tuple[OutdoorSample, ...],
    timezone: str,
    start_utc: datetime,
    end_utc: datetime,
    maximum_hold: timedelta = DEFAULT_MAX_HOLD,
) -> tuple[DailySummary, ...]:
    """Integrate ordered Recorder/live samples with identical semantics."""

    if len(samples) > MAX_BOOTSTRAP_RECORDS:
        raise ValueError("outdoor history exceeds 100000 accepted records")
    integrator = DailyHistoryIntegrator(
        timezone=timezone,
        start_utc=start_utc,
        maximum_hold=maximum_hold,
    )
    for sample in samples:
        integrator.add_sample(sample)
    integrator.advance_to(end_utc)
    return integrator.summaries


def running_mean(
    *,
    summaries: tuple[DailySummary, ...],
    current_local_date: date,
    alpha: float = DEFAULT_ALPHA,
) -> RunningMeanResult:
    """Calculate the finite normalized seven-calendar-day estimator."""

    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
        raise ValueError("alpha must be numeric")
    alpha_value = float(alpha)
    if not math.isfinite(alpha_value) or not MIN_ALPHA <= alpha_value <= MAX_ALPHA:
        raise ValueError("alpha must be finite and within [0.6, 0.9]")
    by_date = {item.local_date: item for item in summaries}
    if len(by_date) != len(summaries):
        raise ValueError("duplicate daily summary dates")
    denominator = sum(alpha_value**index for index in range(7))
    weighted = 0.0
    represented = 0.0
    eligible_dates: list[date] = []
    recent = False
    for index in range(7):
        day = current_local_date - timedelta(days=index + 1)
        summary = by_date.get(day)
        if summary is None or not summary.eligible or summary.mean_c is None:
            continue
        weight = alpha_value**index
        weighted += weight * summary.mean_c
        represented += weight
        eligible_dates.append(day)
        if index < 2:
            recent = True
    count = len(eligible_dates)
    fraction = represented / denominator
    value = weighted / represented if represented > 0.0 else None
    if count == 7:
        quality = HistoryQuality.COMPLETE
    elif count >= 3 and fraction >= 0.6 and recent:
        quality = HistoryQuality.PARTIAL
    elif count:
        quality = HistoryQuality.DIAGNOSTIC
    else:
        quality = HistoryQuality.UNAVAILABLE
    reasons: list[str] = []
    if quality is HistoryQuality.PARTIAL:
        reasons.append("partial_history")
    elif quality is HistoryQuality.DIAGNOSTIC:
        reasons.append("history_not_control_eligible")
    elif quality is HistoryQuality.UNAVAILABLE:
        reasons.append("running_mean_unavailable")
    return RunningMeanResult(
        value,
        quality,
        count,
        fraction,
        max(eligible_dates) if eligible_dates else None,
        tuple(reasons),
    )


def retain_recent_summaries(
    summaries: tuple[DailySummary, ...], *, maximum: int = MAX_SUMMARIES
) -> tuple[DailySummary, ...]:
    """Retain a unique, sorted bounded tail of daily summaries."""

    if maximum < 1:
        raise ValueError("maximum must be positive")
    ordered = sorted(summaries, key=lambda item: item.local_date)
    if len({item.local_date for item in ordered}) != len(ordered):
        raise ValueError("duplicate daily summary dates")
    return tuple(ordered[-maximum:])


def collection_policy_fingerprint(*, maximum_hold_seconds: float, coverage_threshold: float) -> str:
    payload = f"v{HISTORY_ALGORITHM_VERSION}:{maximum_hold_seconds:.9f}:{coverage_threshold:.9f}"
    return sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OutdoorSourceKey:
    source_uuid: str
    attribute: str | None
    timezone: str
    collection_policy_fingerprint: str


@dataclass(slots=True)
class SharedOutdoorSource:
    key: OutdoorSourceKey
    source_identity: str
    source_generation: int
    listener_references: int = 0
    bootstrap_started: bool = False

    def begin_bootstrap(self) -> bool:
        if self.bootstrap_started:
            return False
        self.bootstrap_started = True
        return True


class OutdoorSourceRegistry:
    """Reference-count exactly one collector/bootstrap lineage per physical key."""

    def __init__(self) -> None:
        self._sources: dict[OutdoorSourceKey, SharedOutdoorSource] = {}

    def acquire(
        self, *, key: OutdoorSourceKey, source_identity: str, source_generation: int
    ) -> SharedOutdoorSource:
        source = self._sources.get(key)
        if source is None:
            source = SharedOutdoorSource(key, source_identity, source_generation)
            self._sources[key] = source
        else:
            source.source_identity = source_identity
        source.listener_references += 1
        return source

    def release(self, key: OutdoorSourceKey) -> bool:
        source = self._sources.get(key)
        if source is None or source.listener_references <= 0:
            return False
        source.listener_references -= 1
        if source.listener_references == 0:
            del self._sources[key]
        return True

    @property
    def source_count(self) -> int:
        return len(self._sources)


def serialize_history_state(
    *,
    source_uuid: str,
    source_identity: str,
    source_generation: int,
    timezone: str,
    policy_fingerprint: str,
    summaries: tuple[DailySummary, ...],
    storage_generation: int,
    current_day_accumulator: DailySummary | None = None,
    last_valid_observation: OutdoorSample | None = None,
    last_integrated_utc: datetime | None = None,
) -> dict[str, Any]:
    """Produce deterministic versioned history storage primitives."""

    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "algorithm_version": HISTORY_ALGORITHM_VERSION,
        "source_uuid": source_uuid,
        "source_identity": source_identity,
        "source_generation": source_generation,
        "timezone": timezone,
        "collection_policy_fingerprint": policy_fingerprint,
        "daily_summaries": [
            _serialize_summary(item) for item in retain_recent_summaries(summaries)
        ],
        "current_day_accumulator": (
            _serialize_summary(current_day_accumulator)
            if current_day_accumulator is not None
            else None
        ),
        "last_valid_observation": (
            {
                "observed_at": last_valid_observation.observed_at.isoformat(),
                "value_c": last_valid_observation.value_c,
                "valid": last_valid_observation.valid,
                "provenance": last_valid_observation.provenance,
            }
            if last_valid_observation is not None
            else None
        ),
        "last_integrated_utc": (
            last_integrated_utc.isoformat() if last_integrated_utc is not None else None
        ),
        "storage_generation": storage_generation,
    }


def _serialize_summary(item: DailySummary) -> dict[str, Any]:
    return {
        **asdict(item),
        "local_date": item.local_date.isoformat(),
        "day_start_utc": item.day_start_utc.isoformat(),
        "day_end_utc": item.day_end_utc.isoformat(),
    }


def _parse_summary(raw: object) -> DailySummary:
    if not isinstance(raw, dict):
        raise ValueError("summary must be an object")
    summary = DailySummary(
        date.fromisoformat(str(raw["local_date"])),
        _aware_utc(datetime.fromisoformat(str(raw["day_start_utc"])), "day_start_utc"),
        _aware_utc(datetime.fromisoformat(str(raw["day_end_utc"])), "day_end_utc"),
        float(raw["integral_c_seconds"]),
        float(raw["covered_seconds"]),
        int(raw["observations"]),
        float(raw["largest_uncovered_gap_seconds"]),
        str(raw["provenance"]),
    )
    if (
        not all(
            math.isfinite(value)
            for value in (
                summary.integral_c_seconds,
                summary.covered_seconds,
                summary.largest_uncovered_gap_seconds,
            )
        )
        or summary.day_end_utc <= summary.day_start_utc
        or not 0.0 <= summary.covered_seconds <= summary.day_length_seconds
        or not 0.0 <= summary.largest_uncovered_gap_seconds <= summary.day_length_seconds
        or summary.observations < 0
    ):
        raise ValueError("impossible daily summary")
    return summary


def load_history_state(payload: object) -> HistoryLoadResult:
    """Fail closed on incompatible/corrupt storage while preserving its payload."""

    if not isinstance(payload, dict):
        return HistoryLoadResult(
            (), None, None, None, None, None, ("corrupt_history_storage",), payload
        )
    try:
        if payload["schema_version"] != HISTORY_SCHEMA_VERSION:
            raise ValueError("incompatible schema")
        if payload["algorithm_version"] != HISTORY_ALGORITHM_VERSION:
            raise ValueError("incompatible algorithm")
        ZoneInfo(str(payload["timezone"]))
        raw_summaries = payload["daily_summaries"]
        if not isinstance(raw_summaries, list):
            raise ValueError("daily summaries must be a list")
        summaries = [_parse_summary(raw) for raw in raw_summaries]
        retained = retain_recent_summaries(tuple(summaries))
        if any(left.day_end_utc > right.day_start_utc for left, right in pairwise(retained)):
            raise ValueError("overlapping daily summaries")
        current_raw = payload.get("current_day_accumulator")
        current = _parse_summary(current_raw) if current_raw is not None else None
        last_raw = payload.get("last_valid_observation")
        last_sample = None
        if last_raw is not None:
            if not isinstance(last_raw, dict):
                raise ValueError("last valid observation must be an object")
            last_sample = OutdoorSample(
                _aware_utc(datetime.fromisoformat(str(last_raw["observed_at"])), "observed_at"),
                float(last_raw["value_c"]),
                last_raw["valid"],
                str(last_raw["provenance"]),
            )
            if (
                last_sample.valid is not True
                or last_sample.value_c is None
                or not math.isfinite(last_sample.value_c)
            ):
                raise ValueError("invalid last observation")
        last_integrated_raw = payload.get("last_integrated_utc")
        last_integrated = (
            _aware_utc(datetime.fromisoformat(str(last_integrated_raw)), "last_integrated_utc")
            if last_integrated_raw is not None
            else None
        )
        return HistoryLoadResult(
            retained,
            current,
            last_sample,
            last_integrated,
            int(payload["source_generation"]),
            int(payload["storage_generation"]),
            (),
            None,
        )
    except KeyError, TypeError, ValueError, OverflowError, ZoneInfoNotFoundError:
        return HistoryLoadResult(
            (), None, None, None, None, None, ("corrupt_history_storage",), payload
        )


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    summaries: tuple[DailySummary, ...]
    accepted: bool
    reason: str


def bootstrap_history(
    *,
    records: tuple[OutdoorSample, ...] | None,
    timezone: str,
    start_utc: datetime,
    end_utc: datetime,
    expected_source_generation: int,
    current_source_generation: int,
    completed_at: datetime,
    deadline_utc: datetime,
    maximum_hold: timedelta = DEFAULT_MAX_HOLD,
) -> BootstrapResult:
    """Validate one bounded Recorder result before accepting its summaries."""

    start = _aware_utc(start_utc, "start_utc")
    end = _aware_utc(end_utc, "end_utc")
    completed = _aware_utc(completed_at, "completed_at")
    deadline = _aware_utc(deadline_utc, "deadline_utc")
    if records is None:
        return BootstrapResult((), False, "recorder_unavailable")
    if expected_source_generation != current_source_generation:
        return BootstrapResult((), False, "stale_source_generation")
    if completed > deadline:
        return BootstrapResult((), False, "bootstrap_deadline_exceeded")
    if end <= start or end - start > timedelta(days=8) + maximum_hold:
        return BootstrapResult((), False, "bootstrap_range_exceeded")
    if len(records) > MAX_BOOTSTRAP_RECORDS:
        return BootstrapResult((), False, "bootstrap_record_limit_exceeded")
    try:
        summaries = integrate_outdoor_history(
            samples=records,
            timezone=timezone,
            start_utc=start,
            end_utc=end,
            maximum_hold=maximum_hold,
        )
    except ValueError, ZoneInfoNotFoundError:
        return BootstrapResult((), False, "invalid_recorder_history")
    return BootstrapResult(summaries, True, "bootstrap_complete")
