"""H3 continuity validation and stale-state semantics over scenes/assets."""

from __future__ import annotations

from typing import Any, Sequence

from backend.core.h3_policy import H3PolicyError, parse_continuity, H3ContinuityBlock


class H3ContinuityError(H3PolicyError):
    pass


CONTINUITY_STATUSES = frozenset({
    "ready",
    "source_missing",
    "source_stale",
    "canvas_mismatch",
    "disabled",
    "error",
})


def _visual_type() -> type:
    from backend.schemas import VisualType
    return VisualType


def validate_continuity_graph(
    scene,
    scenes: Sequence,
) -> H3ContinuityBlock:
    block = parse_continuity(scene.settings)
    if not block.enabled:
        return block
    if block.predecessor_scene_id == scene.id:
        raise H3ContinuityError(
            f"Scene {scene.index + 1} links to itself as its continuity predecessor.",
            "continuity_self_link",
        )
    scene_map = {s.id: s for s in scenes if s.project_id == scene.project_id}
    if block.predecessor_scene_id is None:
        return block
    if block.predecessor_scene_id not in scene_map:
        pred_name = f"scene id {block.predecessor_scene_id!r}"
        if any(s.id.startswith(block.predecessor_scene_id[:4]) for s in scenes):
            pred_name = f"missing {pred_name}"
        raise H3ContinuityError(
            f"Predecessor {pred_name} is not a scene in this project.",
            "continuity_missing_scene",
            details={
                "predecessor_scene_id": block.predecessor_scene_id,
                "available_scene_ids": [s.id for s in scenes],
            },
        )
    pred = scene_map[block.predecessor_scene_id]
    if pred.index >= scene.index:
        raise H3ContinuityError(
            f"Predecessor (index {pred.index + 1}) must come before this scene (index {scene.index + 1}).",
            "continuity_forward_link",
            details={"predecessor_index": pred.index, "current_index": scene.index},
        )
    VisualType = _visual_type()
    if pred.visual_type is not VisualType.H3_AUDIOVISUAL:
        raise H3ContinuityError(
            f"Predecessor scene must be H3 audiovisual (its type is {pred.visual_type.value}).",
            "continuity_not_h3",
            details={"predecessor_visual_type": pred.visual_type.value},
        )
    visited: set[str] = set()
    current_id: str | None = block.predecessor_scene_id
    while current_id is not None:
        if current_id in visited:
            raise H3ContinuityError(
                "Continuity chain contains a cycle.",
                "continuity_cycle",
            )
        visited.add(current_id)
        pred_scene = scene_map.get(current_id)
        if pred_scene is None:
            break
        pred_block = parse_continuity(pred_scene.settings)
        current_id = pred_block.predecessor_scene_id
    return block


def _resolve_canvas(settings: dict, project_resolution: tuple[int, int]) -> tuple[int, int]:
    from backend.core.h3_policy import resolve_quality
    resolution = resolve_quality(settings, project_resolution)
    return resolution.canvas


