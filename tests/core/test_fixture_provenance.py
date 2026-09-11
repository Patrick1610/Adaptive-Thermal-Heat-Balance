"""Integrity and isolation checks for checked-in numerical oracle artifacts."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "numerical"
GENERATOR = ROOT / "tools" / "oracle" / "generate_athb_fixtures.py"
LOCK = ROOT / "tools" / "oracle" / "requirements.lock"
LICENSE = ROOT / "LICENSES" / "pythermalcomfort-4.4.2.txt"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )


def test_fixture_artifact_hashes_and_counts_match_provenance() -> None:
    provenance = _load_json(FIXTURE_ROOT / "provenance.json")
    forward = _load_json(FIXTURE_ROOT / "athb_forward_v1.json")
    roots = _load_json(FIXTURE_ROOT / "athb_roots_v1.json")

    assert provenance["artifact_hashes"] == {
        "athb_forward_v1.json": _sha256(FIXTURE_ROOT / "athb_forward_v1.json"),
        "athb_roots_v1.json": _sha256(FIXTURE_ROOT / "athb_roots_v1.json"),
    }
    assert provenance["fixture_counts"] == {
        "dense_forward": len(forward["dense_grid"]["cases"]),
        "named_forward": len(forward["named_cases"]),
        "quantization_boundary_forward": len(forward["quantization_boundary_case_ids"]),
        "root_scenarios": len(roots["scenarios"]),
        "seeded_forward": len(forward["seeded_random"]["cases"]),
        "total_forward": forward["vector_count"],
    }
    assert forward["vector_count"] == 248
    assert forward["quantization_boundary_case_ids"] == [
        "relative-air-speed-numpy-tie-even",
        "public-vote-numpy-tie-even",
        "public-vote-upstream-binary64-straddle",
    ]
    assert len(roots["scenarios"]) == 21


def test_fixture_identity_and_tolerances_are_frozen() -> None:
    artifacts = [
        _load_json(FIXTURE_ROOT / name)
        for name in ("athb_forward_v1.json", "athb_roots_v1.json", "provenance.json")
    ]

    assert {artifact["formulation_id"] for artifact in artifacts} == {"athb_2022_ptc_4_4_2"}
    assert {artifact["numerical_contract_version"] for artifact in artifacts} == {1}
    forward, roots, _ = artifacts
    assert forward["tolerances"] == {
        "public_vote_absolute": 0.00051,
        "unrounded_vote_absolute": 1e-7,
    }
    assert roots["tolerances"] == {
        "displayed_current_vote_absolute": 1e-6,
        "root_temperature_absolute_c": 0.01,
        "unrounded_vote_absolute": 1e-7,
    }
    assert roots["future_production_solver"] is False


def test_generator_and_lock_hashes_match_provenance() -> None:
    provenance = _load_json(FIXTURE_ROOT / "provenance.json")

    assert provenance["generator_sha256"] == _sha256(GENERATOR)
    assert provenance["requirements_lock_sha256"] == _sha256(LOCK)
    assert provenance["isolation"]["imports_production_core"] is False
    assert provenance["isolation"]["normal_tests_regenerate"] is False
    assert provenance["generation_command"] == (
        "python tools/oracle/generate_athb_fixtures.py "
        "--output-dir tests/fixtures/numerical --generated-at 2026-09-11T08:03:24Z"
    )
    assert provenance["generated_at_utc"] == "2026-09-11T08:03:24Z"


def test_oracle_generator_imports_upstream_but_never_production() -> None:
    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"), filename=str(GENERATOR))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    assert any(module.startswith("pythermalcomfort") for module in imports)
    assert not any(module.startswith("custom_components") for module in imports)


def test_normal_tests_do_not_import_or_execute_oracle_generator() -> None:
    forbidden: list[tuple[str, str]] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                modules = []
            for module in modules:
                if module.startswith(("pythermalcomfort", "tools.oracle")):
                    forbidden.append((path.relative_to(ROOT).as_posix(), module))

    assert forbidden == []


def test_oracle_lock_is_complete_for_declared_runtime_graph() -> None:
    lock = LOCK.read_text(encoding="utf-8").lower()
    provenance = _load_json(FIXTURE_ROOT / "provenance.json")
    expected_versions = {
        "llvmlite": "0.49.0",
        "numba": "0.67.0",
        "numpy": "2.2.6",
        "pythermalcomfort": "4.4.2",
        "scipy": "1.18.1",
        "setuptools": "84.0.0",
    }

    assert provenance["package_versions"] == expected_versions
    for package, version in expected_versions.items():
        assert f"{package}=={version}" in lock
    assert lock.count("--hash=sha256:") == len(expected_versions)
    assert "--no-binary numpy" in lock


def test_pinned_revisions_and_source_hashes_match_contract() -> None:
    provenance = _load_json(FIXTURE_ROOT / "provenance.json")

    assert provenance["repository_revisions"] == {
        "pythermalcomfort": "2597e88fed10fec2f40d49759ed9b74e10ce9f89",
        "research_analysis": "8cb4e7eabe25bc6dbbafa554a045ceacd56a865b",
    }
    assert provenance["source_hashes"] == {
        "_pmv_ppd_optimized.py": (
            "dd3c1f3d7ffacea65978cb080a1b676dbadc143eeb8121196dfa6b9b8a9d2518"
        ),
        "pmv_athb.py": "6ea3044b9ab0a229e3f47603d64c3070b707fb31271a9a92ff4fff0c92ec5c9d",
    }


def test_upstream_mit_notice_is_retained_byte_for_byte() -> None:
    retained = LICENSE.read_bytes()
    notice_start = retained.index(b"MIT License\n")
    upstream_notice = retained[notice_start:]

    assert hashlib.sha256(upstream_notice).hexdigest() == (
        "6e5fda37ef6f9b91b5227e5df241a1ddedd141d4daaa4c3e396703b4267237ad"
    )
