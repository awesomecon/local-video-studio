"""Director structured-output and application normalization tests."""

import pytest

from backend.director.engine import DirectorEngine, DirectorPlanDraft
from backend.schemas import DurationMode, Project, VisualType


class CapturingLLM:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.arguments = None

    def complete(self, **kwargs):
        self.arguments = kwargs
        return kwargs["validator"](self.result)


def _draft() -> dict:
    return {
        "title": "A structured plan",
        "outline": ["Opening", "Explanation"],
        "scenes": [
            {
                "index": 4,
                "title": "Second",
                "duration": 10,
                "narration": "Second scene narration.",
                "visual_prompt": "A clear explanatory diagram.",
                "negative_prompt": "watermark",
                "visual_type": "diagram",
                "selected_backend": "flux_comfyui",
                "camera_instruction": "slow push in",
                "transition": "crossfade",
                "music_mood": "curious",
                "seed": 102,
            },
            {
                "index": 2,
                "title": "First",
                "duration": 5,
                "narration": "First scene narration.",
                "visual_prompt": "An establishing documentary still.",
                "negative_prompt": "watermark",
                "visual_type": "flux_still",
                "selected_backend": "flux_comfyui",
                "camera_instruction": "locked wide shot",
                "transition": "dissolve",
                "music_mood": "restrained",
                "seed": 101,
            },
        ],
        "strategy_notes": ["Use motion selectively."],
    }


def test_director_sends_dedicated_schema_and_materializes_domain_plan() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    llm = CapturingLLM(_draft())
    plan = DirectorEngine(llm).plan(project)

    assert llm.arguments["structured"] is True
    schema = llm.arguments["json_schema"]
    assert set(schema["required"]) == {"title", "outline", "scenes", "strategy_notes"}
    scene_schema = schema["$defs"]["DirectorSceneDraft"]
    assert set(scene_schema["required"]) == set(scene_schema["properties"])
    assert schema["additionalProperties"] is False
    assert scene_schema["additionalProperties"] is False
    assert "project_id" not in schema["properties"]
    assert "created_at" not in schema["properties"]
    assert "selected_backend" not in scene_schema["properties"]
    assert schema["properties"]["scenes"]["minItems"] == 3
    assert schema["properties"]["scenes"]["maxItems"] == 12
    # Completion budget scales with the scene ceiling; reasoning stays fixed.
    assert llm.arguments["max_tokens"] == 10_000 + 2_048 + 640 * 12
    assert llm.arguments["thinking_budget_tokens"] == 10_000
    director_prompt = llm.arguments["messages"][0]["content"]
    assert "provided response schema" in director_prompt
    assert "roughly 2–4 sentences, 35–75 spoken words, and 15–30 seconds" in director_prompt
    assert "do not create a new scene merely because a sentence ended" in director_prompt

    assert plan.project_id == project.id
    assert [scene.index for scene in plan.scenes] == [0, 1]
    assert [scene.title for scene in plan.scenes] == ["First", "Second"]
    assert sum(scene.duration for scene in plan.scenes) == pytest.approx(
        project.target_duration
    )
    assert plan.scenes[0].transition == "cut"
    assert plan.scenes[0].visual_type is VisualType.FLUX_STILL
    # The draft's authored backend value is ignored: planned scenes always start
    # at "automatic" and only the scene editor pins a visual backend.
    assert plan.scenes[0].selected_backend == "automatic"


def test_director_defaults_text_bearing_ideogram_still_to_precise_layout() -> None:
    project = Project(
        title="Poster", topic="A fictional launch poster", target_duration=15.0,
        slug="poster",
    )
    scenes = []
    for index in range(3):
        scenes.append({
            "index": index,
            "title": f"Poster {index}",
            "duration": 5,
            "narration": "A short explanatory line.",
            "visual_prompt": "A retro-futurist rocket launch poster.",
            "visual_type": "ideogram4_still",
            "needs_embedded_text": True,
            "text_in_image": "PROJECT HORIZON\nIMAGINED DECADES AGO?",
            "preferred_image_model": "ideogram4_local",
        })
    plan = DirectorEngine(CapturingLLM({
        "title": "Poster plan",
        "outline": ["One", "Two", "Three"],
        "scenes": scenes,
        "strategy_notes": [],
    })).plan(project)

    scene = plan.scenes[0]
    assert scene.settings["ideogram_prompt_mode"] == "precise"
    assert scene.settings["ideogram_prompt_json"] == (
        scene.settings["image_prompts"]["ideogram_prompt_json"]
    )
    texts = [
        element["text"]
        for element in scene.settings["ideogram_prompt_json"]
        ["compositional_deconstruction"]["elements"]
        if element["type"] == "text"
    ]
    assert texts == ["PROJECT HORIZON", "IMAGINED DECADES AGO?"]
    assert scene.camera_instruction == "locked"


