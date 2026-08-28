"""Image-model routing tests for the Ideogram 4 addition.

Covers the detector, router, per-model prompt builders (Krea / Qwen /
Ideogram structured JSON), comparison mode, the local ComfyUI Ideogram
workflow dispatch, and sample scene objects showing how each generator is
selected. Nothing here removes Qwen: it stays a fallback/A-B candidate.
"""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

import httpx
from PIL import Image

from backend.core import load_config
from backend.director import DirectorEngine
from backend.director.image_routing import (
    ImageModelOption,
    build_ideogram_prompt_json,
    build_krea_prompt,
    build_qwen_prompt,
    build_scene_image_routing,
    detect_needs_embedded_text,
    resolve_preferred_image_model,
    serialize_ideogram_prompt_json,
    storyboard_entry,
    validate_ideogram_prompt_json,
)
from backend.models import BackendRegistry, Ideogram4LocalBackend
from backend.pipeline import PipelineService
from backend.schemas import (
    AssetType, ProjectCreate, ProjectPlan, Scene, ThumbnailCandidateRequest,
    ThumbnailPlan, VisualType,
)
from backend.workers.gpu import GPUSnapshot


# ---------------------------------------------------------------------------
# Unit: detector


def test_detector_flags_text_bearing_scene_kinds() -> None:
    flagged = [
        ("thumbnail", "A bold YouTube thumbnail of the reactor", ""),
        ("title card", "", "An animated title card over black"),
        ("poster", "Vintage travel poster composition", ""),
        ("map with labels", "A map with labels marking trade routes", ""),
        ("infographic", "Clean infographic explaining the process", ""),
        ("sign", "A weathered neon sign at dusk", ""),
        ("newspaper", "Close-up of a newspaper page", ""),
        ("document screenshot", "A document screenshot on a monitor", ""),
        ("ui mockup", "A sleek UI mockup on a laptop screen", ""),
    ]
    for label, description, type_description in flagged:
        assert detect_needs_embedded_text(
            title=label,
            visual_description=description,
            visual_type_description=type_description,
        ), label


def test_detector_defaults_cinematic_broll_to_false() -> None:
    assert not detect_needs_embedded_text(
        title="Documentary coverage",
        visual_description=(
            "Wide aerial shot of a river canyon at golden hour, slow drift "
            "over the water, mist rising between ridges"
        ),
    )


def test_detector_honors_authored_literals_and_flag() -> None:
    assert detect_needs_embedded_text(text_in_image=["OPEN LATE"])
    assert detect_needs_embedded_text(text_in_image="HEADLINE\nSUBTITLE")
    assert detect_needs_embedded_text(authored_flag=True)


def test_text_literals_with_distinct_capitalization_are_not_collapsed() -> None:
    payload = build_ideogram_prompt_json(
        "Two case-sensitive labels",
        text_in_image=["Open", "OPEN"],
    )
    text = [
        item["text"]
        for item in payload["compositional_deconstruction"]["elements"]
        if item["type"] == "text"
    ]
    assert text == ["Open", "OPEN"]


def test_authored_literal_whitespace_is_preserved() -> None:
    payload = build_ideogram_prompt_json(
        "A label with intentional spacing",
        text_in_image=["  OpenAI LABS  "],
    )
    text = [
        item["text"]
        for item in payload["compositional_deconstruction"]["elements"]
        if item["type"] == "text"
    ]
    assert text == ["  OpenAI LABS  "]


def test_title_card_and_diagram_visual_types_need_text() -> None:
    assert detect_needs_embedded_text(visual_type="title_card")
    assert detect_needs_embedded_text(visual_type="diagram")
    assert not detect_needs_embedded_text(visual_type="image_motion")


# ---------------------------------------------------------------------------
# Unit: router


def test_router_prefers_ideogram_for_embedded_text() -> None:
    assert resolve_preferred_image_model(needs_embedded_text=True) == (
        ImageModelOption.IDEOGRAM4_LOCAL.value
    )


def test_router_keeps_hybrid_exact_text_background_on_krea() -> None:
    assert resolve_preferred_image_model(
        needs_embedded_text=True,
        visual_type="text_overlay_still",
    ) == ImageModelOption.KREA.value


