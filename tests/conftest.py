"""Home Assistant fixtures and Virtual Installation report plugin."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ("pytest_homeassistant_custom_component",)

ATHB_REPORT_RESULTS: list[Any] = []


@pytest.fixture
def athb_report_results() -> list[Any]:
    """Expose the report list owned by pytest's loaded conftest module."""

    return ATHB_REPORT_RESULTS


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--athb-report",
        action="store",
        default=None,
        help="Write Virtual Installation Markdown, JSON and assertion-summary evidence",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    ATHB_REPORT_RESULTS.clear()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    report_path = session.config.getoption("--athb-report")
    if not report_path:
        return
    if exitstatus != pytest.ExitCode.OK:
        return
    if len(ATHB_REPORT_RESULTS) != 30:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payloads = [asdict(item) for item in ATHB_REPORT_RESULTS]
    lines = ["# ATHB Virtual Installation Validation", "", "All 30 mandatory scenarios passed.", ""]
    for result in payloads:
        lines.extend(
            (
                f"## {result['scenario_id']} — {result['name']}",
                "",
                f"- History: `{result['input_summary']['history_quality']}`; running mean: `{result['input_summary']['running_mean_c']}` °C",
                f"- RH provenance: `{result['input_summary']['rh_provenance']}`",
                f"- Numerical golden: `{result['numerical']['golden_key']}`; current sensation: `{result['numerical']['current_sensation']}`",
                f"- Roots: `{json.dumps(result['numerical']['roots'], sort_keys=True)}`",
                f"- Strategy/profile: `{result['policy']['strategy']}` / `{result['policy']['profile']}`; fallback: `{result['policy']['fallback']}`",
                f"- Requested heating/cooling: `{result['policy']['requested_heating_c']}` / `{result['policy']['requested_cooling_c']}` °C",
                f"- Normalized target: `{json.dumps(result['policy']['normalized'], sort_keys=True)}`",
                f"- Command(s): `{json.dumps(result['broker']['calls'], sort_keys=True)}`",
                f"- Outcome: `{result['broker']['reason']}` / `{result['broker']['acknowledgement']}`",
                f"- Ending ownership: `{result['broker']['ending_ownership']}`",
                f"- Mandatory variants: `{json.dumps(result['execution']['variants'])}`",
                "- Result: **PASS**",
                "",
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    json_path = path.with_suffix(".json")
    json_path.write_text(
        json.dumps(payloads, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    assertion_path = path.with_name(f"{path.stem}.assertions.json")
    assertion_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mandatory_scenarios": 30,
                "passed": 30,
                "failed": 0,
                "skipped": 0,
                "scenario_ids": [item["scenario_id"] for item in payloads],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
