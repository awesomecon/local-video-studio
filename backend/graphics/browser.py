"""Small, shell-free Chromium invocation used by Graphic Screen rendering."""

from __future__ import annotations

import os
from pathlib import Path
from backend.rendering.binaries import discover_chromium as _discover_chromium


def discover_chromium() -> Path | None:
    """Use the same browser discovery policy as tests and UI inspection."""
    return _discover_chromium()


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
