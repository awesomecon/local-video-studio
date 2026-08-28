"""Deterministic content keys and sidecar manifests for the render cache.

Every cached artifact (shot composite, scene render) carries a manifest JSON
beside the media file. The manifest records the full input fingerprint that
produced the file: renderer version, workflow version, FFmpeg identity,
canvas, fps, frame budget, and the SHA-256 of every contributing asset plus
canonicalized settings payloads. A cache lookup recompute the fingerprint and
recomputes the fingerprint and compares it to the stored manifest, so a changed shot invalidates exactly its
own composite — and, transitively through the scene key, only its scene.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .binaries import FFmpegBinaries

RENDERER_VERSION = "lvs-media-render-3"
SHOT_NORMALIZATION_WORKFLOW = "shot-normalize-v2"
SCENE_ASSEMBLY_WORKFLOW = "scene-assemble-v2"

_IDENTITY_CACHE: dict[str, str] = {}


def canonical_json(value: Any) -> str:
    """Stable JSON text used for hashing and manifest comparison."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_key(parts: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of ``parts``."""
    return hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffmpeg_identity(binaries: FFmpegBinaries) -> str:
    """Stable identity of the exact FFmpeg executable used for a cache entry."""
    if binaries.ffmpeg is None:
        return "ffmpeg:unavailable"
    try:
        path = binaries.ffmpeg.resolve()
        cache_key = f"{path}|{path.stat().st_mtime_ns}"
    except OSError:
        return f"ffmpeg:{binaries.ffmpeg}:unreadable"
    cached = _IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [str(path), "-version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        if result.returncode != 0 or not first_line:
            raise OSError("ffmpeg -version produced no output")
    except (OSError, subprocess.TimeoutExpired, IndexError):
        identity = f"ffmpeg:{path.name}:unreadable"
    else:
        identity = content_key({"path": path.name, "version": first_line})
    _IDENTITY_CACHE[cache_key] = identity
    return identity


def fsync_file(path: str | Path) -> None:
    """Flush a finished file to stable storage before it is renamed into place."""
    with Path(path).open("rb") as stream:
        os.fsync(stream.fileno())


def write_manifest(path: str | Path, payload: dict[str, Any]) -> Path:
    """Atomically write ``manifest.json`` beside a cached media file."""
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.",
        suffix=".tmp",
        dir=str(manifest_path.parent),
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, manifest_path)
    finally:
        staged.unlink(missing_ok=True)
    return manifest_path


def load_manifest(path: str | Path) -> dict[str, Any] | None:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def manifest_is_current(path: str | Path, expected: dict[str, Any]) -> bool:
    """True when the stored manifest matches ``expected`` byte-for-byte."""
    stored = load_manifest(path)
    if stored is None:
        return False
    return canonical_json(stored) == canonical_json(expected)


def remove_stale_versions(cache_dir: str | Path, keep_key: str) -> None:
    """Drop sibling key directories of one cache scope except ``keep_key``."""
    scope = Path(cache_dir)
    if not scope.is_dir():
        return
    for child in scope.iterdir():
        if child.is_dir() and child.name != keep_key:
            for member in sorted(child.rglob("*"), reverse=True):
                if member.is_file():
                    member.unlink(missing_ok=True)
                elif member.is_dir():
                    member.rmdir()
            child.rmdir()


__all__ = [
    "RENDERER_VERSION",
    "SCENE_ASSEMBLY_WORKFLOW",
    "SHOT_NORMALIZATION_WORKFLOW",
    "canonical_json",
    "content_key",
    "ffmpeg_identity",
    "fsync_file",
    "load_manifest",
    "manifest_is_current",
    "remove_stale_versions",
    "sha256_file",
    "write_manifest",
]
