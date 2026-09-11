"""Transition-only logging and Home Assistant repair issue management."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

REPAIR_TYPES = {
    "removed_source_or_target",
    "duplicate_target_ownership",
    "corrupt_control_storage",
    "persistent_target_rejection",
    "incompatible_auto_mapping",
    "missing_history_24h",
    "mandatory_input_unavailable_1h",
}


@dataclass(slots=True)
class TransitionLogger:
    """Emit one log record on failure/recovery transitions, not per report."""

    logger: logging.Logger
    _states: dict[str, bool] = field(default_factory=dict)

    def update(self, key: str, active: bool, *, active_message: str, recovery_message: str) -> bool:
        prior = self._states.get(key)
        self._states[key] = active
        if (prior is None and not active) or prior == active:
            return False
        if active:
            self.logger.warning(active_message)
        else:
            self.logger.info(recovery_message)
        return True


@dataclass(slots=True)
class RepairManager:
    hass: HomeAssistant
    entry_id: str
    active: set[str] = field(default_factory=set)

    def update(self, repair_type: str, active: bool) -> bool:
        if repair_type not in REPAIR_TYPES:
            raise ValueError("unknown ATHB repair type")
        issue_id = f"{self.entry_id}_{repair_type}"
        if active and repair_type not in self.active:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=repair_type,
            )
            self.active.add(repair_type)
            return True
        if not active and repair_type in self.active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self.active.remove(repair_type)
            return True
        return False
