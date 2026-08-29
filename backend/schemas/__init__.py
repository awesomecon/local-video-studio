"""Public domain schemas for Local Video Studio."""

from .h3_continuity import (
    H3ContinuityBlock,
    H3ContinuityError,
    CONTINUITY_STATUSES,
    h3_continuity_status,
    validate_continuity_graph,
)
from .models import (
    AspectRatio, Asset, AssetType, DurationMode, GenerationAttempt, GenerationJob,
    JobStatus, PROJECT_SCHEMA_VERSION, Project, ProjectCreate, ProjectPlan,
    ProjectStatus, Scene, SceneStatus, VideoMode, VisualType, utc_now,
)
from .shots import (
    AudioCue, AudioCueKind, AudioMixPolicy, CompiledShot, IMPLICIT_SHOT_SUFFIX,
    MAX_SHOT_SECONDS, MediaSource, OverlayAnchor, OverlayCue, OverlayFit, OverlayKind,
    ReferenceRole, Shot, ShotLane, ShotReference, ShotStartMode, ShotStatus,
    ShotTimingError, ShotTransition, ShotTransitionKind, SourceClassification,
    compile_shot_timings, default_lane_for_visual_type, effective_shots,
    implicit_shot_from_scene, implicit_shot_id, scene_rendered_duration,
    validate_shot_sequence,
)
from .thumbnails import (
    ThumbnailCandidate, ThumbnailCandidateId, ThumbnailCandidateRequest,
    ThumbnailConcept, ThumbnailFontPreset, ThumbnailImageModel, ThumbnailLayoutPreset,
    ThumbnailPalette, ThumbnailPlan, ThumbnailSelection, ThumbnailSide,
    ThumbnailSubjectPosition, ThumbnailTextLayout,
)

__all__ = [
    "AspectRatio", "Asset", "AssetType", "DurationMode", "GenerationAttempt",
    "GenerationJob", "JobStatus", "PROJECT_SCHEMA_VERSION", "Project", "ProjectCreate",
    "ProjectPlan", "ProjectStatus", "Scene", "SceneStatus", "VideoMode", "VisualType", "utc_now",
    "ThumbnailCandidate", "ThumbnailCandidateId", "ThumbnailCandidateRequest",
    "ThumbnailConcept", "ThumbnailFontPreset", "ThumbnailImageModel",
    "ThumbnailLayoutPreset", "ThumbnailPalette", "ThumbnailPlan", "ThumbnailSelection",
    "ThumbnailSide", "ThumbnailSubjectPosition", "ThumbnailTextLayout",
    "CONTINUITY_STATUSES", "H3ContinuityBlock", "H3ContinuityError",
    "h3_continuity_status", "validate_continuity_graph",
    "AudioCue", "AudioCueKind", "AudioMixPolicy", "CompiledShot",
    "IMPLICIT_SHOT_SUFFIX", "MAX_SHOT_SECONDS", "MediaSource", "OverlayAnchor",
    "OverlayCue", "OverlayFit", "OverlayKind", "ReferenceRole", "Shot", "ShotLane",
    "ShotReference", "ShotStartMode", "ShotStatus", "ShotTimingError",
    "ShotTransition", "ShotTransitionKind", "SourceClassification",
    "compile_shot_timings", "default_lane_for_visual_type", "effective_shots",
    "implicit_shot_from_scene", "implicit_shot_id", "scene_rendered_duration",
    "validate_shot_sequence",
]
