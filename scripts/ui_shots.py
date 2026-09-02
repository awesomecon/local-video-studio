#!/usr/bin/env python3
"""Headless UI inspection for the Local Video Studio frontend.

Drives the real app in headless Chromium over the Chrome DevTools Protocol,
captures a PNG of every hash route, and reports console errors and page
exceptions per page. Requires a running backend (this script never starts or
stops servers) and the ``websocket-client`` package (already in the ``dev``
extra).

Usage:
    python3 scripts/ui_shots.py                       # all pages, newest project
    python3 scripts/ui_shots.py --project <uuid>      # explicit project
    python3 scripts/ui_shots.py --only dashboard,thumbnails
    python3 scripts/ui_shots.py --full                # full-page screenshots
    python3 scripts/ui_shots.py --base http://127.0.0.1:8009 --out /tmp/lvs-shots

Output: one PNG per route in --out plus a per-page console report on stdout.
Exit code 1 when a page throws an uncaught exception (resource 404s are
reported as warnings only).

Sandbox notes (why the odd chrome invocation): the Ubuntu chromium snap
wrapper needs a writable /run/user/<uid>, which sandboxes deny. This script
therefore prefers the snap's real binary and uses a throw-away profile under
/tmp. Override with LVS_CHROME=/path/to/chrome when needed.
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover - environment guard
    print("error: websocket-client is required (pip install websocket-client)", file=sys.stderr)
    sys.exit(2)

DEFAULT_BASE = "http://127.0.0.1:8009"
PROJECT_STORAGE_KEY = "lvs-current-project"  # keep in sync with frontend/js/state.js

ROUTES = [
    ("dashboard", "#/"),
    ("new", "#/new"),
    ("project", "#/project"),
    ("script", "#/script"),
    ("storyboard", "#/storyboard"),
    ("thumbnails", "#/thumbnails"),
    ("voice", "#/voice"),
    ("music", "#/music"),
    ("captions", "#/captions"),
    ("editorial", "#/editorial"),
    ("timeline", "#/timeline"),
    ("export", "#/export"),
    ("jobs", "#/jobs"),
    ("settings", "#/settings"),
    ("models", "#/models"),
    ("scene", None),  # filled from the selected project's first scene
]


def find_chrome() -> str:
    """Locate a usable headless-capable Chrome/Chromium binary."""
    candidates = [
        os.environ.get("LVS_CHROME"),
        "/snap/chromium/current/usr/lib/chromium-browser/chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    print("error: no chrome binary found (set LVS_CHROME)", file=sys.stderr)
    sys.exit(2)


def http_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def pick_project(base: str, wanted: str | None) -> str | None:
    if wanted:
        if wanted.lower() == "none":
            return None
        return wanted
    try:
        projects = http_json(f"{base}/api/projects").get("projects", [])
    except Exception as err:
        print(f"error: cannot list projects at {base} ({err}); is the backend running?", file=sys.stderr)
        print("start it with: python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009", file=sys.stderr)
        sys.exit(2)
    return projects[0]["id"] if projects else None


def pick_scene(base: str, project_id: str | None) -> str | None:
    if not project_id:
        return None
    try:
        snap = http_json(f"{base}/api/projects/{project_id}", timeout=15)
    except Exception:
        return None
    scenes = snap.get("scenes") or []
    return scenes[0]["id"] if scenes else None


class CDP:
    """Minimal CDP client over one page target."""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.console: list[str] = []
        self._ids = itertools.count(1)

    def cmd(self, method: str, params: dict | None = None, timeout: float = 60):
        mid = next(self._ids)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.on_event(msg)
        raise TimeoutError(method)

    def on_event(self, msg: dict) -> None:
        method = msg.get("method", "")
        if method == "Runtime.consoleAPICalled":
            p = msg["params"]
            text = " ".join(
                str(arg.get("value", arg.get("description", "?"))) for arg in p.get("args", [])
            )
            self.console.append(f"[console.{p['type']}] {text[:300]}")
        elif method == "Runtime.exceptionThrown":
            detail = msg["params"]["exceptionDetails"]
            description = detail.get("exception", {}).get("description", "")
            self.console.append(f"[EXCEPTION] {detail.get('text', '')} {description[:300]}")
        elif method == "Log.entryAdded":
            entry = msg["params"]["entry"]
            if entry.get("level") in ("error", "warning"):
                self.console.append(f"[log.{entry['level']}] {entry.get('text', '')[:300]}")

    def pump(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            self.ws.settimeout(remaining)
            try:
                self.on_event(json.loads(self.ws.recv()))
            except websocket.WebSocketTimeoutException:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE, help="backend origin (default %(default)s)")
    parser.add_argument("--out", default="/tmp/lvs-shots", help="screenshot output directory")
    parser.add_argument("--project", default=None, help="project id, or 'none' to skip selection (default: newest)")
    parser.add_argument("--only", default=None, help="comma-separated route names to capture")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--settle", type=float, default=4.0, help="seconds to wait per page (default %(default)s)")
    parser.add_argument("--full", action="store_true", help="capture full page instead of the viewport")
    parser.add_argument("--keep-profile", action="store_true", help="keep the throw-away chrome profile")
    args = parser.parse_args()

    project_id = pick_project(args.base, args.project)
    scene_id = pick_scene(args.base, project_id)

    routes = list(ROUTES)
    if scene_id:
        routes[-1] = ("scene", f"#/scene/{scene_id}")
    else:
        routes = routes[:-1]
    if args.only:
        wanted = {name.strip() for name in args.only.split(",")}
        missing = wanted - {name for name, _ in routes}
        if missing:
            print(f"error: unknown routes: {', '.join(sorted(missing))}", file=sys.stderr)
            sys.exit(2)
        routes = [(name, hash_) for name, hash_ in routes if name in wanted]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="lvs-ui-shots-")
    port = 9333
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--hide-scrollbars",
            f"--window-size={args.width},{args.height}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        env=dict(os.environ, HOME=profile),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    try:
        ws_url = None
        for _ in range(60):
            try:
                tabs = http_json(f"http://127.0.0.1:{port}/json", timeout=2)
                page = next(t for t in tabs if t["type"] == "page")
                ws_url = page["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.3)
        if not ws_url:
            raise RuntimeError("chrome did not expose a debugging port")

        cdp = CDP(ws_url)
        cdp.cmd("Runtime.enable")
        cdp.cmd("Log.enable")
        cdp.cmd("Page.enable")
        cdp.cmd("Emulation.setDeviceMetricsOverride", {
            "width": args.width, "height": args.height, "deviceScaleFactor": 1, "mobile": False,
        })

        # Seed the project selection the same way the app persists it, then let
        # every route render against that project.
        cdp.cmd("Page.navigate", {"url": f"{args.base}/index.html"})
        cdp.pump(3)
        if project_id:
            cdp.cmd("Runtime.evaluate", {
                "expression": f"sessionStorage.setItem({PROJECT_STORAGE_KEY!r}, {project_id!r})",
            })

        print(f"project: {project_id or '(none)'}  scene: {scene_id or '(none)'}  out: {out_dir}")
        for name, hash_ in routes:
            cdp.console.clear()
            cdp.cmd("Page.navigate", {"url": f"{args.base}/{hash_}"})
            cdp.pump(args.settle)
            params: dict = {"format": "png"}
            if args.full:
                metrics = cdp.cmd("Runtime.evaluate", {
                    "expression": "JSON.stringify({w: document.documentElement.scrollWidth,"
                                  " h: document.documentElement.scrollHeight})",
                    "returnByValue": True,
                })
                size = json.loads(metrics["result"]["value"])
                cdp.cmd("Emulation.setDeviceMetricsOverride", {
                    "width": min(size["w"], 4000), "height": min(size["h"], 12000),
                    "deviceScaleFactor": 1, "mobile": False,
                })
            shot = cdp.cmd("Page.captureScreenshot", params)
            if args.full:
                cdp.cmd("Emulation.setDeviceMetricsOverride", {
                    "width": args.width, "height": args.height, "deviceScaleFactor": 1, "mobile": False,
                })
            png_path = out_dir / f"{name}.png"
            png_path.write_bytes(base64.b64decode(shot["data"]))

            errors = [line for line in cdp.console
                      if "EXCEPTION" in line or "console.error" in line or "log.error" in line]
            warnings = [line for line in cdp.console if line not in errors]
            status = "ok" if not errors else "FAIL"
            print(f"=== {name} ({hash_}) {status} -> {png_path}")
            for line in errors:
                print(f"  ERR: {line[:400]}")
                if "EXCEPTION" in line:
                    failures.append(f"{name}: {line[:300]}")
            for line in warnings[:8]:
                print(f"  warn: {line[:300]}")
            sys.stdout.flush()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep_profile:
            shutil.rmtree(profile, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} page(s) threw exceptions:")
        for line in failures:
            print(f"  - {line}")
        sys.exit(1)
    print("\nall captured pages ran without exceptions")


if __name__ == "__main__":
    main()
