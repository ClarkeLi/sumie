#!/usr/bin/env python3
"""Build the release archive and checksums into ./releases.

    python tools/make_release.py            # releases/Sumie-v<VERSION>.zip + SHA256SUMS.txt

The archive contains the source tree (plugin source, Python client, docs,
installers) and nothing generated: no .venv, caches, models or build output.
"""
from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()
OUT = ROOT / "releases"
SKIP_DIRS = {".git", ".venv", ".cache", "__pycache__", "models", "dist", "obj", "bin",
             "releases", "canvas-backups", ".claude", ".github", "node_modules"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIX = {".pyc"}
# big marketing media stays on GitHub, not in the install archive
SKIP_PATHS = {"assets/demo.mp4"}


def wanted(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if path.name in SKIP_FILES or path.suffix in SKIP_SUFFIX:
        return False
    return rel.as_posix() not in SKIP_PATHS


def main() -> int:
    OUT.mkdir(exist_ok=True)
    name = f"Sumie-v{VERSION}"
    archive = OUT / f"{name}.zip"
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and wanted(p))
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo.from_file(path, f"{name}/{rel}")
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (OUT / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n")
    print(f"{archive}  ({archive.stat().st_size / 1e6:.2f} MB, {len(files)} files)")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
