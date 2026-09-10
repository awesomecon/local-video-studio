"""Locate media and browser tools without modifying the host environment."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class FFmpegNotFoundError(RuntimeError):
    """Raised when no usable FFmpeg executable can be found."""


@dataclass(frozen=True, slots=True)
class FFmpegBinaries:
    ffmpeg: Path | None
    ffprobe: Path | None
    source: str

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None


def _usable_executable(
    value: str | os.PathLike[str] | None,
    *,
    version_args: Sequence[str] = ("-version",),
    platform_name: str | None = None,
) -> Path | None:
    """Return an absolute executable path when its version probe succeeds."""
    if not value:
        return None
    path = Path(value).expanduser()
    current_platform = platform_name or platform.system()
    if not path.is_file():
        return None
    if current_platform != "Windows" and not os.access(path, os.X_OK):
        return None
    try:
        with ExitStack() as stack:
            command = [str(path), *version_args]
            if version_args == ("--version",):
                profile = stack.enter_context(tempfile.TemporaryDirectory(prefix="lvs-browser-probe-"))
                command.extend(("--headless=new", "--no-first-run", "--disable-background-networking",
                                f"--user-data-dir={profile}"))
            result = subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=5,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Keep the path exactly as found: dispatchers such as /snap/bin/ffmpeg
    # choose their target from argv[0]. Resolving that symlink would invoke
    # the launcher itself and change the command's meaning.
    return Path(os.path.abspath(str(path))) if result.returncode == 0 else None


def _first_usable(
    candidates: Iterable[str | os.PathLike[str] | None],
    *,
    version_args: Sequence[str],
    platform_name: str,
) -> Path | None:
    for candidate in candidates:
        if version_args == ("-version",):
            executable = _usable_executable(candidate)
        else:
            executable = _usable_executable(
                candidate,
                version_args=version_args,
                platform_name=platform_name,
            )
        if executable is not None:
            return executable
    return None


def _from_path(
    names: Iterable[str],
    *,
    version_args: Sequence[str],
    platform_name: str,
) -> Path | None:
    return _first_usable(
        (shutil.which(name) for name in names),
        version_args=version_args,
        platform_name=platform_name,
    )


def _environment_path(
    environ: Mapping[str, str], key: str, *parts: str,
) -> Path | None:
    root = environ.get(key)
    return Path(root, *parts) if root else None


def _platform_install_paths(
    executable: str,
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return conventional install locations for a supported executable."""
    current_platform = platform_name or platform.system()
    environment = os.environ if environ is None else environ

    if executable in {"ffmpeg", "ffprobe"}:
        filename = f"{executable}.exe" if current_platform == "Windows" else executable
        if current_platform == "Windows":
            candidates = (
                _environment_path(environment, "ProgramFiles", "ffmpeg", "bin", filename),
                _environment_path(
                    environment, "ProgramFiles(x86)", "ffmpeg", "bin", filename,
                ),
                _environment_path(
                    environment,
                    "LOCALAPPDATA",
                    "Microsoft",
                    "WinGet",
                    "Links",
                    filename,
                ),
                _environment_path(
                    environment, "ProgramData", "chocolatey", "bin", filename,
                ),
                Path("C:/ffmpeg/bin", filename),
            )
        elif current_platform == "Darwin":
            candidates = tuple(
                Path(prefix, "bin", filename)
                for prefix in ("/opt/homebrew", "/usr/local", "/opt/local", "/usr")
            )
        else:
            candidates = tuple(
                Path(prefix, filename)
                for prefix in ("/usr/local/bin", "/usr/bin", "/snap/bin")
            )
        return tuple(candidate for candidate in candidates if candidate is not None)

    if executable != "chromium":
        raise ValueError(f"Unsupported executable: {executable}")

    if current_platform == "Windows":
        roots = (
            ("ProgramFiles", "Google", "Chrome", "Application", "chrome.exe"),
            ("ProgramFiles(x86)", "Google", "Chrome", "Application", "chrome.exe"),
            ("LOCALAPPDATA", "Google", "Chrome", "Application", "chrome.exe"),
            ("ProgramFiles", "Chromium", "Application", "chrome.exe"),
            ("LOCALAPPDATA", "Chromium", "Application", "chrome.exe"),
        )
        return tuple(
            candidate
            for key, *parts in roots
            if (candidate := _environment_path(environment, key, *parts)) is not None
        )
    if current_platform == "Darwin":
        candidates = (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            _environment_path(
                environment,
                "HOME",
                "Applications",
                "Google Chrome.app",
                "Contents",
                "MacOS",
                "Google Chrome",
            ),
            _environment_path(
                environment,
                "HOME",
                "Applications",
                "Chromium.app",
                "Contents",
                "MacOS",
                "Chromium",
            ),
        )
        return tuple(candidate for candidate in candidates if candidate is not None)
    return (
        Path("/snap/chromium/current/usr/lib/chromium-browser/chrome"),
        Path("/opt/google/chrome/chrome"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/snap/bin/chromium"),
    )


def _bundled_ffmpeg() -> Path | None:
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        return _usable_executable(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, OSError, RuntimeError):
        return None


def _is_snap_shim(path: Path | None) -> bool:
    """Return whether *path* dispatches through the confined Snap launcher."""
    if path is None:
        return False
    try:
        return path.resolve() == Path("/usr/bin/snap")
    except OSError:
        return False


def _sibling_ffprobe(ffmpeg: Path | None) -> Path | None:
    if ffmpeg is None:
        return None
    candidates = (ffmpeg.with_name("ffprobe"), ffmpeg.with_name("ffprobe.exe"))
    return _first_usable(
        candidates,
        version_args=("-version",),
        platform_name=platform.system(),
    )


def discover_chromium(
    *, chromium_override: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find a usable Chromium-family browser without downloading one.

    The explicit argument, or ``LVS_CHROME`` when it is omitted, takes
    precedence over PATH and conventional operating-system install paths.
    """
    current_platform = platform.system()
    override = (
        os.environ.get("LVS_CHROME")
        if chromium_override is None
        else chromium_override
    )
    explicit = _usable_executable(
        override,
        version_args=("--version",),
        platform_name=current_platform,
    )
    if explicit is not None:
        return explicit

    if current_platform == "Windows":
        path_names = ("chrome", "chromium", "chrome.exe", "chromium.exe")
    elif current_platform == "Darwin":
        path_names = ("google-chrome", "chromium", "chrome")
    else:
        path_names = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
    from_path = _from_path(
        path_names,
        version_args=("--version",),
        platform_name=current_platform,
    )
    if from_path is not None:
        if current_platform == "Linux" and _is_snap_shim(from_path):
            real_snap = _usable_executable(
                "/snap/chromium/current/usr/lib/chromium-browser/chrome",
                version_args=("--version",), platform_name=current_platform,
            )
            if real_snap is not None:
                return real_snap
        return from_path
    return _first_usable(
        _platform_install_paths("chromium", platform_name=current_platform),
        version_args=("--version",),
        platform_name=current_platform,
    )


def discover_binaries(
    *,
    ffmpeg_override: str | os.PathLike[str] | None = None,
    ffprobe_override: str | os.PathLike[str] | None = None,
) -> FFmpegBinaries:
    """Find FFmpeg and ffprobe independently without downloading either."""
    current_platform = platform.system()
    explicit_ffmpeg = _usable_executable(ffmpeg_override)
    explicit_ffprobe = _usable_executable(ffprobe_override)
    if explicit_ffmpeg:
        return FFmpegBinaries(
            explicit_ffmpeg,
            explicit_ffprobe or _sibling_ffprobe(explicit_ffmpeg),
            "override",
        )

    path_ffmpeg = _from_path(
        ("ffmpeg",), version_args=("-version",), platform_name=current_platform,
    )
    path_ffprobe = explicit_ffprobe or _from_path(
        ("ffprobe",), version_args=("-version",), platform_name=current_platform,
    )
    # Snap's FFmpeg can pass ``-version`` yet fail to access project paths
    # outside its confinement. Preserve the existing preference for an
    # unconfined imageio-ffmpeg binary, with the PATH shim as the fallback.
    snap_ffmpeg = path_ffmpeg if _is_snap_shim(path_ffmpeg) else None
    if path_ffmpeg is not None and snap_ffmpeg is None:
        return FFmpegBinaries(path_ffmpeg, path_ffprobe, "system")

    installed_ffprobe = path_ffprobe or _first_usable(
        _platform_install_paths("ffprobe", platform_name=current_platform),
        version_args=("-version",),
        platform_name=current_platform,
    )
    if snap_ffmpeg is not None:
        bundled = _bundled_ffmpeg()
        if bundled:
            return FFmpegBinaries(
                bundled,
                installed_ffprobe or _sibling_ffprobe(bundled),
                "imageio_ffmpeg",
            )
        return FFmpegBinaries(snap_ffmpeg, installed_ffprobe, "system")

    installed_ffmpeg = _first_usable(
        _platform_install_paths("ffmpeg", platform_name=current_platform),
        version_args=("-version",),
        platform_name=current_platform,
    )
    if installed_ffmpeg is not None and not _is_snap_shim(installed_ffmpeg):
        return FFmpegBinaries(installed_ffmpeg, installed_ffprobe, "system")
    if snap_ffmpeg is None and _is_snap_shim(installed_ffmpeg):
        snap_ffmpeg = installed_ffmpeg

    bundled = _bundled_ffmpeg()
    if bundled:
        return FFmpegBinaries(
            bundled,
            installed_ffprobe or _sibling_ffprobe(bundled),
            "imageio_ffmpeg",
        )
    if snap_ffmpeg:
        return FFmpegBinaries(snap_ffmpeg, installed_ffprobe, "system")
    return FFmpegBinaries(None, installed_ffprobe, "unavailable")


def require_ffmpeg(binaries: FFmpegBinaries | None = None) -> Path:
    discovered = binaries or discover_binaries()
    if discovered.ffmpeg is None:
        raise FFmpegNotFoundError(
            "FFmpeg was not found via an override, PATH, known install locations, "
            "or imageio-ffmpeg."
        )
    return discovered.ffmpeg
