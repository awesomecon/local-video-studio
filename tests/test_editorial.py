from __future__ import annotations

import json
from html import escape

import pytest
from pydantic import ValidationError

from backend.editorial import (
    EditorialAsset, EditorialAssetType,
    EditorialComposition, EditorialElement, EditorialElementType, EditorialEvent,
    EditorialPlanner, EditorialTemplate, EditPlan, MotionPrimitive,
    build_project_mars_prototype,
    compile_edit_plan_html,
)
from backend.captions import CaptionWord
from backend.core import load_config
from backend.pipeline import PipelineService
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.probe import probe_media
from backend.rendering.process import run_media_process
from backend.schemas import Asset, AssetType, Project, ProjectPlan, Scene, VideoMode
from backend.schemas import ProjectCreate
from backend.tts.audio import wav_duration


def test_legacy_project_payload_defaults_to_classic_without_migration() -> None:
    payload = {
        "title": "Legacy", "topic": "Existing project", "target_duration": 30,
        "slug": "legacy",
    }
    project = Project.model_validate(payload)
    assert project.video_mode is VideoMode.CLASSIC
    assert "video_mode" not in payload


def test_editorial_project_round_trip() -> None:
    project = Project(
        title="Editorial", topic="Project Mars", target_duration=30,
        slug="editorial", video_mode=VideoMode.EDITORIAL,
    )
    assert Project.model_validate_json(project.model_dump_json()).video_mode is VideoMode.EDITORIAL


def test_prototype_is_valid_and_uses_only_approved_vocabulary() -> None:
    plan = build_project_mars_prototype()
    assert plan.duration == 14
    assert plan.width == 1080 and plan.height == 1920
    assert {event.action for event in plan.compositions[0].events} <= set(MotionPrimitive)
    assert [event.time for event in plan.compositions[0].events] == sorted(
        event.time for event in plan.compositions[0].events
    )


def test_edit_plan_rejects_unknown_motion_and_targets() -> None:
    base = {
        "project_id": "p", "compositions": [{
            "id": "c", "start": 0, "duration": 2, "template": "bigTextReveal",
            "elements": [{
                "id": "title", "type": "text", "text": "Exact", "role": "headline",
            }],
            "events": [{"time": 0, "action": "inventedGlitch", "target": "title"}],
        }],
    }
    with pytest.raises(ValidationError):
        EditPlan.model_validate(base)
    base["compositions"][0]["events"][0] = {
        "time": 0, "action": "fade", "target": "missing",
    }
    with pytest.raises(ValidationError, match="unknown target"):
        EditPlan.model_validate(base)


def test_edit_plan_requires_frame_valid_contiguous_compositions() -> None:
    first = EditorialComposition(
        id="first", start=0, duration=2, template=EditorialTemplate.BIG_TEXT_REVEAL,
        elements=[EditorialElement(
            id="first-title", type=EditorialElementType.TEXT,
            text="FIRST", role="headline",
        )],
    )
    gapped = EditorialComposition(
        id="gapped", start=3, duration=1, template=EditorialTemplate.BIG_TEXT_REVEAL,
        elements=[EditorialElement(
            id="gap-title", type=EditorialElementType.TEXT,
            text="GAP", role="headline",
        )],
    )
    with pytest.raises(ValidationError, match="must be contiguous"):
        EditPlan(project_id="p", fps=24, compositions=[first, gapped])
    too_short = first.model_copy(update={"duration": 0.001})
    with pytest.raises(ValidationError, match="at least one frame"):
        EditPlan(project_id="p", fps=24, compositions=[too_short])


def test_compiler_escapes_text_and_emits_seek_contract() -> None:
    plan = EditPlan(project_id="p", compositions=[EditorialComposition(
        id="c", start=0, duration=2, template=EditorialTemplate.ARCHIVE_CANVAS,
        elements=[
            EditorialElement(
                id="year", type=EditorialElementType.TEXT,
                text='</script><script>alert("bad")</script>', role="year",
            ),
        ],
        events=[EditorialEvent(time=0, action=MotionPrimitive.FADE, target="year")],
    )])
    html = compile_edit_plan_html(plan)
    assert "window.renderAt" in html
    assert "</script><script>alert" not in html
    assert "\\u003c/script\\u003e" in html
    # The JSON embedded in the source remains the validated plan, not authored code.
    assert json.loads(plan.model_dump_json())["compositions"][0]["events"][0]["action"] == "fade"


def test_compiler_can_hide_editorial_typography_without_hiding_caption_data() -> None:
    plan = build_project_mars_prototype().model_copy(update={
        "editorial_text_enabled": False,
        "captions_enabled": True,
    })

    html = compile_edit_plan_html(plan)

    assert '<body class="editorial-text-disabled portrait">' in html
    assert '"captions_enabled":true' in html


def test_archive_renderer_binds_roles_not_prototype_element_ids() -> None:
    plan = EditPlan(project_id="p", compositions=[EditorialComposition(
        id="c", start=0, duration=2, template=EditorialTemplate.ARCHIVE_CANVAS,
        assets=[EditorialAsset(
            id="registered-photo", type=EditorialAssetType.EXISTING_ASSET,
            asset_id="database-asset-id",
        )],
        elements=[
            EditorialElement(
                id="llm-authored-year-id", type=EditorialElementType.TEXT,
                text="1969", role="year",
            ),
            EditorialElement(
                id="llm-authored-photo-id", type=EditorialElementType.IMAGE,
                asset_id="registered-photo", role="archive-photo",
            ),
            EditorialElement(
                id="llm-authored-reveal-id", type=EditorialElementType.TEXT,
                text="MOON", role="reveal",
            ),
        ],
        events=[
            EditorialEvent(
                time=0, action=MotionPrimitive.FADE_UP,
                target="llm-authored-year-id",
            ),
        ],
    )])

    html = compile_edit_plan_html(
        plan,
        asset_url_resolver=lambda asset: f"/assets/{asset.asset_id}",
    )

    assert 'id="llm-authored-year-id"' in html
    assert 'id="llm-authored-photo-id"' in html
    assert 'id="llm-authored-reveal-id"' in html
    assert ">1969<" in html and ">MOON<" in html
    assert 'src="/assets/database-asset-id"' in html


