"""Strict, renderer-independent contracts for Editorial Mode edit plans."""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, field_validator, model_validator

from backend.schemas.models import DomainModel, new_id, utc_now


class EditorialTemplate(StrEnum):
    ARCHIVE_CANVAS = "archiveCanvas"
    DOCUMENT_REVEAL = "documentReveal"
    COMPARISON_CANVAS = "comparisonCanvas"
    ILLUSTRATION_CANVAS = "illustrationCanvas"
    BIG_TEXT_REVEAL = "bigTextReveal"


class MotionPrimitive(StrEnum):
    FADE = "fade"
    FADE_UP = "fadeUp"
    SLIDE_IN_LEFT = "slideInLeft"
    SLIDE_IN_RIGHT = "slideInRight"
    SCALE_IN = "scaleIn"
    SLOW_PUSH = "slowPush"
    PAPER_SLIDE = "paperSlide"
    UNDERLINE = "underline"
    HIGHLIGHT = "highlight"
    DRAW_LINE = "drawLine"
    STAGGER_IN = "staggerIn"
    DIM_OTHERS = "dimOthers"
    FOCUS_ONE = "focusOne"
    COLLAPSE_TO_BLACK = "collapseToBlack"
    HARD_CUT = "hardCut"


class EditorialAssetType(StrEnum):
    HISTORICAL_PHOTO = "historical_photo"
    HISTORICAL_VIDEO = "historical_video"
    DOCUMENT = "document"
    USER_UPLOADED_IMAGE = "user_uploaded_image"
    GENERATED_IMAGE = "generated_image"
    GENERATED_VIDEO = "generated_video"
    DIAGRAM = "diagram"
    TYPOGRAPHY = "typography"
    MAP = "map"
    SCRIPTURE_TEXT = "scripture_text"
    COMPARISON = "comparison"
    TIMELINE = "timeline"
    BLACK_SCREEN = "black_screen"
    EXISTING_ASSET = "existing_asset"


class EvidenceClass(StrEnum):
    EVIDENCE = "evidence"
    ILLUSTRATION = "illustration"


class EditPlanSourceKind(StrEnum):
    PLANNER = "planner"
    MANUAL = "manual"


class EditorialElementType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    UNDERLINE = "underline"
    RULER_NODES = "ruler_nodes"
    LINE = "line"
    BLACK_SCREEN = "black_screen"


TEMPLATE_ELEMENT_SLOTS: dict[
    EditorialTemplate, dict[str, EditorialElementType]
] = {
    EditorialTemplate.ARCHIVE_CANVAS: {
        "year": EditorialElementType.TEXT,
        "archive-photo": EditorialElementType.IMAGE,
        "paper": EditorialElementType.DOCUMENT,
        "document-mark": EditorialElementType.UNDERLINE,
        "ruler-grid": EditorialElementType.RULER_NODES,
        "reveal": EditorialElementType.TEXT,
    },
    EditorialTemplate.DOCUMENT_REVEAL: {
        "document": EditorialElementType.DOCUMENT,
        "title": EditorialElementType.TEXT,
        "passage-mark": EditorialElementType.UNDERLINE,
        "annotation": EditorialElementType.TEXT,
        "context-image": EditorialElementType.IMAGE,
        "connector": EditorialElementType.LINE,
    },
    EditorialTemplate.COMPARISON_CANVAS: {
        "headline": EditorialElementType.TEXT,
        "left-image": EditorialElementType.IMAGE,
        "right-image": EditorialElementType.IMAGE,
        "left-label": EditorialElementType.TEXT,
        "right-label": EditorialElementType.TEXT,
        "divider": EditorialElementType.LINE,
    },
    EditorialTemplate.ILLUSTRATION_CANVAS: {
        "illustration": EditorialElementType.IMAGE,
        "headline": EditorialElementType.TEXT,
        "supporting-text": EditorialElementType.TEXT,
        "technical-line": EditorialElementType.LINE,
    },
    EditorialTemplate.BIG_TEXT_REVEAL: {
        "headline": EditorialElementType.TEXT,
        "kicker": EditorialElementType.TEXT,
        "blackout": EditorialElementType.BLACK_SCREEN,
    },
}

TEMPLATE_REQUIRED_ROLES: dict[EditorialTemplate, frozenset[str]] = {
    EditorialTemplate.ARCHIVE_CANVAS: frozenset(),
    EditorialTemplate.DOCUMENT_REVEAL: frozenset({"document"}),
    EditorialTemplate.COMPARISON_CANVAS: frozenset({"left-image", "right-image"}),
    EditorialTemplate.ILLUSTRATION_CANVAS: frozenset({"illustration"}),
    EditorialTemplate.BIG_TEXT_REVEAL: frozenset({"headline"}),
}


