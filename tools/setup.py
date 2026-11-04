#!/usr/bin/env python3
"""Create the project virtualenv and install the Python side of Sumie.

    python tools/setup.py          # core renderer (numpy, opencv, Pillow)
    python tools/setup.py --ai     # + torch, onnxruntime and the model weights

Standard library only, so it runs on macOS, Windows and Linux with any
Python 3.10+. Everything lands inside the repository (.venv/, models/).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"

# (relative path, url, size, sha256). Both files are optional.
MODELS = [
    ("models/anime2sketch/netG.pth",
     "https://huggingface.co/public-data/Anime2Sketch/resolve/main/netG.pth",
     217631959,
     "ccabdcc3f5cf3c07cf65d58776acb21df7dfda825cdc70c9766a93fd62bfc488"),
    ("models/anime-seg/isnetis.onnx",
     "https://huggingface.co/skytnt/anime-seg/resolve/main/isnetis.onnx",
     176069933,
     "f15622d853e8260172812b657053460e20806f04b9e05147d49af7bed31a6e99"),
]


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(*command) -> None:
    print("  $", " ".join(str(c) for c in command), flush=True)
    subprocess.run([str(c) for c in command], check=True)


def ensure_venv() -> Path:
    python = venv_python()
    if not python.exists():
        print(f"==> creating virtualenv in {VENV}")
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    return python


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path, size: int, sha256: str) -> None:
    if target.exists() and target.stat().st_size == size and sha256_of(target) == sha256:
        print(f"==> {target.relative_to(ROOT)} already present and verified")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"==> downloading {target.name} ({size / 2**20:.0f} MB)")
    request = urllib.request.Request(url, headers={"User-Agent": "sumie-setup"})
    done = 0
    with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            out.write(block)
            done += len(block)
            print(f"\r    {done / 2**20:6.1f} / {size / 2**20:.0f} MB", end="", flush=True)
    print()
    if sha256_of(partial) != sha256:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"checksum mismatch for {target.name}; the download was discarded")
    partial.replace(target)
    print(f"    verified sha256 {sha256[:12]}...")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ai", action="store_true",
                        help="also install torch/onnxruntime and download the model weights (~400 MB)")
    parser.add_argument("--no-models", action="store_true",
                        help="with --ai: install packages but skip the weight downloads")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")

    python = ensure_venv()
    run(python, "-m", "pip", "install", "--upgrade", "pip")
    run(python, "-m", "pip", "install", "-r", ROOT / "client" / "requirements.txt")
    if args.ai:
        run(python, "-m", "pip", "install", "-r", ROOT / "client" / "requirements-ai.txt")
        if not args.no_models:
            for relative, url, size, sha256 in MODELS:
                download(url, ROOT / relative, size, sha256)

    print("\n==> checking imports")
    code = "import cv2, numpy, PIL; print('core ok: opencv', cv2.__version__, 'numpy', numpy.__version__)"
    run(python, "-I", "-c", code)
    print(f"\nPython environment ready: {python}")
    print("Next: python tools/install.py   (installs the game plugin)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