def test_archive_template_rejects_unapproved_roles_and_generated_evidence() -> None:
    with pytest.raises(ValidationError, match="does not define element role"):
        EditorialComposition(
            id="c", start=0, duration=2,
            template=EditorialTemplate.ARCHIVE_CANVAS,
            elements=[EditorialElement(
                id="wild", type=EditorialElementType.TEXT,
                text="No arbitrary slots", role="invented-slot",
            )],
        )
    with pytest.raises(ValidationError, match="generated assets cannot"):
        EditorialAsset(
            id="fake-evidence", type=EditorialAssetType.GENERATED_IMAGE,
            evidence_class="evidence", locked=True,
        )
    with pytest.raises(ValidationError, match="must be locked"):
        EditorialAsset(
            id="unlocked-evidence", type=EditorialAssetType.HISTORICAL_PHOTO,
            evidence_class="evidence", locked=False,
        )


@pytest.mark.parametrize(("template", "elements"), [
    ("documentReveal", [
        {"id": "doc", "type": "document", "text": "Verified passage", "role": "document"},
        {"id": "mark", "type": "underline", "role": "passage-mark"},
    ]),
    ("comparisonCanvas", [
        {"id": "left", "type": "image", "asset_id": "left-asset", "role": "left-image"},
        {"id": "right", "type": "image", "asset_id": "right-asset", "role": "right-image"},
    ]),
    ("illustrationCanvas", [
        {"id": "hero", "type": "image", "asset_id": "hero-asset", "role": "illustration"},
    ]),
    ("bigTextReveal", [
        {"id": "headline", "type": "text", "text": "ELON", "role": "headline"},
    ]),
])
def test_template_slot_contracts_accept_only_renderer_owned_roles(
    template: str, elements: list[dict],
) -> None:
    asset_ids = sorted({item["asset_id"] for item in elements if item.get("asset_id")})
    assets = [
        {"id": asset_id, "type": "generated_image", "evidence_class": "illustration"}
        for asset_id in asset_ids
    ]
    composition = EditorialComposition.model_validate({
        "id": "contract", "start": 0, "duration": 4, "template": template,
        "assets": assets, "elements": elements,
    })
    assert composition.template.value == template
    broken = {**elements[0], "role": "invented-slot"}
    with pytest.raises(ValidationError, match="does not define element role"):
        EditorialComposition.model_validate({
            "id": "broken", "start": 0, "duration": 4, "template": template,
            "assets": assets, "elements": [broken, *elements[1:]],
        })


@pytest.mark.parametrize(("template", "elements", "missing"), [
    ("documentReveal", [], "document"),
    ("comparisonCanvas", [
        {"id": "left", "type": "image", "asset_id": "left-asset", "role": "left-image"},
    ], "right-image"),
    ("illustrationCanvas", [], "illustration"),
    ("bigTextReveal", [], "headline"),
])
def test_template_contracts_require_their_core_visual_slots(
    template: str, elements: list[dict], missing: str,
) -> None:
    assets = ([{"id": "left-asset", "type": "generated_image"}] if elements else [])
    with pytest.raises(ValidationError, match=missing):
        EditorialComposition.model_validate({
            "id": "missing", "start": 0, "duration": 4, "template": template,
            "assets": assets, "elements": elements,
        })


class _PlannerLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["validator"](self.payload)


def _editorial_script(project: Project) -> ProjectPlan:
    return ProjectPlan(
        project_id=project.id, title=project.title, outline=["Opening"],
        target_duration=14,
        scenes=[Scene(
            project_id=project.id, index=0, title="Opening", duration=14,
            narration="In 1949, one document imagined a government on Mars.",
        )],
    )


def _planner_payload(scene_id: str, *, asset_id: str | None = None) -> dict:
    assets = []
    elements = [
        {"id": "date", "type": "text", "text": "1949", "role": "year"},
        {"id": "name", "type": "text", "text": "ELON", "role": "reveal"},
    ]
    if asset_id:
        assets.append({
            "id": "photo-asset", "type": "historical_photo",
            "asset_id": asset_id, "evidence_class": "evidence", "locked": True,
        })
        elements.append({
            "id": "photo", "type": "image", "asset_id": "photo-asset",
            "role": "archive-photo",
        })
    return {"compositions": [{
        "id": "opening", "start": 0, "duration": 14,
        "template": "archiveCanvas", "assets": assets, "elements": elements,
        "events": [
            {"time": 0, "action": "fadeUp", "target": "date"},
            {"time": 12.5, "action": "fadeUp", "target": "name"},
        ],
        "narration_refs": [scene_id],
    }]}


def test_editorial_planner_uses_structured_local_llm_and_audio_clock() -> None:
    project = Project(
        title="Mars", topic="Project Mars", target_duration=14, slug="mars",
        video_mode=VideoMode.EDITORIAL, resolution=(1080, 1920), fps=24,
    )
    script = _editorial_script(project)
    llm = _PlannerLLM(_planner_payload(script.scenes[0].id))
    words = [CaptionWord(0, 0.4, "In"), CaptionWord(0.5, 14.0, "Mars.")]

    plan = EditorialPlanner(llm).plan(project, script, word_timings=words)

    assert plan.project_id == project.id
    assert plan.duration == 14 and (plan.width, plan.height, plan.fps) == (1080, 1920, 24)
    assert llm.calls[0]["structured"] is True
    assert llm.calls[0]["thinking_budget_tokens"] == 16_384
    assert "HTML, CSS, JavaScript" in llm.calls[0]["messages"][0]["content"]
    context = json.loads(llm.calls[0]["messages"][1]["content"])
    assert context["word_timestamps"][-1]["end_seconds"] == 14.0
    assert context["approved_templates"] == [item.value for item in EditorialTemplate]
    assert context["template_slots"]["documentReveal"]["document"] == "document"
    assert context["template_required_roles"]["comparisonCanvas"] == [
        "left-image", "right-image",
    ]