def test_ideogram_still_type_routes_to_ideogram_without_text_detection() -> None:
    assert resolve_preferred_image_model(
        needs_embedded_text=False, visual_type="ideogram4_still",
    ) == ImageModelOption.IDEOGRAM4_LOCAL.value


def test_router_defaults_cinematic_scenes_to_krea() -> None:
    assert resolve_preferred_image_model(needs_embedded_text=False) == (
        ImageModelOption.KREA.value
    )
    assert resolve_preferred_image_model(
        needs_embedded_text=False, visual_type="image_motion", image_motion_source="krea2",
    ) == ImageModelOption.KREA.value


def test_router_keeps_qwen_for_legacy_text_still_types() -> None:
    assert resolve_preferred_image_model(
        needs_embedded_text=False, visual_type="qwen_image_still",
    ) == ImageModelOption.QWEN_IMAGE.value
    assert resolve_preferred_image_model(
        needs_embedded_text=False,
        visual_type="image_motion",
        image_motion_source="qwen_image_2512",
    ) == ImageModelOption.QWEN_IMAGE.value


def test_router_explicit_override_wins() -> None:
    assert resolve_preferred_image_model(
        needs_embedded_text=False, authored_preference="ideogram4_local",
    ) == ImageModelOption.IDEOGRAM4_LOCAL.value
    # Unknown names fall back to the rules instead of being trusted.
    assert resolve_preferred_image_model(
        needs_embedded_text=False, authored_preference="midjourney",
    ) == ImageModelOption.KREA.value


def test_comparison_mode_sets_both_test_flags_for_text_scenes() -> None:
    routing = build_scene_image_routing(
        scene_number=3,
        title="Thumbnail",
        visual_description="Bold thumbnail with headline",
        comparison_mode=True,
    )
    assert routing.preferred_image_model == ImageModelOption.IDEOGRAM4_LOCAL.value
    assert routing.test_generate_with_qwen
    assert routing.test_generate_with_ideogram
    assert routing.comparison_pair
    assert routing.scene_number == 3


def test_comparison_flags_stay_off_without_embedded_text() -> None:
    routing = build_scene_image_routing(
        scene_number=1,
        title="B-roll",
        visual_description="River canyon at golden hour",
        comparison_mode=True,
    )
    assert routing.preferred_image_model == ImageModelOption.KREA.value
    assert not routing.test_generate_with_qwen
    assert not routing.test_generate_with_ideogram


# ---------------------------------------------------------------------------
# Unit: prompt builders


def test_krea_prompt_is_cinematic_and_text_free() -> None:
    prompt = build_krea_prompt("A lighthouse above a storm-dark sea", style="documentary")
    assert "lighthouse" in prompt
    assert "free of written words" in prompt
    # Never instructs rendering of lettering.
    assert "exact" not in prompt.lower() or "text" not in prompt.lower()


def test_qwen_prompt_keeps_current_behavior() -> None:
    prompt = build_qwen_prompt(
        "A neon storefront sign at night",
        text_in_image=["OPEN LATE", "NIGHT MARKET"],
    )
    assert prompt.startswith("A neon storefront sign at night\nRender each of these quoted "
                             "strings exactly once, with clear, legible spelling:\n")
    assert '- "OPEN LATE"' in prompt
    assert '- "NIGHT MARKET"' in prompt
    # No literals → plain description, exactly like before.
    assert build_qwen_prompt("Just a valley") == "Just a valley"


def test_ideogram_prompt_json_matches_node_schema_with_exact_text() -> None:
    payload = build_ideogram_prompt_json(
        "A vintage poster of a mountain railway",
        text_in_image=["ALPINE EXPRESS", "DEPARTURES DAILY"],
        style="documentary",
        title="Railways",
    )
    validate_ideogram_prompt_json(payload)
    style = payload["style_description"]
    assert set(style) >= {"aesthetics", "lighting", "art_style", "medium"}
    assert style["medium"] == "graphic_design"
    comp = payload["compositional_deconstruction"]
    assert comp["background"]
    elements = comp["elements"]
    text_elements = [element for element in elements if element["type"] == "text"]
    assert len(text_elements) == 2
    assert [element["text"] for element in text_elements] == [
        "ALPINE EXPRESS", "DEPARTURES DAILY",
    ]
    for element in text_elements:
        assert len(element["bbox"]) == 4
        assert all(isinstance(v, int) and not isinstance(v, bool) for v in element["bbox"])
        assert all(0 <= v <= 1000 for v in element["bbox"])
        height = element["bbox"][2] - element["bbox"][0]
        width = element["bbox"][3] - element["bbox"][1]
        assert width > height, "headline boxes must be horizontal in Ideogram y/x order"
    # Serialization is compact valid JSON ready for the ComfyUI substitution.
    serialized = serialize_ideogram_prompt_json(payload)
    assert json.loads(serialized) == payload


