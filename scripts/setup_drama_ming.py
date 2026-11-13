#!/usr/bin/env python3
"""Explicit, disk-budgeted setup for Drama Q4 and shared-environment Ming TTS.

The default plan is offline. Neither application startup nor speech generation
calls this installer. Model downloads require the download action and flag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.tts_worker.speech_releases import (
    DRAMA_ASSET, DRAMA_SHA256, MING_REVISION, MODEL_SUBDIRS, RELEASES,
)

GIB = 1024**3
RESERVE = 50 * GIB


def existing_parent(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def require_space(path: Path, additional: int) -> None:
    free = shutil.disk_usage(existing_parent(path)).free
    if free - additional < RESERVE:
        raise RuntimeError(
            f"Refusing setup: {free / GIB:.2f} GiB free at {path}; "
            f"needs {additional / GIB:.2f} GiB additional plus 50 GiB reserve."
        )


def model_files(provider: str) -> list[dict]:
    repo, revision, _ = RELEASES[provider]
    with urlopen(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true", timeout=30) as response:
        metadata = json.load(response)
    if metadata["sha"] != revision:
        raise RuntimeError("Model metadata revision mismatch.")
    files = []
    for item in metadata["siblings"]:
        name = item["rfilename"]
        # Keep runtime assets and attribution; omit demo media and reports.
        if name.startswith(("figures/", "samples/", "media/", "eval/", "eval_ab/")) or name.endswith(".pdf"):
            continue
        if not item.get("size"):
            continue
        files.append({"path": name, "size": item["size"], "sha256": item.get("lfs", {}).get("sha256")})
    if not files:
        raise RuntimeError("No model files found in the pinned release.")
    return files


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url: str, path: Path, *, size: int | None = None, sha256: str | None = None) -> str:
    """Stream directly to the destination, without a second model cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    if partial.exists():
        partial.unlink()  # Only this installer's incomplete temporary file.
    require_space(path, size or 256 * 1024**2)
    total = 0
    hasher = hashlib.sha256()
    try:
        with urlopen(url, timeout=120) as response, partial.open("xb") as target:
            while block := response.read(1024**2):
                require_space(path, len(block))
                total += len(block)
                if size is not None and total > size:
                    raise RuntimeError("Download exceeded its advertised file size.")
                target.write(block)
                hasher.update(block)
        if size is not None and total != size:
            raise RuntimeError("Downloaded file size mismatch.")
        if sha256 and hasher.hexdigest() != sha256:
            raise RuntimeError("Downloaded file checksum mismatch.")
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return hasher.hexdigest()


def download(root: Path, providers: list[str]) -> None:
    bundles = [(provider, model_files(provider)) for provider in providers]
    remaining = 0
    for provider, files in bundles:
        destination = root / MODEL_SUBDIRS[provider]
        for item in files:
            file = destination / item["path"]
            valid = file.is_file() and file.stat().st_size == item["size"]
            if valid and item["sha256"]:
                valid = digest(file) == item["sha256"]
            item["present"] = valid
            if not valid:
                remaining += item["size"]
    # Account by actual device, including existing destination symlinks.
    budgets: dict[int, tuple[Path, int]] = {}
    for provider, files in bundles:
        destination = root / MODEL_SUBDIRS[provider]
        device = existing_parent(destination).stat().st_dev
        amount = sum(item["size"] for item in files if not item["present"])
        previous = budgets.get(device, (destination, 0))
        budgets[device] = (destination, previous[1] + amount)
    for destination, amount in budgets.values():
        require_space(destination, amount + GIB)  # Runtime/JIT working margin.
    print(f"Downloading {remaining / GIB:.2f} GiB; reserve: 50 GiB", flush=True)
    for provider, files in bundles:
        repo, revision, _ = RELEASES[provider]
        destination = root / MODEL_SUBDIRS[provider]
        receipt = []
        for item in files:
            file = destination / item["path"]
            if not file.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe file path in model metadata.")
            if not item["present"]:
                print(f"{provider}: {item['path']}", flush=True)
                item["sha256"] = fetch(
                    f"https://huggingface.co/{repo}/resolve/{revision}/{quote(item['path'], safe='/')}",
                    file, size=item["size"], sha256=item["sha256"],
                )
            else:
                item["sha256"] = digest(file)
            receipt.append({key: item[key] for key in ("path", "size", "sha256")})
        document = {"repo": repo, "revision": revision, "files": receipt}
        destination.mkdir(parents=True, exist_ok=True)
        temporary = destination / "lvs-release.json.part"
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        temporary.replace(destination / "lvs-release.json")


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