def test_editorial_planner_resolves_verified_asset_without_trusting_llm_source() -> None:
    project = Project(
        title="Mars", topic="Project Mars", target_duration=14, slug="mars-assets",
        video_mode=VideoMode.EDITORIAL,
    )
    script = _editorial_script(project)
    asset = Asset(
        id="verified", project_id=project.id, scene_id=script.scenes[0].id,
        type=AssetType.IMAGE, filepath="scenes/001/imports/photo.png",
        backend="imported_local", model="user-supplied", seed=0,
    )
    plan = EditorialPlanner(_PlannerLLM(
        _planner_payload(script.scenes[0].id, asset_id=asset.id)
    )).plan(project, script, assets=[asset])

    planned_asset = plan.compositions[0].assets[0]
    assert planned_asset.source == "scenes/001/imports/photo.png"
    assert planned_asset.evidence_class.value == "evidence" and planned_asset.locked


def test_single_composition_regeneration_preserves_clock_neighbors_and_protected_asset() -> None:
    project = Project(
        title="Mars", topic="Project Mars", target_duration=14, slug="mars-partial",
        video_mode=VideoMode.EDITORIAL,
    )
    script = _editorial_script(project)
    scene_id = script.scenes[0].id
    imported = Asset(
        id="user-photo", project_id=project.id, scene_id=scene_id,
        type=AssetType.IMAGE, filepath="scenes/001/imports/user-photo.png",
        backend="imported_local", model="user-supplied", seed=0,
    )
    current = EditorialComposition.model_validate({
        "id": "target", "start": 0, "duration": 7, "template": "archiveCanvas",
        "assets": [{
            "id": "protected-photo", "type": "user_uploaded_image",
            "asset_id": imported.id, "source": str(imported.filepath), "locked": False,
        }],
        "elements": [
            {"id": "old-year", "type": "text", "text": "1949", "role": "year"},
            {"id": "photo-slot", "type": "image", "asset_id": "protected-photo", "role": "archive-photo"},
        ],
        "events": [
            {"time": 0, "action": "fadeUp", "target": "old-year"},
            {"time": 1, "action": "slideInLeft", "target": "photo-slot"},
        ],
        "narration_refs": [scene_id],
    })
    neighbor = EditorialComposition.model_validate({
        "id": "next", "start": 7, "duration": 7, "template": "bigTextReveal",
        "elements": [{
            "id": "next-title", "type": "text", "text": "ELON", "role": "headline",
        }],
        "events": [{"time": 0, "action": "fadeUp", "target": "next-title"}],
        "narration_refs": [scene_id],
    })
    plan = EditPlan(project_id=project.id, compositions=[current, neighbor])
    llm = _PlannerLLM({"composition": {
        "id": "attempted-id", "start": 2, "duration": 5,
        "template": "archiveCanvas",
        "assets": [{
            "id": "protected-photo", "type": "user_uploaded_image",
            "asset_id": imported.id, "source": None, "locked": False,
        }],
        "elements": [
            {"id": "new-year", "type": "text", "text": "A NEW ANGLE", "role": "year"},
            {"id": "attempt-photo", "type": "image", "asset_id": "protected-photo", "role": "archive-photo"},
        ],
        "events": [
            {"time": 0, "action": "scaleIn", "target": "new-year"},
            {"time": 1, "action": "fade", "target": "attempt-photo"},
        ],
        "narration_refs": [scene_id],
    }})

    regenerated, draft = EditorialPlanner(llm).regenerate_composition(
        project, script, plan, "target", assets=[imported],
    )

    assert draft is not None
    assert (regenerated.id, regenerated.start, regenerated.duration) == ("target", 0, 7)
    assert regenerated.template is EditorialTemplate.ARCHIVE_CANVAS
    assert regenerated.narration_refs == [scene_id]
    assert next(item for item in regenerated.assets if item.id == "protected-photo") == current.assets[0]
    assert next(item for item in regenerated.elements if item.role == "archive-photo") == current.elements[1]
    assert any(event.target == "photo-slot" for event in regenerated.events)
    context = json.loads(llm.calls[0]["messages"][1]["content"])
    assert context["regenerate_only"]["id"] == "target"
    assert context["next_composition"]["id"] == "next"
    assert context["protected_asset_ids"] == ["protected-photo"]
    assert context["regenerate_only"]["assets"][0]["source"] is None


def test_editorial_planner_rejects_model_authored_sources_and_unknown_refs() -> None:
    project = Project(
        title="Mars", topic="Project Mars", target_duration=14, slug="mars-reject",
        video_mode=VideoMode.EDITORIAL,
    )
    script = _editorial_script(project)
    with pytest.raises(ValueError, match="unknown narration"):
        EditorialPlanner(_PlannerLLM(_planner_payload("unknown-scene"))).plan(project, script)

    payload = _planner_payload(script.scenes[0].id)
    payload["compositions"][0]["assets"] = [{
        "id": "remote", "type": "historical_photo", "source": "https://example.com/a.jpg",
    }]
    with pytest.raises(ValueError, match="cannot author asset source"):
        EditorialPlanner(_PlannerLLM(payload)).plan(project, script)


def test_editorial_planner_mock_fallback_is_valid_and_project_owned() -> None:
    project = Project(
        title="Mars", topic="Project Mars", target_duration=14, slug="mars-mock",
        video_mode=VideoMode.EDITORIAL,
    )
    script = _editorial_script(project)

    plan = EditorialPlanner().plan(project, script, mock_mode=True)

    assert plan.project_id == project.id and plan.duration == 14
    assert plan.compositions[0].narration_refs == [script.scenes[0].id]
    compile_edit_plan_html(plan)