def _h3_draft_scene(index: int, title: str, *, continue_previous_h3: bool) -> dict:
    return {
        "index": index,
        "title": title,
        "duration": 6,
        "narration": f"{title} narration.",
        "visual_prompt": "synchronized audiovisual shot.",
        "negative_prompt": "watermark",
        "visual_type": "h3_audiovisual",
        "selected_backend": "minimax_h3",
        "camera_instruction": "locked wide shot",
        "transition": "cut",
        "music_mood": "curious",
        "seed": 1000 + index,
        "continue_previous_h3": continue_previous_h3,
    }


def test_director_chains_three_h3_scenes_into_one_continuity_group() -> None:
    project = Project(title="H3 Chain", topic="Testing", target_duration=18.0, slug="h3chain")
    draft = {
        "title": "H3 chain",
        "scenes": [
            _h3_draft_scene(0, "Take one", continue_previous_h3=False),
            _h3_draft_scene(1, "Take two", continue_previous_h3=True),
            _h3_draft_scene(2, "Take three", continue_previous_h3=True),
        ],
    }
    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    assert [s.visual_type for s in plan.scenes] == [VisualType.H3_AUDIOVISUAL] * 3
    chain = [s.settings.get("h3_continuity") for s in plan.scenes]
    assert chain[0] is None  # chain head has no predecessor
    groups = {c["group"] for c in chain[1:] if c}
    assert len(groups) == 1  # 3 scenes share one group, not a fresh group per link
    assert chain[1]["predecessor_scene_id"] == plan.scenes[0].id
    assert chain[2]["predecessor_scene_id"] == plan.scenes[1].id


def test_director_splits_scaled_h3_beats_at_standard_duration_cap() -> None:
    project = Project(title="Long H3 Chain", topic="Testing", target_duration=60.0, slug="longh3")
    draft = {
        "title": "Long H3 chain",
        "scenes": [
            _h3_draft_scene(0, "Take one", continue_previous_h3=False),
            _h3_draft_scene(1, "Take two", continue_previous_h3=True),
            _h3_draft_scene(2, "Take three", continue_previous_h3=True),
        ],
    }

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    assert len(plan.scenes) == 9
    assert sum(scene.duration for scene in plan.scenes) == pytest.approx(
        project.target_duration
    )
    assert all(5 <= scene.duration <= 8 for scene in plan.scenes)
    assert len({scene.visual_prompt for scene in plan.scenes}) == len(plan.scenes)
    for previous, current in zip(plan.scenes, plan.scenes[1:]):
        assert current.settings["h3_continuity"]["predecessor_scene_id"] == previous.id


def test_director_recovers_optional_summary_fields_from_scenes() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    draft = _draft()
    draft.pop("outline")
    draft.pop("strategy_notes")

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    assert plan.outline == ["First", "Second"]
    assert plan.strategy_notes


def test_director_recovers_a_missing_scene_title() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    draft = _draft()
    draft["scenes"][0].pop("title")

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    # The untitled authored scene sorts second by its supplied index.
    assert plan.scenes[1].title == "Scene 2"


def test_director_recovers_missing_derivable_scene_metadata() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    draft = _draft()
    first = draft["scenes"][0]
    narration = first["narration"]
    for field in (
        "index", "title", "duration", "visual_prompt", "negative_prompt",
        "visual_type", "selected_backend", "camera_instruction", "transition",
        "music_mood", "seed",
    ):
        first.pop(field)

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)
    recovered = plan.scenes[0]

    assert recovered.title == "Scene 1"
    assert recovered.narration == narration
    assert recovered.visual_prompt
    assert recovered.negative_prompt
    assert recovered.selected_backend == "automatic"
    assert recovered.camera_instruction
    assert recovered.music_mood
    assert sum(scene.duration for scene in plan.scenes) == project.target_duration