def test_ideogram_prompt_json_valid_without_text_literals() -> None:
    payload = build_ideogram_prompt_json("A calm harbor at dawn")
    validate_ideogram_prompt_json(payload)
    subject_elements = payload["compositional_deconstruction"]["elements"]
    assert len(subject_elements) == 1


def test_validate_rejects_malformed_payloads() -> None:
    for bad in [
        {},  # missing everything
        {"high_level_description": "x", "style_description": {"aesthetics": 1},
         "compositional_deconstruction": {"background": "", "elements": [{"type": "obj", "bbox": [0, 0, 0, 0], "desc": "d"}]}},
    ]:
        try:
            validate_ideogram_prompt_json(bad)
        except ValueError:
            continue
        raise AssertionError(f"payload should be rejected: {bad}")


# ---------------------------------------------------------------------------
# Sample scene objects (requirement 13)


def _sample_scene(project_id: str, index: int) -> Scene:
    """Return the three canonical routing examples as persisted scenes."""

    if index == 0:
        # Thumbnail with headline text -> Ideogram.
        return Scene(
            project_id=project_id, index=0, title="Thumbnail", duration=5,
            narration="Why this channel covers deep time.",
            visual_prompt="Dramatic desert excavation site at sunset, tools in foreground",
            visual_type=VisualType.IMAGE_MOTION,
            needs_embedded_text=True,
            text_in_image="WE BURIED THE PAST",
            preferred_image_model=ImageModelOption.IDEOGRAM4_LOCAL.value,
            seed=11,
            settings={"image_motion_source": "krea2"},
        )
    if index == 1:
        # Documentary scene with no visible text -> Krea.
        return Scene(
            project_id=project_id, index=1, title="Excavation b-roll", duration=5,
            narration="The dig continued through the season.",
            visual_prompt="Archaeologists brushing sediment from stone tools in warm light",
            visual_type=VisualType.IMAGE_MOTION,
            needs_embedded_text=False,
            preferred_image_model=ImageModelOption.KREA.value,
            seed=12,
            settings={"image_motion_source": "krea2"},
        )
    # Fallback / comparison image -> Qwen (legacy text-capable path kept).
    return Scene(
        project_id=project_id, index=2, title="Legacy caption card", duration=5,
        narration="A plaque marks the discovery.",
        visual_prompt="Bronze commemorative plaque on a stone wall",
        visual_type=VisualType.QWEN_IMAGE_STILL,
        needs_embedded_text=True,
        text_in_image="SITE 7 — EST. 1921",
        preferred_image_model=ImageModelOption.QWEN_IMAGE.value,
        seed=13,
        settings={"on_screen_text": ["SITE 7 — EST. 1921"]},
    )


def test_sample_scene_objects_route_to_distinct_models(tmp_path: Path) -> None:
    from backend.schemas import ProjectCreate as PC

    service = PipelineService(load_config(environ={}), database_path=tmp_path / "db.sqlite3",
                              project_root=tmp_path / "projects", temp_root=tmp_path / "tmp")
    project = service.create_project(PC(title="Routing samples", topic="samples", target_duration=10))
    samples = [_sample_scene(project.id, index) for index in range(3)]
    entries = []
    for scene in samples:
        entry = storyboard_entry(scene)
        entries.append(entry)
    thumbnail, b_roll, legacy = entries

    assert thumbnail["preferred_image_model"] == "ideogram4_local"
    assert thumbnail["needs_embedded_text"] is True
    assert "WE BURIED THE PAST" in json.dumps(thumbnail["ideogram_prompt_json"])
    assert thumbnail["scene_number"] == 1

    assert b_roll["preferred_image_model"] == "krea"
    assert b_roll["needs_embedded_text"] is False
    assert "free of written words" in b_roll["krea_prompt"]

    assert legacy["preferred_image_model"] == "qwen_image"
    assert '"SITE 7 — EST. 1921"' in legacy["qwen_prompt"]

    # Every storyboard entry carries all documented fields.
    expected_fields = {
        "scene_number", "narration", "visual_description", "needs_embedded_text",
        "text_in_image", "preferred_image_model", "qwen_prompt", "krea_prompt",
        "ideogram_prompt_mode", "ideogram_prompt_json",
    }
    for entry in entries:
        assert set(entry) == expected_fields


