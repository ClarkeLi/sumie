#!/usr/bin/env python3
"""Local studio service for the Draw & Guess web console.

The BepInEx bridge serves the UI at http://127.0.0.1:8765/ but cannot run
OpenCV/torch. This tiny stdlib-only service runs the Python renderer for the
page: it accepts a pasted image plus options, writes a plan, previews it and
executes it stroke by stroke through the bridge.

Run manually:
    python client/studio_server.py

The plugin can also start it automatically (see the Studio config section).
It listens on 127.0.0.1 only; never expose it to a network.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from studio_common import bounded_int, bounded_number, parse_region  # noqa: E402

VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
MAX_BODY = 40 * 1024 * 1024
MAX_IMAGE = 24 * 1024 * 1024

STYLES = {
    "anime_study": {
        "name": "Anime Study",
        "description": "主体去背景、少色、粗笔蛇形涂色，最后勾线。无填充。",
        "defaults": {"time": 180, "colors": None, "max_strokes": 900,
                     "study_pause": 0.26, "study_speed": 0.90, "lineart": "auto"},
    },
    "anime_human": {
        "name": "Anime Human",
        "description": "像人一样画：先线稿，每笔压感粗细不同，真人节奏，只用油漆桶上色；有无压感都能用。",
        "defaults": {"time": 120, "colors": None, "lineart": "auto"},
    },
    "anime_flat": {
        "name": "Anime Flat",
        "description": "分色块 + 单中心线，适合平涂插画。",
        "defaults": {"time": 120, "colors": 24, "lineart": "auto"},
    },
    "sketch": {
        "name": "Sketch",
        "description": "中心线、排线和少量颜色点缀。",
        "defaults": {"time": 90, "colors": 8, "lineart": "auto"},
    },
    "impasto": {
        "name": "Impasto",
        "description": "多层曲线笔触，厚涂质感。",
        "defaults": {"time": 180, "colors": 24, "lineart": "off"},
    },
    "watercolor": {
        "name": "Watercolor",
        "description": "浅色块、留白和细线，水彩/马克笔感。",
        "defaults": {"time": 120, "colors": 8, "lineart": "auto"},
    },
}
STYLE_IDS = tuple(STYLES)

LINEART = ("auto", "off", "anime2sketch")
DEVICES = ("auto", "cuda", "mps", "cpu")


class BadRequest(ValueError):
    pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# options / command line
# ---------------------------------------------------------------------------

def normalize_options(raw):
    raw = raw or {}
    if not isinstance(raw, dict):
        raise BadRequest("options must be an object")
    style = str(raw.get("style") or "anime_study")
    if style not in STYLES:
        raise BadRequest(f"unknown style: {style}")
    lineart = str(raw.get("lineart") or "auto")
    if lineart not in LINEART:
        raise BadRequest(f"lineart must be one of {', '.join(LINEART)}")
    pace = str(raw.get("pace") or "slow")
    if pace not in ("slow", "instant"):
        raise BadRequest("pace must be slow or instant")
    device = str(raw.get("lineart_device") or "auto")
    if device not in DEVICES:
        raise BadRequest(f"lineart_device must be one of {', '.join(DEVICES)}")
    options = {
        "style": style,
        "time": bounded_number(raw.get("time"), 3, 3600, None, "time"),
        "default_time": bounded_number(raw.get("default_time"), 5, 3600, 180, "default_time"),
        "colors": bounded_int(raw.get("colors"), 2, 64, None, "colors"),
        "max_strokes": bounded_int(raw.get("max_strokes"), 1, 6000, None, "max_strokes"),
        "study_pause": bounded_number(raw.get("study_pause"), 0, 5, 0.26, "study_pause"),
        "study_speed": bounded_number(raw.get("study_speed"), 0, 5, 0.90, "study_speed"),
        "seed": bounded_int(raw.get("seed"), 0, 2147483647, 7, "seed"),
        "margin": bounded_number(raw.get("margin"), 0, 0.30, 0.04, "margin"),
        "max_dim": bounded_int(raw.get("max_dim"), 200, 2000, None, "max_dim"),
        "lineart": lineart,
        "pace": pace,
        "lineart_device": device,
        "canvas_aspect": bounded_number(raw.get("canvas_aspect"), 0.4, 4.0,
                                        16 / 9, "canvas_aspect"),
        "force": bool(raw.get("force")),
        # None = the renderer's own default (skill 2.3, pen auto)
        "human_pressure": bounded_number(raw.get("human_pressure"), 0.3, 3.0, None, "human_pressure"),
        "human_skill": bounded_number(raw.get("human_skill"), 0.4, 3.5, None, "human_skill"),
        "pen": str(raw.get("pen") or "auto"),
        "colour": str(raw.get("colour") or "auto"),
        "borders": bool(raw.get("borders", True)),
        "soften_frame": str(raw.get("soften_frame") or "auto"),
        "game_fps": bounded_number(raw.get("game_fps"), 30, 1000, None, "game_fps"),
        "lineart_threshold": bounded_int(raw.get("lineart_threshold"), 1, 255, None, "lineart_threshold"),
        "line_color": str(raw.get("line_color") or "").strip() or None,
        "fill_mode": str(raw.get("fill_mode") or "auto"),
        "fill_white": bool(raw.get("fill_white", False)),
        "base_fill": bool(raw.get("base_fill", True)),
        "outlines": bool(raw.get("outlines", True)),
        "max_regions": bounded_int(raw.get("max_regions"), 10, 5000, None, "max_regions"),
    }
    if options["pen"] not in ("auto", "direct", "native"):
        raise BadRequest("pen must be auto, direct or native")
    if options["colour"] not in ("auto", "brush", "bucket"):
        raise BadRequest("colour must be auto, brush or bucket")
    if options["soften_frame"] not in ("auto", "on", "off"):
        raise BadRequest("soften_frame must be auto, on or off")
    if options["fill_mode"] not in ("auto", "real", "polygon"):
        raise BadRequest("fill_mode must be auto, real or polygon")
    color = options["line_color"]
    if color and not re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
        raise BadRequest("line_color must be a hex colour like #202020")
    if style != "anime_study":
        options.pop("study_pause", None)
        options.pop("study_speed", None)
    if style != "anime_human":
        options.pop("human_pressure", None)
        options.pop("human_skill", None)
        options.pop("pen", None)
        options.pop("colour", None)
        options.pop("borders", None)
        options.pop("soften_frame", None)
        options.pop("game_fps", None)
    return options


def build_artist_argv(python, script, image, options, *, url=None, token=None,
                      clear=False, region=None, plan_in=None, plan_out=None,
                      preview=None, progress=None, stages=None, dry_run=False,
                      time_value=None, plan_progress=None):
    """Build a subprocess argv list. Pure function, safe to unit-test."""
    argv = [
        str(python), "-u", str(script), str(image),
        "--style", options["style"],
        "--default-time", f'{float(options.get("default_time", 180)):.1f}',
        "--seed", str(options.get("seed", 7)),
        "--margin", f'{float(options.get("margin", 0.04)):.3f}',
        "--lineart", options.get("lineart", "auto"),
        "--pace", options.get("pace", "slow"),
        "--lineart-device", options.get("lineart_device", "auto"),
        "--canvas-aspect", f'{float(options.get("canvas_aspect", 16 / 9)):.5f}',
    ]
    clock = time_value if time_value is not None else options.get("time")
    if clock is not None:
        argv += ["--time", f'{float(clock):.1f}']
    if options.get("max_dim") is not None:
        argv += ["--max-dim", str(int(options["max_dim"]))]
    if options.get("colors") is not None:
        argv += ["--colors", str(int(options["colors"]))]
    if options.get("max_strokes") is not None:
        argv += ["--max-strokes", str(int(options["max_strokes"]))]
    if options.get("study_pause") is not None:
        argv += ["--study-pause", f'{float(options["study_pause"]):.3f}']
    if options.get("study_speed") is not None:
        argv += ["--study-speed", f'{float(options["study_speed"]):.3f}']
    if options.get("human_pressure") is not None:
        argv += ["--pressure", f'{float(options["human_pressure"]):.3f}']
    if options.get("pen"):
        argv += ["--pen", options["pen"]]
    if options.get("colour"):
        argv += ["--colour", options["colour"]]
    if options.get("borders") is False:
        argv += ["--borders", "off"]
    if options.get("soften_frame") not in (None, "auto"):
        argv += ["--soften-frame", options["soften_frame"]]
    if options.get("game_fps") is not None:
        argv += ["--game-fps", f'{float(options["game_fps"]):.1f}']
    if options.get("lineart_threshold") is not None:
        argv += ["--lineart-threshold", str(int(options["lineart_threshold"]))]
    if options.get("line_color"):
        argv += ["--line-color", options["line_color"]]
    if options.get("fill_mode") not in (None, "auto"):
        argv += ["--fill-mode", options["fill_mode"]]
    if options.get("fill_white"):
        argv.append("--fill-white")
    if options.get("base_fill") is False:
        argv.append("--no-base-fill")
    if options.get("outlines") is False:
        argv.append("--no-outlines")
    if options.get("max_regions") is not None:
        argv += ["--max-regions", str(int(options["max_regions"]))]
    if options.get("human_skill") is not None:
        argv += ["--skill", f'{float(options["human_skill"]):.3f}']
    if options.get("force"):
        argv.append("--force")
    argv.append("--clear" if clear else "--no-clear")
    if region:
        x, y, w, h = region
        argv += ["--region", f"{x:.6f},{y:.6f},{w:.6f},{h:.6f}"]
    if url:
        argv += ["--url", str(url)]
    if token:
        argv += ["--token", str(token)]
    if plan_in:
        argv += ["--plan-in", str(plan_in)]
    if plan_out:
        argv += ["--plan-out", str(plan_out)]
    if preview:
        argv += ["--preview", str(preview)]
    if progress:
        argv += ["--progress-file", str(progress)]
    if plan_progress:
        argv += ["--plan-progress-file", str(plan_progress)]
    if stages:
        argv += ["--study-stage-dir", str(stages)]
    if dry_run:
        argv.append("--dry-run")
    return argv


# ---------------------------------------------------------------------------
# images / files
# ---------------------------------------------------------------------------

def sniff_image(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw.startswith(b"BM"):
        return "bmp"
    return None


def decode_image(value):
    if not isinstance(value, str) or not value:
        raise BadRequest("image is required (base64 or data URL)")
    text = value.strip()
    if text.startswith("data:"):
        try:
            header, text = text.split(",", 1)
        except ValueError:
            raise BadRequest("malformed data URL") from None
        if ";base64" not in header.lower():
            raise BadRequest("image data URL must be base64")
    try:
        raw = base64.b64decode(text, validate=False)
    except Exception:
        raise BadRequest("image is not valid base64") from None
    if not raw:
        raise BadRequest("image is empty")
    if len(raw) > MAX_IMAGE:
        raise BadRequest("image is too large (max 24 MB)")
    ext = sniff_image(raw)
    if ext is None:
        raise BadRequest("unsupported image format (PNG/JPEG/WebP/GIF/BMP)")
    return raw, ext


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def fetch_bridge(base_url, token="", timeout=3.0):
    """Return (state, round) from the bridge; empty dicts when unreachable."""
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Api-Key"] = token

    def get(path):
        request = urllib.request.Request(base_url.rstrip("/") + path,
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    try:
        game = get("/api/v1/game")
        if isinstance(game, dict):
            return game, game.get("round") or {}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass
    state = {}
    round_data = {}
    try:
        state = get("/api/v1/state") or {}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass
    try:
        round_data = get("/api/v1/round") or {}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass
    return state, round_data


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(str(temporary), str(target))


def log_tail(path, limit=60, size=12000):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-limit:])[-size:]


def last_result(text):
    """Return the last JSON object that looks like an execution result."""
    decoder = json.JSONDecoder()
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in reversed(starts):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict) and (
                "applied" in value or "sent" in value or "elapsed" in value):
            return value
    return None


def redact_argv(argv):
    """Keep API tokens out of job logs and the web UI."""
    result = list(argv)
    for index, value in enumerate(result[:-1]):
        if value == "--token":
            result[index + 1] = "***"
    return result


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, job_id, directory, mode, image_name, image_ext, signature,
                 options, region, clear, plan_job=None):
        self.id = job_id
        self.directory = Path(directory)
        self.mode = mode
        self.image_name = image_name
        self.image_ext = image_ext
        self.signature = signature
        self.options = options
        self.region = list(region) if region else None
        self.clear = bool(clear)
        self.plan_job = plan_job
        self.status = "queued"
        self.created = time.time()
        self.updated = self.created
        self.error = None
        self.result = None
        self.process = None
        self.cancelled = False
        self.reused_plan = False
        self.lock = threading.Lock()

    @property
    def image_path(self):
        return self.directory / ("input." + self.image_ext)

    @property
    def plan_path(self):
        return self.directory / "plan.json"

    @property
    def preview_path(self):
        return self.directory / "preview.png"

    @property
    def progress_path(self):
        return self.directory / "progress.json"

    @property
    def plan_progress_path(self):
        return self.directory / "plan_progress.json"

    @property
    def log_path(self):
        return self.directory / "run.log"

    @property
    def metrics_path(self):
        return self.directory / "metrics.json"

    @property
    def stages_path(self):
        return self.directory / "stages"

    def touch(self, status=None):
        self.updated = time.time()
        if status:
            self.status = status
        self.save()

    def save(self):
        payload = {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "image_name": self.image_name,
            "image_ext": self.image_ext,
            "signature": self.signature,
            "options": self.options,
            "region": self.region,
            "clear": self.clear,
            "plan_job": self.plan_job,
            "reused_plan": self.reused_plan,
            "error": self.error,
            "result": self.result,
        }
        write_json(self.directory / "job.json", payload)

    def progress(self):
        data = read_json(self.progress_path, {}) or {}
        if not data and self.status in ("queued", "planning"):
            return {"status": self.status, "sent": 0, "ops": 0}
        if data and self.status in ("done", "failed", "cancelled", "interrupted"):
            data = dict(data)
            data["status"] = self.status
        return data

    def stages(self):
        directory = self.stages_path
        if not directory.is_dir():
            return []
        files = sorted(directory.glob("*.png"))
        result = []
        for path in files:
            result.append({
                "name": path.stem,
                "url": f"/jobs/{self.id}/stages/{path.name}?v={int(self.updated)}",
            })
        return result


class Studio:
    def __init__(self, args):
        self.args = args
        self.repo = Path(args.repo).resolve()
        self.work_dir = Path(args.work_dir).resolve()
        self.jobs_dir = self.work_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.artist = self.repo / "client" / "dg_artist.py"
        self.metrics_tool = self.repo / "tools" / "study_metrics.py"
        self.python = args.python
        self.jobs = {}
        self.order = []
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.running = True
        self.active_id = None
        self.server = None
        self.metrics_inflight = set()
        self.metrics_lock = threading.Lock()
        self.load_jobs()
        self.worker = threading.Thread(target=self.worker_loop, name="studio-worker",
                                       daemon=True)
        self.worker.start()

    # -- persistence ------------------------------------------------------
    def load_jobs(self):
        for directory in self.jobs_dir.iterdir():
            if not directory.is_dir():
                continue
            meta = read_json(directory / "job.json", None)
            if not isinstance(meta, dict) or not meta.get("id"):
                continue
            try:
                job = Job(meta["id"], directory, meta.get("mode", "plan"),
                          meta.get("image_name", ""),
                          meta.get("image_ext", "png"),
                          meta.get("signature", ""),
                          meta.get("options") or {}, meta.get("region"),
                          meta.get("clear", False), meta.get("plan_job"))
                job.created = float(meta.get("created") or time.time())
                job.updated = float(meta.get("updated") or job.created)
                job.error = meta.get("error")
                job.result = meta.get("result")
                job.reused_plan = bool(meta.get("reused_plan"))
                if job.status in ("queued", "planning", "drawing"):
                    job.status = "interrupted"
                    job.error = job.error or "studio restarted while this job was running"
                self.jobs[job.id] = job
                self.order.append(job.id)
            except (TypeError, ValueError):
                continue
        self.order.sort(key=lambda jid: self.jobs[jid].created, reverse=True)

    # -- public API -------------------------------------------------------
    def health(self):
        return {
            "ok": True,
            "name": "Draw & Guess Studio",
            "version": VERSION,
            "repo": str(self.repo),
            "python": str(self.python),
            "bridge_url": self.args.bridge_url,
            "jobs": len(self.jobs),
            "active": self.active_id,
        }

    def style_list(self):
        return [dict(id=key, **value) for key, value in STYLES.items()]

    def list_jobs(self, limit=50):
        with self.lock:
            ids = sorted(self.jobs, key=lambda jid: self.jobs[jid].created,
                         reverse=True)
        return [self.snapshot(self.jobs[jid], include_log=False)
                for jid in ids[:max(1, min(200, limit))]]

    def snapshot(self, job, include_log=True):
        image_url = f"/jobs/{job.id}/input.{job.image_ext}?v={int(job.updated)}"
        preview = job.preview_path.is_file()
        plan_data = read_json(job.plan_path, [])
        if not isinstance(plan_data, list):
            plan_data = []
        data = {
            "id": job.id,
            "mode": job.mode,
            "status": job.status,
            "created": job.created,
            "updated": job.updated,
            "options": job.options,
            "region": job.region,
            "clear": job.clear,
            "plan_job": job.plan_job,
            "reused_plan": job.reused_plan,
            "image_name": job.image_name,
            "image_url": image_url,
            "preview_url": (f"/jobs/{job.id}/preview.png?v={int(job.updated)}"
                            if preview else None),
            "stages": job.stages(),
            "progress": job.progress() if job.mode == "draw" else {},
            "plan_progress": read_json(job.plan_progress_path, None),
            "plan_ops": len(plan_data),
            "metrics": read_json(job.metrics_path, None),
            "result": job.result,
            "error": job.error,
        }
        if include_log:
            data["log_tail"] = log_tail(job.log_path)
        return data

    def get(self, job_id):
        return self.jobs.get(job_id)

    def create(self, payload):
        if not isinstance(payload, dict):
            raise BadRequest("body must be a JSON object")
        mode = payload.get("mode") or "plan"
        if mode not in ("plan", "draw"):
            raise BadRequest("mode must be 'plan' or 'draw'")
        options = normalize_options(payload.get("options"))
        state, round_data = fetch_bridge(self.args.bridge_url,
                                         self.args.bridge_token)
        canvas = (state or {}).get("canvas") or {}
        try:
            aspect = float(canvas.get("aspect") or options["canvas_aspect"])
            if 0.4 <= aspect <= 4.0:
                options["canvas_aspect"] = aspect
        except (TypeError, ValueError):
            pass
        if options.get("time") is not None:
            options["plan_time"] = float(options["time"])
        else:
            recommended = (round_data or {}).get("recommended") or {}
            try:
                remaining = float(recommended.get("remaining") or 0)
            except (TypeError, ValueError):
                remaining = 0.0
            options["plan_time"] = (max(3.0, remaining) if remaining > 3
                                    else float(options["default_time"]))
        region_value = payload.get("region")
        region = None
        if region_value not in (None, ""):
            try:
                region = parse_region(region_value)
            except ValueError as exc:
                raise BadRequest(str(exc)) from None
            if region == (0.0, 0.0, 1.0, 1.0):
                region = None
        raw, ext = decode_image(payload.get("image"))
        clear = bool(payload.get("clear", False))
        signature_options = {key: value for key, value in options.items()
                             if key != "plan_time"}
        signature = hashlib.sha256(
            raw + json.dumps({"options": signature_options, "region": region},
                             sort_keys=True).encode("utf-8")).hexdigest()

        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        directory = self.jobs_dir / job_id
        directory.mkdir(parents=True, exist_ok=False)
        (directory / f"input.{ext}").write_bytes(raw)

        plan_job = payload.get("plan_job")
        source = self.jobs.get(plan_job) if plan_job else None
        valid_source = (mode == "draw" and source is not None and
                        source.signature == signature and
                        source.plan_path.is_file())
        if plan_job and not valid_source:
            plan_job = None

        job = Job(job_id, directory, mode,
                  str(payload.get("filename") or "clipboard." + ext),
                  ext, signature, options, region, clear, plan_job)
        if valid_source:
            job.reused_plan = True
            job.plan_path.write_bytes(source.plan_path.read_bytes())
            if source.preview_path.is_file():
                job.preview_path.write_bytes(source.preview_path.read_bytes())
            if source.stages_path.is_dir():
                job.stages_path.mkdir(exist_ok=True)
                for stage in source.stages_path.glob("*.png"):
                    (job.stages_path / stage.name).write_bytes(stage.read_bytes())
        job.save()
        with self.lock:
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
        self.queue.put(job.id)
        return job

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return None
        job.cancelled = True
        process = job.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
        if job.status in ("queued", "planning", "planned", "drawing"):
            job.touch("cancelled")
        return self.snapshot(job)

    def shutdown(self):
        self.running = False
        if self.active_id:
            try:
                self.cancel(self.active_id)
            except Exception:
                traceback.print_exc()
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    # -- worker -----------------------------------------------------------
    def worker_loop(self):
        while self.running:
            try:
                job_id = self.queue.get(timeout=0.4)
            except queue.Empty:
                continue
            job = self.jobs.get(job_id)
            if not job or job.cancelled:
                continue
            self.active_id = job.id
            try:
                if job.mode == "draw" and job.reused_plan:
                    self.run_execute(job)
                else:
                    self.run_plan(job)
                    if job.cancelled:
                        raise Cancelled()
                    if job.mode == "draw":
                        self.run_execute(job)
                    else:
                        job.touch("planned")
            except Cancelled:
                job.touch("cancelled")
            except Exception as exc:
                job.error = str(exc) or exc.__class__.__name__
                job.touch("failed")
                with open(job.log_path, "a", encoding="utf-8") as handle:
                    handle.write("\n[studio] " + job.error + "\n")
                    traceback.print_exc(file=handle)
            finally:
                job.process = None
                self.active_id = None

    def run_process(self, job, argv, status):
        job.touch(status)
        if job.cancelled:
            raise Cancelled()
        with open(job.log_path, "a", encoding="utf-8") as log:
            log.write("\n[studio] " + " ".join(redact_argv(argv)) + "\n")
            log.flush()
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"     # the renderer prints Chinese step titles; Windows defaults to cp1252
            env["PYTHONUTF8"] = "1"
            process = subprocess.Popen(argv, cwd=str(self.repo), stdout=log,
                                       stderr=subprocess.STDOUT, env=env)
            job.process = process
            if job.cancelled and process.poll() is None:
                process.terminate()
            code = process.wait()
            job.process = None
        if job.cancelled:
            raise Cancelled()
        if code != 0:
            raise RuntimeError(f"{Path(argv[2]).name} exited with code {code}")
        return code

    def run_plan(self, job):
        stages = job.stages_path if job.options["style"] == "anime_study" else None
        if stages:
            stages.mkdir(parents=True, exist_ok=True)
        argv = build_artist_argv(
            self.python, self.artist, job.image_path, job.options,
            url=self.args.bridge_url, token=self.args.bridge_token,
            clear=False, region=job.region,
            plan_out=job.plan_path, preview=job.preview_path,
            stages=stages, dry_run=True, plan_progress=job.plan_progress_path,
            time_value=job.options.get("plan_time"))
        self.run_process(job, argv, "planning")
        if not job.plan_path.is_file():
            raise RuntimeError("planning finished without a plan file")
        job.touch("planned")

    def run_execute(self, job):
        if not job.plan_path.is_file():
            raise RuntimeError("no plan to execute")
        argv = build_artist_argv(
            self.python, self.artist, job.image_path, job.options,
            url=self.args.bridge_url, token=self.args.bridge_token,
            clear=job.clear, region=job.region,
            plan_in=job.plan_path, progress=job.progress_path,
            time_value=job.options.get("time"))
        self.run_process(job, argv, "drawing")
        progress = job.progress()
        result = last_result(log_tail(job.log_path, limit=4000, size=24000))
        if result is None:
            result = {key: progress.get(key) for key in
                      ("ops", "sent", "applied", "failed", "elapsed")}
        job.result = result
        job.touch("done")

    # -- metrics ----------------------------------------------------------
    def metrics(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return None
        cached = read_json(job.metrics_path, None)
        if cached:
            return {"status": "ready", "metrics": cached}
        if not job.plan_path.is_file() or not job.image_path.is_file():
            return {"status": "unavailable", "error": "plan or image missing"}
        with self.metrics_lock:
            if job.id in self.metrics_inflight:
                return {"status": "computing"}
            self.metrics_inflight.add(job.id)
        try:
            aspect = f'{float(job.options.get("canvas_aspect", 16 / 9)):.5f}'
            if job.options.get("style") == "anime_human":
                argv = [str(self.python), "-u", str(self.repo / "tools" / "human_eval.py"),
                        str(job.plan_path), "--json", "--canvas-aspect", aspect]
            else:
                argv = [str(self.python), "-u", str(self.metrics_tool),
                        str(job.image_path), str(job.plan_path), "--json",
                        "--canvas-aspect", aspect,
                        "--margin", f'{float(job.options.get("margin", 0.04)):.3f}']
                if job.region:
                    argv += ["--region",
                             ",".join(f"{value:.6f}" for value in job.region)]
            try:
                completed = subprocess.run(argv, cwd=str(self.repo),
                                           capture_output=True, text=True,
                                           timeout=180)
            except subprocess.TimeoutExpired:
                return {"status": "error", "error": "metrics timed out"}
            payload = None
            for line in reversed(completed.stdout.splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                    break
                except ValueError:
                    continue
            if payload is None:
                error = (completed.stderr or completed.stdout or "").strip()
                return {"status": "error", "error": error[-1200:] or "metrics failed"}
            write_json(job.metrics_path, payload)
            return {"status": "ready", "metrics": payload}
        finally:
            with self.metrics_lock:
                self.metrics_inflight.discard(job.id)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DGStudio/" + VERSION
    protocol_version = "HTTP/1.1"

    @property
    def studio(self):
        return self.server.studio

    def log_message(self, fmt, *args):
        if self.studio.args.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"ok": False, "error": str(message)}, status)

    def _file(self, path, content_type=None):
        try:
            data = Path(path).read_bytes()
        except OSError:
            self._error("file not found", 404)
            return
        content_type = content_type or mimetypes.guess_type(str(path))[0] or \
            "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/health"):
                self._json(self.studio.health())
                return
            if path == "/styles":
                self._json(self.studio.style_list())
                return
            if path == "/jobs":
                self._json(self.studio.list_jobs())
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 2 and parts[0] == "jobs":
                job = self.studio.get(parts[1])
                if not job:
                    self._error("job not found", 404)
                    return
                if len(parts) == 2:
                    self._json(self.studio.snapshot(job))
                    return
                if len(parts) == 3 and parts[2] == "metrics":
                    self._json(self.studio.metrics(job.id))
                    return
                if len(parts) == 3 and parts[2] in ("progress.json", "plan.json",
                                                    "metrics.json", "job.json"):
                    self._file(job.directory / parts[2], "application/json")
                    return
                if len(parts) == 3 and parts[2].startswith("input."):
                    self._file(job.directory / parts[2])
                    return
                if len(parts) == 3 and parts[2] == "preview.png":
                    self._file(job.preview_path, "image/png")
                    return
                if len(parts) == 4 and parts[2] == "stages":
                    target = (job.stages_path / parts[3]).resolve()
                    root = job.stages_path.resolve()
                    if not str(target).startswith(str(root) + os.sep):
                        self._error("not found", 404)
                        return
                    self._file(target, "image/png")
                    return
            self._error("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self._error(exc, 500)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("invalid Content-Length") from None
        if length <= 0:
            raise BadRequest("empty body")
        if length > MAX_BODY:
            raise BadRequest("body is too large")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("body must be JSON") from None

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/jobs":
                job = self.studio.create(self._body())
                self._json({"ok": True, "job": self.studio.snapshot(job)},
                           status=201)
                return
            parts = [part for part in path.split("/") if part]
            if (len(parts) == 3 and parts[0] == "jobs" and
                    parts[2] == "cancel"):
                snapshot = self.studio.cancel(parts[1])
                if snapshot is None:
                    self._error("job not found", 404)
                else:
                    self._json({"ok": True, "job": snapshot})
                return
            if path == "/shutdown":
                self._json({"ok": True})
                self.studio.shutdown()
                return
            self._error("not found", 404)
        except BadRequest as exc:
            self._error(exc, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self._error(exc, 500)


def serve(args):
    studio = Studio(args)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.studio = studio
    studio.server = server

    def stop(signum, frame):
        studio.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"studio ready: http://{args.host}:{args.port}/  "
          f"(repo={studio.repo})", flush=True)
    try:
        server.serve_forever(poll_interval=0.4)
    finally:
        studio.running = False
        server.server_close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--bridge-token", default=os.environ.get("DG_BRIDGE_TOKEN", ""))
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--work-dir", default=None,
                        help="job storage; default: <repo>/.cache/studio")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter used for the renderer subprocess")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.work_dir is None:
        args.work_dir = str(Path(args.repo).resolve() / ".cache" / "studio")
    if not Path(args.repo).is_dir():
        raise SystemExit(f"repo not found: {args.repo}")
    if not Path(args.python).is_file():
        raise SystemExit(f"python not found: {args.python}")
    serve(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
