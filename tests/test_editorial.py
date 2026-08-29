from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.editorial import (
    EditorialAsset, EditorialAssetType,
    EditorialComposition, EditorialElement, EditorialElementType, EditorialEvent,
    EditorialTemplate, EditPlan, MotionPrimitive, build_project_mars_prototype,
    compile_edit_plan_html,
)
from backend.schemas import Project, VideoMode


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
            "elements": [{"id": "title", "type": "text", "text": "Exact"}],
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


def test_prototype_compiler_rejects_unimplemented_templates() -> None:
    plan = build_project_mars_prototype()
    plan.compositions[0].template = EditorialTemplate.DOCUMENT_REVEAL

    with pytest.raises(ValueError, match="supports only archiveCanvas"):
        compile_edit_plan_html(plan)