class _SyntheticEditorialRenderer:
    def __init__(self, ffmpeg) -> None:
        self.ffmpeg = ffmpeg
        self.calls: list[tuple[EditPlan, Path, Path | None]] = []

    def render(
        self, plan: EditPlan, output: Path, *, preview_html=None, asset_root=None,
    ) -> Path:
        self.calls.append((plan, output, asset_root))
        output.parent.mkdir(parents=True, exist_ok=True)
        run_media_process([
            str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"color=c=#111315:s={plan.width}x{plan.height}:r={plan.fps}:d={plan.duration}",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], timeout=60)
        return output


def test_editorial_render_only_uses_shared_audio_caption_and_export_pipeline(tmp_path: Path) -> None:
    pipeline = PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )
    project = pipeline.create_project(ProjectCreate(
        title="Editorial Export", topic="narration-led evidence board",
        target_duration=1, resolution=(320, 568), fps=12,
        video_mode=VideoMode.EDITORIAL,
    ))
    pipeline.ensure_plan(project.id)
    project = pipeline._project(project.id)
    pipeline._ensure_narration(project, force=False)
    pipeline._ensure_music(project, force=False)
    pipeline._ensure_subtitles(project, force=False)
    root = pipeline.store.project_path(project)
    duration = wav_duration(root / "narration" / "master.wav")
    script = pipeline.store.load_plan(project.slug)
    plan = EditPlan(
        project_id=project.id, width=320, height=568, fps=12,
        captions_enabled=True,
        compositions=[EditorialComposition(
            id="master", start=0, duration=duration,
            template=EditorialTemplate.ARCHIVE_CANVAS,
            elements=[EditorialElement(
                id="headline", type=EditorialElementType.TEXT,
                text="EVIDENCE", role="year",
            )],
            events=[EditorialEvent(
                time=0, action=MotionPrimitive.FADE_UP, target="headline",
            )],
            narration_refs=[script.scenes[0].id],
        )],
    )
    pipeline.save_edit_plan(project.id, plan)
    synthetic = _SyntheticEditorialRenderer(require_ffmpeg(pipeline.renderer.binaries))
    pipeline._editorial_renderer = synthetic  # type: ignore[assignment]

    job = pipeline.queue_render(project.id, force=True)
    final = pipeline.run_render(project.id, force=True, parent_job_id=job.id)

    info = probe_media(final, pipeline.renderer.binaries)
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == project.resolution
    assert info.fps == project.fps
    assert len(synthetic.calls) == 1
    rendered_plan, rendered_clip, rendered_root = synthetic.calls[0]
    assert rendered_plan.compositions[0].start == 0
    assert rendered_plan.compositions[0].id == "master"
    assert rendered_clip.parent == root / "editorial" / "compositions"
    assert rendered_root == root
    timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["clips"][0]["path"] == "editorial/master.mp4"
    assert {track["kind"] for track in timeline["audio_tracks"]} == {"narration", "music"}
    assert timeline["subtitles"]
    stages = pipeline.project_snapshot(project.id)["stage_state"]["stages"]
    assert stages["editorial_visual"]["outputs"][0] == "editorial/master.mp4"
    assert "editorial/compositions/manifest.json" in stages["editorial_visual"]["outputs"]
    assert stages["render_final"]["outputs"] == ["renders/final.mp4"]
    assert pipeline.jobs.get(job.id).status.value == "completed"


def test_editorial_visual_cache_rerenders_only_the_changed_composition(tmp_path: Path) -> None:
    pipeline = PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )
    project = pipeline.create_project(ProjectCreate(
        title="Cached Editorial", topic="two compositions",
        target_duration=2, resolution=(320, 568), fps=12,
        video_mode=VideoMode.EDITORIAL,
    ))
    pipeline.ensure_plan(project.id)
    project = pipeline._project(project.id)
    pipeline._ensure_narration(project, force=False)
    root = pipeline.store.project_path(project)
    narration_duration = wav_duration(root / "narration" / "master.wav")
    midpoint = narration_duration / 2
    scene_id = pipeline.store.load_plan(project.slug).scenes[0].id

    def composition(identifier: str, start: float, duration: float, text: str):
        return EditorialComposition(
            id=identifier, start=start, duration=duration,
            template=EditorialTemplate.ARCHIVE_CANVAS,
            elements=[EditorialElement(
                id=f"{identifier}-title", type=EditorialElementType.TEXT,
                text=text, role="year",
            )],
            events=[EditorialEvent(
                time=0, action=MotionPrimitive.FADE_UP, target=f"{identifier}-title",
            )],
            narration_refs=[scene_id],
        )

    first = composition("first", 0, midpoint, "FIRST")
    second = composition("second", midpoint, narration_duration - midpoint, "SECOND")
    plan = EditPlan(
        project_id=project.id, width=320, height=568, fps=12,
        compositions=[first, second],
    )
    pipeline.save_edit_plan(project.id, plan)
    synthetic = _SyntheticEditorialRenderer(require_ffmpeg(pipeline.renderer.binaries))
    pipeline._editorial_renderer = synthetic  # type: ignore[assignment]

    pipeline._ensure_editorial_visual(project, force=False)
    assert [call[0].compositions[0].id for call in synthetic.calls] == ["first", "second"]
    changed_second = second.model_copy(update={
        "elements": [second.elements[0].model_copy(update={"text": "SECOND REVISED"})],
    })
    pipeline.save_edit_plan(project.id, plan.model_copy(update={
        "compositions": [first, changed_second],
    }))

    pipeline._ensure_editorial_visual(project, force=False)

    assert [call[0].compositions[0].id for call in synthetic.calls] == [
        "first", "second", "second",
    ]
    manifest = json.loads(
        (root / "editorial" / "compositions" / "manifest.json").read_text(encoding="utf-8")
    )
    assert set(manifest["entries"]) == {"first", "second"}
    info = probe_media(root / "editorial" / "master.mp4", pipeline.renderer.binaries)
    assert info.has_video and info.duration_seconds >= narration_duration - 0.1
    first_clip = root / "editorial" / "compositions" / manifest["entries"]["first"]["file"]
    first_clip.write_bytes(b"corrupt-but-nonempty")
    pipeline._invalidate_stages(project, {"editorial_visual"})
    pipeline._ensure_editorial_visual(project, force=False)
    assert [call[0].compositions[0].id for call in synthetic.calls][-1] == "first"
    assert probe_media(first_clip, pipeline.renderer.binaries).has_video