# ---------------------------------------------------------------------------
# Integration: registry, config, director mock plan


def test_registry_registers_ideogram_alongside_existing_generators() -> None:
    registry = BackendRegistry.from_config({}, mock_mode=False)
    names = registry.names()
    assert "ideogram4_local_comfyui" in names
    # Existing generators are untouched.
    assert "krea2_comfyui" in names
    assert "qwen_image_2512_comfyui" in names
    descriptor = registry.get("ideogram4_local_comfyui").descriptor()
    assert descriptor.model_name == "Ideogram 4"
    assert descriptor.backend_name == "ideogram4_local_comfyui"
    assert descriptor.quantization == "nf4"


def test_image_generation_config_loads_comparison_mode() -> None:
    config = load_config(environ={
        "LOCAL_VIDEO_STUDIO__IMAGE_GENERATION__COMPARISON_MODE": "true",
    })
    assert config.image_generation.comparison_mode is True
    assert config.image_generation.general_model == "krea"
    assert config.image_generation.text_model == "ideogram4_local"
    assert config.image_generation.fallback_text_model == "qwen_image"
    defaults = load_config(environ={})
    assert defaults.image_generation.comparison_mode is False


def test_deterministic_plan_routes_title_card_to_ideogram() -> None:
    from backend.schemas import Project

    request = ProjectCreate(title="Deep time", topic="geology", target_duration=60)
    project = Project(slug="deep-time", **request.model_dump())
    plan = DirectorEngine(None).plan(project, mock_mode=True)
    first = plan.scenes[0]
    assert first.visual_type is VisualType.TITLE_CARD
    assert first.needs_embedded_text is True
    assert first.text_in_image
    assert first.preferred_image_model == ImageModelOption.IDEOGRAM4_LOCAL.value
    # Non-text coverage stays on Krea.
    motion = next(s for s in plan.scenes if s.visual_type is VisualType.IMAGE_MOTION)
    assert motion.preferred_image_model == ImageModelOption.KREA.value
    assert motion.needs_embedded_text is False


# ---------------------------------------------------------------------------
# Integration: ComfyUI dispatch through fake transports


def _service(tmp_path: Path, *, comparison_mode: bool = False) -> PipelineService:
    environ = {
        "LOCAL_VIDEO_STUDIO__PATHS__GENERATION_CACHE_ROOT": str(
            tmp_path / "app" / "generation-cache"
        ),
        "LOCAL_VIDEO_STUDIO__BACKENDS__IDEOGRAM4_LOCAL__MANAGED": "false",
    }
    if comparison_mode:
        environ["LOCAL_VIDEO_STUDIO__IMAGE_GENERATION__COMPARISON_MODE"] = "true"
    snapshot = GPUSnapshot(
        index=0, name="RTX 4090", total_gb=23.5, used_gb=1.0,
        free_gb=22.5, captured_at=0.0,
    )
    service = PipelineService(
        load_config(environ=environ),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=False,
        snapshot_provider=lambda: (snapshot,),
    )
    # Prompt expansion is covered with a fake LLM in unit tests. Integration
    # dispatch stays deterministic and never depends on the user's port 1234.
    service.director.llm = None
    return service


