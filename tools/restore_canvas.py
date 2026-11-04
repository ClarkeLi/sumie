#!/usr/bin/env python3
"""Restore a saved Draw & Guess canvas from a /api/v1/lines JSON export.

The web studio's "导出笔画" button saves the current canvas. This command
replays such a file through POST /api/v1/strokes, which is useful before
restarting the game or after switching from the Sketchbook.

    python3 tools/restore_canvas.py canvas.json
    python3 tools/restore_canvas.py canvas.json --url http://127.0.0.1:8765 --clear
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def extract_lines(data):
    """Accept an array, a bridge result, or a raw bridge response."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError("expected a JSON array or object")
    if isinstance(data.get("lines"), list):
        return data["lines"]
    result = data.get("result")
    if isinstance(result, dict) and isinstance(result.get("lines"), list):
        return result["lines"]
    raise ValueError("no 'lines' array in the file")


def to_strokes(lines):
    strokes = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        points = line.get("points")
        if not isinstance(points, list) or not points:
            continue
        strokes.append({
            "type": line.get("type", 0),
            "color": line.get("color", "#000000"),
            "size": line.get("size"),
            "points": points,
            "space": "normalized",
            "layer": line.get("layer"),
        })
    return strokes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path, help="JSON written by /api/v1/lines")
    parser.add_argument("--url", default=os.environ.get("DG_BRIDGE_URL",
                                                        "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("DG_BRIDGE_TOKEN", ""))
    parser.add_argument("--clear", action="store_true",
                        help="clear the canvas before restoring (default: keep it)")
    parser.add_argument("--no-force", action="store_true",
                        help="do not force when the game reports it cannot draw")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the stroke count without sending")
    args = parser.parse_args(argv)

    try:
        data = json.loads(args.file.read_text(encoding="utf-8"))
        lines = extract_lines(data)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    strokes = to_strokes(lines)
    if not strokes:
        print("error: the file contains no usable strokes", file=sys.stderr)
        return 2
    print(f"strokes: {len(strokes)}  clear={args.clear}  force={not args.no_force}")
    if args.dry_run:
        return 0

    body = json.dumps({"strokes": strokes, "clear": args.clear,
                       "force": not args.no_force}).encode("utf-8")
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/v1/strokes",
        data=body, method="POST",
        headers={"Content-Type": "application/json"})
    if args.token:
        request.add_header("X-Api-Key", args.token)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"error: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result = payload.get("result", payload) if isinstance(payload, dict) else payload
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
