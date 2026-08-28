"""Content-addressed cache for completed local generation artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_LAYOUT_VERSION = "v1"
_ARTIFACT_NAME = "artifact.bin"
_META_NAME = "meta.json"
_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]+")
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CachedGeneration:
    path: Path
    metadata: dict[str, Any]


class GenerationCache:
    """Disk-backed, content-addressed artifact store shared across projects.

    Entries are keyed by a SHA-256 digest of every generation input that can
    change the output. Lookup verifies the stored artifact against its recorded
    size and digest, so partial or corrupted entries degrade to a miss instead
    of publishing broken media. Every method is best-effort: cache failures are
    logged and never interrupt the generation being cached.
    """

    def __init__(self, root: str | Path, *, max_bytes: int | None = None) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    @staticmethod
    def key_hash(payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def entry_dir(self, backend_name: str, key: str) -> Path:
        if not _HEX.fullmatch(key):
            raise ValueError("cache key must be a sha256 hex digest")
        segment = _SEGMENT.sub("-", backend_name).strip("-.") or "backend"
        return self.root / _LAYOUT_VERSION / segment / key

    def lookup(self, backend_name: str, key: str) -> CachedGeneration | None:
        try:
            entry = self.entry_dir(backend_name, key)
            artifact = entry / _ARTIFACT_NAME
            meta_path = entry / _META_NAME
            if not artifact.is_file() or not meta_path.is_file():
                return None
            stat = artifact.stat()
            if stat.st_size == 0:
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                return None
            digest = meta.get("artifact_sha256")
            size = meta.get("artifact_bytes")
            if not isinstance(digest, str) or self._digest(artifact) != digest:
                return None
            if isinstance(size, int) and size >= 0 and stat.st_size != size:
                return None
            self._touch(meta_path, meta)
            metadata = meta.get("metadata")
            return CachedGeneration(
                path=artifact, metadata=dict(metadata) if isinstance(metadata, dict) else {},
            )
        except (OSError, ValueError):
            return None

    def store(
        self,
        backend_name: str,
        key: str,
        source: Path | bytes,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        try:
            entry = self.entry_dir(backend_name, key)
            entry.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact.", dir=entry)
            temporary = Path(temporary_name)
            try:
                if isinstance(source, (bytes, bytearray, memoryview)):
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(bytes(source))
                        handle.flush()
                        os.fsync(handle.fileno())
                else:
                    with os.fdopen(descriptor, "wb") as handle:
                        pass
                    shutil.copyfile(Path(source), temporary)
                    file_descriptor = os.open(temporary, os.O_RDONLY)
                    try:
                        os.fsync(file_descriptor)
                    finally:
                        os.close(file_descriptor)
                digest = self._digest(temporary)
                size = temporary.stat().st_size
                os.replace(temporary, entry / _ARTIFACT_NAME)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            self._write_meta(entry / _META_NAME, {
                "created_at": _utc_now(),
                "last_access_at": _utc_now(),
                "artifact_sha256": digest,
                "artifact_bytes": size,
                "metadata": self._jsonable(dict(metadata or {})),
            })
            self._evict()
            return True
        except (OSError, ValueError) as exc:
            logger.warning("generation cache store failed for %s: %s", backend_name, exc)
            return False

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _jsonable(value: Any) -> Any:
        try:
            json.dumps(value, default=str)
        except (TypeError, ValueError):
            return str(value)
        return value

    @classmethod
    def _write_meta(cls, path: Path, payload: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".meta.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _touch(self, meta_path: Path, meta: dict[str, Any]) -> None:
        try:
            meta["last_access_at"] = _utc_now()
            self._write_meta(meta_path, meta)
        except OSError as exc:
            logger.debug("generation cache access-time refresh failed: %s", exc)

    def _evict(self) -> None:
        if self.max_bytes is None or not self.root.is_dir():
            return
        base = self.root / _LAYOUT_VERSION
        entries: list[tuple[str, int, Path]] = []
        total = 0
        try:
            for backend_dir in sorted(base.iterdir()):
                if not backend_dir.is_dir():
                    continue
                for entry in sorted(backend_dir.iterdir()):
                    artifact = entry / _ARTIFACT_NAME
                    if not entry.is_dir() or not artifact.is_file():
                        continue
                    size = artifact.stat().st_size
                    total += size
                    entries.append((self._access_stamp(entry, artifact), size, entry))
        except OSError as exc:
            logger.debug("generation cache eviction scan failed: %s", exc)
            return
        for stamp, size, entry in sorted(entries):
            if total <= self.max_bytes:
                return
            shutil.rmtree(entry, ignore_errors=True)
            total -= size

    @staticmethod
    def _access_stamp(entry: Path, artifact: Path) -> str:
        meta_path = entry / _META_NAME
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stamp = meta.get("last_access_at") or meta.get("created_at")
            if isinstance(stamp, str):
                datetime.fromisoformat(stamp)
                return stamp
        except (OSError, ValueError):
            pass
        return datetime.fromtimestamp(artifact.stat().st_mtime, tz=timezone.utc).isoformat()
