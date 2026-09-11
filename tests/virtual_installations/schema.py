"""Versioned Virtual Installation fixture loading and strict expansion."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "virtual_installations"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_scenarios() -> tuple[dict[str, Any], ...]:
    baseline = json.loads((FIXTURES / "living_room_base.json").read_text(encoding="utf-8"))
    collection = json.loads((FIXTURES / "scenarios_v1.json").read_text(encoding="utf-8"))
    schema = json.loads((FIXTURES / "schema_v1.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    expanded = []
    for override in collection["scenarios"]:
        scenario = _merge(baseline, override)
        scenario["baseline"] = None if scenario["scenario_id"] == "VI-001" else "living_room_base"
        errors = sorted(validator.iter_errors(scenario), key=lambda item: list(item.path))
        if errors:
            raise AssertionError(
                f"{scenario['scenario_id']} invalid: "
                + "; ".join(f"{list(error.path)} {error.message}" for error in errors)
            )
        expanded.append(scenario)
    return tuple(expanded)
