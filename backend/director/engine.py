"""Director policy that turns a project brief into a validated scene plan."""

from __future__ import annotations

import math
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.local_llm import LocalLLMBackend
from backend.models.ideogram_prompt import aspect_ratio_from_size
from backend.director.image_routing import (
    ROUTABLE_VISUAL_TYPES,
    ImageModelOption,
    build_ideogram_prompt_json,
    build_krea_prompt,
    build_qwen_prompt,
    build_scene_image_routing,
    scene_text_literals,
)
from backend.schemas import DurationMode, Project, ProjectPlan, Scene, VisualType


DirectorTransition = Literal["cut", "crossfade", "fade", "dissolve"]
ImageMotionSource = Literal["krea2", "qwen_image_2512"]
_DIRECTOR_TRANSITIONS = {"cut", "crossfade", "fade", "dissolve"}
_IMAGE_MOTION_SOURCES = {"krea2", "qwen_image_2512"}
_VISUAL_TYPES = {item.value for item in VisualType}
_VISUAL_TYPE_ALIASES = {
    "still": VisualType.FLUX_STILL.value,
    "image": VisualType.IMAGE_MOTION.value,
    "video": VisualType.WAN_VIDEO.value,
    "title": VisualType.TITLE_CARD.value,
}
# Image-model routing values the director may author; anything else normalizes
# to the automatic default and the deterministic router decides.
_AUTHORED_IMAGE_MODELS = {item.value for item in ImageModelOption}

# Structural ceiling on scenes per project, enforced on director output after H3
# expansion. Keep every schema/prompt/materialization bound derived from this.
MAX_PROJECT_SCENES = 128


# Keep the narration guidance compact enough for the local model to follow while
# covering the recurring prose habits that make generated scripts sound canned.
# Project instructions still define the subject, tone, and any deliberate
# exceptions; this policy supplies the default editorial standard.
NARRATION_STYLE_POLICY = (
    "Write narration in direct, natural prose for the stated audience. Cut filler, "
    "jargon, emphasis crutches, expendable adverbs, and vague claims. Prefer active "
    "voice; name human actors when the evidence does, and do not give abstractions "
    "human agency. Avoid 'not X, but Y' reversals, negative-list reveals, instant-answer "
    "rhetorical questions, dramatic fragments, and routine three-item lists. Use concrete "
    "nouns and verbs, vary sentence length, and never use em dashes. Preserve nuance, "
    "uncertainty, quotations, and technical terms; do not trade accuracy for punchiness."
)


def _text_list(value: Any, *, limit: int) -> list[str]:
    """Return a bounded list of nonempty strings from model-authored metadata."""

    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [item.strip() for item in items if isinstance(item, str) and item.strip()][:limit]


