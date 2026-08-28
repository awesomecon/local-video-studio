"""Deterministic per-shot normalization with a content-addressed cache.

Every shot becomes one silent intermediate at the project canvas/fps:

- images and graphic screens loop for the shot duration; image motion uses
  the shared eased camera-motion filter path;
- video honors ``source_in_seconds``/``source_out_seconds``, never silently
  loops, and only clone-pads its final frame under an explicit
  ``settings["pad_final_frame"] = true`` policy (otherwise the shortfall is
  reported so QC can surface it);
- rotation (via FFmpeg autorotation), pixel aspect, frame rate, colorspace
  container, and canvas size are all normalized;
- overlays are applied as ordered structured ``overlay`` filters.

Intermediates are encoded with a deliberate high-quality mezzanine policy
(``MEZZANINE_ENCODE``) so the one extra lossy generation into the scene
render costs exact-text graphics and fine detail as little as possible.

The cache key fingerprints every contributing input: source asset hash,
overlay payload and asset hashes, shot settings, renderer/workflow version,
FFmpeg identity, canvas, fps, frame budget, and the mezzanine encode
settings. A cached composite is only reused when its stored manifest still
matches that fingerprint AND the file's SHA-256 matches the manifest, so
missing, stale, corrupted, or mismatched artifacts always rebuild.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from backend.schemas.shots import Shot, VisualType
from backend.timeline.shots import frame_count

from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from .commands import _camera_motion_filter
from .manifests import (
    RENDERER_VERSION,
    SHOT_NORMALIZATION_WORKFLOW,
    canonical_json,
    content_key,
    ffmpeg_identity,
    fsync_file,
    load_manifest,
    sha256_file,
    write_manifest,
)
from .overlays import append_overlay_filters, resolve_overlays
from .process import run_media_process
from .probe import MediaInfo, count_video_frames, probe_media

# High-quality mezzanine policy for normalized intermediates: the scene
# render re-encodes these once more, so this stage deliberately spends
# bitrate (low CRF) to protect exact text and fine detail.
MEZZANINE_ENCODE = {"codec": "libx264", "preset": "medium", "crf": 12}

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}
_IMAGE_VISUAL_TYPES = {
    VisualType.GRAPHIC_SCREEN,
    VisualType.TEXT_OVERLAY_STILL,
    VisualType.TITLE_CARD,
    VisualType.DIAGRAM,
    VisualType.KREA2_STILL,
    VisualType.IDEOGRAM4_STILL,
    VisualType.QWEN_IMAGE_STILL,
    VisualType.FLUX_STILL,
    VisualType.IMAGE_MOTION,
}
_VIDEO_VISUAL_TYPES = {
    VisualType.H3_AUDIOVISUAL,
    VisualType.H3_REFERENCE,
    VisualType.WAN_VIDEO,
}


class ShotNormalizationError(RuntimeError):
    """A normalized shot composite failed validation or policy checks."""


#: Camera instructions that mean "no motion". Mirrors the pipeline's
#: ``_camera_motion`` vocabulary so a still marked ``locked`` (the UI default
#: for static stills) is never sent through the Ken Burns path.
_STATIC_CAMERA_INSTRUCTIONS = {"", "locked", "locked off", "none", "no motion", "static"}


def _camera_motion_active(shot: Shot) -> bool:
    """True when the shot asks for real still motion (not a static marker)."""
    normalized = shot.camera_instruction.strip().lower().replace("_", " ").replace("-", " ")
    return normalized not in _STATIC_CAMERA_INSTRUCTIONS


def resolve_source_kind(shot: Shot, source_path: Path) -> str:
    """Classify the visual source as ``image`` or ``video`` deterministically."""
    if shot.visual_type in _IMAGE_VISUAL_TYPES:
        return "image"
    if shot.visual_type in _VIDEO_VISUAL_TYPES:
        return "video"
    return "image" if source_path.suffix.lower() in _IMAGE_EXTENSIONS else "video"


@dataclass(frozen=True, slots=True)
class NormalizationInputs:
    """Everything needed to normalize one shot's visual."""

    shot: Shot
    source_path: Path
    overlay_paths: Mapping[str, Path] = field(default_factory=dict)
    canvas_width: int = 1920
    canvas_height: int = 1080
    fps: int = 24


@dataclass(frozen=True, slots=True)
class NormalizedShot:
    """Result of one ``ShotNormalizer.normalize`` call."""

    shot_id: str
    cache_key: str
    path: Path
    expected_frames: int
    actual_frames: int
    duration_seconds: float
    cache_hit: bool
    shortfall_frames: int
    manifest: dict[str, Any]


