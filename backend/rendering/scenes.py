"""Assemble one scene's normalized shot intermediates into ``rendered.mp4``.

The scene render consumes exactly the intermediates named by a
:class:`~backend.timeline.shots.SceneRenderPlan` and joins them with V1
intra-scene transitions — ``cut``, ``crossfade``/``dissolve`` (compiled
identically), ``fade_through_black``, and ``dip_to_white``. Every trim and
blend offset is derived from the plan's integer frame counts, so frame counts
are exact by construction:

- cut: plain concat;
- crossfade: the previous shot's last ``o`` frames blend linearly into the
  incoming shot's first ``o`` frames, replacing both;
- fade through black / dip to white: the outgoing tail fades to the dip
  color over ``o // 2`` frames, the incoming head fades back over the
  remaining ``o - o // 2`` frames, and the overlap interior is dropped, which
  is what keeps the rendered length at ``sum - overlaps`` frames.

Publication is atomic: FFmpeg writes to a staged file beside the destination,
the staged output must pass probe/frame-count QC, and only then does it
replace any previous render. A failed QC never touches the published file.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.timeline.shots import RENDERER_TRANSITION_KINDS, SceneRenderPlan

from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from .manifests import (
    RENDERER_VERSION,
    SCENE_ASSEMBLY_WORKFLOW,
    content_key,
    ffmpeg_identity,
    fsync_file,
    load_manifest,
    sha256_file,
    write_manifest,
)
from .process import run_media_process
from .probe import count_video_frames, probe_media


_XFADE_TRANSITIONS = {
    "crossfade": "fade",
    "fade_through_black": "fadeblack",
    "dip_to_white": "fadewhite",
}


class SceneInputError(ValueError):
    """The provided intermediates do not satisfy the compiled plan."""


class SceneQCError(RuntimeError):
    """A freshly rendered scene failed probe/QC; nothing was published."""


@dataclass(frozen=True, slots=True)
class SceneEncodeOptions:
    video_codec: str = "libx264"
    video_preset: str = "ultrafast"
    crf: int = 20

    def validate(self) -> None:
        if not 0 <= self.crf <= 51:
            raise ValueError("CRF must be between 0 and 51")


def build_scene_command(
    binaries: FFmpegBinaries,
    plan: SceneRenderPlan,
    intermediate_paths: list[Path],
    destination: Path,
    options: SceneEncodeOptions | None = None,
) -> list[str]:
    """Build the scene assembly argv from compiled timings. Pure function."""
    selected = options or SceneEncodeOptions()
    selected.validate()
    if len(intermediate_paths) != len(plan.frame_counts):
        raise SceneInputError(
            f"plan needs {len(plan.frame_counts)} intermediates, "
            f"got {len(intermediate_paths)}"
        )
    fps = plan.fps
    argv = [
        str(require_ffmpeg(binaries)), "-hide_banner", "-loglevel", "error", "-y",
    ]
    for path in intermediate_paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        argv.extend(["-i", str(path)])

    filters: list[str] = []
    for index in range(len(intermediate_paths)):
        # The trailing settb pins every source to the identical 1/fps
        # timebase: containers produced from different encoders decode with
        # different timebases, and xfade refuses mismatched inputs.
        # setsar=1 pins square pixels: intermediates from the camera-motion
        # path (or stale caches) can carry odd container SARs such as
        # 12096:12095, and concat/xfade reject inputs whose SARs disagree.
        filters.append(
            f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS,setsar=1,fps={fps},"
            f"settb=expr=1/{fps}[src{index}]"
        )

    counts = plan.frame_counts
    current_label = "src0"
    running_frames = counts[0]
    for boundary in plan.boundaries:
        position = boundary.position
        if boundary.kind not in RENDERER_TRANSITION_KINDS:
            raise SceneInputError(
                f"unsupported transition kind {boundary.kind!r} after shot {position - 1}"
            )
        overlap = boundary.overlap_frames
        output_label = f"joined{position}"
        if boundary.kind == "cut":
            filters.append(
                f"[{current_label}][src{position}]concat=n=2:v=1:a=0,"
                # concat re-times its output to AVTB; repin so any following
                # xfade sees matched timebases on both sides.
                f"settb=expr=1/{fps}[{output_label}]"
            )
            running_frames += counts[position]
        else:
            # xfade composites the last ``overlap`` frames of the accumulated
            # stream with the first ``overlap`` frames of the next shot; its
            # fadeblack/fadewhite transitions are exactly our dip moves.
            offset = (running_frames - overlap) / fps
            duration = overlap / fps
            filters.append(
                f"[{current_label}][src{position}]xfade="
                f"transition={_XFADE_TRANSITIONS[boundary.kind]}:"
                f"duration={duration:.9f}:offset={offset:.9f},"
                # xfade emits an AVTB output link; repin it so any following
                # xfade sees matched timebases on both sides.
                f"settb=expr=1/{fps}[{output_label}]"
            )
            running_frames += counts[position] - overlap
        current_label = output_label

    # Final explicit fps restores rate metadata lost inside trim/blend
    # chains so the muxed container carries the project fps, not a guess.
    filters.append(f"[{current_label}]fps={fps},format=yuv420p[vout]")
    argv.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", "[vout]",
            "-an",
            "-c:v", selected.video_codec,
            "-preset", selected.video_preset,
            "-crf", str(selected.crf),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-frames:v", str(plan.total_frames),
            str(destination),
        ]
    )
    return argv


def _valid_scene_artifact(
    manifest_path: Path,
    media_path: Path,
    expected: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the stored manifest only for a fully trustworthy cached render.

    ``expected`` lists the identity fields the request implies (cache key,
    renderer version, workflow, scene id, fps, frame budget, duration, and
    encode settings). Every one must match the stored manifest exactly, and
    the media must hash to the manifest's recorded SHA-256 — so missing,
    stale, tampered, corrupted, or truncated artifacts always fall through
    to a fresh render.
    """
    if not media_path.is_file() or media_path.stat().st_size == 0:
        return None
    stored = load_manifest(manifest_path)
    if not isinstance(stored, dict):
        return None
    for field_name, expected_value in expected.items():
        if stored.get(field_name) != expected_value:
            return None
    recorded_sha = stored.get("media_sha256")
    if not isinstance(recorded_sha, str) or len(recorded_sha) != 64:
        return None
    if sha256_file(media_path) != recorded_sha:
        return None
    return stored


