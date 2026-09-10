"""Project-relative path syntax independent of the operating system.

These helpers do not migrate files or guess how to rebase absolute legacy
paths. Both slash styles mean directory separators in portable path fields.
"""

from __future__ import annotations

from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath


def portable_relative_path(value: str | PurePath) -> str:
    """Return a relative path in canonical forward-slash form.

    Inspect Windows syntax even on POSIX: drive-relative paths, UNC shares,
    rooted paths and mixed-separator traversal must never become local names.
    Absolute legacy paths require explicit repair rather than basename guesses.
    """
    raw = value.as_posix() if isinstance(value, PurePath) else value
    if not isinstance(raw, str):
        raise ValueError("path must be a string or path")
    windows = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    if windows.drive or windows.root or normalized.startswith("/"):
        raise ValueError("path must be project-relative for portability")
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError("path cannot escape the project directory")
    if any(ord(character) < 32 for character in raw) or ":" in raw:
        raise ValueError("path contains unsupported characters")
    path = PurePosixPath(normalized)
    if not path.parts:
        raise ValueError("path must identify a file or directory inside the project")
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
        raise ValueError("path must stay inside the project directory")
    return path