def _fake_comfy(submitted: dict, filename: str, *, ideogram_resident: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={
                "system": {},
                "comfyui_version": "0.18.1",
                "devices": [{
                    "torch_vram_total": 16 * 1024**3 if ideogram_resident else 0,
                    "torch_vram_free": 32 * 1024**2 if ideogram_resident else 0,
                }],
            })
        if request.url.path == "/prompt":
            body = json.loads(request.content)
            submitted[body["client_id"]] = body["prompt"]
            return httpx.Response(200, json={"prompt_id": f"pid-{filename}"})
        if request.url.path.startswith("/history/pid-"):
            return httpx.Response(200, json={
                f"pid-{filename}": {
                    "status": {"status_str": "success"},
                    "outputs": {"3" if "ideogram" in filename else "10": {"images": [{
                        "filename": filename, "subfolder": "", "type": "output",
                    }]}},
                },
            })
        if request.url.path == "/view":
            if filename == "ideogram_thumb.png":
                data = BytesIO()
                Image.new("RGB", (1536, 864), "navy").save(data, format="PNG")
                return httpx.Response(200, content=data.getvalue())
            return httpx.Response(200, content=f"png:{filename}".encode())
        if request.url.path == "/free":
            return httpx.Response(200)
        raise AssertionError(f"unexpected ComfyUI path: {request.url.path}")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def test_ideogram_scene_dispatches_structured_json_workflow(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.config.backends.ideogram4_local.managed = True
    starts: list[bool] = []
    service.ideogram_worker.ensure_running = lambda: starts.append(True) or True  # type: ignore[method-assign]
    project = service.create_project(ProjectCreate(
        title="Poster night", topic="generated signage", target_duration=10,
    ))
    scene = Scene(
        project_id=project.id, index=0, title="Poster", duration=5,
        narration="A poster hangs in the window.",
        visual_prompt="A framed concert poster on a brick wall, warm spotlight",
        negative_prompt="watermark", visual_type=VisualType.IMAGE_MOTION,
        needs_embedded_text=True,
        text_in_image="MIDNIGHT CHOIR\nLIVE TONIGHT",
        preferred_image_model=ImageModelOption.IDEOGRAM4_LOCAL.value,
        seed=77,
        settings={"image_motion_source": "krea2"},
    )
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(poll_interval=0, client_factory=_fake_comfy(submitted, "ideo_00001.png")),
        name="ideogram4_local_comfyui", replace=True,
    )

    asset = service.generate_scene(scene.id)

    assert starts == [True]
    assert asset.type is AssetType.IMAGE
    assert asset.backend == "ideogram4_local_comfyui"
    assert asset.model == "Ideogram 4"
    assert asset.workflow_version == "ideogram4-nf4-v1"
    prompt = next(iter(submitted.values()))
    node_inputs = prompt["2"]["inputs"]
    assert prompt["1"]["class_type"] == "Ideogram4PipelineLoader"
    assert prompt["1"]["inputs"]["model_weights"] == "4.0 NF4"
    assert prompt["2"]["class_type"] == "Ideogram4Generate"
    structured = json.loads(node_inputs["prompt"])
    validate_ideogram_prompt_json(structured)
    assert any(e.get("text") == "MIDNIGHT CHOIR"
               for e in structured["compositional_deconstruction"]["elements"])
    assert any(e.get("text") == "LIVE TONIGHT"
               for e in structured["compositional_deconstruction"]["elements"])
    assert int(node_inputs["seed"]) == 77
    assert prompt["3"]["class_type"] == "SaveImage"
    # The published visual lands at the standard scene location.
    assert (service.store.project_path(project) / asset.filepath).name == "visual.png"


def test_ideogram_scene_recovers_worker_residency_before_cold_load_vram_gate(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service._snapshot_provider = lambda: (_ for _ in ()).throw(
        AssertionError("warm Ideogram must not run the cold-load VRAM check")
    )
    project = service.create_project(ProjectCreate(
        title="Warm Ideogram", topic="resident model reuse", target_duration=10,
    ))
    scene = Scene(
        project_id=project.id, index=0, title="Poster", duration=5,
        narration="A poster appears.", visual_prompt="A bold local event poster",
        visual_type=VisualType.IDEOGRAM4_STILL,
        preferred_image_model=ImageModelOption.IDEOGRAM4_LOCAL.value,
        seed=79,
    )
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(
            poll_interval=0,
            client_factory=_fake_comfy(
                submitted, "ideogram_warm_scene.png", ideogram_resident=True,
            ),
        ),
        name="ideogram4_local_comfyui",
        replace=True,
    )

    asset = service.generate_scene(scene.id)

    assert asset.backend == "ideogram4_local_comfyui"
    assert submitted
    assert service.resident_comfy_backend == "ideogram4_local_comfyui"


