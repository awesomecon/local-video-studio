"""CI must not quietly skip coverage because a required tool disappeared."""
import os

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get("LVS_REQUIRE_CORE_TOOLS") != "1":
        return
    from backend.graphics.browser import discover_chromium
    from backend.rendering.binaries import discover_binaries

    binaries = discover_binaries()
    missing = [
        name for name, present in (
            ("FFmpeg", binaries.ffmpeg),
            ("ffprobe", binaries.ffprobe),
            ("Chromium", discover_chromium()),
        )
        if not present
    ]
    if missing:
        raise pytest.UsageError(
            "CI requires usable FFmpeg, ffprobe and Chromium; missing: "
            + ", ".join(missing)
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("LVS_REQUIRE_CORE_TOOLS") == "1" and report.skipped:
        reason = str(report.longrepr).lower()
        if any(tool in reason for tool in ("ffmpeg", "ffprobe", "chromium")):
            report.outcome = "failed"
            report.longrepr = "Required core tool coverage attempted to skip in CI"
