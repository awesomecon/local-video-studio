"""Local-LLM planning for validated, renderer-owned Editorial compositions."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import Field

from backend.captions import CaptionWord
from backend.models.errors import BackendError, BackendErrorCode
from backend.models.local_llm import LocalLLMBackend
from backend.schemas import Asset, Project, ProjectPlan, Scene, VideoMode
from backend.schemas.models import DomainModel

from .models import (
    EditPlan, EditorialAssetType, EditorialComposition, EditorialTemplate,
    EvidenceClass, MotionPrimitive, TEMPLATE_ELEMENT_SLOTS, TEMPLATE_REQUIRED_ROLES,
)


class EditorialPlanDraft(DomainModel):
    """LLM-authored portion of an Edit Plan; project-owned fields are excluded."""

    compositions: list[EditorialComposition] = Field(min_length=1, max_length=32)


class EditorialCompositionDraft(DomainModel):
    """One LLM-authored replacement used for explicit partial regeneration."""

    composition: EditorialComposition


class EditorialPlanner:
    """Decide what happens while keeping layout and motion implementation trusted."""

    THINKING_BUDGET_TOKENS = 16_384
    MAX_COMPLETION_TOKENS = 32_768

    def __init__(self, llm: LocalLLMBackend | None = None) -> None:
        self.llm = llm

    def plan(
        self,
        project: Project,
        script: ProjectPlan,
        *,
        assets: Sequence[Asset] = (),
        word_timings: Sequence[CaptionWord] = (),
        mock_mode: bool = False,
    ) -> EditPlan:
        return self.plan_with_draft(
            project,
            script,
            assets=assets,
            word_timings=word_timings,
            mock_mode=mock_mode,
        )[0]

    def plan_with_draft(
        self,
        project: Project,
        script: ProjectPlan,
        *,
        assets: Sequence[Asset] = (),
        word_timings: Sequence[CaptionWord] = (),
        mock_mode: bool = False,
    ) -> tuple[EditPlan, EditorialPlanDraft | None]:
        if project.video_mode is not VideoMode.EDITORIAL:
            raise ValueError("the Editorial Planner requires an Editorial Mode project")
        if script.project_id != project.id:
            raise ValueError("script plan does not belong to the Editorial project")
        if mock_mode or self.llm is None:
            return self._deterministic_plan(project, script, word_timings), None

        schema = EditorialPlanDraft.model_json_schema()
        context = self._context(project, script, assets, word_timings)
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        try:
            draft = self._complete(messages, schema)
        except BackendError as exc:
            if exc.code is not BackendErrorCode.INVALID_RESPONSE:
                raise
            messages.append({
                "role": "user",
                "content": (
                    "The previous JSON failed the validated Editorial contract. Return a "
                    "complete corrected object using only the provided template slots, asset "
                    "ids, narration refs, and motion names. Do not add source URLs or code."
                ),
            })
            draft = self._complete(messages, schema)
        return self._materialize(project, script, draft, assets, word_timings), draft

    def regenerate_composition(
        self,
        project: Project,
        script: ProjectPlan,
        plan: EditPlan,
        composition_id: str,
        *,
        assets: Sequence[Asset] = (),
        word_timings: Sequence[CaptionWord] = (),
        mock_mode: bool = False,
    ) -> tuple[EditorialComposition, EditorialCompositionDraft | None]:
        """Regenerate one composition without changing its timeline or protected media."""
        if project.video_mode is not VideoMode.EDITORIAL:
            raise ValueError("the Editorial Planner requires an Editorial Mode project")
        if script.project_id != project.id or plan.project_id != project.id:
            raise ValueError("Editorial regeneration inputs do not belong to the project")
        try:
            index = next(
                i for i, composition in enumerate(plan.compositions)
                if composition.id == composition_id
            )
        except StopIteration as exc:
            raise ValueError(f"unknown Editorial composition {composition_id!r}") from exc
        current = plan.compositions[index]
        if mock_mode or self.llm is None:
            return current, None

        context = self._context(project, script, assets, word_timings)
        regenerate_only = current.model_dump(mode="json")
        for item in regenerate_only.get("assets", []):
            if isinstance(item, dict):
                # Persisted paths are trusted application state, not planner output.
                # The planner sees the binding but must return source=null; the exact
                # protected record is restored after validation.
                item["source"] = None
        context.update({
            "regenerate_only": regenerate_only,
            "previous_composition": (
                plan.compositions[index - 1].model_dump(mode="json") if index else None
            ),
            "next_composition": (
                plan.compositions[index + 1].model_dump(mode="json")
                if index + 1 < len(plan.compositions) else None
            ),
            "fixed_fields": [
                "id", "start", "duration", "template", "narration_refs", "caption_refs",
            ],
            "protected_asset_ids": [
                item.id for item in current.assets if self._asset_is_protected(item, assets)
            ],
        })
        messages = [
            {"role": "system", "content": self._system_prompt() + (
                " Regenerate only regenerate_only. Keep its fixed_fields unchanged. "
                "Protected assets and their bound element slots are immutable; return their "
                "ids and bindings unchanged but always set source=null. Use neighboring "
                "compositions for continuity, not as output."
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        schema = EditorialCompositionDraft.model_json_schema()
        try:
            draft = self._complete_composition(messages, schema)
        except BackendError as exc:
            if exc.code is not BackendErrorCode.INVALID_RESPONSE:
                raise
            messages.append({
                "role": "user",
                "content": (
                    "The replacement failed validation. Return one corrected composition, "
                    "preserving every fixed field and protected asset binding exactly."
                ),
            })
            draft = self._complete_composition(messages, schema)
        authored = draft.composition.model_copy(update={
            "id": current.id,
            "start": current.start,
            "duration": current.duration,
            "template": current.template,
            "narration_refs": current.narration_refs,
            "caption_refs": current.caption_refs,
        })
        resolved = self._materialize_composition(
            project, script, authored, assets, word_timings,
        )
        return self._restore_protected_content(current, resolved, assets), draft

    def _complete(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
    ) -> EditorialPlanDraft:
        assert self.llm is not None
        payload = self.llm.complete(
            messages=messages,
            structured=True,
            json_schema=schema,
            validator=EditorialPlanDraft.model_validate,
            max_tokens=self.MAX_COMPLETION_TOKENS,
            temperature=0.2,
            thinking_budget_tokens=self.THINKING_BUDGET_TOKENS,
        )
        return payload if isinstance(payload, EditorialPlanDraft) else EditorialPlanDraft.model_validate(payload)

    def _complete_composition(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
    ) -> EditorialCompositionDraft:
        assert self.llm is not None
        payload = self.llm.complete(
            messages=messages,
            structured=True,
            json_schema=schema,
            validator=EditorialCompositionDraft.model_validate,
            max_tokens=self.MAX_COMPLETION_TOKENS,
            temperature=0.2,
            thinking_budget_tokens=self.THINKING_BUDGET_TOKENS,
        )
        return (
            payload if isinstance(payload, EditorialCompositionDraft)
            else EditorialCompositionDraft.model_validate(payload)
        )

    def _materialize(
        self,
        project: Project,
        script: ProjectPlan,
        draft: EditorialPlanDraft,
        assets: Sequence[Asset],
        word_timings: Sequence[CaptionWord],
    ) -> EditPlan:
        if any(asset.project_id != project.id for asset in assets):
            raise ValueError("available Editorial assets must belong to the project")
        compositions = [
            self._materialize_composition(project, script, authored, assets, word_timings)
            for authored in draft.compositions
        ]
        return EditPlan(
            project_id=project.id,
            width=project.resolution[0],
            height=project.resolution[1],
            fps=project.fps,
            compositions=compositions,
        )

    def _materialize_composition(
        self,
        project: Project,
        script: ProjectPlan,
        authored: EditorialComposition,
        assets: Sequence[Asset],
        word_timings: Sequence[CaptionWord],
    ) -> EditorialComposition:
        if any(asset.project_id != project.id for asset in assets):
            raise ValueError("available Editorial assets must belong to the project")
        if not authored.elements or not authored.events:
            raise ValueError("each Editorial composition requires elements and timed events")
        duration = self._timeline_duration(script.scenes, word_timings)
        if authored.start + authored.duration > duration + 0.1:
            raise ValueError("Editorial composition extends beyond the narration timeline")
        known_narration = {scene.id for scene in script.scenes}
        if not authored.narration_refs:
            raise ValueError("Editorial composition requires narration references")
        if any(ref not in known_narration for ref in authored.narration_refs):
            raise ValueError("Editorial composition references unknown narration")
        registered = {asset.id: asset for asset in assets}
        resolved_assets = []
        for planned_asset in authored.assets:
            if planned_asset.source is not None:
                raise ValueError("the Editorial Planner cannot author asset source paths or URLs")
            registered_asset = (
                registered.get(planned_asset.asset_id)
                if planned_asset.asset_id is not None else None
            )
            if planned_asset.asset_id is not None and registered_asset is None:
                raise ValueError("Editorial composition references an unknown project asset")
            if planned_asset.type is EditorialAssetType.EXISTING_ASSET and registered_asset is None:
                raise ValueError("existing_asset recommendations require a registered asset_id")
            if planned_asset.evidence_class is EvidenceClass.EVIDENCE:
                if registered_asset is None or registered_asset.backend != "imported_local":
                    raise ValueError(
                        "only verified user-imported local media may be planned as evidence"
                    )
            resolved_assets.append(planned_asset.model_copy(update={
                "source": str(registered_asset.filepath) if registered_asset else None,
            }))
        return EditorialComposition.model_validate({
            **authored.model_dump(mode="python"),
            "assets": resolved_assets,
        })

    @staticmethod
    def _asset_is_protected(planned_asset, assets: Sequence[Asset]) -> bool:
        registered = {asset.id: asset for asset in assets}
        linked = registered.get(planned_asset.asset_id or "")
        return bool(
            planned_asset.locked
            or planned_asset.type is EditorialAssetType.USER_UPLOADED_IMAGE
            or planned_asset.metadata.get("manual_replacement") is True
            or (linked is not None and linked.backend == "imported_local")
        )

    def _restore_protected_content(
        self,
        current: EditorialComposition,
        replacement: EditorialComposition,
        assets: Sequence[Asset],
    ) -> EditorialComposition:
        protected = {
            item.id: item for item in current.assets if self._asset_is_protected(item, assets)
        }
        if not protected:
            return replacement
        protected_elements = [
            item for item in current.elements if item.asset_id in protected
        ]
        protected_ids = {item.id for item in protected_elements}
        protected_roles = {item.role for item in protected_elements}
        displaced_ids = {
            item.id for item in replacement.elements
            if item.role in protected_roles and item.id not in protected_ids
        }
        merged_assets = [item for item in replacement.assets if item.id not in protected]
        merged_assets.extend(protected.values())
        merged_elements = [
            item for item in replacement.elements
            if item.id not in protected_ids and item.role not in protected_roles
        ]
        merged_elements.extend(protected_elements)
        replacement_events = [
            event for event in replacement.events if event.target not in displaced_ids
        ]
        event_targets = {event.target for event in replacement_events}
        restored_events = [
            event for event in current.events
            if event.target in protected_ids and event.target not in event_targets
        ]
        merged_events = sorted(
            [*replacement_events, *restored_events], key=lambda event: event.time,
        )
        return EditorialComposition.model_validate({
            **replacement.model_dump(mode="python"),
            "assets": merged_assets,
            "elements": merged_elements,
            "events": merged_events,
        })

    @classmethod
    def _deterministic_plan(
        cls,
        project: Project,
        script: ProjectPlan,
        word_timings: Sequence[CaptionWord],
    ) -> EditPlan:
        duration = cls._timeline_duration(script.scenes, word_timings)
        refs = [scene.id for scene in script.scenes]
        numeral = re.search(r"\b\d{4}\b", " ".join(scene.narration for scene in script.scenes))
        headline = numeral.group(0) if numeral else project.title.upper()[:18]
        reveal = project.title.upper()[:24]
        composition = EditorialComposition.model_validate({
            "id": "editorial-001",
            "start": 0,
            "duration": duration,
            "template": EditorialTemplate.ARCHIVE_CANVAS,
            "elements": [
                {"id": "headline", "type": "text", "text": headline, "role": "year"},
                {"id": "reveal", "type": "text", "text": reveal, "role": "reveal"},
            ],
            "events": [
                {"time": 0, "action": "fadeUp", "target": "headline", "duration": 0.8},
                {
                    "time": max(0.0, duration - 1.5), "action": "collapseToBlack",
                    "target": "canvas", "duration": min(0.8, duration / 3),
                },
                {
                    "time": max(0.0, duration - 0.7), "action": "fadeUp",
                    "target": "reveal", "duration": min(0.6, duration / 3),
                },
            ],
            "narration_refs": refs,
        })
        return EditPlan(
            project_id=project.id,
            width=project.resolution[0], height=project.resolution[1], fps=project.fps,
            compositions=[composition],
        )

    @staticmethod
    def _timeline_duration(
        scenes: Sequence[Scene], word_timings: Sequence[CaptionWord],
    ) -> float:
        if word_timings:
            return max(word.end_seconds for word in word_timings)
        return sum(scene.duration for scene in scenes)

    @classmethod
    def _context(
        cls,
        project: Project,
        script: ProjectPlan,
        assets: Sequence[Asset],
        word_timings: Sequence[CaptionWord],
    ) -> dict[str, Any]:
        duration = cls._timeline_duration(script.scenes, word_timings)
        planned_duration = sum(scene.duration for scene in script.scenes) or duration
        scale = duration / planned_duration
        cursor = 0.0
        narration = []
        for scene in script.scenes:
            start = cursor * scale
            cursor += scene.duration
            narration.append({
                "ref": scene.id,
                "text": scene.narration,
                "start": round(start, 3),
                "end": round(cursor * scale, 3),
            })
        return {
            "project": {
                "title": project.title, "topic": project.topic, "style": project.style,
                "audience": project.audience, "instructions": project.instructions,
                "width": project.resolution[0], "height": project.resolution[1],
                "fps": project.fps, "duration": duration,
            },
            "narration": narration,
            "word_timestamps": [word.to_dict() for word in word_timings],
            "available_assets": [cls._asset_context(asset) for asset in assets],
            "approved_templates": [item.value for item in EditorialTemplate],
            "template_slots": {
                template.value: {role: kind.value for role, kind in slots.items()}
                for template, slots in TEMPLATE_ELEMENT_SLOTS.items()
            },
            "template_required_roles": {
                template.value: sorted(roles)
                for template, roles in TEMPLATE_REQUIRED_ROLES.items()
            },
            "approved_motion_primitives": [item.value for item in MotionPrimitive],
            "approved_image_models": ["krea", "qwen_image", "ideogram4_local"],
        }

    @staticmethod
    def _asset_context(asset: Asset) -> dict[str, Any]:
        source = asset.settings.get("source")
        return {
            "asset_id": asset.id,
            "type": asset.type.value,
            "scene_id": asset.scene_id,
            "backend": asset.backend,
            "model": asset.model,
            "source_classification": (
                source.get("classification") if isinstance(source, dict) else None
            ),
        }

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are the Editorial Planner for a deterministic documentary motion-graphics "
            "renderer. Return only JSON matching the supplied schema. Decide WHAT visual "
            "events happen; never return HTML, CSS, JavaScript, font names, colors, coordinates, "
            "or invented animation names. Narration/audio timestamps are the master clock. "
            "Prefer one evolving 5–20 second composition over sentence-by-sentence full-screen "
            "images. Choose only an approved template and use only that template's exact unique "
            "element roles and types from template_slots; omit unused optional roles but include "
            "the core visual roles implied by the template. Element ids may be concise unique slugs; "
            "events target those ids. Use only supplied narration refs and registered asset ids. "
            "Set source=null: the application resolves registered assets. A missing visual may be "
            "recommended as generated_image with evidence_class=illustration. For each such asset, "
            "include validated generation instructions with a concrete visual-only prompt, optional "
            "negative_prompt, seed, and one approved_image_model. Generated media is never evidence. "
            "Evidence requires a supplied imported_local asset and locked=true. "
            "Important dates, names, quotations, and labels belong in deterministic text elements, "
            "not image prompts. Keep captions separate from editorial text. Use restrained, "
            "deliberate events from the approved motion list and keep every event within its "
            "composition duration."
        )
