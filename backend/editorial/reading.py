"""Build an editable Orthodox reading from a project's existing script."""

from __future__ import annotations

import re

from pydantic import Field

from backend.schemas.models import DomainModel, Project, ProjectPlan, Scene

from .models import (
    EditPlan, EditorialAsset, EditorialAssetType, EditorialCaptionStyle,
    EditorialComposition, EditorialElement, EditorialElementType,
    EditorialEvent, EditorialImageGeneration, EditorialImageModel,
    EditorialTemplate, MotionPrimitive,
)


class ReadingPlanRequest(DomainModel):
    opening_text: str = Field(default="", max_length=134)
    attribution: str = Field(default="", max_length=90)
    closing_quote: str = Field(default="", max_length=134)
    source_credit: str = Field(default="", max_length=180)
    image_model: EditorialImageModel = EditorialImageModel.QWEN_IMAGE_21


READING_PORTRAIT_ID = "reading-portrait"


def build_reading_plan(project: Project, script: ProjectPlan, request: ReadingPlanRequest) -> EditPlan:
    scenes = sorted((scene for scene in script.scenes if scene.narration.strip()), key=lambda s: s.index)
    if not scenes:
        raise ValueError("Add spoken text to your script before creating an Orthodox Reading edit plan.")

    fps = project.fps
    duration_frames = [max(1, round(scene.duration * fps)) for scene in scenes]
    first_words = " ".join(scene.narration.strip() for scene in scenes[:2])
    question = re.search(r"(?:^|[.!]\s+)([^.!?]{6,120}\?)", first_words)
    title = request.opening_text.strip() or (question.group(1).strip() if question else project.title[:134])
    closing = request.closing_quote.strip() or project.title[:134]
    attribution = request.attribution.strip()
    source = request.source_credit.strip()
    compositions: list[EditorialComposition] = []
    cursor = 0

    def add(
        frames: int, scene: Scene, template: EditorialTemplate,
        elements: list[EditorialElement], assets: list[EditorialAsset],
        events: list[EditorialEvent],
    ) -> None:
        nonlocal cursor
        if frames <= 0:
            return
        compositions.append(EditorialComposition(
            id=f"reading-{len(compositions) + 1:03d}",
            start=cursor / fps, duration=frames / fps, template=template,
            assets=assets, elements=elements, events=events, narration_refs=[scene.id],
            transition_out=MotionPrimitive.FADE,
        ))
        cursor += frames

    def card(frames: int, scene: Scene, headline: str, *, credit: str = "") -> None:
        items = [
            EditorialElement(
                id="portrait", type=EditorialElementType.IMAGE, role="portrait",
                asset_id=READING_PORTRAIT_ID,
            ),
            EditorialElement(
                id="headline", type=EditorialElementType.TEXT, role="headline", text=headline,
            ),
        ]
        for role, value in (("attribution", attribution), ("source", credit)):
            if value:
                items.append(EditorialElement(id=role, type=EditorialElementType.TEXT, role=role, text=value))
        portrait = EditorialAsset(
            id=READING_PORTRAIT_ID, type=EditorialAssetType.USER_UPLOADED_IMAGE,
            label="Upload an icon or portrait",
        )
        add(frames, scene, EditorialTemplate.READING_CARD, items, [portrait], [])

    def still(frames: int, scene: Scene) -> None:
        prompt = scene.visual_prompt.strip() or (
            "A quiet, realistically lit natural landscape suitable for a contemplative spoken reading. "
            "Use the following passage only as visual inspiration. "
            f"{scene.narration[:2400]} Avoid lettering, graphics, and logos."
        )
        for size in range(0, frames, 119 * fps):
            length = min(119 * fps, frames - size)
            asset = EditorialAsset(
                id="still", type=EditorialAssetType.GENERATED_IMAGE,
                label=scene.title or "Reading illustration",
                generation=EditorialImageGeneration(
                    prompt=prompt[:8000], negative_prompt=scene.negative_prompt[:4000],
                    seed=scene.seed, model=request.image_model,
                ),
            )
            element = EditorialElement(
                id="image", type=EditorialElementType.IMAGE, role="image", asset_id=asset.id,
            )
            motion = EditorialEvent(time=0, action=MotionPrimitive.SLOW_PUSH, target="image", duration=min(30, length / fps))
            add(length, scene, EditorialTemplate.READING_IMAGE, [element], [asset], [motion])

    for index, (scene, frames) in enumerate(zip(scenes, duration_frames)):
        opening = min(3 * fps, max(1, frames // 3)) if index == 0 else 0
        if opening and len(scenes) > 1 and re.match(r"^(a reading|a passage)\b", scene.narration.strip(), re.I):
            opening = min(5 * fps, frames)
        remaining = frames - opening
        end = min(3 * fps, max(1, remaining // 3), remaining) if index == len(scenes) - 1 else 0
        if opening:
            card(opening, scene, title)
        still(remaining - end, scene)
        if end:
            card(end, scene, closing, credit=source)

    compositions[-1] = compositions[-1].model_copy(update={"transition_out": None})
    return EditPlan(
        project_id=project.id, width=project.resolution[0], height=project.resolution[1], fps=fps,
        reading_layout=True, closing_hold_seconds=2, caption_style=EditorialCaptionStyle.READING,
        compositions=compositions,
    )