def _integer(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _positive_number(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


class DirectorSceneDraft(BaseModel):
    """Only fields the LLM should author for a scene."""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(default=0, ge=0, description="Zero-based scene order.")
    title: str = Field(default="", max_length=300)
    duration: float = Field(default=1, gt=0, description="Scene duration in seconds.")
    narration: str = ""
    visual_prompt: str = ""
    negative_prompt: str = "watermark, logo, illegible text, distortion"
    visual_type: VisualType = VisualType.IMAGE_MOTION
    image_motion_source: ImageMotionSource = "krea2"
    graphic_instructions: str = Field(default="", max_length=8_000)
    graphic_text: list[str] = Field(default_factory=list, max_length=160)
    visual_type_description: str = Field(default="", max_length=4000)
    camera_instruction: str = "slow push in"
    transition: DirectorTransition = "crossfade"
    music_mood: str = "curious and restrained"
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    continue_previous_h3: bool = Field(default=False, description="Set true when this scene is the next shot in an H3 continuity chain.")
    # --- Image-model routing metadata (Ideogram 4 addition) ---
    # The director flags scenes whose picture must contain readable words
    # (thumbnail, title card, poster, labeled map, infographic, sign, document
    # screenshot, UI mockup) and lists the exact strings. Routing then prefers
    # Ideogram 4 for those while Krea stays the default cinematic generator;
    # Qwen remains available as fallback/A-B test.
    needs_embedded_text: bool = Field(
        default=False,
        description="True when the image must contain readable words.",
    )
    text_in_image: str = Field(default="", max_length=2000, description="Exact strings to render inside the image, newline-separated.")
    preferred_image_model: str = Field(default="", description="Optional authored model: krea, qwen_image, or ideogram4_local.")
    test_generate_with_qwen: bool = Field(default=False, description="Also render a Qwen Image version for comparison.")
    test_generate_with_ideogram: bool = Field(default=False, description="Also render an Ideogram 4 version for comparison.")

    @model_validator(mode="before")
    @classmethod
    def normalize_recoverable_metadata(cls, value: Any) -> Any:
        """Coerce invented or malformed production metadata to safe defaults."""

        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        transition = payload.get("transition")
        transition = transition.strip().lower() if isinstance(transition, str) else ""
        if transition not in _DIRECTOR_TRANSITIONS:
            payload["transition"] = "crossfade"
        else:
            payload["transition"] = transition
        visual_type = payload.get("visual_type")
        visual_type = visual_type.strip().lower() if isinstance(visual_type, str) else ""
        visual_type = _VISUAL_TYPE_ALIASES.get(visual_type, visual_type)
        payload["visual_type"] = (
            visual_type if visual_type in _VISUAL_TYPES else VisualType.IMAGE_MOTION.value
        )
        image_motion_source = payload.get("image_motion_source")
        image_motion_source = (
            image_motion_source.strip().lower()
            if isinstance(image_motion_source, str)
            else ""
        )
        payload["image_motion_source"] = (
            image_motion_source
            if image_motion_source in _IMAGE_MOTION_SOURCES
            else "krea2"
        )
        payload["index"] = _integer(
            payload.get("index"), default=0, minimum=0, maximum=2**31 - 1,
        )
        payload["duration"] = _positive_number(payload.get("duration"), default=1)
        payload["seed"] = _integer(
            payload.get("seed"), default=0, minimum=0, maximum=2**63 - 1,
        )
        string_defaults = {
            "title": "",
            "narration": "",
            "visual_prompt": "",
            "negative_prompt": "watermark, logo, illegible text, distortion",
            "camera_instruction": "slow push in",
            "music_mood": "curious and restrained",
            "graphic_instructions": "",
            "visual_type_description": "",
        }
        for field, default in string_defaults.items():
            if not isinstance(payload.get(field), str):
                payload[field] = default
            else:
                payload[field] = payload[field].strip()
        payload["title"] = payload["title"][:300]
        payload["graphic_instructions"] = payload["graphic_instructions"][:8_000]
        payload["visual_type_description"] = payload.get("visual_type_description", "")[:4_000]
        payload["graphic_text"] = [item[:500] for item in _text_list(payload.get("graphic_text"), limit=160)]
        if not payload["negative_prompt"]:
            payload["negative_prompt"] = string_defaults["negative_prompt"]
        if not payload["camera_instruction"]:
            payload["camera_instruction"] = string_defaults["camera_instruction"]
        if not payload["music_mood"]:
            payload["music_mood"] = string_defaults["music_mood"]
        payload["continue_previous_h3"] = bool(payload.get("continue_previous_h3"))
        # Image-routing metadata: coerce invented values to safe defaults. An
        # unknown preferred_image_model is dropped (empty means "automatic"),
        # letting the deterministic router pick Krea/Ideogram/Qwen instead of
        # trusting a hallucinated model name.
        payload["needs_embedded_text"] = bool(payload.get("needs_embedded_text"))
        text_in_image = payload.get("text_in_image")
        if isinstance(text_in_image, list):
            text_in_image = "\n".join(str(item) for item in text_in_image)
        payload["text_in_image"] = (
            text_in_image[:2000] if isinstance(text_in_image, str) else ""
        )
        authored_model = payload.get("preferred_image_model")
        authored_model = (
            authored_model.strip().lower() if isinstance(authored_model, str) else ""
        )
        payload["preferred_image_model"] = (
            authored_model if authored_model in _AUTHORED_IMAGE_MODELS else ""
        )
        payload["test_generate_with_qwen"] = bool(payload.get("test_generate_with_qwen"))
        payload["test_generate_with_ideogram"] = bool(
            payload.get("test_generate_with_ideogram")
        )
        return payload


class DirectorPlanDraft(BaseModel):
    """Schema-constrained response returned by the local director model."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", max_length=300)
    # These summary fields are recoverable from the authored scenes. Some reasoning
    # models omit them even when constrained decoding is requested, so do not discard
    # an otherwise complete script over missing derived metadata.
    outline: list[str] = Field(default_factory=list, max_length=MAX_PROJECT_SCENES)
    scenes: list[DirectorSceneDraft] = Field(min_length=1, max_length=MAX_PROJECT_SCENES)
    strategy_notes: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def normalize_plan_metadata(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        title = payload.get("title")
        payload["title"] = title.strip()[:300] if isinstance(title, str) else ""
        payload["outline"] = _text_list(payload.get("outline"), limit=MAX_PROJECT_SCENES)
        payload["strategy_notes"] = _text_list(payload.get("strategy_notes"), limit=32)
        raw_scenes = payload.get("scenes")
        if isinstance(raw_scenes, Mapping):
            raw_scenes = [raw_scenes]
        scenes: list[dict[str, Any]] = []
        if isinstance(raw_scenes, list):
            for item in raw_scenes:
                if isinstance(item, Mapping):
                    scenes.append(dict(item))
                elif isinstance(item, str) and item.strip():
                    scenes.append({"narration": item.strip()})
        payload["scenes"] = scenes[:MAX_PROJECT_SCENES]
        return payload


class DirectorEngine:
    """Plan economical coverage and reserve costly video for meaningful shots."""

    # llama.cpp counts hidden reasoning and the visible response against
    # max_tokens. Reasoning gets a fixed budget (do not shrink it as a latency
    # optimization); the response budget scales with the plan's scene ceiling so
    # long projects are not truncated into a retryable `finish_reason=length`.
    THINKING_BUDGET_TOKENS = 10_000
    RESPONSE_BASE_TOKENS = 2_048
    RESPONSE_TOKENS_PER_SCENE = 640

    def __init__(self, llm: LocalLLMBackend | None = None) -> None:
        self.llm = llm

    @classmethod
    def _max_completion_tokens(cls, scene_max: int) -> int:
        return (
            cls.THINKING_BUDGET_TOKENS
            + cls.RESPONSE_BASE_TOKENS
            + cls.RESPONSE_TOKENS_PER_SCENE * scene_max
        )

    def plan(self, project: Project, *, mock_mode: bool = False) -> ProjectPlan:
        return self.plan_with_draft(project, mock_mode=mock_mode)[0]

    def plan_with_draft(
        self,
        project: Project,
        *,
        mock_mode: bool = False,
        comparison_mode: bool | None = None,
    ) -> tuple[ProjectPlan, DirectorPlanDraft | None]:
        """Plan scenes while exposing the normalized draft for restartable diagnostics.

        ``comparison_mode`` enables side-by-side Qwen/Ideogram test renders for
        text scenes; when omitted it is read from the project settings so a
        per-project override beats the global config default.
        """

        if comparison_mode is None:
            project_settings = getattr(project, "settings", None)
            comparison_mode = bool(
                project_settings.get("comparison_mode", False)
                if isinstance(project_settings, dict) else False
            )
        if mock_mode or self.llm is None:
            return self._deterministic_plan(project, comparison_mode=comparison_mode), None
        schema = DirectorPlanDraft.model_json_schema()
        scene_count = self._scene_count(project)
        scene_min = scene_count.get("min", 3)
        scene_max = scene_count.get("max", MAX_PROJECT_SCENES)
        # llama.cpp's schema-to-grammar enforces required keys but not minLength /
        # minItems, so the pydantic draft defaults must never be trusted silently:
        # the corrective pass below re-asks when creative fields arrive empty.
        schema["required"] = list(schema["properties"])
        schema["additionalProperties"] = False
        schema["properties"]["title"]["minLength"] = 1
        scene_schema = schema["$defs"]["DirectorSceneDraft"]
        scene_schema["required"] = list(scene_schema["properties"])
        scene_schema["additionalProperties"] = False
        scene_schema["properties"]["title"]["minLength"] = 1
        scene_schema["properties"]["narration"]["minLength"] = 1
        schema["properties"]["scenes"]["minItems"] = scene_min
        schema["properties"]["scenes"]["maxItems"] = scene_max
        max_tokens = self._max_completion_tokens(scene_max)
        messages = [
            {"role": "system", "content": self._system_prompt(project)},
            {"role": "user", "content": self._brief(project)},
        ]
        draft = self._complete_draft(messages, schema, max_tokens=max_tokens)
        problems = self._unfinished_scenes(draft)
        if problems:
            description = "; ".join(
                f"scene {index + 1}: {', '.join(fields)}" for index, fields in problems
            )
            messages.append({
                "role": "user",
                "content": (
                    "The previous plan left required creative fields empty or reused "
                    f"narration text as a visual prompt ({description}). Return the "
                    "complete corrected JSON: every scene needs an authored title and a "
                    "visual_prompt that describes the picture itself — never the "
                    "narration sentence and never text meant to appear on screen. "
                    "Graphic-screen scenes need concise graphic_instructions and exact "
                    "graphic_text. Scenes flagged needs_embedded_text need exact "
                    "text_in_image strings. Keep the narration unchanged."
                ),
            })
            draft = self._complete_draft(messages, schema, max_tokens=max_tokens)
        return self._materialize_plan(project, draft, comparison_mode=comparison_mode), draft

    def _complete_draft(
        self, messages: list[dict[str, str]], schema: dict[str, Any], *, max_tokens: int
    ) -> DirectorPlanDraft:
        assert self.llm is not None
        payload = self.llm.complete(
            messages=messages,
            structured=True,
            json_schema=schema,
            validator=DirectorPlanDraft.model_validate,
            max_tokens=max_tokens,
            temperature=0.2,
            thinking_budget_tokens=self.THINKING_BUDGET_TOKENS,
        )
        return (
            payload
            if isinstance(payload, DirectorPlanDraft)
            else DirectorPlanDraft.model_validate(payload)
        )

    @staticmethod
    def _unfinished_scenes(draft: DirectorPlanDraft) -> list[tuple[int, list[str]]]:
        """List (position, missing fields) for scenes that bypassed field authorship.

        Grammar-constrained decoding can emit empty strings for required keys
        (minLength is not enforced), and a confused model can copy narration into
        visual_prompt. Both ruin downstream image generation, where prose renders
        as literal garbled on-screen lettering, so they are not recoverable
        silently: they trigger one corrective re-ask.
        """

        problems: list[tuple[int, list[str]]] = []
        for position, scene in enumerate(draft.scenes):
            missing: list[str] = []
            if not scene.title.strip():
                missing.append("title")
            prompt = scene.visual_prompt.strip()
            if not prompt:
                missing.append("visual_prompt")
            elif scene.narration.strip() and (
                prompt in scene.narration or scene.narration.strip() in prompt
            ):
                missing.append("visual_prompt (must not reuse narration text)")
            if (
                scene.visual_type is VisualType.GRAPHIC_SCREEN
                and not scene.graphic_instructions.strip()
            ):
                missing.append("graphic_instructions")
            if (
                scene.visual_type is VisualType.QWEN_IMAGE_STILL
                or (
                    scene.visual_type is VisualType.IMAGE_MOTION
                    and scene.image_motion_source == "qwen_image_2512"
                )
            ) and not scene.graphic_text:
                missing.append("graphic_text (the requested in-image strings)")
            if (
                scene.needs_embedded_text
                or scene.preferred_image_model == ImageModelOption.IDEOGRAM4_LOCAL.value
            ) and not scene.text_in_image.strip():
                missing.append(
                    "text_in_image (the exact readable strings for the Ideogram prompt)"
                )
            if missing:
                problems.append((position, missing))
        return problems

    @staticmethod
    def _system_prompt(project: Project) -> str:
        scene_count = DirectorEngine._scene_count(project)
        if project.duration_mode is DurationMode.LLM:
            # No runtime baseline is sent at all: the model owns the runtime.
            duration_rule = (
                "Scene indexes start at zero. You alone decide the final runtime: no "
                "target duration is provided, so author each scene duration from its "
                "narration length at two to three spoken words per second plus a "
                "breath of padding; durations are not rescaled afterward, so the "
                "summed scene durations are the final video length. "
            )
        else:
            duration_rule = (
                "Scene indexes start at zero and durations must sum to the requested "
                "target. "
            )
        return (
            "You are a documentary director. Return only the JSON object constrained by the "
            "provided response schema. "
            f"Create at least {scene_count.get('min', 3)} scenes and no more than {scene_count.get('max', MAX_PROJECT_SCENES)}. "
            + duration_rule +
            "Give each scene a title, narration at two to three spoken words per second, and a "
            "visual_prompt that describes only visible content—not narration, scene numbers, or overlays. "
            + NARRATION_STYLE_POLICY + " "
            "Keep one visual idea for roughly 2–4 sentences, 35–75 spoken words, and 15–30 seconds. "
            "For Krea, specify ONE focal subject or place; no montage, grid, split-screen, or location change; "
            "do not create a new scene merely because a sentence ended. Krea is text-free: no words, signs, "
            "logos, documents, captions, or watermarks. In negative_prompt forbid text, extra panels, duplicates, "
            "anatomy errors, gore, and fabricated evidence. Text routing: use text_overlay_still when a cinematic Krea "
            "background needs one or two exact-text regions, one per text_in_image line. "
            "Use graphic_screen when typography "
            "or structured information is the composition: quotations, timelines, charts, diagrams, interfaces, or "
            "three or more labels. Supply exact graphic_text and graphic_instructions, never HTML. Use Ideogram 4 for integrated typography/layout "
            "in posters, signs, covers, labels, or UI: use ideogram4_still plus ideogram4_local and put 1–2 exact "
            "lines in text_in_image. Generated imagery is never evidence. For non-critical integrated text, set "
            "image_motion_source=qwen_image_2512; otherwise use "
            "image_motion_source=krea2. For image_motion choose slow push in, slow pull out, pan left, pan "
            "right, drift up, or drift down; always set krea2_still camera_instruction to locked; lock "
            "ideogram4_still and qwen_image_still too. Reserve minimax_h3 for a continuous fictional shot, speech, "
            "or hero action; use 5–8 seconds. Set continue_previous_h3=true only on successors and preserve continuity. "
            "Include visual_type_description. Use REAL/reused_media for factual people, organizations, "
            "documents, laws, historical events, and editorial footage, with authentic source metadata. "
            "Keep critical H3 text in a Graphic Screen overlay. preferred_image_model may be krea, "
            "qwen_image, or ideogram4_local."
        )

    @staticmethod
    def _scene_count(project: Project) -> dict[str, int]:
        # In llm duration mode nothing about the requested runtime is sent, so the
        # schema cannot derive scene limits from it either: use the full
        # structural ceiling (the project scene cap) instead.
        if project.duration_mode is DurationMode.LLM:
            return {"min": 3, "max": MAX_PROJECT_SCENES}
        horizon = project.target_duration
        base = max(3, min(24, math.ceil(horizon / 20)))
        return {"min": base, "max": min(MAX_PROJECT_SCENES, max(base, math.ceil(horizon / 5)))}

    @staticmethod
    def _materialize_plan(
        project: Project, draft: DirectorPlanDraft, *, comparison_mode: bool = False,
    ) -> ProjectPlan:
        """Add application-owned fields and normalize ordering/duration deterministically."""

        emitted_indexes = [scene.index for scene in draft.scenes]
        ordered = (
            sorted(draft.scenes, key=lambda scene: scene.index)
            if len(set(emitted_indexes)) == len(emitted_indexes)
            else list(draft.scenes)
        )
        requested = project.target_duration
        generated = sum(scene.duration for scene in ordered)
        if project.duration_mode is DurationMode.LLM:
            # The model owns the runtime outright: authored durations are adopted
            # verbatim as the project target, with no baseline-derived clamping.
            total = generated
        else:
            total = requested
        scale = total / generated
        scaled_durations = [scene.duration * scale for scene in ordered]
        scaled_durations[-1] = total - sum(scaled_durations[:-1])

        # Preserve the project duration without materializing an H3 scene that the
        # Standard policy will reject. Long logical beats become real restartable
        # scenes, with distinct progression prompts and explicit continuity links.
        expanded: list[tuple[DirectorSceneDraft, float]] = []
        for draft_scene, scaled_duration in zip(ordered, scaled_durations, strict=True):
            if draft_scene.visual_type is not VisualType.H3_AUDIOVISUAL:
                expanded.append((draft_scene, scaled_duration))
                continue
            if scaled_duration < 1.0:
                raise ValueError(
                    "Director materialization would create an H3 scene below the 1-second minimum."
                )
            part_count = max(1, math.ceil(scaled_duration / 8.0))
            part_duration = scaled_duration / part_count
            for part_index in range(part_count):
                duration = (
                    scaled_duration - part_duration * (part_count - 1)
                    if part_index == part_count - 1 else part_duration
                )
                if part_count == 1:
                    part = draft_scene
                else:
                    part_number = part_index + 1
                    # A missing base prompt must fall back to a visual description
                    # first, otherwise the continuation suffix alone becomes the
                    # whole prompt and the H3 model receives sequencing metadata.
                    base_prompt = draft_scene.visual_prompt.strip()
                    if not base_prompt or (
                        draft_scene.narration.strip()
                        and (
                            base_prompt in draft_scene.narration
                            or draft_scene.narration.strip() in base_prompt
                        )
                    ):
                        base_prompt = DirectorEngine._fallback_visual_prompt(
                            project, title=draft_scene.title, index=draft_scene.index,
                            motion=True,
                        )
                    part = draft_scene.model_copy(update={
                        "title": f"{draft_scene.title or 'H3 sequence'} — part {part_number}",
                        "visual_prompt": (
                            f"{base_prompt} Sequence {draft_scene.index + 1}, "
                            f"continuation beat {part_number} of {part_count}; advance the action "
                            "while preserving established "
                            "subject, wardrobe, setting, lens, lighting, and screen direction."
                        ).strip(),
                        "continue_previous_h3": (
                            draft_scene.continue_previous_h3 or part_index > 0
                        ),
                    })
                expanded.append((part, duration))
        if len(expanded) > MAX_PROJECT_SCENES:
            raise ValueError(
                f"Director materialization would exceed the {MAX_PROJECT_SCENES}-scene project limit."
            )
        ordered = [item[0] for item in expanded]
        durations = [item[1] for item in expanded]

        scenes: list[Scene] = []
        h3_groups: dict[str, str] = {}
        last_h3_scene: Scene | None = None
        improvised: list[int] = []
        for index, scene in enumerate(ordered):
            # Never fall back to narration text for image prompts: text-to-image
            # models render prompt wording as literal garbled on-screen lettering.
            title = scene.title.strip() or f"Scene {index + 1}"
            narration = scene.narration.strip() or (
                f"This scene explains {project.topic} through the lens of {title}."
            )
            visual_prompt = scene.visual_prompt.strip()
            if not visual_prompt or visual_prompt in narration or narration in visual_prompt:
                visual_prompt = DirectorEngine._fallback_visual_prompt(
                    project, title=title, index=index
                )
                improvised.append(index + 1)
            settings: dict = {}
            if scene.visual_type_description:
                settings["visual_type_description"] = scene.visual_type_description.strip()[:4_000]
            if scene.visual_type is VisualType.GRAPHIC_SCREEN:
                settings["graphic_screen"] = {
                    "instructions": (
                        scene.graphic_instructions
                        or scene.visual_prompt.strip()
                        or f"Design a static explanatory graphic for this line: {narration}"
                    ),
                    "exact_text": scene.graphic_text,
                    "revision": 0,
                }
            elif scene.visual_type is VisualType.TEXT_OVERLAY_STILL:
                settings.update({
                    "text_overlay_background_model": "krea",
                    "text_overlay_layout": "auto",
                    "text_overlay_colors": ["#F2EEE5", "#E78A2E"],
                })
            elif scene.visual_type is VisualType.QWEN_IMAGE_STILL:
                settings["on_screen_text"] = scene.graphic_text
            elif scene.visual_type is VisualType.IMAGE_MOTION:
                settings["image_motion_source"] = scene.image_motion_source
                if scene.image_motion_source == "qwen_image_2512":
                    settings["on_screen_text"] = scene.graphic_text
            is_h3 = scene.visual_type is VisualType.H3_AUDIOVISUAL
            if is_h3:
                settings["h3_quality"] = "standard"
                if scene.continue_previous_h3 and last_h3_scene is not None:
                    prev_id = last_h3_scene.id
                    group = h3_groups.get(prev_id) or f"h3-chain-{last_h3_scene.index + 1:03d}"
                    settings["h3_continuity"] = {
                        "enabled": True,
                        "group": group,
                        "predecessor_scene_id": prev_id,
                    }
            # Image-model routing. The script stage stores a validated Quick
            # mode fallback for restartable preview/provenance; the generation
            # stage performs local Magic Prompt expansion immediately before
            # Ideogram encoding, or uses user-supplied Precise JSON.
            # router merges the director's flags with the keyword detector and
            # picks the primary model: Ideogram 4 for scenes whose picture must
            # contain readable words, Krea for ordinary cinematic coverage, and
            # Qwen stays registered as fallback/A-B test. Per-model prompts are
            # built here so the storyboard artifact carries them verbatim.
            routing_literals = scene_text_literals(
                settings if isinstance(settings, dict) else {},
                scene.text_in_image,
            )
            routing = build_scene_image_routing(
                scene_number=index + 1,
                visual_type=scene.visual_type.value,
                image_motion_source=str(
                    (settings or {}).get("image_motion_source", scene.image_motion_source)
                ),
                title=title,
                visual_description=visual_prompt,
                visual_type_description=scene.visual_type_description,
                text_in_image=routing_literals,
                authored_needs_embedded_text=scene.needs_embedded_text or None,
                authored_preference=scene.preferred_image_model,
                test_generate_with_qwen=scene.test_generate_with_qwen,
                test_generate_with_ideogram=scene.test_generate_with_ideogram,
                comparison_mode=comparison_mode,
            )
            ideogram_prompt_json = build_ideogram_prompt_json(
                visual_prompt,
                text_in_image=routing_literals,
                style=project.style,
                title=title,
                aspect_ratio=aspect_ratio_from_size(*project.resolution),
            )
            ideogram_prompt_mode = (
                "precise"
                if scene.visual_type is VisualType.IDEOGRAM4_STILL and routing_literals
                else "quick"
            )
            if ideogram_prompt_mode == "precise":
                # Short integrated image text receives deterministic native
                # regions by default. The scene editor can still switch back
                # to Quick mode for free-form Magic Prompt expansion.
                settings["ideogram_prompt_json"] = ideogram_prompt_json
            settings["image_prompts"] = {
                "krea_prompt": build_krea_prompt(visual_prompt, style=project.style),
                "qwen_prompt": build_qwen_prompt(visual_prompt, text_in_image=routing_literals),
                "ideogram_prompt_json": ideogram_prompt_json,
                "ideogram_prompt_mode": ideogram_prompt_mode,
            }
            settings["ideogram_prompt_mode"] = ideogram_prompt_mode
            settings["image_routing"] = {
                "needs_embedded_text": routing.needs_embedded_text,
                "preferred_image_model": routing.preferred_image_model,
                "test_generate_with_qwen": routing.test_generate_with_qwen,
                "test_generate_with_ideogram": routing.test_generate_with_ideogram,
            }
            scenes.append(
                Scene(
                    project_id=project.id,
                    index=index,
                    title=title,
                    duration=durations[index],
                    narration=narration,
                    visual_prompt=visual_prompt,
                    negative_prompt=scene.negative_prompt,
                    visual_type=scene.visual_type,
                    needs_embedded_text=routing.needs_embedded_text,
                    text_in_image=routing.text_in_image,
                    preferred_image_model=routing.preferred_image_model,
                    test_generate_with_qwen=routing.test_generate_with_qwen,
                    test_generate_with_ideogram=routing.test_generate_with_ideogram,
                    settings=settings,
                    camera_instruction=(
                        "locked"
                        if scene.visual_type in {
                            VisualType.KREA2_STILL, VisualType.QWEN_IMAGE_STILL,
                            VisualType.IDEOGRAM4_STILL, VisualType.TEXT_OVERLAY_STILL,
                        }
                        else scene.camera_instruction
                    ),
                    transition="cut" if index == 0 else scene.transition,
                    music_mood=scene.music_mood,
                    seed=scene.seed or 10_000 + index,
                )
            )
            if is_h3:
                new_scene = scenes[-1]
                continuity = settings.get("h3_continuity")
                group = (
                    continuity["group"]
                    if continuity
                    else f"h3-chain-{new_scene.index + 1:03d}"
                )
                h3_groups[new_scene.id] = group
                last_h3_scene = new_scene
        outline = draft.outline or [scene.title for scene in scenes]
        strategy_notes = draft.strategy_notes or [
            "Use stills and controlled motion for explanatory coverage.",
            "Reserve local video generation for selected motion and hero shots.",
            "Assemble and mix deterministically with FFmpeg.",
        ]
        if project.duration_mode is DurationMode.LLM:
            strategy_notes = [
                (
                    "Director runtime control: the script sized the video at "
                    f"{total:.0f} s."
                ),
                *strategy_notes,
            ]
        if improvised:
            strategy_notes = [
                "Director warning: "
                f"scene(s) {', '.join(str(item) for item in improvised)} arrived without an "
                "authored visual prompt; placeholder descriptions were synthesized — review "
                "the storyboard and repair these scenes before regenerating visuals.",
                *strategy_notes,
            ]
        return ProjectPlan(
            project_id=project.id,
            title=draft.title or project.title,
            outline=outline,
            scenes=scenes,
            target_duration=total,
            strategy_notes=strategy_notes,
        )

    @staticmethod
    def _fallback_visual_prompt(
        project: Project, *, title: str, index: int, motion: bool = False
    ) -> str:
        """Synthesize a visual description — never prose narration — for a missing prompt.

        The earlier fallback embedded the full narration sentence, which VL text
        encoders (Krea 2 Turbo) render as garbled on-screen lettering, and could
        duplicate the word "documentary" when the project style already is one.
        """

        del index
        style = project.style.strip() or "documentary"
        if "documentary" not in style.lower():
            style = f"{style} documentary"
        topic = project.topic.strip().rstrip(".")
        subject = title.strip().rstrip(".")
        if not subject or re.fullmatch(r"scene\s*\d+", subject, re.IGNORECASE):
            subject = ""
        shot = "shot with deliberate subject motion" if motion else "explanatory still"
        subject_clause = f", centered on {subject}" if subject else ""
        return (
            f"{style} {shot} about {topic}{subject_clause}; clear "
            "single focal subject, grounded realistic detail, clean composition"
        )

    @staticmethod
    def _brief(project: Project) -> str:
        # In llm duration mode the requested runtime is withheld entirely: the
        # director decides it from the script alone.
        duration_line = (
            ""
            if project.duration_mode is DurationMode.LLM
            else f"Duration: {project.target_duration} seconds\n"
        )
        return (
            f"Project id: {project.id}\nTopic: {project.topic}\nTitle: {project.title}\n"
            f"{duration_line}"
            f"Style: {project.style}\n"
            f"Audience: {project.audience}\nAspect ratio: {project.aspect_ratio.value}\n"
            f"Visual quality: {project.visual_quality}\nInstructions: {project.instructions}"
        )

    @staticmethod
    def _deterministic_plan(project: Project, *, comparison_mode: bool = False) -> ProjectPlan:
        scene_count = DirectorEngine._scene_count(project)
        count = scene_count.get("min", 3)
        duration = project.target_duration / count
        outline = [
            "Open with the central question and historical context",
            "Explain the mechanism with clear visual evidence",
            "Connect the mechanism to its practical impact",
            "Conclude with the lasting significance",
        ]
        scenes: list[Scene] = []
        for index in range(count):
            position = index / max(1, count - 1)
            if index == 0:
                visual_type = VisualType.TITLE_CARD
                title = "Opening question"
                text_in_image = project.title.strip() or title
            elif index == count - 1:
                visual_type = VisualType.IMAGE_MOTION
                title = "Conclusion"
                text_in_image = ""
            elif index == count // 2:
                visual_type = VisualType.WAN_VIDEO
                title = "Hero motion detail"
                text_in_image = ""
            elif index % 7 == 3:
                visual_type = VisualType.DIAGRAM
                title = "How it works"
                text_in_image = f"{title}: {project.topic}"
            elif index % 6 == 2:
                visual_type = VisualType.WAN_VIDEO
                title = "Motion detail"
                text_in_image = ""
            else:
                visual_type = VisualType.IMAGE_MOTION
                title = "Documentary coverage"
                text_in_image = ""
            narration = (
                f"Scene {index + 1} explains {project.topic} at the "
                f"{round(position * 100)} percent point of the story."
            )
            routing = build_scene_image_routing(
                scene_number=index + 1,
                visual_type=visual_type.value,
                image_motion_source="krea2",
                title=title,
                visual_description=title,
                text_in_image=text_in_image,
                comparison_mode=comparison_mode and visual_type.value in ROUTABLE_VISUAL_TYPES,
            )
            scenes.append(
                Scene(
                    project_id=project.id,
                    index=index,
                    title=title,
                    duration=duration,
                    narration=narration,
                    visual_prompt=(
                        f"{project.style} image about {project.topic}; {title}; "
                        "historically grounded, clear composition, no text or watermark"
                    ),
                    negative_prompt="watermark, logo, illegible text, duplicate objects, distortion",
                    visual_type=visual_type,
                    needs_embedded_text=routing.needs_embedded_text,
                    text_in_image=routing.text_in_image,
                    preferred_image_model=routing.preferred_image_model,
                    test_generate_with_qwen=routing.test_generate_with_qwen,
                    test_generate_with_ideogram=routing.test_generate_with_ideogram,
                    settings=(
                        {"image_motion_source": "krea2"}
                        if visual_type is VisualType.IMAGE_MOTION
                        else {}
                    ),
                    camera_instruction="slow push in" if visual_type is VisualType.IMAGE_MOTION else "locked",
                    transition="crossfade" if index else "cut",
                    music_mood="curious and restrained",
                    seed=10_000 + index,
                )
            )
        scenes[-1].duration += project.target_duration - sum(scene.duration for scene in scenes)
        return ProjectPlan(
            project_id=project.id,
            title=project.title,
            outline=outline,
            scenes=scenes,
            target_duration=project.target_duration,
            strategy_notes=[
                "Use stills and controlled motion for explanatory coverage.",
                "Reserve local video diffusion for selected motion/hero shots.",
                "Assemble and mix deterministically with FFmpeg.",
            ],
        )

    @staticmethod
    def metadata_prompt(project: Project, plan: ProjectPlan) -> Mapping[str, Any]:
        return {
            "topic": project.topic,
            "title": project.title,
            "scene_count": len(plan.scenes),
            "audience": project.audience,
            "style": project.style,
        }