# Deterministic template library: renderer-owned layout for all five templates
# ---------------------------------------------------------------------------

THEME_MARKERS = (
    "--charcoal:#111315",
    "--ivory:#e9dfc6",
    "--rust:#b9532f",
    "--blue:#6f91a6",
    '"DejaVu Sans Condensed"',
    '"DejaVu Serif"',
    "grain",
    "window.renderAt",
    "__editorialReady",
)

MOTION_NAMES = [item.value for item in MotionPrimitive]


def _element(eid: str, role: str, etype: EditorialElementType, **fields) -> EditorialElement:
    return EditorialElement(id=eid, role=role, type=etype, **fields)


def _asset(
    aid: str,
    atype: EditorialAssetType = EditorialAssetType.GENERATED_IMAGE,
) -> EditorialAsset:
    return EditorialAsset(id=aid, type=atype)


def _composition(
    cid: str,
    template: EditorialTemplate,
    *,
    start: float = 0.0,
    duration: float = 4.0,
    assets: list[EditorialAsset] | None = None,
    elements: list[EditorialElement] | None = None,
    events: list[EditorialEvent] | None = None,
) -> EditorialComposition:
    return EditorialComposition(
        id=cid,
        start=start,
        duration=duration,
        template=template,
        assets=assets or [],
        elements=elements or [],
        events=events or [],
    )


def _template_composition(template: EditorialTemplate, *, cid: str = "c1") -> EditorialComposition:
    """Minimal valid composition for every template, using non-prototype ids."""
    if template is EditorialTemplate.ARCHIVE_CANVAS:
        return _composition(cid, template, assets=[_asset("photo-1")], elements=[
            _element("year-1", "year", EditorialElementType.TEXT, text="1949"),
            _element("photo-1", "archive-photo", EditorialElementType.IMAGE, asset_id="photo-1"),
        ])
    if template is EditorialTemplate.DOCUMENT_REVEAL:
        return _composition(cid, template, assets=[_asset("scan-1")], elements=[
            _element("doc-1", "document", EditorialElementType.DOCUMENT, asset_id="scan-1"),
            _element("title-1", "title", EditorialElementType.TEXT, text="SHEET"),
        ])
    if template is EditorialTemplate.COMPARISON_CANVAS:
        return _composition(cid, template, assets=[_asset("left-1"), _asset("right-1")], elements=[
            _element("left-1", "left-image", EditorialElementType.IMAGE, asset_id="left-1"),
            _element("right-1", "right-image", EditorialElementType.IMAGE, asset_id="right-1"),
        ])
    if template is EditorialTemplate.ILLUSTRATION_CANVAS:
        return _composition(cid, template, assets=[_asset("hero-1")], elements=[
            _element("hero-1", "illustration", EditorialElementType.IMAGE, asset_id="hero-1"),
        ])
    return _composition(cid, template, elements=[
        _element("head-1", "headline", EditorialElementType.TEXT, text="WORD"),
    ])


def _plan_payload(html: str) -> dict:
    return json.loads(html.split("const PLAN=")[1].splitlines()[0].rstrip(";"))


def test_every_template_compiles_with_shared_theme_and_seek_contract() -> None:
    for template in EditorialTemplate:
        html = compile_edit_plan_html(EditPlan(project_id="p", compositions=[
            _template_composition(template, cid=template.value),
        ]))
        for marker in THEME_MARKERS:
            assert marker in html, (template.value, marker)
        assert f'data-composition="{template.value}"' in html
        assert '<body class="editorial-text-enabled portrait">' in html
        # Compositions are isolated: hidden until renderAt activates the interval.
        assert ".composition{position:absolute;inset:0;display:none" in html
        # Every approved motion primitive stays available in the compiled runtime.
        for name in MOTION_NAMES:
            assert f"case '{name}':" in html, (template.value, name)


def test_renderer_scales_vertical_preview_and_uses_landscape_layout() -> None:
    vertical = EditPlan(
        project_id="vertical", width=320, height=568,
        compositions=[_template_composition(EditorialTemplate.BIG_TEXT_REVEAL)],
    )
    vertical_html = compile_edit_plan_html(vertical)
    assert '<body class="editorial-text-enabled portrait">' in vertical_html
    assert "width:1080px;height:1920px" in vertical_html
    assert "transform:scale(0.29583333)" in vertical_html

    horizontal = EditPlan(
        project_id="horizontal", width=1920, height=1080,
        compositions=[_template_composition(EditorialTemplate.COMPARISON_CANVAS)],
    )
    horizontal_html = compile_edit_plan_html(horizontal)
    assert '<body class="editorial-text-enabled landscape">' in horizontal_html
    assert "width:1920px;height:1080px" in horizontal_html
    assert "transform:scale(1.00000000)" in horizontal_html
    assert ".landscape .comparison-card" in horizontal_html


