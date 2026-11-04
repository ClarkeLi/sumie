"""Anime foreground segmentation (skytnt/anime-seg ISNet, ONNX).

Returns a soft alpha for the character. Falls back to None when the weights or
onnxruntime are missing so callers can use the classical mask instead.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

import planlog

MODEL = Path(__file__).resolve().parent.parent / "models" / "anime-seg" / "isnetis.onnx"
_session = None


def available(path=MODEL):
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return Path(path).is_file()


CACHE = Path(__file__).resolve().parent.parent / ".cache" / "coreml"


def _open(ort, path):
    """Prefer CoreML (Neural Engine / GPU) with a compiled-model cache.

    The first run compiles for ~7s, later runs load in ~0.4s and infer in ~0.3s
    using a fraction of the CPU. Plain CPU inference spins every core
    (~7 CPU-seconds per 0.9s of wall time), which is what made the fans run.
    Falls back to a few-thread CPU session if CoreML is unavailable or fails.
    """
    if "CoreMLExecutionProvider" in ort.get_available_providers():
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            planlog.note("anime-seg on CoreML (first run compiles and is cached)")
            return ort.InferenceSession(str(path), providers=[
                ("CoreMLExecutionProvider", {"ModelCacheDirectory": str(CACHE), "MLComputeUnits": "ALL"}),
                "CPUExecutionProvider"])
        except Exception:
            pass
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


def _load(path):
    global _session
    if _session is None:
        import onnxruntime as ort
        planlog.note(f"loading anime-seg ONNX ({Path(path).name}, {Path(path).stat().st_size // 2**20} MB)")
        _session = _open(ort, path)
    return _session


def foreground_alpha(rgb, path=MODEL, size=1024):
    """float32 alpha 0..1 with the input's height and width."""
    session = _load(path)
    h0, w0 = rgb.shape[:2]
    scaled = rgb.astype(np.float32) / 255.0
    h, w = (size, max(1, int(size * w0 / h0))) if h0 > w0 else (max(1, int(size * h0 / w0)), size)
    ph, pw = size - h, size - w
    canvas = np.zeros((size, size, 3), np.float32)
    canvas[ph // 2:ph // 2 + h, pw // 2:pw // 2 + w] = cv2.resize(scaled, (w, h))
    planlog.note(f"anime-seg inference {size}x{size}")
    out = session.run(None, {"img": canvas.transpose(2, 0, 1)[None]})[0][0, 0]
    out = out[ph // 2:ph // 2 + h, pw // 2:pw // 2 + w]
    return np.clip(cv2.resize(out, (w0, h0)), 0.0, 1.0)