def h3_continuity_status(
    scene,
    scenes: Sequence,
    asset_summaries: dict[str, dict[str, Any]],
    project_resolution: tuple[int, int] = (1920, 1080),
) -> dict:
    block = parse_continuity(scene.settings)
    if not block.enabled:
        return {
            "enabled": False,
            "group": block.group or "",
            "predecessor_scene_id": block.predecessor_scene_id,
            "status": "disabled",
            "detail": "H3 continuity is disabled for this scene.",
        }

    try:
        validate_continuity_graph(scene, scenes)
    except H3PolicyError as exc:
        return {
            "enabled": True,
            "group": block.group or "",
            "predecessor_scene_id": block.predecessor_scene_id,
            "status": "error",
            "detail": f"Continuity is enabled but the graph is invalid: {exc}",
        }

    pred_id = block.predecessor_scene_id
    if pred_id is None:
        return {
            "enabled": True,
            "group": block.group or f"h3-chain-{scene.index + 1:03d}",
            "predecessor_scene_id": pred_id,
            "status": "ready",
            "detail": "First scene in continuity group; uses unconditioned workflow.",
        }

    pred_scene = next((s for s in scenes if s.id == pred_id), None)
    if pred_scene is None:
        return {
            "enabled": True,
            "group": block.group or "",
            "predecessor_scene_id": pred_id,
            "status": "source_missing",
            "detail": f"Predecessor scene {pred_id!r} is missing from this project.",
        }
    VisualType = _visual_type()
    if pred_scene.visual_type is not VisualType.H3_AUDIOVISUAL:
        return {
            "enabled": True,
            "group": block.group or "",
            "predecessor_scene_id": pred_id,
            "status": "source_missing",
            "detail": f"Predecessor scene is not H3 audiovisual (type: {pred_scene.visual_type.value}).",
        }

    try:
        pred_canvas = _resolve_canvas(pred_scene.settings, project_resolution)
        scene_canvas = _resolve_canvas(scene.settings, project_resolution)
        if pred_canvas != scene_canvas:
            return {
                "enabled": True,
                "group": block.group or "",
                "predecessor_scene_id": pred_id,
                "status": "canvas_mismatch",
                "detail": f"Predecessor canvas {pred_canvas[0]}x{pred_canvas[1]} does not match this scene's canvas {scene_canvas[0]}x{scene_canvas[1]}. Align presets or disable continuity.",
            }
    except H3PolicyError as exc:
        return {
            "enabled": True,
            "group": block.group or "",
            "predecessor_scene_id": pred_id,
            "status": "source_missing",
            "detail": f"Could not resolve predecessor canvas: {exc}",
        }

    pred_summary = asset_summaries.get(pred_scene.id) or {}
    pred_video_sha256 = pred_summary.get("hash")

    if pred_video_sha256 is None:
        return {
            "enabled": True,
            "group": block.group or "",
            "predecessor_scene_id": pred_id,
            "status": "source_missing",
            "detail": f"Predecessor scene {pred_scene.index + 1} ({pred_scene.title or 'untitled'}) has no current visual asset.",
        }

    scene_map = {item.id: item for item in scenes}

    def stale_chain(current_scene, visited: set[str]) -> bool:
        if current_scene.id in visited:
            return True
        visited.add(current_scene.id)
        current_block = parse_continuity(current_scene.settings)
        current_pred_id = current_block.predecessor_scene_id
        if not current_block.enabled or current_pred_id is None:
            return False
        current_pred = scene_map.get(current_pred_id)
        current_pred_summary = asset_summaries.get(current_pred_id) or {}
        current_summary = asset_summaries.get(current_scene.id) or {}
        current_settings = current_summary.get("settings") or {}
        recorded = current_settings.get("h3_continuity")
        if current_summary.get("id") is not None:
            if not isinstance(recorded, dict):
                return True
            if (
                recorded.get("predecessor_scene_id") != current_pred_id
                or recorded.get("predecessor_asset_id") != current_pred_summary.get("id")
                or recorded.get("predecessor_video_sha256") != current_pred_summary.get("hash")
            ):
                return True
        return current_pred is not None and stale_chain(current_pred, visited)

    if stale_chain(scene, set()):
        return {
            "enabled": True,
            "group": block.group or f"h3-chain-{pred_scene.index + 1:03d}",
            "predecessor_scene_id": pred_id,
            "status": "source_stale",
            "detail": "Continuity source changed. Regenerate this scene after any earlier stale scene in the chain.",
        }

    return {
        "enabled": True,
        "group": block.group or f"h3-chain-{pred_scene.index + 1:03d}",
        "predecessor_scene_id": pred_id,
        "status": "ready",
        "detail": f"Predecessor scene {pred_scene.index + 1} ({pred_scene.title or 'untitled'}) has a valid visual; continuity ready for generation.",
    }
