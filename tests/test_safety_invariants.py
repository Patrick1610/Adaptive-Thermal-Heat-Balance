"""Bind every normative safety invariant to named passing repository tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
MAPPING = ROOT / "tests/fixtures/safety_invariants_v1.json"


def test_all_26_safety_invariants_have_named_test_bindings() -> None:
    payload = json.loads(MAPPING.read_text(encoding="utf-8"))
    invariants = payload["invariants"]
    assert payload["schema_version"] == 1
    assert [item["id"] for item in invariants] == [f"SI-{index:02d}" for index in range(1, 27)]
    for invariant in invariants:
        assert invariant["requirement"]
        assert len(invariant["tests"]) >= 2
        for node_id in invariant["tests"]:
            relative, test_name = node_id.split("::", 1)
            source = (ROOT / relative).read_text(encoding="utf-8")
            assert re.search(
                rf"^(?:async )?def {re.escape(test_name)}(?:\[|\()", source, re.MULTILINE
            )