def test_document_reveal_binds_roles_to_layout_regions() -> None:
    composition = _composition(
        "dr-comp", EditorialTemplate.DOCUMENT_REVEAL, duration=6,
        assets=[_asset("scan-7f"), _asset("photo-91")],
        elements=[
            _element(
                "sheet-7f", "document", EditorialElementType.DOCUMENT,
                text="The 1949 memorandum", asset_id="scan-7f",
            ),
            _element("title-9c", "title", EditorialElementType.TEXT, text="ONE PLAN"),
            _element("mark-31", "passage-mark", EditorialElementType.UNDERLINE),
            _element(
                "note-b2", "annotation", EditorialElementType.TEXT,
                text="Marginal reading note",
            ),
            _element("photo-91e", "context-image", EditorialElementType.IMAGE, asset_id="photo-91"),
            _element("join-08", "connector", EditorialElementType.LINE),
        ],
        events=[
            EditorialEvent(time=0.0, action=MotionPrimitive.FADE_UP, target="title-9c"),
            EditorialEvent(time=0.8, action=MotionPrimitive.PAPER_SLIDE, target="sheet-7f"),
            EditorialEvent(time=2.0, action=MotionPrimitive.UNDERLINE, target="mark-31"),
            EditorialEvent(time=2.8, action=MotionPrimitive.DRAW_LINE, target="join-08"),
            EditorialEvent(time=3.6, action=MotionPrimitive.FADE, target="note-b2"),
            EditorialEvent(time=4.4, action=MotionPrimitive.SLIDE_IN_RIGHT, target="photo-91e"),
        ],
    )
    html = compile_edit_plan_html(
        EditPlan(project_id="p", compositions=[composition]),
        asset_url_resolver=lambda asset: f"/media/{asset.id}",
    )
    assert 'id="sheet-7f" class="source-sheet editorial-element"' in html
    assert 'id="title-9c" class="document-title editorial-element editorial-type"' in html
    assert 'id="mark-31" class="passage-mark draw editorial-element"' in html
    assert 'id="note-b2" class="annotation editorial-element editorial-type"' in html
    assert 'id="photo-91e" class="context-photo editorial-element"' in html
    assert 'id="join-08" class="connector-line draw editorial-element"' in html
    assert ">ONE PLAN<" in html
    assert ">The 1949 memorandum<" in html
    assert ">Marginal reading note<" in html
    assert 'src="/media/scan-7f"' in html
    assert 'src="/media/photo-91"' in html


def test_comparison_canvas_binds_roles_to_layout_regions() -> None:
    composition = _composition(
        "cmp-comp", EditorialTemplate.COMPARISON_CANVAS, duration=5,
        assets=[_asset("left-asset"), _asset("right-asset")],
        elements=[
            _element("head-a1", "headline", EditorialElementType.TEXT, text="TWO STAGES"),
            _element("img-left-a2", "left-image", EditorialElementType.IMAGE, asset_id="left-asset"),
            _element("img-right-a3", "right-image", EditorialElementType.IMAGE, asset_id="right-asset"),
            _element("cap-left-a4", "left-label", EditorialElementType.TEXT, text="BEFORE"),
            _element("cap-right-a5", "right-label", EditorialElementType.TEXT, text="AFTER"),
            _element("cut-a6", "divider", EditorialElementType.LINE),
        ],
        events=[
            EditorialEvent(time=0.0, action=MotionPrimitive.SLIDE_IN_LEFT, target="img-left-a2"),
            EditorialEvent(time=0.6, action=MotionPrimitive.SLIDE_IN_RIGHT, target="img-right-a3"),
            EditorialEvent(time=1.2, action=MotionPrimitive.DRAW_LINE, target="cut-a6"),
            EditorialEvent(time=1.8, action=MotionPrimitive.HARD_CUT, target="head-a1"),
        ],
    )
    html = compile_edit_plan_html(
        EditPlan(project_id="p", compositions=[composition]),
        asset_url_resolver=lambda asset: f"/assets/{asset.id}",
    )
    assert 'id="head-a1" class="comparison-headline editorial-element editorial-type"' in html
    assert 'id="img-left-a2" class="comparison-card left-card editorial-element"' in html
    assert 'id="img-right-a3" class="comparison-card right-card editorial-element"' in html
    assert 'id="cap-left-a4" class="comparison-label left-label editorial-element editorial-type"' in html
    assert 'id="cap-right-a5" class="comparison-label right-label editorial-element editorial-type"' in html
    assert 'id="cut-a6" class="divider-line draw editorial-element" data-draw-axis="y"' in html
    assert ">TWO STAGES<" in html and ">BEFORE<" in html and ">AFTER<" in html
    assert 'src="/assets/left-asset"' in html and 'src="/assets/right-asset"' in html


def test_illustration_canvas_binds_roles_to_layout_regions() -> None:
    composition = _composition(
        "ill-comp", EditorialTemplate.ILLUSTRATION_CANVAS, duration=5,
        assets=[EditorialAsset(id="hero-asset", type=EditorialAssetType.GENERATED_IMAGE, asset_id="hero-asset")],
        elements=[
            _element("hero-b1", "illustration", EditorialElementType.IMAGE, asset_id="hero-asset"),
            _element("head-b2", "headline", EditorialElementType.TEXT, text="MARS MAP"),
            _element(
                "copy-b3", "supporting-text", EditorialElementType.TEXT,
                text="A technical reading of the plate.",
            ),
            _element("rule-b4", "technical-line", EditorialElementType.LINE),
        ],
        events=[
            EditorialEvent(time=0.0, action=MotionPrimitive.SCALE_IN, target="hero-b1"),
            EditorialEvent(time=1.2, action=MotionPrimitive.DRAW_LINE, target="rule-b4"),
            EditorialEvent(time=1.8, action=MotionPrimitive.FADE_UP, target="head-b2"),
            EditorialEvent(time=2.6, action=MotionPrimitive.FADE, target="copy-b3"),
        ],
    )
    # The resolver sees EditorialAsset objects; project code keys off asset_id.
    html = compile_edit_plan_html(
        EditPlan(project_id="p", compositions=[composition]),
        asset_url_resolver=lambda asset: f"/images/{asset.asset_id}",
    )
    assert 'id="hero-b1" class="illustration-frame editorial-element"' in html
    assert 'id="head-b2" class="illustration-headline editorial-element editorial-type"' in html
    assert 'id="copy-b3" class="supporting-copy editorial-element editorial-type"' in html
    assert 'id="rule-b4" class="technical-rule draw editorial-element"' in html
    assert ">MARS MAP<" in html and ">A technical reading of the plate.<" in html
    assert 'src="/images/hero-asset"' in html