def test_first_class_ideogram_still_and_shot_use_magic_prompt_path(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(ProjectCreate(
        title="Ideogram stills", topic="native still routing", target_duration=10,
    ))
    scene = Scene(
        project_id=project.id, index=0, title="Native still", duration=5,
        visual_prompt='A neon storefront sign saying "OPEN LATE"',
        visual_type=VisualType.IDEOGRAM4_STILL,
        text_in_image="OPEN LATE", seed=101,
    )
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(
            poll_interval=0,
            client_factory=_fake_comfy(submitted, "ideogram_scene_still.png"),
        ),
        name="ideogram4_local_comfyui", replace=True,
    )

    scene_asset = service.generate_scene(scene.id)
    assert scene_asset.backend == "ideogram4_local_comfyui"

    shot = service.create_shot(scene.id, {
        "duration_seconds": 3,
        "lane": "image",
        "visual_type": "ideogram4_still",
        "visual_prompt": 'A theater marquee reading "CAFÉ NOIR"',
        "settings": {
            "ideogram_prompt_mode": "quick",
            "text_in_image": "CAFÉ NOIR",
        },
        "seed": 102,
    })
    shot_asset = service.generate_shot(shot.id)
    assert shot_asset.backend == "ideogram4_local_comfyui"
    assert shot_asset.settings["visual_type"] == "ideogram4_still"
    workflows = list(submitted.values())
    shot_prompt = json.loads(workflows[-1]["2"]["inputs"]["prompt"])
    texts = [
        element["text"]
        for element in shot_prompt["compositional_deconstruction"]["elements"]
        if element["type"] == "text"
    ]
    assert "CAFÉ NOIR" in texts


def test_ideogram_precise_scene_preserves_native_kjnodes_json(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(ProjectCreate(
        title="Precise perfume", topic="product advertisement", target_duration=10,
    ))
    precise = {
        "high_level_description": "A luxury black perfume advertisement.",
        "style_description": {
            "aesthetics": "minimal, premium, luxurious",
            "lighting": "dramatic controlled studio lighting",
            "photo": "high-end 85mm commercial product photography",
            "medium": "photograph",
            "color_palette": ["#080808", "#FFFFFF", "#C0C0C0"],
        },
        "compositional_deconstruction": {
            "background": "Black marble with subtle glossy reflections.",
            "elements": [
                {
                    "type": "text", "bbox": [70, 160, 220, 840],
                    "text": "NOIR", "desc": "Large elegant centered serif typography.",
                    "color_palette": ["#FFFFFF"],
                },
                {
                    "type": "obj", "bbox": [280, 300, 760, 700],
                    "desc": "A centered black glass perfume bottle.",
                    "color_palette": ["#111111", "#C0C0C0"],
                },
                {
                    "type": "text", "bbox": [800, 280, 880, 720],
                    "text": "EAU DE PARFUM", "desc": "Small uppercase typography.",
                },
            ],
        },
    }
    scene = Scene(
        project_id=project.id, index=0, title="Perfume", duration=5,
        visual_prompt="This natural-language prompt must not replace precise JSON.",
        visual_type=VisualType.IMAGE_MOTION, needs_embedded_text=True,
        preferred_image_model=ImageModelOption.IDEOGRAM4_LOCAL.value, seed=88,
        settings={
            "image_motion_source": "krea2",
            "ideogram_prompt_mode": "precise",
            "ideogram_prompt_json": precise,
        },
    )
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(
            poll_interval=0,
            client_factory=_fake_comfy(submitted, "ideo_precise.png"),
        ),
        name="ideogram4_local_comfyui",
        replace=True,
    )

    service.generate_scene(scene.id)

    structured = json.loads(next(iter(submitted.values()))["2"]["inputs"]["prompt"])
    assert structured == precise
    assert structured["compositional_deconstruction"]["elements"][0]["text"] == "NOIR"
    assert structured["compositional_deconstruction"]["elements"][0]["bbox"] == [
        70, 160, 220, 840,
    ]


