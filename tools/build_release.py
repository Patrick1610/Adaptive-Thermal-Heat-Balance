#!/usr/bin/env python3
"""Build a byte-reproducible local ATHB integration archive."""

from __future__ import annotations

import argparse
import json
import stat
import zipfile
from pathlib import Path

ARCHIVE_TIMESTAMP = (2026, 9, 11, 0, 0, 0)


def integration_files(root: Path) -> tuple[Path, ...]:
    """Return the complete runtime package without caches or generated files."""

    package = root / "custom_components" / "athb"
    return tuple(
        sorted(
            path
            for path in package.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    )


def build_archive(root: Path, output: Path) -> Path:
    """Write a deterministic archive with canonical paths and permissions."""

    manifest = json.loads(
        (root / "custom_components" / "athb" / "manifest.json").read_text(encoding="utf-8")
    )
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"athb-{manifest['version']}.zip"
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in integration_files(root):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(
                info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist"))
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(build_archive(root, arguments.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