def test_big_text_reveal_binds_roles_to_layout_regions() -> None:
    composition = _composition(
        "big-comp", EditorialTemplate.BIG_TEXT_REVEAL, duration=5,
        elements=[
            _element("kicker-c1", "kicker", EditorialElementType.TEXT, text="1949"),
            _element("headline-c2", "headline", EditorialElementType.TEXT, text="ELON"),
            _element("blackout-c3", "blackout", EditorialElementType.BLACK_SCREEN),
        ],
        events=[
            EditorialEvent(time=0.0, action=MotionPrimitive.FADE, target="kicker-c1"),
            EditorialEvent(time=0.7, action=MotionPrimitive.HARD_CUT, target="headline-c2"),
            EditorialEvent(time=3.5, action=MotionPrimitive.FADE, target="blackout-c3"),
        ],
    )
    html = compile_edit_plan_html(EditPlan(project_id="p", compositions=[composition]))
    assert 'id="kicker-c1" class="big-kicker editorial-element editorial-type"' in html
    assert 'id="headline-c2" class="big-headline editorial-element editorial-type"' in html
    assert 'id="blackout-c3" class="blackout editorial-element"' in html
    assert ">1949<" in html and ">ELON<" in html
    # This template carries no imagery and therefore no resolved URLs.
    assert "src=" not in html


@pytest.mark.parametrize(("template", "placeholder"), [
    (EditorialTemplate.DOCUMENT_REVEAL, "source-sheet"),
    (EditorialTemplate.COMPARISON_CANVAS, "photo-art"),
    (EditorialTemplate.ILLUSTRATION_CANVAS, "illustration-art"),
])
def test_new_templates_without_asset_urls_render_placeholders_not_broken_images(
    template: EditorialTemplate, placeholder: str,
) -> None:
    html = compile_edit_plan_html(EditPlan(project_id="p", compositions=[
        _template_composition(template, cid=template.value),
    ]))
    assert "<img" not in html
    assert placeholder in html


def test_new_templates_resolve_asset_urls_through_the_resolver_only() -> None:
    url = '/api/projects/p/assets/a1/file?ref="scan"&n=2'
    composition = _composition(
        "asset-comp", EditorialTemplate.DOCUMENT_REVEAL,
        assets=[_asset("scan-x"), _asset("photo-x")],
        elements=[
            _element("sheet-x", "document", EditorialElementType.DOCUMENT, asset_id="scan-x"),
            _element("photo-xe", "context-image", EditorialElementType.IMAGE, asset_id="photo-x"),
        ],
    )
    html = compile_edit_plan_html(
        EditPlan(project_id="p", compositions=[composition]),
        asset_url_resolver=lambda asset: url if asset.id == "photo-x" else None,
    )
    assert f'src="{escape(url, quote=True)}"' in html
    assert url not in html
    # Only the resolved asset emits an <img>; the other stays on the fallback.
    assert html.count("<img") == 1


def test_new_templates_render_plan_text_escaped_and_never_as_markup() -> None:
    payload = '"><img src=x onerror=alert(1)>'
    variants = [
        _composition(
            "esc-dr", EditorialTemplate.DOCUMENT_REVEAL,
            assets=[_asset("scan-e")],
            elements=[
                _element("esc-doc", "document", EditorialElementType.DOCUMENT, text=payload, asset_id="scan-e"),
                _element("esc-title", "title", EditorialElementType.TEXT, text=payload),
                _element("esc-note", "annotation", EditorialElementType.TEXT, text=payload),
            ],
        ),
        _composition(
            "esc-cc", EditorialTemplate.COMPARISON_CANVAS,
            assets=[_asset("la-e"), _asset("ra-e")],
            elements=[
                _element("esc-head", "headline", EditorialElementType.TEXT, text=payload),
                _element("la-e2", "left-image", EditorialElementType.IMAGE, asset_id="la-e"),
                _element("ra-e2", "right-image", EditorialElementType.IMAGE, asset_id="ra-e"),
                _element("esc-ll", "left-label", EditorialElementType.TEXT, text=payload),
                _element("esc-rl", "right-label", EditorialElementType.TEXT, text=payload),
            ],
        ),
        _composition(
            "esc-ic", EditorialTemplate.ILLUSTRATION_CANVAS,
            assets=[_asset("hero-e")],
            elements=[
                _element("hero-e2", "illustration", EditorialElementType.IMAGE, asset_id="hero-e"),
                _element("esc-ih", "headline", EditorialElementType.TEXT, text=payload),
                _element("esc-copy", "supporting-text", EditorialElementType.TEXT, text=payload),
            ],
        ),
        _composition(
            "esc-bt", EditorialTemplate.BIG_TEXT_REVEAL,
            elements=[
                _element("esc-kick", "kicker", EditorialElementType.TEXT, text=payload),
                _element("esc-bh", "headline", EditorialElementType.TEXT, text=payload),
            ],
        ),
    ]
    for composition in variants:
        html = compile_edit_plan_html(EditPlan(project_id="p", compositions=[composition]))
        assert "<img src=x" not in html
        assert payload not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        # The only script tag in the document is the trusted runtime; plan
        # text is embedded as escaped data, never as markup.
        assert html.count("<script>") == 1
        assert html.count("</script>") == 1


