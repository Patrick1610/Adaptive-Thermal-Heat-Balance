"""Repository-level checks for the custom-integration package."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from custom_components.athb import DOMAIN

ROOT = Path(__file__).parents[1]
MANIFEST_PATH = ROOT / "custom_components" / "athb" / "manifest.json"
PRODUCTION_ROOT = ROOT / "custom_components" / "athb"
FORBIDDEN_RUNTIME_PACKAGES = {"numba", "numpy", "scipy"}


def test_manifest_has_integration_identity_and_no_runtime_requirements() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest == {
        "codeowners": ["@Patrick1610"],
        "config_flow": True,
        "dependencies": ["climate"],
        "documentation": "https://github.com/Patrick1610/Adaptive-Thermal-Heat-Balance",
        "domain": "athb",
        "integration_type": "device",
        "iot_class": "calculated",
        "issue_tracker": ("https://github.com/Patrick1610/Adaptive-Thermal-Heat-Balance/issues"),
        "name": "Adaptive Thermal Heat Balance",
        "requirements": [],
        "version": "0.2.13",
    }
    assert manifest["domain"] == DOMAIN


def test_production_python_imports_are_standard_library_only() -> None:
    forbidden_imports: list[tuple[Path, str]] = []
    home_assistant_imports: list[tuple[Path, str]] = []
    non_standard_imports: list[tuple[Path, str]] = []

    for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name.split(".", maxsplit=1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported = [node.module.split(".", maxsplit=1)[0]]
            for package in imported:
                if package in FORBIDDEN_RUNTIME_PACKAGES:
                    forbidden_imports.append((path.relative_to(ROOT), package))
                if package == "homeassistant":
                    home_assistant_imports.append((path.relative_to(ROOT), package))
                if package not in sys.stdlib_module_names:
                    non_standard_imports.append((path.relative_to(ROOT), package))

    assert forbidden_imports == []
    assert all("core/" not in path.as_posix() for path, _package in home_assistant_imports)
    assert {package for _path, package in non_standard_imports} <= {"homeassistant", "voluptuous"}


def test_only_command_broker_uses_climate_service_adapter() -> None:
    call_sites = []
    for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ".services.async_call(" in line:
                call_sites.append((path.relative_to(PRODUCTION_ROOT).as_posix(), number))
    assert call_sites == [("adapters/climate.py", 79)]
    broker = (PRODUCTION_ROOT / "adapters/broker.py").read_text(encoding="utf-8")
    assert "self._service.async_set_temperature(service_payload(intent), context)" in broker


def test_pure_core_contains_only_implemented_standard_library_modules() -> None:
    production_files = {
        path.relative_to(PRODUCTION_ROOT).as_posix()
        for path in PRODUCTION_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".json", ".py"}
    }

    assert production_files == {
        "__init__.py",
        "adapters/__init__.py",
        "adapters/broker.py",
        "adapters/climate.py",
        "adapters/outdoor_history.py",
        "adapters/recorder.py",
        "adapters/sources.py",
        "adapters/storage.py",
        "binary_sensor.py",
        "button.py",
        "calculation.py",
        "config_flow.py",
        "config_schema.py",
        "const.py",
        "controller.py",
        "core/__init__.py",
        "core/athb_engine.py",
        "core/climate.py",
        "core/contracts.py",
        "core/history.py",
        "core/inverse.py",
        "core/locations.py",
        "core/ownership.py",
        "core/pipeline.py",
        "core/pmv_core.py",
        "core/policy.py",
        "core/psychrometrics.py",
        "core/radiant.py",
        "core/surface.py",
        "core/sources.py",
        "core/stale_heating.py",
        "core/trace.py",
        "diagnostics.py",
        "device_activity.py",
        "entity.py",
        "manifest.json",
        "runtime.py",
        "repairs.py",
        "select.py",
        "sensor.py",
        "strings.json",
        "switch.py",
        "translations/en.json",
        "translations/nl.json",
    }


def test_implementation_checklist_has_every_task_once_in_order() -> None:
    checklist = (ROOT / "docs" / "implementation" / "ATHB_TASK_CHECKLIST.md").read_text(
        encoding="utf-8"
    )
    expected = [f"ATHB-{number:03d}" for number in range(1, 30)]
    identifiers = re.findall(r"\| (ATHB-\d{3}) \|", checklist)

    assert re.findall(r"ATHB-\d{3}", checklist) == expected
    assert identifiers == expected
    rows = [line for line in checklist.splitlines() if line.startswith("| ATHB-")]
    statuses = [row.rsplit("|", 2)[1].strip() for row in rows]
    assert set(statuses) <= {"Complete", "Pending"}
    assert statuses[:24] == ["Complete"] * 24
    assert statuses == sorted(statuses, key={"Complete": 0, "Pending": 1}.__getitem__)