def test_ideogram_thumbnail_uses_quality_workflow_and_normalizes_output(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    project = service.create_project(ProjectCreate(
        title="Project Horizon", topic="a historical exploration proposal", target_duration=10,
    ))
    plan = ThumbnailPlan.model_validate(service.thumbnails.snapshot(project.id)["plan"])
    service.thumbnails.save_plan(
        project.id,
        plan.model_copy(update={"image_model": "ideogram4_local"}),
    )
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(
            poll_interval=0,
            client_factory=_fake_comfy(submitted, "ideogram_thumb.png"),
        ),
        name="ideogram4_local_comfyui",
        replace=True,
    )

    job = service.thumbnails.queue_candidate(
        project.id, ThumbnailCandidateRequest(candidate_id="candidate-01"),
    )
    candidate = service.thumbnails.run_candidate_job(job.id)

    workflow = next(iter(submitted.values()))
    inputs = workflow["2"]["inputs"]
    assert inputs["sampler_preset"] == "4.0 Quality 48"
    assert int(inputs["width"]) == 1536
    assert int(inputs["height"]) == 864
    validate_ideogram_prompt_json(json.loads(inputs["prompt"]))
    root = service.store.project_path(project)
    with Image.open(root / candidate.composite_path) as image:
        assert image.size == (1280, 720)
    manifest = json.loads((root / candidate.manifest_path).read_text(encoding="utf-8"))
    assert manifest["workflow_version"] == "ideogram4-thumbnail-nf4-quality48-v2"
    assert manifest["ideogram_prompt_mode"] == "quick"
    assert manifest["ideogram_prompt_json"] == json.loads(inputs["prompt"])
    assert manifest["ideogram_protected_text"]
    assert manifest["ideogram_prompt_warnings"] == [
        "Local LLM unavailable; used deterministic Ideogram prompt fallback."
    ]


def test_ideogram_thumbnail_recovers_worker_residency_before_vram_gate(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service._snapshot_provider = lambda: (_ for _ in ()).throw(
        AssertionError("warm Ideogram must not run the cold-load VRAM check")
    )
    project = service.create_project(ProjectCreate(
        title="Warm Thumbnail", topic="resident model reuse", target_duration=10,
    ))
    plan = ThumbnailPlan.model_validate(service.thumbnails.snapshot(project.id)["plan"])
    service.thumbnails.save_plan(
        project.id,
        plan.model_copy(update={"image_model": "ideogram4_local"}),
    )
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(
            poll_interval=0,
            client_factory=_fake_comfy(
                submitted, "ideogram_thumb.png", ideogram_resident=True,
            ),
        ),
        name="ideogram4_local_comfyui",
        replace=True,
    )

    job = service.thumbnails.queue_candidate(
        project.id, ThumbnailCandidateRequest(candidate_id="candidate-01"),
    )
    candidate = service.thumbnails.run_candidate_job(job.id)

    assert submitted
    assert candidate.candidate_id == "candidate-01"
    assert service.resident_comfy_backend == "ideogram4_local_comfyui"


def test_comparison_mode_renders_both_variants_separately(tmp_path: Path) -> None:
    service = _service(tmp_path, comparison_mode=True)
    project = service.create_project(ProjectCreate(
        title="Compare", topic="text rendering", target_duration=10,
    ))
    scene = Scene(
        project_id=project.id, index=0, title="Headline", duration=5,
        narration="A headline dominates the frame.",
        visual_prompt="Newsroom wall behind glass, city lights beyond",
        visual_type=VisualType.IMAGE_MOTION,
        needs_embedded_text=True,
        text_in_image="THE DAILY SIGNAL",
        seed=91,
        settings={"image_motion_source": "krea2"},
    )
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(poll_interval=0, client_factory=_fake_comfy(submitted, "ideo_cmp.png")),
        name="ideogram4_local_comfyui", replace=True,
    )
    from backend.models import QwenImage2512Backend
    service.registry.register(
        QwenImage2512Backend(poll_interval=0, client_factory=_fake_comfy(submitted, "qwen_cmp.png")),
        name="qwen_image_2512_comfyui", replace=True,
    )

    asset = service.generate_scene(scene.id)

    root = service.store.project_path(project)
    # Primary (routed to Ideogram) plus a separate Qwen variant for review.
    assert (root / "scenes" / "001" / "visual.png").is_file()
    qwen_variant = root / "scenes" / "001" / "comparisons" / "qwen" / "visual.png"
    assert qwen_variant.is_file()
    assert (root / "scenes" / "001" / "comparisons" / "ideogram").exists() is False
    assert asset.settings["role"] == "visual"

    comparison_assets = [
        a for a in service.database.list_assets(project.id, scene.id)
        if a.settings.get("role") == "comparison"
    ]
    assert len(comparison_assets) == 1
    assert comparison_assets[0].backend == "qwen_image_2512_comfyui"
    assert comparison_assets[0].settings["comparison_for_scene"] is True
    assert comparison_assets[0].settings["primary_image_model"] == "ideogram4_local"