class EditorialAsset(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=120)
    type: EditorialAssetType
    evidence_class: EvidenceClass = EvidenceClass.ILLUSTRATION
    asset_id: str | None = Field(default=None, min_length=1, max_length=120)
    source: str | None = Field(default=None, max_length=4000)
    locked: bool = False
    label: str = Field(default="", max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def portable_source(cls, value: str | None) -> str | None:
        if value is None or value.startswith(("http://", "https://")):
            return value
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("editorial asset source must be project-relative or an explicit URL")
        return value

    @model_validator(mode="after")
    def protect_evidence(self) -> "EditorialAsset":
        if self.type in {
            EditorialAssetType.GENERATED_IMAGE,
            EditorialAssetType.GENERATED_VIDEO,
        } and self.evidence_class is EvidenceClass.EVIDENCE:
            raise ValueError("generated assets cannot be classified as factual evidence")
        if self.evidence_class is EvidenceClass.EVIDENCE and not self.locked:
            raise ValueError("evidence assets must be locked against ordinary regeneration")
        return self


class EditorialElement(DomainModel):
    id: str = Field(min_length=1, max_length=120)
    type: EditorialElementType
    text: str = Field(default="", max_length=4000)
    asset_id: str | None = Field(default=None, min_length=1, max_length=120)
    role: str = Field(default="", max_length=120)
    count: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def require_content(self) -> "EditorialElement":
        if self.type is EditorialElementType.TEXT and not self.text.strip():
            raise ValueError("text elements require non-empty text")
        if self.type is EditorialElementType.IMAGE and not self.asset_id:
            raise ValueError("image elements require asset_id")
        return self


class EditorialEvent(DomainModel):
    time: float = Field(ge=0)
    action: MotionPrimitive
    target: str = Field(min_length=1, max_length=120)
    duration: float = Field(default=0.6, ge=0, le=30)
    value: float | str | int | None = None

    @model_validator(mode="after")
    def finite_numbers(self) -> "EditorialEvent":
        if not math.isfinite(self.time) or not math.isfinite(self.duration):
            raise ValueError("event timing must be finite")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("event value must be finite")
        return self


class EditorialComposition(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=120)
    start: float = Field(ge=0)
    duration: float = Field(gt=0, le=120)
    template: EditorialTemplate
    assets: list[EditorialAsset] = Field(default_factory=list, max_length=80)
    elements: list[EditorialElement] = Field(default_factory=list, max_length=200)
    events: list[EditorialEvent] = Field(default_factory=list, max_length=500)
    transition_out: MotionPrimitive | None = None
    narration_refs: list[str] = Field(default_factory=list, max_length=200)
    caption_refs: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_graph(self) -> "EditorialComposition":
        if not math.isfinite(self.start) or not math.isfinite(self.duration):
            raise ValueError("composition timing must be finite")
        asset_ids = [asset.id for asset in self.assets]
        element_ids = [element.id for element in self.elements]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset ids must be unique within a composition")
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("element ids must be unique within a composition")
        roles = [element.role for element in self.elements if element.role]
        if len(roles) != len(set(roles)):
            raise ValueError("element roles must be unique within a composition")
        slots = TEMPLATE_ELEMENT_SLOTS[self.template]
        for element in self.elements:
            expected = slots.get(element.role)
            if expected is None:
                raise ValueError(
                    f"{self.template.value} does not define element role {element.role!r}"
                )
            if element.type is not expected:
                raise ValueError(
                    f"{self.template.value} role {element.role!r} requires type {expected.value}"
                )
        missing_roles = TEMPLATE_REQUIRED_ROLES[self.template] - set(roles)
        if missing_roles:
            raise ValueError(
                f"{self.template.value} requires element roles "
                f"{', '.join(sorted(missing_roles))}"
            )
        known_assets = set(asset_ids)
        for element in self.elements:
            if element.asset_id and element.asset_id not in known_assets:
                raise ValueError(f"element {element.id!r} references an unknown asset")
        known_targets = set(element_ids) | {"canvas"}
        previous = -1.0
        for event in self.events:
            if event.target not in known_targets:
                raise ValueError(f"event references unknown target {event.target!r}")
            if event.time > self.duration:
                raise ValueError("event starts after the composition ends")
            if event.time < previous:
                raise ValueError("events must be ordered by time")
            previous = event.time
        return self


class EditPlan(DomainModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    project_id: str = Field(min_length=1, max_length=120)
    width: int = Field(default=1080, ge=320, le=7680)
    height: int = Field(default=1920, ge=320, le=7680)
    fps: int = Field(default=24, ge=1, le=120)
    compositions: list[EditorialComposition] = Field(min_length=1, max_length=200)
    editorial_text_enabled: bool = True
    captions_enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def duration(self) -> float:
        return max(item.start + item.duration for item in self.compositions)

    @model_validator(mode="after")
    def validate_timeline(self) -> "EditPlan":
        ids = [item.id for item in self.compositions]
        if len(ids) != len(set(ids)):
            raise ValueError("composition ids must be unique")
        previous_end = 0.0
        for index, item in enumerate(self.compositions):
            start_frame = round(item.start * self.fps)
            end_frame = round((item.start + item.duration) * self.fps)
            if end_frame <= start_frame:
                raise ValueError("each Editorial composition must occupy at least one frame")
            if index == 0 and start_frame != 0:
                raise ValueError("the first Editorial composition must start at zero")
            if index and round(previous_end * self.fps) != start_frame:
                raise ValueError(
                    "editorial compositions must be contiguous; use an explicit black composition"
                )
            previous_end = item.start + item.duration
        return self


class EditPlanProvenance(DomainModel):
    """Portable fingerprints of the inputs an Edit Plan was authored against."""

    schema_version: int = Field(default=1, ge=1, le=1)
    project_id: str = Field(min_length=1, max_length=120)
    source_kind: EditPlanSourceKind
    project_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    script_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    word_timings_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime = Field(default_factory=utc_now)
