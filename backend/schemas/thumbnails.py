"""Bounded, portable contracts for the local Thumbnail Studio."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .models import DomainModel, utc_now


ThumbnailCandidateId = Literal["candidate-01", "candidate-02", "candidate-03"]
ThumbnailSide = Literal["left", "right"]
ThumbnailSubjectPosition = Literal["left", "center", "right"]
ThumbnailPalette = Literal["sunset", "electric", "midnight", "paper"]
ThumbnailFontPreset = Literal["impact", "clean", "editorial"]
ThumbnailLayoutPreset = Literal["stacked", "split", "banner"]
ThumbnailImageModel = Literal["krea", "ideogram4_local"]
ThumbnailIdeogramPromptMode = Literal["quick", "precise"]


class ThumbnailConcept(DomainModel):
    prompt: str = Field(min_length=1, max_length=4_000)
    avoid_prompt: str = Field(
        default="text, letters, words, typography, logos, watermarks",
        max_length=2_000,
    )
    seed: int = Field(default=40_000, ge=0, le=2**63 - 1)
    subject_position: ThumbnailSubjectPosition = "left"
    text_placement: ThumbnailSide = "right"


class ThumbnailTextLayout(DomainModel):
    title: str = Field(min_length=1, max_length=120)
    hook: str = Field(default="", max_length=60)
    palette: ThumbnailPalette = "sunset"
    font_preset: ThumbnailFontPreset = "impact"
    outline: bool = True
    shadow: bool = True
    layout_preset: ThumbnailLayoutPreset = "stacked"


class ThumbnailPlan(DomainModel):
    schema_version: Literal[1] = 1
    project_id: str
    proposed_title: str = Field(min_length=1, max_length=120)
    hook: str = Field(default="", max_length=60)
    audience: str = Field(default="general", max_length=120)
    topic: str = Field(min_length=1, max_length=2_000)
    style: str = Field(default="documentary", max_length=120)
    canvas: tuple[int, int] = (1280, 720)
    concept: ThumbnailConcept
    text_layout: ThumbnailTextLayout
    image_model: ThumbnailImageModel = "krea"
    ideogram_prompt_mode: ThumbnailIdeogramPromptMode = "quick"
    ideogram_prompt_json: dict[str, Any] | None = None
    auto_derived_title: bool = True
    auto_derived_hook: bool = True
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("canvas")
    @classmethod
    def fixed_v1_canvas(cls, value: tuple[int, int]) -> tuple[int, int]:
        if value != (1280, 720):
            raise ValueError("Thumbnail Studio v1 output must be 1280x720")
        return value

    @model_validator(mode="after")
    def precise_ideogram_requires_json(self) -> ThumbnailPlan:
        if (
            self.image_model == "ideogram4_local"
            and self.ideogram_prompt_mode == "precise"
            and self.ideogram_prompt_json is None
        ):
            raise ValueError("Precise Ideogram thumbnail mode requires native prompt JSON")
        return self


class ThumbnailCandidateRequest(DomainModel):
    candidate_id: ThumbnailCandidateId | None = None
    source_asset_id: str | None = Field(default=None, min_length=1, max_length=100)
    source_candidate_id: ThumbnailCandidateId | None = None

    @model_validator(mode="after")
    def one_source(self) -> ThumbnailCandidateRequest:
        if self.source_asset_id and self.source_candidate_id:
            raise ValueError("choose either a source asset or a source candidate")
        return self


class ThumbnailCandidate(DomainModel):
    schema_version: Literal[1] = 1
    candidate_id: ThumbnailCandidateId
    artwork_path: str
    composite_path: str
    manifest_path: str
    artwork_hash: str
    composite_hash: str
    selected: bool = False
    stale: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class ThumbnailSelection(DomainModel):
    schema_version: Literal[1] = 1
    project_id: str
    candidate_id: ThumbnailCandidateId
    composite_path: str
    composite_hash: str
    selected_at: datetime = Field(default_factory=utc_now)
