"""Local-LLM proposals for ACE-Step music generation settings.

The proposal is advisory: callers present it in the UI and the user explicitly
saves it before any project setting or generated soundtrack changes.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field

from backend.schemas.models import DomainModel, Project, Scene


MUSIC_DIRECTION_MAX_TOKENS = 1200
MUSIC_DIRECTION_THINKING_BUDGET_TOKENS = 1600
MUSIC_DIRECTION_TEMPERATURE = 0.25


class MusicDirectionProposal(DomainModel):
    """Validated musical choices suitable for the ACE-Step form."""

    direction: str = Field(min_length=1, max_length=2000)
    bpm: int = Field(ge=40, le=220)
    key_scale: str = Field(min_length=3, max_length=30)
    time_signature: Literal["2", "3", "4", "6"] = "4"
    intensity: Literal["subtle", "balanced", "expressive"] = "balanced"
    rationale: str = Field(default="", max_length=600)


MUSIC_DIRECTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "minLength": 1, "maxLength": 2000},
        "bpm": {"type": "integer", "minimum": 40, "maximum": 220},
        "key_scale": {"type": "string", "minLength": 3, "maxLength": 30},
        "time_signature": {"type": "string", "enum": ["2", "3", "4", "6"]},
        "intensity": {
            "type": "string",
            "enum": ["subtle", "balanced", "expressive"],
        },
        "rationale": {"type": "string", "maxLength": 600},
    },
    "required": [
        "direction", "bpm", "key_scale", "time_signature", "intensity", "rationale",
    ],
    "additionalProperties": False,
}


def build_music_direction_prompt(
    project: Project,
    scenes: list[Scene],
    *,
    current_settings: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build a compact prompt using only data already stored in the project."""
    scene_context = [
        {
            "index": scene.index + 1,
            "title": scene.title,
            "narration": scene.narration[:700],
            "music_mood": scene.music_mood,
            "duration_seconds": scene.duration,
        }
        for scene in scenes[:40]
    ]
    context = {
        "title": project.title,
        "topic": project.topic,
        "audience": project.audience,
        "style": project.style,
        "instructions": project.instructions[:2000],
        "target_duration_seconds": project.target_duration,
        "scenes": scene_context,
        "current_music_settings": current_settings or {},
    }
    system = (
        "You are a music supervisor for a narrated short video. Choose coherent settings "
        "for an instrumental ACE-Step underscore. The direction must be a production-ready "
        "music-generation prompt describing genre, instrumentation, rhythm, texture, arc, "
        "and what to avoid. It must support speech instead of competing with it. Choose one "
        "specific BPM from 40 through 220, a conventional key and mode such as 'D minor' or "
        "'F major', a time signature of 2, 3, 4, or 6, and an intensity preset. Do not include "
        "lyrics or vocals. Return only the requested JSON object."
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "Create music settings from this local project context:\n"
            + json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def llm_music_direction(
    backend: Any,
    *,
    project: Project,
    scenes: list[Scene],
    current_settings: dict[str, Any] | None = None,
    model: str | None = None,
) -> MusicDirectionProposal:
    """Ask the configured local LLM for validated, reviewable music settings."""
    payload = backend.complete(
        messages=build_music_direction_prompt(
            project, scenes, current_settings=current_settings,
        ),
        structured=True,
        json_schema=MUSIC_DIRECTION_JSON_SCHEMA,
        validator=MusicDirectionProposal.model_validate,
        max_tokens=MUSIC_DIRECTION_MAX_TOKENS,
        temperature=MUSIC_DIRECTION_TEMPERATURE,
        thinking_budget_tokens=MUSIC_DIRECTION_THINKING_BUDGET_TOKENS,
        model=model,
    )
    return (
        payload
        if isinstance(payload, MusicDirectionProposal)
        else MusicDirectionProposal.model_validate(payload)
    )