def test_all_motion_primitives_are_plannable_and_compiled() -> None:
    raw = [
        (0.0, MotionPrimitive.FADE, "year-m", None),
        (0.6, MotionPrimitive.FADE_UP, "year-m", None),
        (1.2, MotionPrimitive.SLIDE_IN_LEFT, "photo-m", None),
        (1.8, MotionPrimitive.SLIDE_IN_RIGHT, "paper-m", None),
        (2.4, MotionPrimitive.SCALE_IN, "reveal-m", None),
        (3.0, MotionPrimitive.SLOW_PUSH, "reveal-m", None),
        (3.6, MotionPrimitive.PAPER_SLIDE, "paper-m", None),
        (4.2, MotionPrimitive.UNDERLINE, "mark-m", None),
        (4.8, MotionPrimitive.HIGHLIGHT, "mark-m", None),
        (5.4, MotionPrimitive.DRAW_LINE, "mark-m", None),
        (6.0, MotionPrimitive.STAGGER_IN, "rulers-m", None),
        (7.2, MotionPrimitive.DIM_OTHERS, "rulers-m", 4),
        (7.2, MotionPrimitive.FOCUS_ONE, "rulers-m", 4),
        (8.4, MotionPrimitive.COLLAPSE_TO_BLACK, "canvas", None),
        (9.6, MotionPrimitive.HARD_CUT, "reveal-m", None),
    ]
    composition = _composition(
        "motions", EditorialTemplate.ARCHIVE_CANVAS, duration=12,
        assets=[_asset("ph-m"), _asset("pd-m", EditorialAssetType.DOCUMENT)],
        elements=[
            _element("year-m", "year", EditorialElementType.TEXT, text="1949"),
            _element("photo-m", "archive-photo", EditorialElementType.IMAGE, asset_id="ph-m"),
            _element("paper-m", "paper", EditorialElementType.DOCUMENT, text="DOC", asset_id="pd-m"),
            _element("mark-m", "document-mark", EditorialElementType.UNDERLINE),
            _element("rulers-m", "ruler-grid", EditorialElementType.RULER_NODES, count=10),
            _element("reveal-m", "reveal", EditorialElementType.TEXT, text="ELON"),
        ],
        events=[EditorialEvent(time=t, action=a, target=tr, value=v) for t, a, tr, v in raw],
    )
    html = compile_edit_plan_html(EditPlan(project_id="p", compositions=[composition]))
    for name in MOTION_NAMES:
        assert f"case '{name}':" in html
    payload = _plan_payload(html)
    assert [event["action"] for event in payload["compositions"][0]["events"]] == [
        item[1].value for item in raw
    ]


def test_multi_template_plan_embeds_seek_data_and_isolates_layouts() -> None:
    compositions = [
        _composition(
            "comp-a", EditorialTemplate.ARCHIVE_CANVAS, start=0.0, duration=3.0,
            elements=[_element("ya", "year", EditorialElementType.TEXT, text="1949")],
        ),
        _composition(
            "comp-b", EditorialTemplate.DOCUMENT_REVEAL, start=3.0, duration=4.0,
            assets=[_asset("sb")],
            elements=[
                _element("db", "document", EditorialElementType.DOCUMENT, asset_id="sb"),
                _element("tb", "title", EditorialElementType.TEXT, text="DOC"),
            ],
        ),
        _composition(
            "comp-c", EditorialTemplate.COMPARISON_CANVAS, start=7.0, duration=3.0,
            assets=[_asset("lc"), _asset("rc")],
            elements=[
                _element("lc2", "left-image", EditorialElementType.IMAGE, asset_id="lc"),
                _element("rc2", "right-image", EditorialElementType.IMAGE, asset_id="rc"),
                _element("dc", "divider", EditorialElementType.LINE),
            ],
        ),
        _composition(
            "comp-d", EditorialTemplate.BIG_TEXT_REVEAL, start=10.0, duration=4.0,
            elements=[
                _element("hd", "headline", EditorialElementType.TEXT, text="END"),
                _element("bo", "blackout", EditorialElementType.BLACK_SCREEN),
            ],
        ),
    ]
    html = compile_edit_plan_html(EditPlan(project_id="p", compositions=compositions))

    payload = _plan_payload(html)
    assert [(c["template"], c["start"], c["duration"]) for c in payload["compositions"]] == [
        ("archiveCanvas", 0, 3), ("documentReveal", 3, 4),
        ("comparisonCanvas", 7, 3), ("bigTextReveal", 10, 4),
    ]
    assert payload["editorial_text_enabled"] is True
    assert payload["captions_enabled"] is True

    # Each layout's unique draft label sits inside its own section, in plan order.
    sections = [
        ("comp-a", "EVIDENCE MAP"),
        ("comp-b", "SOURCE READING"),
        ("comp-c", "FIG. 03"),
        ("comp-d", "FIG. 05"),
    ]
    starts = [html.index(f'data-composition="{cid}"') for cid, _ in sections]
    assert starts == sorted(starts)
    ends = [html.index(marker) for _, marker in sections]
    boundaries = starts[1:] + [html.index("</main>")]
    for start, end, boundary in zip(starts, ends, boundaries):
        assert start < end < boundary


@pytest.mark.parametrize(("template", "typed_node", "kept_node"), [
    (EditorialTemplate.ARCHIVE_CANVAS,
     'class="year editorial-element editorial-type"',
     'class="archive-photo editorial-element"'),
    (EditorialTemplate.DOCUMENT_REVEAL,
     'class="document-title editorial-element editorial-type"',
     'class="source-sheet editorial-element"'),
    (EditorialTemplate.COMPARISON_CANVAS,
     'class="comparison-headline editorial-element editorial-type"',
     'class="comparison-card left-card editorial-element"'),
    (EditorialTemplate.ILLUSTRATION_CANVAS,
     'class="illustration-headline editorial-element editorial-type"',
     'class="illustration-frame editorial-element"'),
    (EditorialTemplate.BIG_TEXT_REVEAL,
     'class="big-headline editorial-element editorial-type"',
     'class="blackout editorial-element"'),
])
def test_typography_disable_hides_type_but_keeps_imagery_and_caption_data(
    template: EditorialTemplate, typed_node: str, kept_node: str,
) -> None:
    composition = _template_composition(template, cid=template.value)
    plan = EditPlan(
        project_id="p",
        compositions=[composition],
        editorial_text_enabled=False,
        captions_enabled=True,
    )
    html = compile_edit_plan_html(
        plan,
        asset_url_resolver=lambda asset: f"/m/{asset.id}",
    )
    assert '<body class="editorial-text-disabled portrait">' in html
    assert '"captions_enabled":true' in html
    assert ".editorial-text-disabled .editorial-type{visibility:hidden}" in html
    assert ".editorial-text-disabled .ruler-node span{visibility:hidden}" in html
    # Typography nodes are flagged; imagery and graphic nodes are not.
    assert typed_node in html
    assert kept_node in html
    # Resolved imagery still renders (and the resolved URL stays escaped-safe).
    if template is not EditorialTemplate.BIG_TEXT_REVEAL:
        assert '<img class="asset-image' in html
        assert "src=" in html