def test_director_ignores_invented_model_fields() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    draft = _draft()
    draft["generator"] = "invented top-level value"
    draft["scenes"][0]["generator"] = "invented scene value"

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    assert len(plan.scenes) == 2
    assert plan.scenes[1].title == "Second"


def test_director_normalizes_invalid_recoverable_metadata() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    draft = _draft()
    first = draft["scenes"][0]
    first.update({
        "index": -4,
        "title": None,
        "duration": "unknown",
        "visual_prompt": None,
        "negative_prompt": None,
        "visual_type": "cinematic_masterpiece",
        "selected_backend": "mystery_generator",
        "camera_instruction": None,
        "transition": "zoom",
        "music_mood": None,
        "seed": -1,
    })

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)
    recovered = plan.scenes[0]

    assert recovered.title == "Scene 1"
    assert recovered.transition == "cut"
    assert recovered.visual_type is VisualType.IMAGE_MOTION
    assert recovered.selected_backend == "automatic"
    assert recovered.seed == 10_000
    assert recovered.visual_prompt
    assert recovered.negative_prompt


def test_director_normalizes_top_level_shapes_aliases_and_numeric_strings() -> None:
    project = Project(title="Fallback title", topic="Testing", target_duration=60.0, slug="test")
    draft = {
        "title": None,
        "outline": "Single outline item",
        "scenes": {
            "index": "2",
            "duration": "12.5",
            "narration": None,
            "visual_type": "video",
            "selected_backend": "wan",
            "transition": "FADE",
            "seed": "42",
        },
        "strategy_notes": ["Keep it local", None, {"bad": "shape"}],
    }

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)
    scene = plan.scenes[0]

    assert plan.title == project.title
    assert plan.outline == ["Single outline item"]
    assert plan.strategy_notes[-1] == "Keep it local"
    # The scene arrived without an authored title or visual prompt, so the plan
    # must surface a warning instead of silently dumping narration into prompts.
    assert plan.strategy_notes[0].startswith("Director warning:")
    assert scene.narration
    assert scene.visual_type is VisualType.WAN_VIDEO
    assert scene.selected_backend == "automatic"
    assert scene.transition == "cut"
    assert scene.seed == 42


def test_director_salvages_scalar_scenes_and_discards_unusable_items() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    draft = {
        "scenes": ["Authored narration.", 123, None, {"narration": None}],
        "outline": list(range(100)),
    }

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    assert len(plan.scenes) == 2
    assert plan.scenes[0].narration == "Authored narration."
    assert plan.scenes[1].narration
    assert plan.outline == ["Scene 1", "Scene 2"]


def test_system_prompt_is_concise_and_does_not_duplicate_schema() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    prompt = DirectorEngine._system_prompt(project)

    assert "provided response schema" in prompt
    assert '"properties"' not in prompt
    # Raised from 2_000 when image-model routing (Ideogram 4 vs Qwen vs Krea)
    # rules were added to the director contract; still a hard concision bound.
    assert len(prompt) < 2_700
    assert "Use Ideogram 4 for integrated typography/layout" in prompt
    assert "Krea is text-free" in prompt
    assert "use text_overlay_still when a cinematic Krea background" in prompt
    assert "Use graphic_screen when typography" in prompt
    assert "Use REAL/reused_media for factual people" in prompt


def test_director_locks_krea_stills_and_requests_varied_image_motion() -> None:
    project = Project(title="Motion", topic="Testing", target_duration=20.0, slug="motion")
    draft = _draft()
    draft["scenes"] = [{
        **draft["scenes"][0],
        "visual_type": "krea2_still",
        "camera_instruction": "slow push in",
    }]

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)
    prompt = DirectorEngine._system_prompt(project)

    assert plan.scenes[0].camera_instruction == "locked"
    assert "slow pull out" in prompt
    assert "always set krea2_still camera_instruction to locked" in prompt


def test_director_routes_qwen_text_and_locks_the_still() -> None:
    project = Project(title="Text", topic="Storefront", target_duration=20.0, slug="text")
    draft = _draft()
    draft["scenes"] = [{
        **draft["scenes"][0],
        "visual_type": "qwen_image_still",
        "graphic_text": ["OPEN LATE"],
        "camera_instruction": "slow push in",
    }]

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    assert plan.scenes[0].camera_instruction == "locked"
    assert plan.scenes[0].settings["on_screen_text"] == ["OPEN LATE"]


