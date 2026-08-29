"""Deterministic acceptance fixture for the first Editorial Mode milestone."""

from __future__ import annotations

from .models import (
    EditorialAsset, EditorialAssetType, EditorialComposition, EditorialElement,
    EditorialElementType, EditorialEvent, EditorialTemplate, EditPlan,
    EvidenceClass, MotionPrimitive,
)


def build_project_mars_prototype(*, project_id: str = "editorial-prototype") -> EditPlan:
    """Return the 1949 → Project Mars → ten rulers → ELON sequence."""
    return EditPlan(
        project_id=project_id,
        width=1080,
        height=1920,
        fps=24,
        compositions=[EditorialComposition(
            id="intro-1949",
            start=0,
            duration=14,
            template=EditorialTemplate.ARCHIVE_CANVAS,
            assets=[
                EditorialAsset(
                    id="von-braun-photo",
                    type=EditorialAssetType.HISTORICAL_PHOTO,
                    evidence_class=EvidenceClass.EVIDENCE,
                    label="Historical photograph placeholder",
                    locked=True,
                ),
                EditorialAsset(
                    id="project-mars-document",
                    type=EditorialAssetType.DOCUMENT,
                    evidence_class=EvidenceClass.EVIDENCE,
                    label="Project Mars document placeholder",
                    locked=True,
                ),
            ],
            elements=[
                EditorialElement(id="year", type=EditorialElementType.TEXT, text="1949", role="year"),
                EditorialElement(id="photo", type=EditorialElementType.IMAGE, asset_id="von-braun-photo", role="archive-photo"),
                EditorialElement(
                    id="document", type=EditorialElementType.DOCUMENT,
                    text="THE MARS PROJECT", asset_id="project-mars-document", role="paper",
                ),
                EditorialElement(id="passage", type=EditorialElementType.UNDERLINE, role="document-mark"),
                EditorialElement(id="rulers", type=EditorialElementType.RULER_NODES, count=10, role="ruler-grid"),
                EditorialElement(id="elon", type=EditorialElementType.TEXT, text="ELON", role="reveal"),
            ],
            events=[
                EditorialEvent(time=0, action=MotionPrimitive.FADE_UP, target="year", duration=0.8),
                EditorialEvent(time=1.2, action=MotionPrimitive.SLIDE_IN_LEFT, target="photo", duration=0.9),
                EditorialEvent(time=3.4, action=MotionPrimitive.PAPER_SLIDE, target="document", duration=1.0),
                EditorialEvent(time=5.0, action=MotionPrimitive.UNDERLINE, target="passage", duration=0.8),
                EditorialEvent(time=6.5, action=MotionPrimitive.STAGGER_IN, target="rulers", duration=1.3),
                EditorialEvent(time=9.0, action=MotionPrimitive.DIM_OTHERS, target="rulers", duration=0.8, value=6),
                EditorialEvent(time=9.0, action=MotionPrimitive.FOCUS_ONE, target="rulers", duration=0.8, value=6),
                EditorialEvent(time=11.5, action=MotionPrimitive.COLLAPSE_TO_BLACK, target="canvas", duration=1.0),
                EditorialEvent(time=12.5, action=MotionPrimitive.FADE_UP, target="elon", duration=0.8),
            ],
        )],
    )
