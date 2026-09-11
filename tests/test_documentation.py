"""Documentation and translation completeness checks."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_required_product_documentation_exists_and_names_safety_boundaries() -> None:
    required = {
        "README.md",
        "docs/scientific-model.md",
        "docs/configuration.md",
        "docs/operations.md",
        "docs/installation.md",
        "docs/validation.md",
    }
    for relative in required:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert len(text) > 500

    combined = "\n".join((ROOT / relative).read_text(encoding="utf-8") for relative in required)
    for phrase in (
        "Efficient",
        "Balanced",
        "Comfort",
        "climate.set_temperature",
        "never changes",
        "declared",
        "uniform",
        "surface",
        "critical local-air",
        "Virtual Installation",
        "no live Home Assistant installation",
    ):
        assert phrase in combined


def test_english_dutch_and_canonical_strings_have_matching_complete_keys() -> None:
    paths = (
        ROOT / "custom_components/athb/strings.json",
        ROOT / "custom_components/athb/translations/en.json",
        ROOT / "custom_components/athb/translations/nl.json",
    )
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def keys(value: object, prefix: str = "") -> set[str]:
        if not isinstance(value, dict):
            return {prefix}
        return {
            item
            for key, child in value.items()
            for item in keys(child, f"{prefix}.{key}" if prefix else key)
        }

    assert keys(documents[0]) == keys(documents[1]) == keys(documents[2])
    assert set(documents[0]["issues"]) == {
        "removed_source_or_target",
        "duplicate_target_ownership",
        "corrupt_control_storage",
        "persistent_target_rejection",
        "incompatible_auto_mapping",
        "missing_history_24h",
        "mandatory_input_unavailable_1h",
    }


def test_hacs_metadata_is_minimal_and_points_to_integration_subdirectory() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs == {
        "name": "Adaptive Thermal Heat Balance",
        "content_in_root": False,
        "homeassistant": "2026.9.0",
    }