def _copy_into_place(source: Path, destination: Path) -> None:
    """Copy ``source`` over ``destination`` atomically via a staged sibling."""
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=destination.suffix,
        dir=str(destination.parent),
    )
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        shutil.copyfile(source, staged)
        fsync_file(staged)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class SceneRenderResult:
    scene_id: str
    path: Path
    manifest_path: Path | None
    total_frames: int
    duration_seconds: float
    cache_key: str
    cache_hit: bool = False


class SceneAssembler:
    """Compile cached shot intermediates into one atomically published scene."""

    def __init__(
        self,
        binaries: FFmpegBinaries | None = None,
        *,
        cache_root: str | Path | None = None,
        temp_root: str | Path | None = None,
        renderer_version: str = RENDERER_VERSION,
    ) -> None:
        self.binaries = binaries or discover_binaries()
        require_ffmpeg(self.binaries)
        self.cache_root = Path(cache_root) if cache_root else None
        self.temp_root = Path(temp_root) if temp_root else None
        self.renderer_version = renderer_version

    def _scope_directory(self, plan_scene_id: str, key: str) -> Path:
        return self.cache_root / "scenes" / plan_scene_id / key

    def scene_cache_key(
        self,
        plan: SceneRenderPlan,
        shot_keys: dict[str, str],
        *,
        encode: SceneEncodeOptions | None = None,
    ) -> str:
        missing = [shot_id for shot_id in plan.ordered_shot_ids() if shot_id not in shot_keys]
        if missing:
            raise SceneInputError(f"missing shot cache keys for {missing}")
        selected = encode or SceneEncodeOptions()
        selected.validate()
        fingerprint = {
            "renderer": self.renderer_version,
            "workflow": SCENE_ASSEMBLY_WORKFLOW,
            "ffmpeg": ffmpeg_identity(self.binaries),
            "scene_id": plan.scene_id,
            "fps": plan.fps,
            "total_frames": plan.total_frames,
            "encode": {
                "codec": selected.video_codec,
                "preset": selected.video_preset,
                "crf": selected.crf,
            },
            "shots": [
                {
                    "shot_id": shot.shot_id,
                    "frames": frames,
                    "cache_key": shot_keys[shot.shot_id],
                }
                for shot, frames in zip(plan.shots, plan.frame_counts, strict=True)
            ],
            "boundaries": [
                {
                    "kind": boundary.kind,
                    "offset_frames": boundary.offset_frames,
                    "overlap_frames": boundary.overlap_frames,
                }
                for boundary in plan.boundaries
            ],
        }
        return content_key(fingerprint)

    def render(
        self,
        plan: SceneRenderPlan,
        intermediates: dict[str, Path],
        destination: str | Path,
        *,
        shot_keys: dict[str, str],
        manifest_path: str | Path | None = None,
        options: SceneEncodeOptions | None = None,
        job_id: str | None = None,
    ) -> SceneRenderResult:
        """Assemble, QC, and atomically publish one scene render.

        With ``cache_root`` configured this is a real second-level cache: an
        unchanged scene (same plan, same shot keys, same encode settings,
        same FFmpeg) reuses the cached artifact after revalidating its
        manifest fingerprint and media SHA-256 instead of re-encoding. The
        published media is fsynced before it is renamed into place, and the
        manifest that vouches for it (including the media SHA-256) is written
        immediately after, so a crash can only ever leave an unvouched file —
        never a half-published one.
        """
        selected = options or SceneEncodeOptions()
        selected.validate()
        ordered_paths = self._validated_intermediates(plan, intermediates)
        expected_size = self._intermediate_geometry(ordered_paths[0])

        key = self.scene_cache_key(plan, shot_keys, encode=selected)
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest_target = (
            Path(manifest_path)
            if manifest_path is not None
            else output.parent / f"{output.stem}.manifest.json"
        )

        identity = {
            "cache_key": key,
            "renderer_version": self.renderer_version,
            "workflow": SCENE_ASSEMBLY_WORKFLOW,
            "scene_id": plan.scene_id,
            "fps": plan.fps,
            "total_frames": plan.total_frames,
            "duration_seconds": plan.duration_seconds,
            "encode": {
                "codec": selected.video_codec,
                "preset": selected.video_preset,
                "crf": selected.crf,
            },
        }
        if self.cache_root is not None:
            scope = self._scope_directory(plan.scene_id, key)
            cached_media = scope / "rendered.mp4"
            cached_manifest = scope / "manifest.json"
            hit_payload = _valid_scene_artifact(
                cached_manifest, cached_media, identity,
            )
            if hit_payload is not None:
                _copy_into_place(cached_media, output)
                write_manifest(manifest_target, {**hit_payload, "media": output.name})
                return SceneRenderResult(
                    scene_id=plan.scene_id,
                    path=output,
                    manifest_path=manifest_target,
                    total_frames=plan.total_frames,
                    duration_seconds=plan.duration_seconds,
                    cache_key=key,
                    cache_hit=True,
                )

        if self.temp_root:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        descriptor, staged_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=output.suffix,
            dir=str(output.parent),
        )
        os.close(descriptor)
        staged = Path(staged_name)
        base_payload = self._manifest_payload(
            plan, shot_keys, key, selected,
            width=expected_size[0], height=expected_size[1],
        )
        try:
            run_media_process(
                build_scene_command(
                    self.binaries, plan, ordered_paths, staged, selected,
                ),
                timeout=max(120.0, plan.total_frames / plan.fps * 20 + 60),
                job_id=job_id,
            )
            self._quality_check(staged, plan, expected_size)
            media_digest = sha256_file(staged)
            media_size = staged.stat().st_size
            fsync_file(staged)

            if self.cache_root is not None:
                # Publish into the cache first (media, then its manifest), so
                # the artifact is vouched for before anything consumes it;
                # each location's manifest names its own media file.
                scope = self._scope_directory(plan.scene_id, key)
                scope.mkdir(parents=True, exist_ok=True)
                cached_media = scope / "rendered.mp4"
                os.replace(staged, cached_media)
                write_manifest(scope / "manifest.json", {
                    **base_payload,
                    "media": cached_media.name,
                    "media_sha256": media_digest,
                    "media_bytes": media_size,
                })
                _copy_into_place(cached_media, output)
                published_payload = {
                    **base_payload,
                    "media": output.name,
                    "media_sha256": media_digest,
                    "media_bytes": media_size,
                }
            else:
                os.replace(staged, output)
                published_payload = {
                    **base_payload,
                    "media": output.name,
                    "media_sha256": media_digest,
                    "media_bytes": media_size,
                }
        finally:
            staged.unlink(missing_ok=True)
        write_manifest(manifest_target, published_payload)
        return SceneRenderResult(
            scene_id=plan.scene_id,
            path=output,
            manifest_path=manifest_target,
            total_frames=plan.total_frames,
            duration_seconds=plan.duration_seconds,
            cache_key=key,
            cache_hit=False,
        )

    def _validated_intermediates(
        self,
        plan: SceneRenderPlan,
        intermediates: dict[str, Path],
    ) -> list[Path]:
        missing = [
            shot.shot_id
            for shot in plan.shots
            if shot.shot_id not in intermediates
        ]
        if missing:
            raise SceneInputError(f"missing normalized intermediates for shots {missing}")
        ordered_paths: list[Path] = []
        for shot, expected_frames in zip(plan.shots, plan.frame_counts, strict=True):
            path = Path(intermediates[shot.shot_id])
            actual = count_video_frames(path, self.binaries)
            if actual != expected_frames:
                raise SceneInputError(
                    f"intermediate for shot {shot.shot_id!r} has {actual} frames; "
                    f"the plan needs exactly {expected_frames}"
                )
            ordered_paths.append(path)
        return ordered_paths

    def _intermediate_geometry(self, first_intermediate: Path) -> tuple[int, int]:
        """Expected canvas for the scene render, taken from the inputs.

        Intermediates were themselves validated against the project canvas,
        fps, silence, duration, and frame count by the normalizer; here they
        are checked for frame-count agreement with the plan and used as the
        resolution baseline the final render must reproduce.
        """
        try:
            info = probe_media(first_intermediate, self.binaries)
        except (OSError, ValueError) as exc:
            raise SceneInputError(f"cannot inspect intermediate: {exc}") from exc
        if info.width is None or info.height is None or not info.has_video:
            raise SceneInputError("first intermediate has no usable video stream")
        return info.width, info.height


    def _manifest_payload(
        self,
        plan: SceneRenderPlan,
        shot_keys: dict[str, str],
        key: str,
        options: SceneEncodeOptions,
        *,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        return {
            "scene_id": plan.scene_id,
            "cache_key": key,
            "renderer_version": self.renderer_version,
            "workflow": SCENE_ASSEMBLY_WORKFLOW,
            "ffmpeg": ffmpeg_identity(self.binaries),
            "fps": plan.fps,
            "width": width,
            "height": height,
            "total_frames": plan.total_frames,
            "duration_seconds": plan.duration_seconds,
            "encode": {
                "codec": options.video_codec,
                "preset": options.video_preset,
                "crf": options.crf,
            },
            "shots": [
                {
                    "shot_id": shot.shot_id,
                    "frames": frames,
                    "adjusted": shot.adjusted,
                    "start_seconds": shot.start_seconds,
                    "cache_key": shot_keys.get(shot.shot_id),
                }
                for shot, frames in zip(plan.shots, plan.frame_counts, strict=True)
            ],
            "boundaries": [
                {
                    "position": boundary.position,
                    "kind": boundary.kind,
                    "offset_seconds": boundary.offset_seconds,
                    "offset_frames": boundary.offset_frames,
                    "overlap_frames": boundary.overlap_frames,
                }
                for boundary in plan.boundaries
            ],
        }

    def _quality_check(
        self,
        staged: Path,
        plan: SceneRenderPlan,
        expected_size: tuple[int, int],
    ) -> None:
        """Gate publication on full probe/QC of the staged scene render."""
        try:
            info = probe_media(staged, self.binaries)
        except (OSError, ValueError) as exc:
            raise SceneQCError(f"scene {plan.scene_id} render is unreadable: {exc}") from exc
        if not info.has_video:
            raise SceneQCError(f"scene {plan.scene_id} render has no video stream")
        if info.has_audio:
            raise SceneQCError(f"scene {plan.scene_id} render must be silent")
        if (info.width, info.height) != expected_size:
            raise SceneQCError(
                f"scene {plan.scene_id} render is "
                f"{info.width}x{info.height}; expected {expected_size[0]}x{expected_size[1]}"
            )
        fps_tolerance = max(0.05, plan.fps * 0.01)
        if info.fps is None or abs(info.fps - plan.fps) > fps_tolerance:
            raise SceneQCError(
                f"scene {plan.scene_id} render runs at {info.fps!r} fps; "
                f"expected {plan.fps}"
            )
        duration_tolerance = max(2 / plan.fps, 0.05)
        expected_duration = plan.total_frames / plan.fps
        if (
            info.duration_seconds is None
            or abs(info.duration_seconds - expected_duration) > duration_tolerance
        ):
            raise SceneQCError(
                f"scene {plan.scene_id} render duration {info.duration_seconds}s "
                f"is not within {duration_tolerance:.3f}s of {expected_duration:.6f}s"
            )
        actual_frames = count_video_frames(staged, self.binaries)
        if actual_frames != plan.total_frames:
            raise SceneQCError(
                f"scene {plan.scene_id} render has {actual_frames} frames; "
                f"expected exactly {plan.total_frames}"
            )
