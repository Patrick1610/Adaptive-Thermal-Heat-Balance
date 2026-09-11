"""Bounded decision trace tests."""

from __future__ import annotations

import pytest

from custom_components.athb.core.trace import DecisionTraceRing


def test_trace_ring_deduplicates_and_retains_twenty_material_decisions() -> None:
    ring = DecisionTraceRing()
    first = ring.add(generation=1, payload={"target": 19.5, "reason": None})
    assert first is not None
    assert ring.add(generation=2, payload={"target": 19.5, "reason": None}) is None
    for generation in range(2, 23):
        ring.add(generation=generation, payload={"target": generation})

    assert len(ring.items) == 20
    assert ring.latest is not None
    assert ring.latest.payload == {"target": 22}
    assert ring.items[0].generation == 3


@pytest.mark.parametrize(
    "payload", [{"bad": float("nan")}, {"bad": float("inf")}, {"bad": object()}]
)
def test_trace_rejects_nonfinite_and_non_json_values(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="trace"):
        DecisionTraceRing().add(generation=1, payload=payload)


def test_trace_validates_capacity_and_generation() -> None:
    with pytest.raises(ValueError, match="maximum"):
        DecisionTraceRing(21)
    with pytest.raises(ValueError, match="generation"):
        DecisionTraceRing().add(generation=-1, payload={})
