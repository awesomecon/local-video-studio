"""Project-relative path syntax independent of the operating system.

These helpers do not migrate files or guess how to rebase absolute legacy
paths. Both slash styles mean directory separators in portable path fields.
"""

from __future__ import annotations

from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

_MAX_FILENAME_LENGTH = 100
#: Device names Windows refuses to create, with or without an extension.
WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}
_WINDOWS_RESERVED = WINDOWS_RESERVED_NAMES
#: Characters Windows refuses in any filename; sanitized, never passed through.
_WINDOWS_INVALID_CHARS = '<>|?*"'


def _preview(value: object) -> str:
    text = value.as_posix() if isinstance(value, PurePath) else str(value)
    return text if len(text) <= 120 else text[:117] + "..."


def portable_relative_path(value: str | PurePath) -> str:
    """Return a relative path in canonical forward-slash form.

    Inspect Windows syntax even on POSIX: drive-relative paths, UNC shares,
    rooted paths and mixed-separator traversal must never become local names.
    Absolute legacy paths require explicit repair rather than basename guesses:
    re-import the file into the project or fix the stored path, because the
    original machine location is meaningless on any other host.
    """
    raw = value.as_posix() if isinstance(value, PurePath) else value
    if not isinstance(raw, str):
        raise ValueError(f"path must be a string or path, got {_preview(value)!r}")
    windows = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    if windows.drive or windows.root or normalized.startswith("/"):
        raise ValueError(
            f"path must be project-relative for portability, got {_preview(raw)!r}; "
            "absolute machine paths cannot transfer: re-import the file into the project"
        )
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError(
            f"path cannot escape the project directory, got {_preview(raw)!r}"
        )
    if any(ord(character) < 32 for character in raw) or ":" in raw:
        raise ValueError(f"path contains unsupported characters, got {_preview(raw)!r}")
    path = PurePosixPath(normalized)
    if not path.parts:
        raise ValueError(f"path must identify a file or directory inside the project, got {_preview(raw)!r}")
    return path.as_posix()


def resolve_project_path(root: Path, value: str | PurePath) -> Path:
    """Resolve a portable path, rejecting traversal and symlink escapes.

    Missing paths are allowed for output destinations. Resolving is read-only;
    callers remain responsible for creating or opening the returned path.
    """
    relative = portable_relative_path(value)
    project_root = root.resolve()
    path = project_root.joinpath(*PurePosixPath(relative).parts).resolve()
    if not path.is_relative_to(project_root) or path == project_root:
        raise ValueError(
            f"path must stay inside the project directory, got {_preview(value)!r}"
        )
    return path


def resolve_asset_path(root: Path, value: str | PurePath) -> Path:
    """Resolve a stored media reference at a filesystem boundary.

    This is the only sanctioned conversion from a persisted project-relative
    reference to a native path: validation, canonicalization, and symlink
    containment are enforced together so resolution sites cannot drift.
    """
    try:
        return resolve_project_path(root, value)
    except ValueError as exc:
        raise ValueError(
            f"cannot resolve stored media path {_preview(value)!r}: {exc}"
        ) from exc


def safe_portable_filename(name: str | PurePath, *, max_length: int = _MAX_FILENAME_LENGTH) -> str:
    """Return a single path segment safe to create on Windows and POSIX.

    Windows strips trailing dots/spaces (so ``"take "`` and ``"take"`` would
    collide) and refuses reserved device names with any extension (``"con.png"``
    cannot be created, and neither can ``"con .png"`` because the stem is
    evaluated after stripping). Characters Windows forbids outright
    (``<>|?*"``) are replaced. Existing stored names are never rewritten by
    this helper; apply it only when generating new files or directories.
    """
    raw = name.as_posix() if isinstance(name, PurePath) else name
    if not isinstance(raw, str):
        raise ValueError(f"filename must be a string or path, got {_preview(name)!r}")
    if "/" in raw or "\\" in raw or ":" in raw or any(ord(c) < 32 for c in raw):
        raise ValueError(f"filename must be a single segment, got {_preview(raw)!r}")
    cleaned = "".join("_" if c in _WINDOWS_INVALID_CHARS else c for c in raw.strip())
    cleaned = cleaned.rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"filename has no usable characters, got {_preview(raw)!r}")

    def _reserve(value: str) -> str:
        # Windows evaluates the stem after stripping trailing dots/spaces,
        # so "con .png" and "con." are as reserved as "con.png".
        if value.split(".")[0].strip().upper() in _WINDOWS_RESERVED:
            return f"_{value}"
        return value

    cleaned = _reserve(cleaned)
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    # Enforce the budget in bytes as well: transfer targets and filesystems
    # may count UTF-8 bytes, and non-ASCII characters occupy several. Slicing
    # a str never splits a code point, so shrinking by characters is safe.
    while len(cleaned.encode("utf-8")) > max_length:
        cleaned = cleaned[:-1]
    cleaned = _reserve(cleaned.rstrip(". "))
    if not cleaned:
        raise ValueError(f"filename has no usable characters, got {_preview(raw)!r}")
    return cleaned