def test_explicit_test_flags_render_both_candidates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(ProjectCreate(
        title="AB test", topic="signage", target_duration=10,
    ))
    scene = Scene(
        project_id=project.id, index=0, title="Sign", duration=5,
        narration="The sign glows.",
        visual_prompt="Rooftop neon sign above a wet street",
        visual_type=VisualType.QWEN_IMAGE_STILL,
        needs_embedded_text=True,
        text_in_image="HOTEL LUNA",
        preferred_image_model=ImageModelOption.QWEN_IMAGE.value,
        test_generate_with_qwen=True,
        test_generate_with_ideogram=True,
        seed=55,
        settings={"on_screen_text": ["HOTEL LUNA"]},
    )
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    submitted: dict = {}
    service.registry.register(
        Ideogram4LocalBackend(poll_interval=0, client_factory=_fake_comfy(submitted, "ideo_ab.png")),
        name="ideogram4_local_comfyui", replace=True,
    )
    from backend.models import QwenImage2512Backend
    service.registry.register(
        QwenImage2512Backend(poll_interval=0, client_factory=_fake_comfy(submitted, "qwen_ab.png")),
        name="qwen_image_2512_comfyui", replace=True,
    )

    service.generate_scene(scene.id)

    root = service.store.project_path(project)
    # Primary stays the Qwen still; Ideogram renders beside it.
    assert (root / "scenes" / "001" / "visual.png").is_file()
    assert (root / "scenes" / "001" / "comparisons" / "ideogram" / "visual.png").is_file()
    assert (root / "scenes" / "001" / "comparisons" / "qwen").exists() is False
    payloads = list(submitted.values())
    assert any(p["2"]["inputs"]["clip_name"].startswith("qwen") for p in payloads)
    ideogram_payloads = [p for p in payloads if "3" in p and p["3"]["class_type"] == "SaveImage"]
    assert len(ideogram_payloads) == 1


def test_unrouted_scene_keeps_legacy_krea_dispatch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(ProjectCreate(
        title="Legacy", topic="cinematic coverage", target_duration=10,
    ))
    scene = Scene(
        project_id=project.id, index=0, title="B-roll", duration=5,
        narration="Water moves through the canyon.",
        visual_prompt="Aerial river canyon at golden hour",
        visual_type=VisualType.IMAGE_MOTION,
        seed=21,
        settings={"image_motion_source": "krea2"},
    )
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    submitted: dict = {}
    service.registry.register(
        __import__("backend.models", fromlist=["Krea2Backend"]).Krea2Backend(
            poll_interval=0, client_factory=_fake_comfy(submitted, "krea_legacy.png"),
        ),
        name="krea2_comfyui", replace=True,
    )

    asset = service.generate_scene(scene.id)

    assert asset.backend == "krea2_comfyui"
    assert asset.model == "Krea 2 Turbo"
    # No routing artifacts were created.
    assert not (service.store.project_path(project) / "scenes" / "001" / "comparisons").exists()


def test_storyboard_artifact_written_by_plan_stage(tmp_path: Path) -> None:
    service = PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
        initialize=True,
    )
    project = service.create_project(ProjectCreate(
        title="Storyboard doc", topic="tidal power", target_duration=30,
    ))
    plan = service.ensure_plan(project.id)
    storyboard_path = (
        service.store.project_path(service._project(project.id)) / "script" / "storyboard.json"
    )
    payload = json.loads(storyboard_path.read_text(encoding="utf-8"))
    assert payload["project_id"] == project.id
    assert len(payload["scenes"]) == len(plan.scenes)
    first = payload["scenes"][0]
    assert first["scene_number"] == 1
    assert first["narration"] == plan.scenes[0].narration
    assert first["visual_description"] == plan.scenes[0].visual_prompt
    validate_ideogram_prompt_json(first["ideogram_prompt_json"])
    assert set(first) == {
        "scene_number", "narration", "visual_description", "needs_embedded_text",
        "text_in_image", "preferred_image_model", "qwen_prompt", "krea_prompt",
        "ideogram_prompt_mode", "ideogram_prompt_json",
    }