def _stable_shot_payload(shot: Shot) -> dict[str, Any]:
    """Shot payload for hashing, minus fields that never change pixels."""
    payload = shot.model_dump(mode="json")
    for volatile in ("created_at", "updated_at", "status", "locked"):
        payload.pop(volatile, None)
    return payload


def _fingerprint(
    inputs: NormalizationInputs,
    *,
    frames: int,
    renderer_version: str,
    binaries: FFmpegBinaries,
) -> dict[str, Any]:
    shot = inputs.shot
    overlay_entries: list[list[Any]] = []
    for cue in sorted(shot.overlays, key=lambda item: item.id):
        resolved_path = inputs.overlay_paths.get(cue.id)
        asset_hash = sha256_file(resolved_path) if resolved_path else None
        overlay_entries.append([
            cue.model_dump(mode="json"),
            asset_hash,
        ])
    return {
        "renderer": renderer_version,
        "workflow": SHOT_NORMALIZATION_WORKFLOW,
        "ffmpeg": ffmpeg_identity(binaries),
        "canvas": [inputs.canvas_width, inputs.canvas_height],
        "fps": inputs.fps,
        "frames": frames,
        "duration_seconds": round(frames / inputs.fps, 9),
        "encode": dict(MEZZANINE_ENCODE),
        "kind": resolve_source_kind(shot, inputs.source_path),
        "source_sha256": sha256_file(inputs.source_path),
        "shot": _stable_shot_payload(shot),
        "overlays": overlay_entries,
    }


def build_normalization_command(
    binaries: FFmpegBinaries,
    inputs: NormalizationInputs,
    *,
    destination: Path,
    frames: int,
) -> tuple[list[str], str]:
    """Build the silent-normalization argv; return ``(argv, base_label)``.

    Pure construction: no subprocess runs here, which keeps command shape
    reviewable and testable independently of execution.
    """
    ffmpeg_path = require_ffmpeg(binaries)
    shot = inputs.shot
    width, height, fps = inputs.canvas_width, inputs.canvas_height, inputs.fps
    duration = frames / fps
    kind = resolve_source_kind(shot, inputs.source_path)
    pad_final_frame = bool(shot.settings.get("pad_final_frame"))

    argv = [str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y"]
    if kind == "image":
        argv.extend(
            [
                "-loop", "1",
                "-framerate", str(fps),
                "-t", f"{duration:.6f}",
                "-i", str(inputs.source_path),
            ]
        )
        if _camera_motion_active(shot):
            base = _camera_motion_filter(
                shot.camera_instruction,
                width=width,
                height=height,
                fps=fps,
                duration_seconds=duration,
            )
        else:
            base = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
                f"trim=duration={duration:.6f},settb=AVTB,setpts=PTS-STARTPTS"
            )
    else:
        trim_in, trim_out = shot.source_in_seconds, shot.source_out_seconds
        if trim_in is not None and trim_out is not None:
            window = min(trim_out - trim_in, duration)
            argv.extend(["-ss", f"{trim_in:.6f}", "-t", f"{window:.6f}"])
        else:
            argv.extend(["-t", f"{duration:.6f}"])
        argv.extend(["-i", str(inputs.source_path)])
        padding = (
            f"tpad=stop_mode=clone:stop_duration={duration:.6f},"
            if pad_final_frame
            else ""
        )
        base = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},{padding}"
            f"trim=duration={duration:.6f},settb=AVTB,setpts=PTS-STARTPTS"
        )
    # A trailing explicit fps restores the frame-rate metadata that setpts
    # drops; without it, overlay enable windows evaluate t against a wrong
    # assumed rate (verified against FFmpeg 7 behavior).
    base = f"{base},fps={fps}"

    filters = [f"[0:v]{base}[base0]"]
    resolved_overlays = [
        replace(item, input_index=item.input_index + 1)
        for item in resolve_overlays(
            shot.overlays,
            dict(inputs.overlay_paths),
            canvas_width=width,
            canvas_height=height,
        )
    ]
    for item in resolved_overlays:
        cue = item.cue
        argv.extend(
            [
                "-loop", "1",
                "-framerate", str(fps),
                "-t", f"{duration:.6f}",
                "-i", str(inputs.overlay_paths[cue.id]),
            ]
        )
    label = append_overlay_filters(filters, resolved_overlays, base_label="base0")

    filters.append(f"[{label}]format=yuv420p[vout]")
    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map", "[vout]",
            "-an",
            "-c:v", MEZZANINE_ENCODE["codec"],
            "-preset", MEZZANINE_ENCODE["preset"],
            "-crf", str(MEZZANINE_ENCODE["crf"]),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-frames:v", str(frames),
            str(destination),
        ]
    )
    return argv, label


