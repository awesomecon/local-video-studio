"""Platform-mocked tests for optional media and browser executables."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import backend.rendering.binaries as binaries_module
from backend.rendering.binaries import discover_binaries, discover_chromium


def _successful_version_probe(
    calls: list[list[str]],
):
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    return run


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    path.chmod(0o755)
    return path


def test_usable_executable_handles_spaces_and_preserves_dispatch_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _executable(tmp_path / "real launcher")
    dispatcher = tmp_path / "Browser Tools" / "chrome shim"
    dispatcher.parent.mkdir()
    dispatcher.symlink_to(target)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        _successful_version_probe(calls),
    )

    found = binaries_module._usable_executable(
        dispatcher,
        version_args=("--version",),
        platform_name="Linux",
    )

    assert found == dispatcher.absolute()
    assert found != dispatcher.resolve()
    # A version probe must not initialize a browser: bare `--version` only,
    # never a headed stack with a profile directory.
    assert calls == [[str(dispatcher), "--version"]]


def test_usable_executable_rejects_failed_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _executable(tmp_path / "ffmpeg")
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1),
    )

    assert binaries_module._usable_executable(
        candidate, platform_name="Linux",
    ) is None


@pytest.mark.parametrize(
    ("platform_name", "executable", "environment", "expected"),
    (
        (
            "Windows",
            "ffmpeg",
            {"ProgramFiles": "C:/Program Files"},
            Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        ),
        (
            "Darwin",
            "ffprobe",
            {},
            Path("/opt/homebrew/bin/ffprobe"),
        ),
        (
            "Linux",
            "chromium",
            {},
            Path("/snap/chromium/current/usr/lib/chromium-browser/chrome"),
        ),
    ),
)
def test_platform_install_paths_are_selected_from_mocked_os(
    platform_name: str,
    executable: str,
    environment: dict[str, str],
    expected: Path,
) -> None:
    candidates = binaries_module._platform_install_paths(
        executable,
        platform_name=platform_name,
        environ=environment,
    )

    assert expected in candidates


def test_chromium_override_precedes_path_and_install_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = _executable(tmp_path / "Chrome Override" / "chrome")
    path_browser = _executable(tmp_path / "on-path" / "chromium")
    installed = _executable(tmp_path / "installed" / "chrome")
    calls: list[list[str]] = []
    monkeypatch.setattr(binaries_module.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LVS_CHROME", str(override))
    monkeypatch.setattr(
        binaries_module.shutil,
        "which",
        lambda name: str(path_browser),
    )
    monkeypatch.setattr(
        binaries_module,
        "_platform_install_paths",
        lambda *args, **kwargs: (installed,),
    )
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        _successful_version_probe(calls),
    )

    assert discover_chromium() == override.absolute()
    assert calls == [[str(override), "--version"]]


def test_chromium_path_precedes_linux_install_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_browser = _executable(tmp_path / "path browser" / "chromium")
    installed = _executable(tmp_path / "installed browser" / "chrome")
    monkeypatch.setattr(binaries_module.platform, "system", lambda: "Linux")
    monkeypatch.delenv("LVS_CHROME", raising=False)
    monkeypatch.setattr(
        binaries_module.shutil,
        "which",
        lambda name: str(path_browser) if name == "chromium" else None,
    )
    monkeypatch.setattr(
        binaries_module,
        "_platform_install_paths",
        lambda *args, **kwargs: (installed,),
    )
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )

    assert discover_chromium() == path_browser.absolute()


def test_ffmpeg_override_keeps_sibling_ffprobe_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffmpeg = _executable(tmp_path / "FFmpeg Tools" / "ffmpeg.exe")
    ffprobe = _executable(ffmpeg.with_name("ffprobe.exe"))
    monkeypatch.setattr(binaries_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )

    discovered = discover_binaries(ffmpeg_override=ffmpeg)

    assert discovered.ffmpeg == ffmpeg.absolute()
    assert discovered.ffprobe == ffprobe.absolute()
    assert discovered.source == "override"


def test_ffmpeg_path_precedes_known_install_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_ffmpeg = _executable(tmp_path / "path tools" / "ffmpeg")
    installed_ffmpeg = _executable(tmp_path / "installed tools" / "ffmpeg")
    monkeypatch.setattr(binaries_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        binaries_module.shutil,
        "which",
        lambda name: str(path_ffmpeg) if name == "ffmpeg" else None,
    )
    monkeypatch.setattr(
        binaries_module,
        "_platform_install_paths",
        lambda name, **kwargs: (installed_ffmpeg,) if name == "ffmpeg" else (),
    )
    monkeypatch.setattr(
        binaries_module,
        "_bundled_ffmpeg",
        lambda: pytest.fail("PATH FFmpeg must finish discovery"),
    )
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )

    discovered = discover_binaries()

    assert discovered.ffmpeg == path_ffmpeg.absolute()
    assert discovered.source == "system"


def test_ffmpeg_falls_back_to_windows_install_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_ffmpeg = _executable(tmp_path / "Program Files" / "ffmpeg.exe")
    monkeypatch.setattr(binaries_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(binaries_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        binaries_module,
        "_platform_install_paths",
        lambda name, **kwargs: (installed_ffmpeg,) if name == "ffmpeg" else (),
    )
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )

    discovered = discover_binaries()

    assert discovered.ffmpeg == installed_ffmpeg.absolute()
    assert discovered.source == "system"


def test_ffprobe_remains_available_when_ffmpeg_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffprobe = _executable(tmp_path / "local tools" / "ffprobe")
    monkeypatch.setattr(binaries_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(binaries_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        binaries_module,
        "_platform_install_paths",
        lambda name, **kwargs: (ffprobe,) if name == "ffprobe" else (),
    )
    monkeypatch.setattr(binaries_module, "_bundled_ffmpeg", lambda: None)
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )

    discovered = discover_binaries()

    assert discovered.ffmpeg is None
    assert discovered.ffprobe == ffprobe.absolute()
    assert discovered.available is False
    assert discovered.source == "unavailable"


def test_windows_does_not_require_posix_executable_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "Program Files" / "chrome.exe"
    executable.parent.mkdir()
    executable.touch(mode=0o600)
    monkeypatch.setattr(
        binaries_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )
    monkeypatch.setattr(
        binaries_module.os,
        "access",
        lambda path, mode: pytest.fail("Windows must not check POSIX execute bits"),
    )

    assert binaries_module._usable_executable(
        executable,
        platform_name="Windows",
    ) == executable.absolute()
