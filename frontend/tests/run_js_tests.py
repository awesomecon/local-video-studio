#!/usr/bin/env python3
"""Focused frontend logic tests for the multi-shot groundwork.

Runs `tests/js/frontend.test.js` (pure ES modules: shots.js + router.js)
through the machine's Chromium in headless mode, then asserts on the
reported results. No backend and no network are involved.

Usage (standalone script, not a pytest module):

    python3 frontend/tests/run_js_tests.py

The browser binary resolves from $LVS_CHROME, then the Ubuntu snap path.
Exit codes: 0 = all tests passed (or browser unavailable -> SKIP),
1 = one or more tests failed, or the harness did not report results.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
FRONTEND = TESTS_DIR.parent
HARNESS_NAME = ".run-js-tests-harness.html"
MARKER = "LVSTESTS "

HARNESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8" /><title>lvs-js-tests</title></head>
<body>
  <div id="app"></div>
  <pre id="out">PENDING</pre>
  <script type="module" src="./js/frontend.test.js"></script>
</body>
</html>
"""


def find_chrome() -> str | None:
    candidate = os.environ.get("LVS_CHROME")
    if candidate and Path(candidate).exists():
        return candidate
    snap = "/snap/chromium/current/usr/lib/chromium-browser/chrome"
    if Path(snap).exists():
        return snap
    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def main() -> int:
    test_js = TESTS_DIR / "js" / "frontend.test.js"
    if not test_js.exists():
        print(f"FAIL: missing test module {test_js}")
        return 1

    chrome = find_chrome()
    if chrome is None:
        print("SKIP: no Chromium/Chrome binary found "
              "(set LVS_CHROME to run the frontend logic tests)")
        return 0

    harness = TESTS_DIR / HARNESS_NAME
    harness.write_text(HARNESS_HTML, encoding="utf-8")
    profile = tempfile.mkdtemp(prefix="lvs-jstests-profile-")
    env_home = tempfile.mkdtemp(prefix="lvs-jstests-home-")
    try:
        cmd = [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--allow-file-access-from-files",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=4000",
            "--timeout=15000",
            "--dump-dom",
            f"file://{harness}",
        ]
        env = dict(os.environ, HOME=env_home)
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90, env=env,
        )
        dom = html.unescape(proc.stdout)

        match = re.search(re.escape(MARKER) + r"(\{.*\})\s*</pre>", dom, re.S)
        if not match:
            tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
            print("FAIL: harness produced no results block "
                  "(module graph failed to load?)")
            pending = "PENDING" in dom
            if pending:
                print("The test module did not finish executing.")
            if tail:
                print("--- chromium stderr tail ---")
                print(tail)
            return 1

        payload = json.loads(match.group(1))
        failures = 0
        for name, ok, message in payload["results"]:
            status = "PASS" if ok else "FAIL"
            print(f"{status}: {name}")
            if not ok:
                failures += 1
                if message:
                    print(f"      {message}")
        total = payload.get("total", len(payload["results"]))
        passed = payload.get("passed", total - failures)
        print(f"{'OK' if failures == 0 else 'FAIL'}: "
              f"{passed}/{total} frontend logic tests passed")
        return 1 if failures else 0
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(env_home, ignore_errors=True)
        harness.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
