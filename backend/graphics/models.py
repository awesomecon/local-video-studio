"""Bounded contracts for local-LLM authored Graphic Screens."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GraphicScreenResponse(BaseModel):
    """The only model-authored payload accepted by the graphic renderer."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=300)
    design_summary: str = Field(min_length=1, max_length=2_000)
    visible_text: list[str] = Field(min_length=1, max_length=160)
    html_body: str = Field(min_length=1, max_length=80_000)
    css: str = Field(default="", max_length=40_000)

    @field_validator("visible_text")
    @classmethod
    def bounded_visible_text(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 500 for item in value):
            raise ValueError("visible_text entries must be non-empty and at most 500 characters")
        if sum(len(item) for item in value) > 20_000:
            raise ValueError("visible_text is too large")
        return value


class GraphicScreenManifest(BaseModel):
    """Portable provenance for an approved Graphic Screen variant."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    workflow_version: str = "graphic-screen-v1"
    sanitizer_version: str = "graphic-screen-sanitizer-v1"
    renderer: str = "chromium-headless"
    renderer_version: str = "unknown"
    project_resolution: tuple[int, int]
    design_instructions: str = Field(max_length=8_000)
    visible_text: list[str] = Field(max_length=160)
    title: str = Field(max_length=300)
    design_summary: str = Field(max_length=2_000)
    model: str
    model_version: str = "server-managed"
    generation_prompt_version: str = "graphic-screen-prompt-v2"
    font_identity: str = "Noto Sans"
    font_hash: str | None = None
    attempt_number: int = Field(ge=1)
    source_hash: str | None = None
    png_hash: str | None = None
    created_at: datetime = Field(default_factory=_now)