class ShotNormalizer:
    """Normalize shots into cached, deterministic silent intermediates.

    ``cache_root`` is required and must be persistent: composites are the
    unit of scene assembly, so a normalized file must outlive the call that
    produced it. Without a real cache location this class refuses to run
    rather than returning a path inside a directory about to be deleted.
    """

    def __init__(
        self,
        binaries: FFmpegBinaries | None = None,
        cache_root: str | Path | None = None,
        *,
        renderer_version: str = RENDERER_VERSION,
        temp_root: str | Path | None = None,
    ) -> None:
        self.binaries = binaries or discover_binaries()
        require_ffmpeg(self.binaries)
        if cache_root is None:
            raise ValueError(
                "ShotNormalizer requires a persistent cache_root directory; "
                "normalization output is consumed later by scene assembly"
            )
        self.renderer_version = renderer_version
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.temp_root = Path(temp_root) if temp_root else None

    def scope_directory(self, inputs: NormalizationInputs) -> Path:
        return self.cache_root / "shots" / inputs.shot.id

    def fingerprint(self, inputs: NormalizationInputs, *, frames: int) -> dict[str, Any]:
        return _fingerprint(
            inputs, frames=frames, renderer_version=self.renderer_version,
            binaries=self.binaries,
        )

    def cache_key(self, inputs: NormalizationInputs, *, frames: int) -> str:
        return content_key(self.fingerprint(inputs, frames=frames))

    def normalize(
        self,
        inputs: NormalizationInputs,
        *,
        duration_seconds: float,
        job_id: str | None = None,
    ) -> NormalizedShot:
        """Return the cached composite for this exact input set, rendering it if needed."""
        if not inputs.source_path.is_file():
            raise FileNotFoundError(inputs.source_path)
        frames = frame_count(duration_seconds, inputs.fps)
        fingerprint = self.fingerprint(inputs, frames=frames)
        key = content_key(fingerprint)

        scope = self.scope_directory(inputs)
        media_path = scope / key / "composite.mp4"
        manifest_path = scope / key / "manifest.json"
        hit = self._valid_cache_entry(manifest_path, media_path, fingerprint, frames)
        if hit is not None:
            outcomes = hit["outcomes"]
            return NormalizedShot(
                shot_id=inputs.shot.id,
                cache_key=key,
                path=media_path,
                expected_frames=frames,
                actual_frames=int(outcomes["actual_frames"]),
                duration_seconds=duration_seconds,
                cache_hit=True,
                shortfall_frames=int(outcomes["shortfall_frames"]),
                manifest=hit,
            )

        if self.temp_root:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="lvs-shot-",
            dir=str(self.temp_root) if self.temp_root else None,
        ) as temporary:
            workspace = Path(temporary)
            staged = workspace / "composite.mp4"
            argv, _ = build_normalization_command(
                self.binaries, inputs, destination=staged, frames=frames,
            )
            run_media_process(argv, timeout=max(120.0, frames / inputs.fps * 20 + 60), job_id=job_id)
            outcome = self._validate_output(staged, inputs, frames=frames, key=key)
            # Crash-consistent order: fsync the validated media, move it into
            # the cache scope, then write the manifest that vouches for it.
            # A crash before the manifest leaves an unvouched file the next
            # run treats as a miss; a crash after leaves a valid entry.
            fsync_file(staged)
            media_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(media_path))
            write_manifest(manifest_path, outcome)
            return NormalizedShot(
                shot_id=inputs.shot.id,
                cache_key=key,
                path=media_path,
                expected_frames=frames,
                actual_frames=int(outcome["outcomes"]["actual_frames"]),
                duration_seconds=duration_seconds,
                cache_hit=False,
                shortfall_frames=int(outcome["outcomes"]["shortfall_frames"]),
                manifest=outcome,
            )

    def _valid_cache_entry(
        self,
        manifest_path: Path,
        media_path: Path,
        fingerprint: dict[str, Any],
        expected_frames: int,
    ) -> dict[str, Any] | None:
        """Return the stored manifest only for a fully trustworthy artifact.

        Rejects missing manifests/media, stale fingerprints, corrupted or
        truncated media (SHA mismatch), and entries whose recorded frame
        count no longer matches this request.
        """
        if not media_path.is_file() or media_path.stat().st_size == 0:
            return None
        stored = load_manifest(manifest_path)
        if not isinstance(stored, dict):
            return None
        stored_fingerprint = stored.get("cache_fingerprint")
        if not isinstance(stored_fingerprint, dict):
            return None
        if canonical_json(stored_fingerprint) != canonical_json(fingerprint):
            return None
        outcomes = stored.get("outcomes")
        if not isinstance(outcomes, dict):
            return None
        if int(stored.get("expected_frames", -1)) != expected_frames:
            return None
        recorded_sha = outcomes.get("media_sha256")
        if not isinstance(recorded_sha, str) or len(recorded_sha) != 64:
            return None
        if sha256_file(media_path) != recorded_sha:
            return None
        return stored

    def _validate_output(
        self,
        media: Path,
        inputs: NormalizationInputs,
        *,
        frames: int,
        key: str,
    ) -> dict[str, Any]:
        if not media.is_file() or media.stat().st_size == 0:
            raise ShotNormalizationError(
                f"normalization produced no output for shot {inputs.shot.id!r}"
            )
        info = probe_media(media, self.binaries)
        self._check_probe(info, inputs)
        actual = count_video_frames(media, self.binaries)
        shortfall = max(0, frames - actual)
        pad_policy = bool(inputs.shot.settings.get("pad_final_frame"))
        is_video = resolve_source_kind(inputs.shot, inputs.source_path) == "video"
        if shortfall and (not is_video or pad_policy):
            raise ShotNormalizationError(
                f"shot {inputs.shot.id!r} produced {actual} of {frames} frames; "
                "enable settings['pad_final_frame'] or extend the source media"
            )
        if actual > frames:
            raise ShotNormalizationError(
                f"shot {inputs.shot.id!r} produced {actual} frames; expected {frames}"
            )
        # Duration must agree with the accepted frame count; this keeps the
        # container stamps consistent while still allowing a documented,
        # unpadded video shortfall to end early.
        self._check_duration(info, actual, inputs)
        fingerprint = self.fingerprint(inputs, frames=frames)
        return {
            "cache_key": key,
            "cache_fingerprint": fingerprint,
            "shot_id": inputs.shot.id,
            "renderer_version": self.renderer_version,
            "workflow": SHOT_NORMALIZATION_WORKFLOW,
            "media": media.name,
            "expected_frames": frames,
            "outcomes": {
                "actual_frames": actual,
                "shortfall_frames": shortfall,
                "width": info.width,
                "height": info.height,
                "fps": info.fps,
                "duration_seconds": info.duration_seconds,
                "has_audio": info.has_audio,
                "media_sha256": sha256_file(media),
                "media_bytes": media.stat().st_size,
            },
        }

    def _check_probe(self, info: MediaInfo, inputs: NormalizationInputs) -> None:
        shot_id = inputs.shot.id
        if not info.has_video:
            raise ShotNormalizationError(f"normalized shot {shot_id!r} has no video stream")
        if info.has_audio:
            raise ShotNormalizationError(f"normalized shot {shot_id!r} must be silent")
        expected_size = (inputs.canvas_width, inputs.canvas_height)
        if (info.width, info.height) != expected_size:
            raise ShotNormalizationError(
                f"normalized shot {shot_id!r} is "
                f"{info.width}x{info.height}; expected {expected_size[0]}x{expected_size[1]}"
            )
        fps_tolerance = max(0.05, inputs.fps * 0.01)
        if info.fps is None or abs(info.fps - inputs.fps) > fps_tolerance:
            raise ShotNormalizationError(
                f"normalized shot {shot_id!r} runs at {info.fps!r} fps; "
                f"expected {inputs.fps}"
            )

    def _check_duration(
        self, info: MediaInfo, accepted_frames: int, inputs: NormalizationInputs,
    ) -> None:
        expected_duration = accepted_frames / inputs.fps
        duration_tolerance = max(2 / inputs.fps, 0.05)
        if (
            info.duration_seconds is None
            or abs(info.duration_seconds - expected_duration) > duration_tolerance
        ):
            raise ShotNormalizationError(
                f"normalized shot {inputs.shot.id!r} duration {info.duration_seconds}s "
                f"is not within {duration_tolerance:.3f}s of its "
                f"{accepted_frames}-frame length ({expected_duration:.6f}s)"
            )


def _load_manifest_dict(manifest_path: Path) -> dict[str, Any] | None:
    value = load_manifest(manifest_path)
    return value if isinstance(value, dict) else None


__all__ = [
    "NormalizationInputs",
    "NormalizedShot",
    "ShotNormalizationError",
    "ShotNormalizer",
    "build_normalization_command",
    "resolve_source_kind",
]