def test_director_can_select_qwen_for_text_bearing_image_motion() -> None:
    project = Project(title="Moving Text", topic="Storefront", target_duration=20.0, slug="moving-text")
    draft = _draft()
    draft["scenes"] = [{
        **draft["scenes"][0],
        "visual_type": "image_motion",
        "image_motion_source": "qwen_image_2512",
        "graphic_text": ["OPEN LATE"],
        "camera_instruction": "slow push in",
    }]

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)
    prompt = DirectorEngine._system_prompt(project)

    assert plan.scenes[0].camera_instruction == "slow push in"
    assert plan.scenes[0].settings["image_motion_source"] == "qwen_image_2512"
    assert plan.scenes[0].settings["on_screen_text"] == ["OPEN LATE"]
    assert "image_motion_source=qwen_image_2512" in prompt
    assert "image_motion_source=krea2" in prompt


def test_director_preserves_graphic_screen_routing_without_html() -> None:
    project = Project(title="Graphic", topic="Token prediction", target_duration=20.0, slug="graphic")
    draft = _draft()
    draft["scenes"] = [{
        **draft["scenes"][0],
        "visual_type": "graphic_screen",
        "graphic_instructions": "A clean left-to-right token probability diagram.",
        "graphic_text": ["INPUT", "62%"],
    }]

    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    settings = plan.scenes[0].settings["graphic_screen"]
    assert plan.scenes[0].visual_type is VisualType.GRAPHIC_SCREEN
    assert settings["instructions"] == "A clean left-to-right token probability diagram."
    assert settings["exact_text"] == ["INPUT", "62%"]
    assert "html" not in settings


