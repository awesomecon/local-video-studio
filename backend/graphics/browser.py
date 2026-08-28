"""Small, shell-free Chromium invocation used by Graphic Screen rendering."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REAL_SNAP_CHROMIUM = Path("/snap/chromium/current/usr/lib/chromium-browser/chrome")


def discover_chromium() -> Path | None:
    """Prefer the real snap binary over the failing snap wrapper.

    The ``chromium`` wrapper on this machine cannot create its DBus transient
    scope from service contexts; the packaged Chrome binary it wraps runs
    fine with a throw-away profile and a ``HOME`` override (same approach as
    scripts/ui_shots.py).
    """
    override = os.environ.get("LVS_CHROME")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    if REAL_SNAP_CHROMIUM.is_file():
        return REAL_SNAP_CHROMIUM
    for candidate in ("chromium", "chromium-browser"):
        found = shutil.which(candidate)
        if found:
            return Path(found)
    for candidate in (Path("/snap/bin/chromium"), Path("/usr/bin/chromium-browser")):
        if candidate.is_file():
            return candidate
    return None


def chromium_argv(
    executable: Path, *, document: Path, output: Path, profile: Path, width: int, height: int,
    transparent: bool = False,
) -> list[str]:
    """Return a fixed trusted argv. Callers must use ``shell=False``."""
    arguments = [
        str(executable),
        "--headless=new",
        "--disable-gpu", "--disable-extensions",
        "--disable-background-networking", "--disable-component-update", "--disable-sync",
        "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
        "--force-device-scale-factor=1", f"--window-size={width},{height}",
        f"--user-data-dir={profile}", f"--screenshot={output}",
    ]
    if os.environ.get("LVS_CHROMIUM_NO_SANDBOX", "").strip().lower() in {"1", "true", "yes"}:
        # Explicit operator opt-in for hosts whose Chromium setuid/namespace
        # helper cannot run at all (some CI containers). The default argv
        # always keeps the Chromium sandbox enabled.
        arguments.insert(2, "--no-sandbox")
    if transparent:
        # Transparent page background for overlay PNGs; the document must not
        # paint an opaque body background. The compositor flags make the
        # first-paint race far less likely, though callers must still treat
        # an all-transparent capture as invalid and retry.
        arguments[2:2] = [
            "--default-background-color=00000000",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=2000",
        ]
    arguments.append(document.resolve().as_uri())
    return arguments
