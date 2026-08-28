"""Shot-scope generation request builder coverage (deterministic, no GPU).

Krea 2 / Qwen-Image-2512 / H3 builders must produce GenerationRequest
payloads with typed reference-asset hashes folded into the cache key payload,
canvas rules matching the scene-scope pipeline, and clear failures for
unsupported references and policy violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.h3_policy import (
    CONTINUATION_WORKFLOW_VERSION,
    FIRST_SHOT_WORKFLOW_VERSION,
    H3PolicyError,
)
from backend.models.shot_requests import (
    ResolvedReference,
    ShotGenerationPlan,
    ShotRequestError,
    build_h3_request,
    build_krea2_request,
    build_qwen_image_request,
    build_shot_request,
    krea2_canvas,
    qwen_image_canvas,
    resolve_reference_assets,
)
from backend.schemas.models import Asset, AssetType, Project, ProjectCreate, VisualType
from backend.schemas.shots import (
    ReferenceRole,
    Shot,
    ShotLane,
    ShotReference,
)

BACKEND_IDENTITY = {
    "model": "Krea 2 Turbo",
    "model_version": "open-v1.0",
    "quantization": "fp8_scaled",
}


def make_project(**overrides: object) -> Project:
    fields = {
        "id": "proj-1",
        "title": "Mars",
        "topic": "documentary",
        "target_duration": 600.0,
        "resolution": (1920, 1080),
        "fps": 24,
        "slug": "mars",
    }
    fields.update(overrides)
    return Project(**fields)


def make_shot(**overrides: object) -> Shot:
    fields = {
        "project_id": "proj-1",
        "scene_id": "scene-1",
        "index": 0,
        "duration_seconds": 5.0,
        "lane": ShotLane.IMAGE,
        "visual_type": VisualType.KREA2_STILL,
        "visual_prompt": "a rust-red canyon",
        "negative_prompt": "blurry",
        "seed": 42,
    }
    fields.update(overrides)
    return Shot(**fields)


def make_reference(
    role: ReferenceRole = ReferenceRole.FIRST_FRAME,
    asset_id: str = "asset-1",
    *,
    path: Path | None = None,
) -> ResolvedReference:
    return ResolvedReference(
        role=role,
        asset_id=asset_id,
        path=path or Path("/tmp/kilo/unused.png"),
        sha256="a" * 64,
    )


# ---------------------------------------------------------------------------
# Canvas rules


def test_krea2_canvas_follows_project_aspect_and_overrides() -> None:
    assert krea2_canvas({}, (1920, 1080)) == (1344, 768)
    assert krea2_canvas({}, (1080, 1920)) == (768, 1344)
    assert krea2_canvas({}, (1024, 1024)) == (1024, 1024)
    assert krea2_canvas({"krea_canvas": "1024x640"}, (1920, 1080)) == (1024, 640)
    with pytest.raises(ShotRequestError, match="WIDTHxHEIGHT"):
        krea2_canvas({"krea_canvas": "big"}, (1920, 1080))
    with pytest.raises(ShotRequestError, match="16 px"):
        krea2_canvas({"krea_canvas": "1001x640"}, (1920, 1080))
    with pytest.raises(ShotRequestError, match="megapixel"):
        krea2_canvas({"krea_canvas": "2048x1408"}, (1920, 1080))


def test_qwen_canvas_follows_project_aspect_and_overrides() -> None:
    assert qwen_image_canvas({}, (1920, 1080)) == (1664, 928)
    assert qwen_image_canvas({}, (1080, 1920)) == (928, 1664)
    assert qwen_image_canvas({"qwen_image_canvas": "1280x720"}, (1920, 1080)) == (1280, 720)
    with pytest.raises(ShotRequestError, match="megapixel"):
        qwen_image_canvas({"qwen_image_canvas": "1600x1200"}, (1920, 1080))


# ---------------------------------------------------------------------------
# Krea 2


def test_krea2_request_carries_shot_identity_and_sampler_recipe() -> None:
    shot = make_shot()
    plan = build_krea2_request(
        shot, make_project(), Path("out"), backend_identity=BACKEND_IDENTITY,
    )
    request = plan.request
    assert request.prompt == "a rust-red canyon"
    assert request.negative_prompt == "blurry"
    assert request.seed == 42
    assert (request.width, request.height) == (1344, 768)
    assert request.job_id.startswith("proj-1:shot-krea2:")
    assert shot.id in request.job_id
    assert request.settings["shot_id"] == shot.id
    assert plan.cache_payload["workflow_version"] == "krea2-turbo-fp8-v1"
    assert plan.cache_payload["cfg"] == 1.0
    assert plan.cache_payload["references"] == []
    assert plan.provenance["shot_id"] == shot.id


def test_krea2_rejects_typed_references_instead_of_dropping_them() -> None:
    shot = make_shot(reference_assets=[
        ShotReference(role=ReferenceRole.COMPOSITION, asset_id="asset-1"),
    ])
    references = (make_reference(ReferenceRole.COMPOSITION),)
    with pytest.raises(ShotRequestError, match="cannot condition on") as excinfo:
        build_krea2_request(
            shot, make_project(), Path("out"),
            backend_identity=BACKEND_IDENTITY, references=references,
        )
    assert excinfo.value.code == "reference_role_unsupported"


# ---------------------------------------------------------------------------
# Qwen-Image-2512


def test_qwen_request_appends_exact_on_screen_text() -> None:
    shot = make_shot(
        visual_type=VisualType.QWEN_IMAGE_STILL,
        settings={"on_screen_text": ["MARS 2026", "  "]},
    )
    plan = build_qwen_image_request(
        shot, make_project(), Path("out"),
        backend_identity={"model": "Qwen-Image-2512", "model_version": "2512"},
    )
    assert '"MARS 2026"' in plan.request.prompt
    assert plan.cache_payload["on_screen_text"] == ["MARS 2026"]
    # Qwen honors a negative prompt; it must reach the cache key.
    assert plan.cache_payload["negative_prompt"] == "blurry"


def test_qwen_negative_prompt_changes_cache_key() -> None:
    project = make_project()
    first = build_qwen_image_request(
        make_shot(visual_type=VisualType.QWEN_IMAGE_STILL),
        project, Path("out"), backend_identity={},
    )
    second = build_qwen_image_request(
        make_shot(visual_type=VisualType.QWEN_IMAGE_STILL, negative_prompt="text"),
        project, Path("out"), backend_identity={},
    )
    assert first.cache_payload["negative_prompt"] != second.cache_payload["negative_prompt"]
    assert first.cache_payload != second.cache_payload


# ---------------------------------------------------------------------------
# H3


def test_h3_first_shot_uses_unconditioned_workflow_and_frame_grid() -> None:
    shot = make_shot(
        lane=ShotLane.H3,
        visual_type=VisualType.H3_AUDIOVISUAL,
        settings={"h3_quality": "standard"},
    )
    plan = build_h3_request(
        shot, make_project(), Path("out"),
        backend_identity={"model": "H3-Base"},
    )
    assert plan.request.duration_seconds == 5.0
    assert (plan.request.width, plan.request.height) == (1024, 576)
    assert plan.request.references == ()
    assert plan.request.settings["substitutions"] == {"length": 124}
    assert plan.request.settings["workflow_version"] == FIRST_SHOT_WORKFLOW_VERSION
    assert plan.provenance["effective_frames"] == 124
    assert plan.provenance["native_audio_mix_policy"] == "mute"


def test_h3_continuity_reference_switches_to_conditioned_workflow() -> None:
    shot = make_shot(
        lane=ShotLane.H3,
        visual_type=VisualType.H3_AUDIOVISUAL,
        settings={
            "h3_quality": "standard",
            "h3_continuity": {
                "enabled": True,
                "group": "hero",
                "predecessor_shot_id": "pred-shot",
            },
        },
    )
    reference = make_reference(ReferenceRole.CONTINUITY, asset_id="asset-pred")
    plan = build_h3_request(
        shot, make_project(), Path("out"),
        backend_identity={}, references=(reference,),
    )
    assert plan.request.references == (reference.path,)
    assert plan.request.settings["workflow_version"] == CONTINUATION_WORKFLOW_VERSION
    entry = plan.cache_payload["references"][0]
    assert entry == {"role": "continuity", "asset_id": "asset-pred", "sha256": "a" * 64}


def test_h3_rejects_multiple_first_frame_references() -> None:
    shot = make_shot(lane=ShotLane.H3, visual_type=VisualType.H3_AUDIOVISUAL)
    references = (
        make_reference(ReferenceRole.FIRST_FRAME, "asset-a"),
        make_reference(ReferenceRole.CONTINUITY, "asset-b"),
    )
    with pytest.raises(ShotRequestError, match="at most one"):
        build_h3_request(
            shot, make_project(), Path("out"),
            backend_identity={}, references=references,
        )


def test_h3_rejects_unsupported_reference_roles() -> None:
    shot = make_shot(lane=ShotLane.H3, visual_type=VisualType.H3_AUDIOVISUAL)
    with pytest.raises(ShotRequestError, match="style") as excinfo:
        build_h3_request(
            shot, make_project(), Path("out"),
            backend_identity={},
            references=(make_reference(ReferenceRole.STYLE),),
        )
    assert excinfo.value.code == "reference_role_unsupported"


def test_h3_enforces_preset_duration_cap() -> None:
    shot = make_shot(
        lane=ShotLane.H3,
        visual_type=VisualType.H3_AUDIOVISUAL,
        duration_seconds=12.0,
        settings={"h3_quality": "standard"},
    )
    with pytest.raises(H3PolicyError):
        build_h3_request(
            shot, make_project(), Path("out"), backend_identity={},
        )


def test_h3_cache_key_tracks_reference_hash_changes() -> None:
    from backend.storage.generation_cache import GenerationCache

    def payload_for(digest: str) -> dict:
        shot = make_shot(
            lane=ShotLane.H3,
            visual_type=VisualType.H3_AUDIOVISUAL,
            settings={"h3_quality": "standard"},
        )
        plan = build_h3_request(
            shot, make_project(), Path("out"),
            backend_identity={},
            references=(
                ResolvedReference(
                    role=ReferenceRole.FIRST_FRAME,
                    asset_id="asset-1",
                    path=Path("/tmp/kilo/frame.png"),
                    sha256=digest,
                ),
            ),
        )
        return plan.cache_payload

    first_key = GenerationCache.key_hash(payload_for("a" * 64))
    second_key = GenerationCache.key_hash(payload_for("b" * 64))
    assert first_key != second_key
    # Identical inputs produce identical keys.
    assert first_key == GenerationCache.key_hash(payload_for("a" * 64))


# ---------------------------------------------------------------------------
# Typed reference resolution


def _write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_resolve_reference_assets_hashes_contained_files(tmp_path: Path) -> None:
    project_root = tmp_path / "projects" / "proj"
    media = _write_bytes(project_root / "assets" / "key.png", b"png-bytes")
    asset = Asset(
        project_id="proj-1", scene_id="scene-1", shot_id="other-shot",
        type=AssetType.IMAGE, filepath=Path("assets/key.png"),
        backend="mock", model="m", seed=1,
    )
    shot = make_shot(reference_assets=[
        ShotReference(role=ReferenceRole.FIRST_FRAME, asset_id=asset.id),
    ])
    resolved = resolve_reference_assets(shot, {asset.id: asset}, project_root)
    assert len(resolved) == 1
    assert resolved[0].path == media.resolve()
    assert len(resolved[0].sha256) == 64


def test_resolve_reference_assets_rejects_unknown_asset(tmp_path: Path) -> None:
    shot = make_shot(reference_assets=[
        ShotReference(role=ReferenceRole.CHARACTER, asset_id="ghost"),
    ])
    with pytest.raises(ShotRequestError, match="not an asset") as excinfo:
        resolve_reference_assets(shot, {}, tmp_path)
    assert excinfo.value.code == "reference_unknown_asset"


def test_resolve_reference_assets_rejects_files_outside_project(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "projects" / "proj"
    # The Asset schema itself forbids ".." paths; simulate a stale index row
    # that predates validation via model_construct so the resolver's
    # containment check is what catches the escape.
    asset = Asset.model_construct(
        id="asset-esc", project_id="proj-1", scene_id="scene-1",
        type=AssetType.IMAGE, filepath=Path("../../outside.png"),
        backend="mock", model="m", seed=1, settings={},
    )
    _write_bytes(tmp_path / "outside.png", b"escaped")
    shot = make_shot(reference_assets=[
        ShotReference(role=ReferenceRole.SOURCE_EVIDENCE, asset_id=asset.id),
    ])
    with pytest.raises(ShotRequestError, match="escapes the project") as excinfo:
        resolve_reference_assets(shot, {asset.id: asset}, project_root)
    assert excinfo.value.code == "reference_outside_project"


def test_resolve_reference_assets_requires_readable_media(tmp_path: Path) -> None:
    project_root = tmp_path / "projects" / "proj"
    asset = Asset(
        project_id="proj-1", scene_id="scene-1",
        type=AssetType.IMAGE, filepath=Path("assets/missing.png"),
        backend="mock", model="m", seed=1,
    )
    shot = make_shot(reference_assets=[
        ShotReference(role=ReferenceRole.CONTINUITY, asset_id=asset.id),
    ])
    with pytest.raises(ShotRequestError, match="no readable media") as excinfo:
        resolve_reference_assets(shot, {asset.id: asset}, project_root)
    assert excinfo.value.code == "reference_missing_media"


# ---------------------------------------------------------------------------
# Generic dispatch


def test_build_shot_request_dispatches_by_visual_type() -> None:
    plan = build_shot_request(
        make_shot(visual_type=VisualType.IMAGE_MOTION),
        make_project(), Path("out"), backend_identity={},
    )
    assert isinstance(plan, ShotGenerationPlan)
    assert plan.cache_payload["kind"] == "krea2_image"


def test_build_shot_request_fails_for_unbuilt_visual_types() -> None:
    from backend.models.lane_resolver import LaneErrorCode, LaneResolutionError

    with pytest.raises(LaneResolutionError) as excinfo:
        build_shot_request(
            make_shot(lane=ShotLane.HTML, visual_type=VisualType.TITLE_CARD),
            make_project(), Path("out"), backend_identity={},
        )
    assert excinfo.value.code == LaneErrorCode.UNRESOLVED_VISUAL_TYPE.value