class SequenceLLM:
    """Captures director calls and returns queued payloads in order."""

    def __init__(self, results: list[dict]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        return kwargs["validator"](result)


def _empty_creative_draft() -> dict:
    draft = _draft()
    scene = draft["scenes"][0]
    scene.pop("title")
    scene.pop("visual_prompt")
    return draft


def test_director_reasks_once_when_creative_fields_are_empty() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    llm = SequenceLLM([_empty_creative_draft(), _draft()])

    plan, draft = DirectorEngine(llm).plan_with_draft(project)

    assert len(llm.calls) == 2
    repair = llm.calls[1]["messages"][-1]
    assert repair["role"] == "user"
    assert "title" in repair["content"] and "visual_prompt" in repair["content"]
    # The corrected plan is used; no degradation warning is emitted.
    assert any(scene.title == "First" for scene in plan.scenes)
    assert not plan.strategy_notes[0].startswith("Director warning:")
    assert draft.scenes


def test_director_fallback_prompt_never_contains_narration_or_duplicated_style() -> None:
    project = Project(
        title="Local LLMs", topic="local inference", target_duration=60.0,
        slug="local-llms", style="documentary",
    )
    # The corrective re-ask returns the same incomplete payload: fallbacks apply.
    llm = SequenceLLM([_empty_creative_draft(), _empty_creative_draft()])

    plan, draft = DirectorEngine(llm).plan_with_draft(project)

    assert len(llm.calls) == 2
    scene = plan.scenes[0]
    assert scene.visual_prompt
    assert scene.narration not in scene.visual_prompt
    assert scene.visual_prompt not in scene.narration
    assert "documentary documentary" not in scene.visual_prompt.lower()
    assert ":" not in scene.visual_prompt.split(";")[0]
    assert plan.strategy_notes[0].startswith("Director warning:")
    # The normalized draft is exposed so the pipeline can persist it.
    assert draft is not None and not draft.scenes[0].title


def test_director_replaces_visual_prompt_that_copies_narration() -> None:
    project = Project(title="Test", topic="Testing", target_duration=60.0, slug="test")
    copied = _draft()
    copied["scenes"][0]["visual_prompt"] = copied["scenes"][0]["narration"]
    llm = SequenceLLM([copied, copied])

    plan = DirectorEngine(llm).plan(project)

    assert plan.scenes[0].visual_prompt != plan.scenes[0].narration
    assert plan.strategy_notes[0].startswith("Director warning:")


def test_completion_budget_scales_with_scene_ceiling() -> None:
    small = Project(title="S", topic="T", target_duration=60.0, slug="s")
    large = Project(title="L", topic="T", target_duration=480.0, slug="l")
    llm_small, llm_large = CapturingLLM(_draft()), CapturingLLM(_draft())

    DirectorEngine(llm_small).plan(small)
    DirectorEngine(llm_large).plan(large)

    assert llm_large.arguments["max_tokens"] > llm_small.arguments["max_tokens"]
    assert (
        llm_large.arguments["thinking_budget_tokens"]
        == llm_small.arguments["thinking_budget_tokens"]
    )


def _llm_mode_draft(first: float, second: float) -> dict:
    draft = _draft()
    draft["scenes"][0]["duration"] = first
    draft["scenes"][1]["duration"] = second
    return draft


def test_llm_duration_mode_adopts_script_sized_runtime() -> None:
    project = Project(
        title="Runtime", topic="Testing", target_duration=60.0,
        duration_mode=DurationMode.LLM, slug="runtime",
    )
    plan = DirectorEngine(CapturingLLM(_llm_mode_draft(25, 35))).plan(project)

    # Authored durations are adopted verbatim as the runtime.
    assert sum(scene.duration for scene in plan.scenes) == 60.0
    assert plan.target_duration == 60.0
    assert plan.strategy_notes[0] == (
        "Director runtime control: the script sized the video at 60 s."
    )


def test_llm_duration_mode_honors_runtimes_beyond_the_request() -> None:
    project = Project(
        title="Runaway", topic="Testing", target_duration=60.0,
        duration_mode=DurationMode.LLM, slug="runaway",
    )
    plan = DirectorEngine(CapturingLLM(_llm_mode_draft(80, 70))).plan(project)

    # No guardrail clamp: the director's 150 s script is the final runtime.
    assert plan.target_duration == 150.0
    assert sum(scene.duration for scene in plan.scenes) == 150.0
    assert "150 s" in plan.strategy_notes[0]


def test_llm_duration_mode_honors_undersized_scripts() -> None:
    project = Project(
        title="Short", topic="Testing", target_duration=60.0,
        duration_mode=DurationMode.LLM, slug="short",
    )
    plan = DirectorEngine(CapturingLLM(_llm_mode_draft(10, 5))).plan(project)

    # No floor lift: the director's 15 s script is the final runtime.
    assert plan.target_duration == 15.0
    assert sum(scene.duration for scene in plan.scenes) == 15.0


def test_llm_duration_mode_h3_expansion_preserves_adopted_runtime() -> None:
    project = Project(
        title="H3 Runtime", topic="Testing", target_duration=18.0,
        duration_mode=DurationMode.LLM, slug="h3runtime",
    )
    draft = {
        "title": "H3 chain",
        "scenes": [
            _h3_draft_scene(0, "Take one", continue_previous_h3=False),
            _h3_draft_scene(1, "Take two", continue_previous_h3=True),
        ],
    }
    draft["scenes"][0]["duration"] = 14
    plan = DirectorEngine(CapturingLLM(draft)).plan(project)

    # Authored 20 s exceeds the request; H3 splitting preserves the adopted total.
    assert plan.target_duration == 20.0
    assert sum(scene.duration for scene in plan.scenes) == 20.0
    assert len(plan.scenes) == 3


def test_llm_duration_mode_sends_no_runtime_baseline() -> None:
    project = Project(
        id="ceiling-fixed-project", title="Ceiling", topic="Testing", target_duration=40.0,
        duration_mode=DurationMode.LLM, slug="ceiling",
    )
    llm = CapturingLLM(_draft())
    DirectorEngine(llm).plan(project)

    schema_scenes = llm.arguments["json_schema"]["properties"]["scenes"]
    # With no baseline to derive from, the schema uses the structural ceiling.
    assert schema_scenes["minItems"] == 3
    assert schema_scenes["maxItems"] == 128
    prompt = llm.arguments["messages"][0]["content"]
    assert "You alone decide the final runtime" in prompt
    assert "no target duration is provided" in prompt
    assert "40" not in prompt
    brief = llm.arguments["messages"][1]["content"]
    assert "Duration:" not in brief
    assert "40" not in brief

    fixed = Project(title="F", topic="T", target_duration=40.0, slug="f")
    fixed_prompt = DirectorEngine._system_prompt(fixed)
    assert "durations must sum to the requested target" in fixed_prompt
    assert "You control the final runtime" not in fixed_prompt
