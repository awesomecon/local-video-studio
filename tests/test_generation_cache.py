from __future__ import annotations

import json
from pathlib import Path

from backend.storage.generation_cache import GenerationCache


def test_store_and_lookup_roundtrip_with_bytes(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path)
    key = cache.key_hash({"prompt": "a lighthouse", "seed": 7})

    stored = cache.store("krea2_comfyui", key, b"generated-image", metadata={"model": "Krea 2 Turbo"})
    cached = cache.lookup("krea2_comfyui", key)

    assert stored is True
    assert cached is not None
    assert cached.path.read_bytes() == b"generated-image"
    assert cached.metadata["model"] == "Krea 2 Turbo"


def test_store_and_lookup_roundtrip_with_file_source(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path)
    source = tmp_path / "output.mp4"
    source.write_bytes(b"0" * 4096)
    key = cache.key_hash({"kind": "h3_video", "frames": 125})

    cache.store("comfyui", key, source)

    cached = cache.lookup("comfyui", key)
    assert cached is not None
    assert cached.path.read_bytes() == b"0" * 4096
    assert source.is_file()


def test_key_hash_is_order_insensitive_and_input_sensitive() -> None:
    first = GenerationCache.key_hash({"a": 1, "b": [1, 2]})
    second = GenerationCache.key_hash({"b": [1, 2], "a": 1})
    third = GenerationCache.key_hash({"a": 1, "b": [1, 3]})

    assert first == second
    assert first != third
    assert len(first) == 64


def test_lookup_returns_none_for_unknown_or_corrupt_entries(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path)
    key = cache.key_hash({"prompt": "x"})

    assert cache.lookup("krea2_comfyui", key) is None

    cache.store("krea2_comfyui", key, b"payload")
    entry = tmp_path / "v1" / "krea2_comfyui" / key

    (entry / "artifact.bin").write_bytes(b"tampered")
    assert cache.lookup("krea2_comfyui", key) is None

    cache.store("krea2_comfyui", key, b"payload")
    (entry / "meta.json").unlink()
    assert cache.lookup("krea2_comfyui", key) is None

    cache.store("krea2_comfyui", key, b"payload")
    (entry / "meta.json").write_text("{not json", encoding="utf-8")
    assert cache.lookup("krea2_comfyui", key) is None

    cache.store("krea2_comfyui", key, b"payload")
    (entry / "artifact.bin").write_bytes(b"")
    assert cache.lookup("krea2_comfyui", key) is None


def test_lookup_rejects_size_mismatch_without_full_trust(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path)
    key = cache.key_hash({"prompt": "size-check"})
    cache.store("comfyui", key, b"12345")
    meta_path = tmp_path / "v1" / "comfyui" / key / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["artifact_bytes"] = 999
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert cache.lookup("comfyui", key) is None


def _entry(tmp_path: Path, backend: str, key: str) -> Path:
    return tmp_path / "v1" / backend / key


def _restamp(entry: Path, value: str) -> None:
    meta_path = entry / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["last_access_at"] = value
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_eviction_removes_least_recently_accessed_entries(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path)
    key_a = cache.key_hash({"n": 1})
    key_b = cache.key_hash({"n": 2})
    key_c = cache.key_hash({"n": 3})
    assert cache.store("comfyui", key_a, b"0" * 6)
    assert cache.store("comfyui", key_b, b"0" * 8)
    assert cache.store("comfyui", key_c, b"0" * 8)
    _restamp(_entry(tmp_path, "comfyui", key_a), "2026-01-03T00:00:00+00:00")
    _restamp(_entry(tmp_path, "comfyui", key_b), "2026-01-01T00:00:00+00:00")
    _restamp(_entry(tmp_path, "comfyui", key_c), "2026-01-02T00:00:00+00:00")

    cache.max_bytes = 16
    cache._evict()

    assert cache.lookup("comfyui", key_b) is None
    assert cache.lookup("comfyui", key_a) is not None
    assert cache.lookup("comfyui", key_c) is not None


def test_lookup_refreshes_last_access_stamp(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path)
    key = cache.key_hash({"n": 1})
    cache.store("comfyui", key, b"payload")
    entry = _entry(tmp_path, "comfyui", key)
    _restamp(entry, "2026-01-01T00:00:00+00:00")

    cache.lookup("comfyui", key)

    meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    assert meta["last_access_at"] != "2026-01-01T00:00:00+00:00"


def test_uncapped_cache_never_evicts_and_missing_root_is_a_miss(tmp_path: Path) -> None:
    uncapped = GenerationCache(tmp_path / "cache-root")
    key = uncapped.key_hash({"n": 1})

    assert uncapped.store("comfyui", key, b"0" * 64) is True
    assert uncapped.lookup("comfyui", key) is not None

    untouched = GenerationCache(tmp_path / "missing-root")
    assert untouched.lookup("comfyui", key) is None


def test_invalid_backend_names_are_sanitized(tmp_path: Path) -> None:
    cache = GenerationCache(tmp_path)
    key = cache.key_hash({"n": 1})

    assert cache.store("../../evil backend#", key, b"data") is True
    assert (tmp_path / "v1").is_dir()
    segments = sorted(entry.name for entry in (tmp_path / "v1").iterdir())
    assert segments == ["evil-backend"]
    assert cache.lookup("evil-backend", key) is not None
