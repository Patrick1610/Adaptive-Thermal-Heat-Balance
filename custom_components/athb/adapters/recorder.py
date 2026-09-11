"""Isolated, optional Home Assistant Recorder history reader."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial

from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.recorder import get_instance

from ..core.history import OutdoorSample
from ..core.sources import SourceKind, convert_source_value


class HomeAssistantRecorderHistoryReader:
    """Read one bounded source window on Recorder's own executor."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id

    async def read_window(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[OutdoorSample, ...]:
        if "recorder" not in self._hass.config.components:
            return ()
        recorder = get_instance(self._hass)
        query = partial(
            get_significant_states,
            self._hass,
            start_utc,
            end_utc,
            [self._entity_id],
            include_start_time_state=True,
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=False,
        )
        result = await recorder.async_add_executor_job(query)
        samples: list[OutdoorSample] = []
        for item in result.get(self._entity_id, ()):  # exported API returns State here
            if not isinstance(item, State):
                continue
            converted = convert_source_value(
                SourceKind.OUTDOOR,
                item.state,
                str(item.attributes.get("unit_of_measurement", "")),
            )
            samples.append(
                OutdoorSample(
                    (
                        start_utc
                        if item.last_updated <= start_utc
                        else item.last_updated.astimezone(UTC)
                    ),
                    converted[0] if converted is not None else None,
                    converted is not None,
                    "measured",
                    item.last_updated <= start_utc,
                )
            )
        return tuple(sorted(samples, key=lambda sample: sample.observed_at))
