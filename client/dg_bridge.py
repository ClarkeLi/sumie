#!/usr/bin/env python3
"""Draw & Guess Bridge client / CLI.

Standard library only. Talks to the BepInEx plugin's local HTTP API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_URL = os.environ.get("DG_BRIDGE_URL", "http://127.0.0.1:8765")
DEFAULT_TOKEN = os.environ.get("DG_BRIDGE_TOKEN", "")


class BridgeError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BridgeClient:
    def __init__(self, base_url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, data: bytes | None = None,
                 content_type: str | None = None, timeout: float | None = None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Api-Key"] = self.token
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                if isinstance(payload, dict) and payload.get("ok") is False:
                    raise BridgeError(payload.get("error", "unknown bridge error"), response.status)
                return payload.get("result", payload) if isinstance(payload, dict) else payload
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
                message = payload.get("error", raw.decode("utf-8", "replace"))
            except Exception:
                message = raw.decode("utf-8", "replace") or str(error)
            raise BridgeError(message, error.code) from None
        except urllib.error.URLError as error:
            raise BridgeError(f"Cannot reach the bridge at {self.base_url}: {error.reason}") from None

    def _post_json(self, path: str, payload: dict, timeout: float | None = None):
        return self._request("POST", path, json.dumps(payload).encode("utf-8"),
                             "application/json", timeout)

    def state(self):
        return self._request("GET", "/api/v1/state")

    def lines(self, limit: int = 10000):
        return self._request("GET", f"/api/v1/lines?limit={int(limit)}")

    def round(self):
        return self._request("GET", "/api/v1/round")

    def fill(self, points, color: str | None = None, force: bool = False, space: str = "normalized"):
        payload = {"points": points, "force": force, "space": space}
        if color:
            payload["color"] = color
        return self._post_json("/api/v1/fill", payload)

    def layers(self):
        """Virtual layer capabilities of the plugin (404 on plugins without them)."""
        return self._request("GET", "/api/v1/layers")

    def layer(self, index: int):
        return self._post_json("/api/v1/layer", {"index": int(index)})

    def draw_strokes(self, strokes, clear: bool = False, force: bool = False):
        if isinstance(strokes, dict):
            payload = dict(strokes)
            payload.setdefault("clear", clear)
            payload.setdefault("force", force)
        else:
            payload = {"strokes": strokes, "clear": clear, "force": force}
        return self._post_json("/api/v1/strokes", payload)

    def screenshot(self, path):
        """Ask the game to save a PNG of its window to `path` (a path on this machine)."""
        return self._request("GET", "/api/v1/screenshot?" + urllib.parse.urlencode({"path": str(path)}))

    def pen_status(self):
        return self._request("GET", "/api/v1/pen")

    def pen_stroke(self, samples, force: bool = False, ignore_focus: bool = True,
                   timeout: float | None = None, vlayer: int | None = None):
        """Replay [[t_ms, x, y, pressure], ...] through the game's virtual pen."""
        payload = {"samples": samples, "force": force, "ignoreFocus": ignore_focus}
        if vlayer is not None:
            payload["vlayer"] = int(vlayer)
        return self._post_json("/api/v1/stroke/pen", payload,
                               timeout or (samples[-1][0] / 1000.0 + 20.0))

    def draw_image(self, path: str | Path, *, cells: int = 48, levels: int = 8,
                   clear: bool = True, force: bool = False, skip_background: bool = True,
                   margin: float = 0.04, max_strokes: int = 6000, per_frame: int = 24):
        image = Path(path).read_bytes()
        query = urllib.parse.urlencode({
            "cells": cells,
            "levels": levels,
            "clear": str(clear).lower(),
            "force": str(force).lower(),
            "skipBackground": str(skip_background).lower(),
            "margin": margin,
            "maxStrokes": max_strokes,
            "perFrame": per_frame,
        })
        return self._request("POST", "/api/v1/image?" + query, image, "application/octet-stream")

    def clear(self, force: bool = False):
        return self._post_json("/api/v1/clear", {"force": force})

    def undo(self):
        return self._post_json("/api/v1/undo", {})

    def redo(self):
        return self._post_json("/api/v1/redo", {})

    def submit(self):
        return self._post_json("/api/v1/submit", {})

    def color(self, color: str):
        return self._post_json("/api/v1/color", {"color": color})

    def brush(self, size: int):
        return self._post_json("/api/v1/brush", {"size": int(size)})

    def tool(self, name: str):
        return self._post_json("/api/v1/tool", {"tool": name})

    def buttons(self):
        return self._request("GET", "/api/v1/ui/buttons")

    def click(self, text: str | None = None, name: str | None = None, index: int = 0):
        payload: dict[str, object] = {"index": int(index)}
        if text:
            payload["text"] = text
        if name:
            payload["name"] = name
        return self._post_json("/api/v1/ui/click", payload)


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def summarize(state: dict) -> str:
    if not state.get("available"):
        return "未连接画布: " + str(state.get("hint", ""))
    canvas = state.get("canvas") or {}
    size = f"{canvas.get('width', 0):.1f}x{canvas.get('height', 0):.1f}" if canvas.get("initialized") else "not initialized"
    return (f"{state.get('module')} | canDraw={state.get('canDraw')} | tool={state.get('tool')} | "
            f"color={state.get('color')} | brush={state.get('brushSize')} | lines={state.get('lines')} | canvas={size}")


