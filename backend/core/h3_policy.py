"""MiniMax H3 quality presets, duration policy, and frame-grid helpers.

This is the single Python policy source; the Scene Editor mirrors it through
the `/api/h3/policy` endpoint. Do not duplicate preset truth in JavaScript.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class H3Quality(StrEnum):
    FAST_SAFE = "fast_safe"
    STANDARD = "standard"
    HIGH = "high"
    CUSTOM = "custom"


H3_FPS = 24
H3_FRAME_GRID_STEP = 17
H3_FRAME_GRID_OFFSET = 5
H3_MAX_DURATION_SECONDS = 20.0
H3_MIN_DURATION_SECONDS = 1.0

LAST_FRAME_EXTRACTOR_VERSION = "last-frame-v2"
CONTINUATION_WORKFLOW_VERSION = "minimax-h3-av-first-frame-v1"
FIRST_SHOT_WORKFLOW_VERSION = "minimax-h3-av-v1"


@dataclass(frozen=True, slots=True)
class H3Preset:
    preset: str
    label: str
    landscape_canvas: tuple[int, int] | None  # None => auto-canvas rule
    portrait_canvas: tuple[int, int] | None
    normal_min_seconds: float
    normal_max_seconds: float
    max_seconds: float
    long_shot_allowed: bool
    evidence: str  # human-readable evidence label
    description: str
    warning: str | None  # advisory for this machine


H3_PRESETS: dict[str, H3Preset] = {
    H3Quality.FAST_SAFE: H3Preset(
        preset=H3Quality.FAST_SAFE,
        label="Fast / Safe",
        landscape_canvas=(896, 512),
        portrait_canvas=(512, 896),
        normal_min_seconds=5.0,
        normal_max_seconds=8.0,
        max_seconds=8.0,
        long_shot_allowed=True,
        evidence="20 s observed on this machine (2026-08-20); 5–8 s is the normal target",
        description="Compact, most VRAM headroom; 20 s is user-validated.",
        warning="20 s is observed only at 896x512 / 512x896. Other canvases have not been validated at that length.",
    ),
    H3Quality.STANDARD: H3Preset(
        preset=H3Quality.STANDARD,
        label="Standard",
        landscape_canvas=(1024, 576),
        portrait_canvas=(576, 1024),
        normal_min_seconds=5.0,
        normal_max_seconds=8.0,
        max_seconds=8.0,
        long_shot_allowed=False,
        evidence="5 s validated (Auto/1344x768); 1024x576 cap requires live validation",
        description="Normal YouTube shot; use Fast / Safe for long shots.",
        warning="8 s at 1024x576 is a recommended target, not a live-validated guarantee.",
    ),
    H3Quality.HIGH: H3Preset(
        preset=H3Quality.HIGH,
        label="High",
        landscape_canvas=None,
        portrait_canvas=None,
        normal_min_seconds=5.0,
        normal_max_seconds=5.0,
        max_seconds=5.0,
        long_shot_allowed=False,
        evidence="5 s observed on this machine (Auto/1344x768); 20 s at 896x512 is Fast / Safe only",
        description="Highest native canvas validated at 5 s; long shots must use Fast / Safe.",
        warning=None,
    ),
    H3Quality.CUSTOM: H3Preset(
        preset=H3Quality.CUSTOM,
        label="Custom",
        landscape_canvas=None,
        portrait_canvas=None,
        normal_min_seconds=1.0,
        normal_max_seconds=20.0,
        max_seconds=20.0,
        long_shot_allowed=False,
        evidence="Advanced/manual override; fit is not promised",
        description="User-supplied canvas and duration; no guarantee of fit.",
        warning="Custom canvases are not validated against this machine's observed limits.",
    ),
}


def auto_canvas(width: int, height: int) -> tuple[int, int]:
    base = 768
    cap = base * 1344
    if width >= height:
        nominal_w, nominal_h = base * width / height, base
    else:
        nominal_w, nominal_h = base, base * height / width
    if nominal_w * nominal_h > cap:
        scale = (cap / (nominal_w * nominal_h)) ** 0.5
        nominal_w, nominal_h = nominal_w * scale, nominal_h * scale
    w = max(32, round(nominal_w / 32) * 32)
    h = max(32, round(nominal_h / 32) * 32)
    return (int(w), int(h))


class H3PolicyError(ValueError):
    def __init__(self, message: str, code: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True, slots=True)
class H3Resolution:
    quality: str
    preset: H3Preset
    canvas: tuple[int, int]
    long_shot: bool
    label: str
    max_seconds: float


def parse_canvas_override(value: str) -> tuple[int, int]:
    try:
        parts = value.lower().split("x", 1)
        w = int(parts[0].strip())
        h = int(parts[1].strip())
    except Exception as exc:
        raise H3PolicyError(
            f"Canvas override {value!r} is not a 'WIDTHxHEIGHT' pair.",
            "invalid_canvas",
        ) from exc
    if min(w, h) < 256:
        raise H3PolicyError(
            f"Canvas override {value!r}: must be at least 256 px per side.",
            "invalid_canvas",
        )
    if w % 32 != 0 or h % 32 != 0:
        raise H3PolicyError(
            f"Canvas override {value!r}: must be aligned to 32 px.",
            "invalid_canvas",
        )
    return (w, h)


def resolve_quality(
    settings: dict,
    project_resolution: tuple[int, int] = (1920, 1080),
) -> H3Resolution:
    canvas_raw = settings.get("h3_canvas")
    quality_raw = settings.get("h3_quality")
    long_shot = bool(settings.get("h3_long_shot", False))

    if isinstance(canvas_raw, str) and canvas_raw.strip().lower() != "auto":
        canvas = parse_canvas_override(str(canvas_raw).strip())
        preset = H3_PRESETS[H3Quality.CUSTOM]
        # Custom resolution carries the parsed explicit canvas
        return H3Resolution(
            quality=H3Quality.CUSTOM,
            preset=preset,
            canvas=canvas,
            long_shot=long_shot,
            label=f"Custom — {canvas[0]}x{canvas[1]}",
            max_seconds=20.0,
        )

    preset_id = H3Quality.CUSTOM
    if quality_raw is not None:
        quality_str = str(quality_raw).strip()
        if quality_str in H3_PRESETS:
            preset_id = quality_str
        else:
            raise H3PolicyError(
                f"Unknown H3 quality preset {quality_str!r}.",
                "invalid_preset",
                details={"allowed": list(H3_PRESETS)},
            )
    else:
        # Legacy scenes without h3_quality: treat as auto / high
        preset_id = H3Quality.HIGH

    preset = H3_PRESETS[preset_id]
    if preset_id == H3Quality.CUSTOM:
        raise H3PolicyError(
            "Custom H3 quality requires an explicit 32-pixel-aligned canvas override.",
            "custom_canvas_required",
        )
    landscape = project_resolution[0] >= project_resolution[1]
    if preset.landscape_canvas is None:
        canvas = auto_canvas(*project_resolution)
    else:
        canvas = preset.landscape_canvas if landscape else preset.portrait_canvas
    return H3Resolution(
        quality=preset_id,
        preset=preset,
        canvas=canvas,
        long_shot=long_shot,
        label=preset.label,
        max_seconds=preset.max_seconds,
    )


def h3_frame_count(seconds: float) -> int:
    frames = max(5, int(round(seconds * H3_FPS)))
    while frames % H3_FRAME_GRID_STEP != H3_FRAME_GRID_OFFSET:
        frames += 1
    return frames


def h3_effective_duration(frames: int) -> float:
    return frames / H3_FPS


def validate_duration(
    resolution: H3Resolution, seconds: float
) -> None:
    if seconds < H3_MIN_DURATION_SECONDS:
        raise H3PolicyError(
            f"Duration {seconds}s is below the minimum {H3_MIN_DURATION_SECONDS}s.",
            "duration_too_short",
            details={"requested": seconds, "minimum": H3_MIN_DURATION_SECONDS},
        )
    if seconds > H3_MAX_DURATION_SECONDS:
        raise H3PolicyError(
            f"Duration {seconds}s exceeds the global 20 s ceiling.",
            "duration_exceeds_maximum",
            details={"requested": seconds, "maximum": H3_MAX_DURATION_SECONDS},
        )
    preset = resolution.preset
    max_s = preset.max_seconds
    if resolution.long_shot:
        if not preset.long_shot_allowed:
            raise H3PolicyError(
                "Long-shot mode is only allowed with Fast / Safe (896x512 / 512x896).",
                "long_shot_invalid_preset",
                details={
                    "preset": resolution.quality,
                    "allowed_for_long_shot": [k for k, p in H3_PRESETS.items() if p.long_shot_allowed],
                },
            )
        max_s = H3_MAX_DURATION_SECONDS
    if seconds > max_s:
        raise H3PolicyError(
            f"Duration {seconds}s exceeds the preset cap of {max_s:g} s for {preset.label}. "
            f"Use Fast / Safe for shots above {max_s:g} s, or reduce duration.",
            "duration_out_of_range",
            details={
                "requested": seconds,
                "preset": resolution.quality,
                "preset_max": max_s,
                "preset_normal_max": preset.normal_max_seconds,
                "preset_normal_min": preset.normal_min_seconds,
            },
        )


@dataclass(frozen=True, slots=True)
class H3ContinuityBlock:
    enabled: bool = False
    group: str = ""
    predecessor_scene_id: str | None = None


def parse_continuity(settings: dict) -> H3ContinuityBlock:
    continuity = settings.get("h3_continuity")
    if continuity is None:
        return H3ContinuityBlock()
    if isinstance(continuity, bool) and not continuity:
        return H3ContinuityBlock()
    if not isinstance(continuity, dict):
        raise H3PolicyError(
            "H3 continuity must be an object or false.",
            "invalid_continuity",
        )
    block = dict(continuity)
    enabled = block.get("enabled", False)
    if not isinstance(enabled, bool):
        raise H3PolicyError(
            "H3 continuity enabled must be true or false.",
            "invalid_continuity",
        )
    if not enabled:
        return H3ContinuityBlock()
    group_raw = block.get("group", "")
    if group_raw is not None and not isinstance(group_raw, str):
        raise H3PolicyError(
            "H3 continuity group must be text.",
            "invalid_continuity",
        )
    group = str(group_raw or "").strip()
    pred = block.get("predecessor_scene_id")
    if pred is not None and not isinstance(pred, str):
        raise H3PolicyError(
            "H3 continuity predecessor_scene_id must be text or null.",
            "invalid_continuity",
        )
    if pred is not None and pred.strip() == "":
        pred = None
    else:
        pred = pred.strip() if pred is not None else None
    return H3ContinuityBlock(enabled=enabled, group=group, predecessor_scene_id=pred)


# Policy payload used by the API endpoint so the UI never duplicates truth.

def h3_policy_payload() -> dict:
    return {
        "fps": H3_FPS,
        "frame_grid_step": H3_FRAME_GRID_STEP,
        "frame_grid_offset": H3_FRAME_GRID_OFFSET,
        "min_seconds": H3_MIN_DURATION_SECONDS,
        "max_seconds": H3_MAX_DURATION_SECONDS,
        "long_shot_max_seconds": H3_MAX_DURATION_SECONDS,
        "continuation_workflow_version": CONTINUATION_WORKFLOW_VERSION,
        "first_shot_workflow_version": FIRST_SHOT_WORKFLOW_VERSION,
        "extractor_version": LAST_FRAME_EXTRACTOR_VERSION,
        "presets": {
            preset_id: {
                "label": p.label,
                "landscape_canvas": list(p.landscape_canvas) if p.landscape_canvas else None,
                "portrait_canvas": list(p.portrait_canvas) if p.portrait_canvas else None,
                "normal_min_seconds": p.normal_min_seconds,
                "normal_max_seconds": p.normal_max_seconds,
                "max_seconds": p.max_seconds,
                "long_shot_allowed": p.long_shot_allowed,
                "evidence": p.evidence,
                "description": p.description,
                "warning": p.warning,
            }
            for preset_id, p in H3_PRESETS.items()
        },
    }
