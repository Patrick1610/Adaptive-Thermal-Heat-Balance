"""Deterministic archive and HACS repository-layout checks."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from tools.build_release import ARCHIVE_TIMESTAMP, build_archive, integration_files

ROOT = Path(__file__).parents[1]


def test_hacs_layout_and_runtime_manifest_are_self_contained() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "custom_components/athb/manifest.json").read_text(encoding="utf-8")
    )
    integrations = [path for path in (ROOT / "custom_components").iterdir() if path.is_dir()]
    assert integrations == [ROOT / "custom_components/athb"]
    assert hacs == {
        "name": "Adaptive Thermal Heat Balance",
        "content_in_root": False,
        "render_readme": True,
    }
    assert {
        "domain",
        "documentation",
        "issue_tracker",
        "codeowners",
        "name",
        "version",
    } <= manifest.keys()
    assert manifest["domain"] == "athb"
    assert manifest["requirements"] == []


def test_release_archive_is_reproducible_complete_and_importable(
    tmp_path: Path,
) -> None:
    first = build_archive(ROOT, tmp_path / "first")
    second = build_archive(ROOT, tmp_path / "second")
    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    expected = [path.relative_to(ROOT).as_posix() for path in integration_files(ROOT)]
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == expected
        assert all(info.date_time == ARCHIVE_TIMESTAMP for info in archive.infolist())
        assert not any(
            "__pycache__" in name or name.endswith(".pyc") for name in archive.namelist()
        )
        archive.extractall(tmp_path / "extracted")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import custom_components.athb; "
                "import custom_components.athb.core.athb_engine; "
                "import custom_components.athb.runtime"
            ),
        ],
        cwd=tmp_path / "extracted",
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
