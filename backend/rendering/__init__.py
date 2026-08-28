"""Deterministic FFmpeg rendering and media inspection utilities."""

from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from .frames import (
    LAST_FRAME_EXTRACTOR_VERSION,
    build_last_frame_command,
    compute_sha256,
    extract_last_frame,
)
from .manifests import (
    RENDERER_VERSION,
    SCENE_ASSEMBLY_WORKFLOW,
    SHOT_NORMALIZATION_WORKFLOW,
    content_key,
    ffmpeg_identity,
)
from .renderer import FFmpegRenderer, RenderOptions
from .scenes import (
    SceneAssembler,
    SceneEncodeOptions,
    SceneInputError,
    SceneQCError,
    build_scene_command,
)
from .shots import (
    MEZZANINE_ENCODE,
    NormalizationInputs,
    NormalizedShot,
    ShotNormalizationError,
    ShotNormalizer,
    build_normalization_command,
)
from .overlays import ResolvedOverlay, resolve_placement, resolve_overlays

__all__ = [
    "FFmpegBinaries",
    "FFmpegRenderer",
    "RenderOptions",
    "LAST_FRAME_EXTRACTOR_VERSION",
    "RENDERER_VERSION",
    "SCENE_ASSEMBLY_WORKFLOW",
    "SHOT_NORMALIZATION_WORKFLOW",
    "MEZZANINE_ENCODE",
    "NormalizationInputs",
    "NormalizedShot",
    "ResolvedOverlay",
    "SceneAssembler",
    "SceneEncodeOptions",
    "SceneInputError",
    "SceneQCError",
    "ShotNormalizer",
    "ShotNormalizationError",
    "build_last_frame_command",
    "build_normalization_command",
    "build_scene_command",
    "content_key",
    "discover_binaries",
    "extract_last_frame",
    "ffmpeg_identity",
    "require_ffmpeg",
    "resolve_overlays",
    "resolve_placement",
]