def command_watch(client: BridgeClient, interval: float):
    last = None
    try:
        while True:
            state = client.state()
            signature = json.dumps(state, sort_keys=True, ensure_ascii=False)
            if signature != last:
                last = signature
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] {summarize(state)}", flush=True)
            time.sleep(max(0.25, interval))
    except KeyboardInterrupt:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draw & Guess Bridge client")
    parser.add_argument("--url", default=DEFAULT_URL, help="bridge base URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="API token if configured")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show current drawing state")
    lines = sub.add_parser("lines", help="dump current line data")
    lines.add_argument("--limit", type=int, default=10000)
    lines.add_argument("--out", help="write JSON to this file")

    draw_json = sub.add_parser("draw-json", help="send strokes from a JSON file")
    draw_json.add_argument("file")
    draw_json.add_argument("--clear", action="store_true")
    draw_json.add_argument("--force", action="store_true")

    image = sub.add_parser("draw-image", help="draw a PNG/JPEG through the in-game image renderer")
    image.add_argument("file")
    image.add_argument("--cells", type=int, default=48, help="cells along the long edge (4-200)")
    image.add_argument("--levels", type=int, default=8, help="per-channel color levels (2-64)")
    image.add_argument("--no-clear", action="store_true", help="append instead of clearing first")
    image.add_argument("--force", action="store_true")
    image.add_argument("--keep-bg", action="store_true", help="draw near-white background cells too")
    image.add_argument("--margin", type=float, default=0.04)
    image.add_argument("--max-strokes", type=int, default=6000)
    image.add_argument("--per-frame", type=int, default=24)

    color = sub.add_parser("color", help="set the active color")
    color.add_argument("value", help="#RRGGBB, RRGGBB or r,g,b")
    brush = sub.add_parser("brush", help="set brush size")
    brush.add_argument("size", type=int)
    tool = sub.add_parser("tool", help="activate a drawing tool")
    tool.add_argument("name", help="Brush, Eraser, Dropper, Fill, Shape, MultiEraser, TipEx")

    clear = sub.add_parser("clear", help="clear the canvas")
    clear.add_argument("--force", action="store_true")
    sub.add_parser("undo")
    sub.add_parser("redo")
    sub.add_parser("submit", help="submit the current drawing (in-game rounds)")

    sub.add_parser("round", help="show round timers and remaining seconds")
    sub.add_parser("buttons", help="list in-game UI buttons (debug/automation)")
    click = sub.add_parser("click", help="click an in-game UI button by label")
    click.add_argument("text")
    click.add_argument("--index", type=int, default=0)

    sub.add_parser("ui", help="open the bridge web console")
    watch = sub.add_parser("watch", help="poll and print state changes")
    watch.add_argument("--interval", type=float, default=2.0)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    client = BridgeClient(args.url, args.token)
    try:
        if args.command == "status":
            print_json(client.state())
        elif args.command == "lines":
            result = client.lines(args.limit)
            if args.out:
                Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"saved {result.get('count', 0)} lines to {args.out}")
            else:
                print_json(result)
        elif args.command == "draw-json":
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            result = client.draw_strokes(data, clear=args.clear, force=args.force)
            print_json(result)
        elif args.command == "draw-image":
            result = client.draw_image(args.file, cells=args.cells, levels=args.levels,
                                       clear=not args.no_clear, force=args.force,
                                       skip_background=not args.keep_bg, margin=args.margin,
                                       max_strokes=args.max_strokes, per_frame=args.per_frame)
            print_json(result)
        elif args.command == "color":
            print_json(client.color(args.value))
        elif args.command == "brush":
            print_json(client.brush(args.size))
        elif args.command == "tool":
            print_json(client.tool(args.name))
        elif args.command == "clear":
            print_json(client.clear(args.force))
        elif args.command == "undo":
            print_json(client.undo())
        elif args.command == "redo":
            print_json(client.redo())
        elif args.command == "submit":
            print_json(client.submit())
        elif args.command == "round":
            print_json(client.round())
        elif args.command == "buttons":
            print_json(client.buttons())
        elif args.command == "click":
            print_json(client.click(args.text, index=args.index))
        elif args.command == "ui":
            webbrowser.open(client.base_url + "/")
            print(client.base_url + "/")
        elif args.command == "watch":
            return command_watch(client, args.interval)
        return 0
    except BridgeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
