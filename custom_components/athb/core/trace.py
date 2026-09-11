"""Bounded, finite, deterministic decision-trace storage."""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

MAX_DECISION_TRACES = 20


def _validate_json_value(value: Any, path: str = "trace") -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a non-JSON value")


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    decision_id: str
    generation: int
    canonical_payload: str

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.canonical_payload)
        assert isinstance(value, dict)
        return value


class DecisionTraceRing:
    """Retain at most twenty materially distinct coherent decisions."""

    def __init__(self, maximum: int = MAX_DECISION_TRACES) -> None:
        if maximum < 1 or maximum > MAX_DECISION_TRACES:
            raise ValueError("trace maximum must be within 1..20")
        self._items: deque[DecisionTrace] = deque(maxlen=maximum)

    def add(self, *, generation: int, payload: dict[str, Any]) -> DecisionTrace | None:
        if isinstance(generation, bool) or generation < 0:
            raise ValueError("trace generation must be non-negative")
        _validate_json_value(payload)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if self._items and self._items[-1].canonical_payload == canonical:
            return None
        identity = sha256(f"{generation}:{canonical}".encode()).hexdigest()[:16]
        trace = DecisionTrace(identity, generation, canonical)
        self._items.append(trace)
        return trace

    @property
    def items(self) -> tuple[DecisionTrace, ...]:
        return tuple(self._items)

    @property
    def latest(self) -> DecisionTrace | None:
        return self._items[-1] if self._items else None