def prepare(root: Path) -> None:
    if not shutil.which("uv"):
        raise RuntimeError("uv is required; this installer does not install development tools.")
    require_space(root, 8 * GIB)  # Dependencies plus temporary wheel extraction.
    cache = root / "cache/drama-ming"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "tmp").mkdir(exist_ok=True)
    environment = {**os.environ, "UV_CACHE_DIR": str(cache / "uv"), "TMPDIR": str(cache / "tmp"),
                   "PIP_CACHE_DIR": str(cache / "pip"), "XDG_CACHE_HOME": str(cache),
                   "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    service = root / "services/ming-tts"
    source = service / "source"
    service.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with tempfile.TemporaryDirectory(dir=cache / "tmp") as temporary:
            archive = Path(temporary) / "ming.tar.gz"
            fetch(f"https://github.com/inclusionAI/Ming-3.0-tts/archive/{MING_REVISION}.tar.gz", archive)
            unpack = Path(temporary) / "source"
            extract(archive, unpack)
            shutil.move(str(unpack / f"Ming-3.0-tts-{MING_REVISION}"), source)
        (source / "lvs-source-revision").write_text(MING_REVISION)
    elif not (source / "lvs-source-revision").is_file() or (source / "lvs-source-revision").read_text() != MING_REVISION:
        raise RuntimeError("Existing Ming source is not this installer's pinned checkout; use another destination.")
    python = service / ".venv/bin/python"
    if not python.exists():
        subprocess.run(["uv", "venv", "--python", sys.executable, str(service / ".venv")],
                       env=environment, check=True)
    # Tiny's stack wins. 0.5B-only dependencies are added without installing
    # its contradictory Torch/x-transformers pins. Compatibility is tested,
    # never inferred from successful dependency resolution.
    subprocess.run([
        "uv", "pip", "install", "--no-cache", "--index-strategy", "unsafe-best-match",
        "--python", str(python),
        "-r", str(source / "requirements-tiny.txt"), "diffusers==0.33.0", "inflect",
        "fastapi>=0.115,<1", "uvicorn>=0.30,<1", "httpx>=0.27,<1",
    ], env=environment, check=True)
    require_space(root, 0)
    drama = root / "services/drama-rs"
    if not (drama / "drama").exists():
        with tempfile.TemporaryDirectory(dir=cache / "tmp") as temporary:
            archive = Path(temporary) / DRAMA_ASSET
            fetch(f"https://github.com/apiplant/drama-rs/releases/download/v0.3.0/{DRAMA_ASSET}",
                  archive, sha256=DRAMA_SHA256)
            unpack = Path(temporary) / "drama"
            extract(archive, unpack)
            binaries = [file for file in unpack.rglob("drama") if file.is_file()]
            if len(binaries) != 1:
                raise RuntimeError("Unexpected drama-rs release layout.")
            # Keep companion libraries/licenses from the release archive.
            drama.mkdir(parents=True, exist_ok=True)
            shutil.copytree(binaries[0].parent, drama, dirs_exist_ok=True)
    print("Runtimes prepared. Model downloads were skipped.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "prepare", "download"))
    parser.add_argument("--root", type=Path, required=True, help="Explicit user-configured AI storage root")
    parser.add_argument("--provider", choices=tuple(RELEASES), action="append")
    parser.add_argument("--confirm-download", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    providers = args.provider or list(RELEASES)
    free = shutil.disk_usage(existing_parent(root)).free
    for provider in providers:
        print(f"{provider}: {RELEASES[provider][2] / GIB:.2f} GiB -> {root / MODEL_SUBDIRS[provider]}")
    print(f"Free: {free / GIB:.2f} GiB; required reserve: 50 GiB", flush=True)
    try:
        if args.action == "prepare":
            prepare(root)
        elif args.action == "download":
            if not args.confirm_download:
                parser.error("Model downloads require --confirm-download after reviewing the size and destination.")
            download(root, providers)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
