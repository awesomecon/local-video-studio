import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from backend.core import load_config
from backend.pipeline import PipelineService
from backend.pipeline.service import PipelineError
from backend.schemas import (
    AssetType,
    GenerationJob,
    JobStatus,
    ProjectCreate,
    ThumbnailCandidateRequest,
    ThumbnailConcept,
    ThumbnailPlan,
    ThumbnailTextLayout,
)
from backend.workers.gpu import GPUSnapshot


def make_service(tmp_path: Path) -> PipelineService:
    return PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )


def make_project(service: PipelineService):
    return service.create_project(ProjectCreate(
        title="Exact Local Title",
        topic="deep sea discoveries",
        target_duration=30,
    ))


def test_thumbnail_schema_is_bounded_and_canvas_is_fixed() -> None:
    concept = ThumbnailConcept(prompt="local artwork")
    layout = ThumbnailTextLayout(title="Exact words")
    with pytest.raises(ValidationError, match="1280x720"):
        ThumbnailPlan(
            project_id="p", proposed_title="Title", topic="Topic",
            canvas=(640, 360), concept=concept, text_layout=layout,
        )
    with pytest.raises(ValidationError):
        ThumbnailTextLayout(title="x", palette="remote-neon")
    with pytest.raises(ValidationError):
        ThumbnailConcept(prompt="x", seed=-1)
    with pytest.raises(ValidationError):
        ThumbnailTextLayout(title="x" * 121)


def test_art_prompt_scrubs_exact_copy_so_krea_never_paints_the_title(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    plan = ThumbnailPlan(
        project_id="p",
        proposed_title="How Local LLMs Work",
        topic="local llm tooling",
        concept=ThumbnailConcept(
            prompt=("A compelling documentary YouTube thumbnail artwork about "
                    "How Local LLMs Work for beginners"),
        ),
        text_layout=ThumbnailTextLayout(
            title="How Local LLMs Work", hook="how local llms work",
        ),
    )
    prompt = service.thumbnails._art_prompt(plan)
    assert "How Local LLMs Work" not in prompt
    assert "the core idea" in prompt
    assert "No text, no letters" in prompt


def test_ideogram_thumbnail_prompt_uses_horizontal_saved_styling(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    plan = ThumbnailPlan(
        project_id="p",
        proposed_title="Project Horizon",
        topic="exploration documentary",
        concept=ThumbnailConcept(
            prompt="A dramatic desert landscape with an explorer",
            avoid_prompt="text, letters, words, typography, logos, watermarks",
            text_placement="right",
        ),
        text_layout=ThumbnailTextLayout(
            title="DID HISTORY PREDICT THIS?",
            hook="PROJECT HORIZON",
            palette="electric",
            font_preset="editorial",
            layout_preset="split",
        ),
        image_model="ideogram4_local",
    )

    payload = service.thumbnails._ideogram_thumb_prompt_json(plan)
    text_elements = [
        element
        for element in payload["compositional_deconstruction"]["elements"]
        if element["type"] == "text"
    ]
    subject = next(
        element
        for element in payload["compositional_deconstruction"]["elements"]
        if element["type"] == "obj"
    )

    text_by_literal = {element["text"]: element for element in text_elements}
    assert text_by_literal["PROJECT HORIZON"]["bbox"] == [80, 500, 210, 950]
    assert text_by_literal["DID HISTORY PREDICT THIS?"]["bbox"] == [620, 500, 850, 950]
    assert subject["bbox"] == [100, 20, 920, 430]
    assert all(
        element["bbox"][3] - element["bbox"][1]
        > element["bbox"][2] - element["bbox"][0]
        for element in text_elements
    )
    assert "editorial serif" in text_by_literal["DID HISTORY PREDICT THIS?"]["desc"]
    assert "zero rotation" in text_by_literal["DID HISTORY PREDICT THIS?"]["desc"]
    assert text_by_literal["DID HISTORY PREDICT THIS?"]["color_palette"] == ["#FFFFFF"]
    assert text_by_literal["PROJECT HORIZON"]["color_palette"] == ["#00D9FF"]
    assert "cinematic 16:9 thumbnail" in payload["high_level_description"]
    serialized = json.dumps(payload)
    assert "pseudo-text" not in serialized
    assert "photograph of a poster" not in serialized
    assert "article or text document" not in serialized

    assert "thick contrasting outline" in text_by_literal["DID HISTORY PREDICT THIS?"]["desc"]
    assert "compact drop shadow" in text_by_literal["DID HISTORY PREDICT THIS?"]["desc"]


def test_ideogram_replaces_legacy_topic_dump_with_visual_direction(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    plan = ThumbnailPlan(
        project_id="p",
        proposed_title="Did Project Horizon Predict Modern Flight?",
        topic="exploration, engineering, institutions, and historical evidence",
        concept=ThumbnailConcept(
            prompt=(
                "A compelling documentary YouTube thumbnail artwork about exploration, "
                "engineering, institutions, and historical evidence; one strong "
                "expressive subject on the left, dramatic lighting, clear visual metaphor"
            ),
        ),
        text_layout=ThumbnailTextLayout(title="PROJECT HORIZON", hook="IMAGINED DECADES AGO?"),
        image_model="ideogram4_local",
    )

    payload = service.thumbnails._ideogram_thumb_prompt_json(plan)
    description = payload["high_level_description"]

    assert "exploration, engineering, institutions" not in description
    assert "main person or object named by the headline" in description
    assert "article or text document" not in json.dumps(payload)


def test_ideogram_thumbnail_runs_magic_prompt_with_real_ratio_and_exact_copy(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    service.mock_mode = False

    class FakeLLM:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def complete(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "aspect_ratio": "16:9",
                "high_level_description": "A cinematic thumbnail from Magic Prompt.",
                "compositional_deconstruction": {
                    "background": "A dramatic red desert at dusk.",
                    "elements": [
                        {
                            "type": "text",
                            "bbox": [50, 500, 350, 950],
                            "text": "LVS_IDEOGRAM_TEXT_000",
                            "desc": "Large headline.",
                        },
                        {
                            "type": "text",
                            "bbox": [360, 500, 470, 950],
                            "text": "LVS_IDEOGRAM_TEXT_001",
                            "desc": "Small kicker.",
                        },
                        {
                            "type": "obj",
                            "bbox": [80, 20, 940, 450],
                            "desc": "An astronaut in the red desert.",
                        },
                    ],
                },
            }

    llm = FakeLLM()
    service.director.llm = llm
    plan = ThumbnailPlan(
        project_id="p",
        proposed_title="Project Horizon",
        topic="exploration documentary",
        concept=ThumbnailConcept(prompt="An astronaut crossing a red desert at dusk"),
        text_layout=ThumbnailTextLayout(
            title=" Project Horizon ", hook="CAFÉ\nNOIR", layout_preset="split",
        ),
        image_model="ideogram4_local",
    )

    result = service.thumbnails._build_ideogram_thumb_prompt(plan)

    assert len(llm.calls) == 1
    user_message = llm.calls[0]["messages"][1]["content"]
    assert "TARGET IMAGE ASPECT RATIO: 16:9 (width:height)." in user_message
    assert "LVS_IDEOGRAM_TEXT_000" in user_message
    assert "LVS_IDEOGRAM_TEXT_001" in user_message
    assert " Project Horizon " not in user_message
    texts = [
        element["text"]
        for element in result["structured_prompt"]["compositional_deconstruction"]["elements"]
        if element["type"] == "text"
    ]
    assert texts == ["CAFÉ\nNOIR", " Project Horizon "]
    assert result["protected_text"] == [" Project Horizon ", "CAFÉ\nNOIR"]
    assert result["warnings"] == []


def test_thumbnail_magic_prompt_is_saved_reused_and_marked_stale(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    plan = ThumbnailPlan.model_validate(service.thumbnails.snapshot(project.id)["plan"])
    plan = service.thumbnails.save_plan(
        project.id,
        plan.model_copy(update={"image_model": "ideogram4_local"}),
    )
    first = service.thumbnails.prepare_ideogram_magic_prompt(project.id)
    prompt_path = (
        service.store.project_path(project)
        / "thumbnails" / "ideogram-magic-prompt.json"
    )
    assert prompt_path.is_file()
    assert first["serialized_prompt"].startswith('{"high_level_description":')
    snapshot = service.thumbnails.snapshot(project.id)
    assert snapshot["magic_prompt"]["status"] == "saved"
    assert snapshot["magic_prompt"]["stale"] is False

    reused = service.thumbnails.prepare_ideogram_magic_prompt(project.id)
    assert reused["reused"] is True
    regenerated = service.thumbnails.prepare_ideogram_magic_prompt(
        project.id, regenerate=True,
    )
    assert regenerated["same_as_previous"] is True
    assert prompt_path.is_file()

    changed = plan.model_copy(update={
        "concept": plan.concept.model_copy(update={"prompt": "A different focal subject"}),
    })
    service.thumbnails.save_plan(project.id, changed)
    assert service.thumbnails.snapshot(project.id)["magic_prompt"]["stale"] is True


def test_precise_thumbnail_plan_bypasses_llm_and_preserves_native_json(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    base = ThumbnailPlan.model_validate(service.thumbnails.snapshot(project.id)["plan"])
    quick_plan = base.model_copy(update={"image_model": "ideogram4_local"})
    precise_json = service.thumbnails._ideogram_thumb_prompt_json(quick_plan)
    precise_plan = ThumbnailPlan.model_validate({
        **quick_plan.model_dump(mode="python"),
        "ideogram_prompt_mode": "precise",
        "ideogram_prompt_json": precise_json,
    })
    service.thumbnails.save_plan(project.id, precise_plan)

    class FailingLLM:
        def complete(self, **_kwargs):
            raise AssertionError("Precise mode must not call the local LLM")

    service.director.llm = FailingLLM()
    saved = service.thumbnails.prepare_ideogram_magic_prompt(
        project.id, regenerate=True,
    )

    assert saved["prompt_mode"] == "precise"
    assert saved["structured_prompt"] == precise_json
    assert saved["protected_text"] == [
        base.text_layout.hook,
        base.text_layout.title,
    ] if base.text_layout.hook else [base.text_layout.title]
    assert "Magic Prompt was bypassed" in saved["warnings"][0]


def test_precise_thumbnail_requires_exact_saved_title_and_hook(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    base = ThumbnailPlan.model_validate(service.thumbnails.snapshot(project.id)["plan"])
    quick_plan = base.model_copy(update={"image_model": "ideogram4_local"})
    precise_json = service.thumbnails._ideogram_thumb_prompt_json(quick_plan)
    precise_json["compositional_deconstruction"]["elements"] = [
        element
        for element in precise_json["compositional_deconstruction"]["elements"]
        if element.get("text") != base.text_layout.title
    ]
    precise_plan = ThumbnailPlan.model_validate({
        **quick_plan.model_dump(mode="python"),
        "ideogram_prompt_mode": "precise",
        "ideogram_prompt_json": precise_json,
    })
    service.thumbnails.save_plan(project.id, precise_plan)

    with pytest.raises(ValueError, match="missing exact thumbnail text"):
        service.thumbnails.prepare_ideogram_magic_prompt(
            project.id, regenerate=True,
        )


def test_thumbnail_magic_prompt_survives_failure_before_ideogram_load(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    plan = ThumbnailPlan.model_validate(service.thumbnails.snapshot(project.id)["plan"])
    plan = service.thumbnails.save_plan(
        project.id,
        plan.model_copy(update={"image_model": "ideogram4_local"}),
    )
    service._snapshot_provider = lambda: (GPUSnapshot(
        index=0, name="test GPU", total_gb=24.0, used_gb=23.0,
        free_gb=1.0, captured_at=0.0,
    ),)
    # Keep this unit test independent of any real localhost Ideogram service.
    # The managed worker may be started before the cold-load VRAM gate, but it
    # must not load the model when the gate rejects the job.
    worker_starts: list[bool] = []
    service.ideogram_worker.ensure_running = (  # type: ignore[method-assign]
        lambda: worker_starts.append(True) or True
    )
    service._prepare_comfy_backend = lambda _name: False  # type: ignore[method-assign]

    # This machine snapshot is below the configured threshold. Dispatch fails
    # at the VRAM gate, after prompt preparation but before the model loads.
    with pytest.raises(PipelineError, match="Free system VRAM"):
        service.thumbnails._dispatch_ideogram4(
            project, plan, tmp_path / "attempt", "job-no-vram", 17,
        )
    assert worker_starts == [True]
    saved = service.thumbnails.snapshot(project.id)["magic_prompt"]
    assert saved["status"] == "saved"
    assert saved["structured_prompt"]


def test_existing_ideogram_candidate_prompt_is_migrated_to_saved_preview(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    plan = ThumbnailPlan.model_validate(service.thumbnails.snapshot(project.id)["plan"])
    plan = service.thumbnails.save_plan(
        project.id,
        plan.model_copy(update={"image_model": "ideogram4_local"}),
    )
    built = service.thumbnails._build_ideogram_thumb_prompt(plan)
    candidate_dir = service.store.project_path(project) / "thumbnails" / "candidate-01"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "manifest.json").write_text(json.dumps({
        "candidate_id": "candidate-01",
        "image_model": "ideogram4_local",
        "stale": False,
        "original_title": plan.text_layout.title,
        "original_hook": plan.text_layout.hook,
        "ideogram_prompt_mode": "quick",
        "ideogram_prompt_json": built["structured_prompt"],
        "ideogram_protected_text": built["protected_text"],
        "ideogram_prompt_warnings": built["warnings"],
        "created_at": "2026-08-27T12:00:00+00:00",
    }, ensure_ascii=False), encoding="utf-8")

    recovered = service.thumbnails.snapshot(project.id)["magic_prompt"]
    assert recovered["status"] == "saved"
    assert recovered["migrated_from_candidate"] == "candidate-01"
    assert recovered["structured_prompt"] == built["structured_prompt"]
    assert (
        service.store.project_path(project)
        / "thumbnails" / "ideogram-magic-prompt.json"
    ).is_file()


def test_mock_candidate_is_portable_restartable_and_selectable(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    initial = service.thumbnails.snapshot(project.id)
    assert initial["plan"]["canvas"] == [1280, 720]
    assert initial["plan"]["hook"] == ""
    assert "single recognizable focal subject" in initial["plan"]["concept"]["prompt"]
    assert (service.store.project_path(project) / "thumbnails/thumbnail-plan.json").is_file()

    job = service.thumbnails.queue_candidate(
        project.id, ThumbnailCandidateRequest(candidate_id="candidate-01"),
    )
    candidate = service.thumbnails.run_candidate_job(job.id)
    assert service.jobs.get(job.id).status is JobStatus.COMPLETED
    root = service.store.project_path(project)
    with Image.open(root / candidate.composite_path) as image:
        assert image.size == (1280, 720)
    manifest = json.loads((root / candidate.manifest_path).read_text(encoding="utf-8"))
    assert manifest["original_title"] == "Exact Local Title"
    assert "No text, no letters" in manifest["krea_prompt"]
    assert manifest["font_hash"]
    assert manifest["attempt_history"][-1]["job_id"] == job.id
    roles = {asset.settings.get("role") for asset in service.database.list_assets(project.id)}
    assert {"thumbnail_artwork", "thumbnail_candidate"}.issubset(roles)
    assert all(
        attempt.job_id == job.id
        for attempt in service.database.list_attempts(job_id=job.id)
    )

    selection = service.thumbnails.select(project.id, candidate.candidate_id)
    assert selection.composite_hash == hashlib.sha256(
        (root / candidate.composite_path).read_bytes()
    ).hexdigest()
    changed_plan = ThumbnailPlan.model_validate(initial["plan"]).model_copy(
        update={"proposed_title": "Changed title"}
    )
    service.thumbnails.save_plan(project.id, changed_plan)
    stale = service.thumbnails.snapshot(project.id)
    assert stale["selection"] is None
    assert stale["candidates"][0]["stale"] is True
    with pytest.raises(ValueError, match="stale candidate"):
        service.thumbnails.select(project.id, candidate.candidate_id)
    replacement = service.thumbnails.queue_candidate(
        project.id,
        ThumbnailCandidateRequest(candidate_id=candidate.candidate_id),
        candidate_id=candidate.candidate_id,
    )
    service.thumbnails.run_candidate_job(replacement.id)
    selection = service.thumbnails.select(project.id, candidate.candidate_id)
    restarted = make_service(tmp_path)
    restored = restarted.thumbnails.snapshot(project.id)
    assert restored["candidates"][0]["selected"] is True
    assert restored["selection"]["composite_hash"] == selection.composite_hash


def test_failed_regeneration_preserves_completed_candidate(tmp_path: Path, monkeypatch) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    first = service.thumbnails.queue_candidate(
        project.id, ThumbnailCandidateRequest(candidate_id="candidate-01"),
    )
    candidate = service.thumbnails.run_candidate_job(first.id)
    composite = service.store.project_path(project) / candidate.composite_path
    before = composite.read_bytes()

    def fail_composite(*args, **kwargs):
        raise RuntimeError("local compositor test failure")

    monkeypatch.setattr(service.graphic_renderer, "render_thumbnail", fail_composite)
    replacement = service.thumbnails.queue_candidate(
        project.id,
        ThumbnailCandidateRequest(candidate_id="candidate-01"),
        candidate_id="candidate-01",
    )
    with pytest.raises(RuntimeError, match="compositor test failure"):
        service.thumbnails.run_candidate_job(replacement.id)
    assert composite.read_bytes() == before
    assert service.jobs.get(replacement.id).status is JobStatus.FAILED
    attempts = service.database.list_attempts(job_id=replacement.id)
    assert len(attempts) == 1 and attempts[0].success is False
    manifest = json.loads(composite.with_name("manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempt_history"][-1]["status"] == "failed"


def test_candidate_deletion_frees_slot_clears_selection_and_keeps_archive(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    project = make_project(service)

    def generate(slot: str):
        job = service.thumbnails.queue_candidate(
            project.id, ThumbnailCandidateRequest(candidate_id=slot),
        )
        return service.thumbnails.run_candidate_job(job.id)

    for slot in ("candidate-01", "candidate-02", "candidate-03"):
        generate(slot)
    selected = service.thumbnails.select(project.id, "candidate-01")
    assert selected.candidate_id == "candidate-01"
    root = service.store.project_path(project)
    candidate_rows_before = {
        asset.id
        for asset in service.database.list_assets(project.id)
        if str(asset.filepath).startswith("thumbnails/candidate-01/")
    }
    assert candidate_rows_before

    # An active job blocks deletion so a running generation cannot resurrect files.
    active = service.jobs.enqueue(GenerationJob(
        project_id=project.id, stage="thumbnail:candidate-02",
    ))
    with pytest.raises(ValueError, match="active thumbnail job"):
        service.thumbnails.delete_candidate(project.id, "candidate-02")
    service.jobs.cancel(active.id)

    result = service.thumbnails.delete_candidate(project.id, "candidate-01")
    assert not (root / "thumbnails" / "candidate-01").exists()
    archived = root / result["archived_to"]
    assert (archived / "composite.png").is_file()
    assert (archived / "manifest.json").is_file()
    snapshot = service.thumbnails.snapshot(project.id)
    assert [item["candidate_id"] for item in snapshot["candidates"]] == [
        "candidate-02", "candidate-03",
    ]
    assert snapshot["selection"] is None
    remaining_assets = {
        asset.id
        for asset in service.database.list_assets(project.id)
        if str(asset.filepath).startswith("thumbnails/candidate-01/")
    }
    assert candidate_rows_before.isdisjoint(remaining_assets)

    # The freed slot accepts new work again, including duplicates.
    duplicate = service.thumbnails.queue_candidate(
        project.id, ThumbnailCandidateRequest(source_candidate_id="candidate-02"),
    )
    assert duplicate.stage == "thumbnail:candidate-01"
    service.thumbnails.run_candidate_job(duplicate.id)
    assert len(service.thumbnails.snapshot(project.id)["candidates"]) == 3

    # Deleting the refilled slot works too, and only then is it reported missing.
    service.thumbnails.delete_candidate(project.id, "candidate-01")
    with pytest.raises(FileNotFoundError):
        service.thumbnails.delete_candidate(project.id, "candidate-01")
    with pytest.raises(FileNotFoundError):
        service.thumbnails.delete_candidate(project.id, "candidate-99")


def test_source_asset_must_be_project_scoped(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = make_project(service)
    second = service.create_project(ProjectCreate(
        title="Other", topic="other", target_duration=10,
    ))
    generated = service._mock_generate(
        first, "image", project_dir=service.store.project_path(first) / "references",
        prompt="source", seed=1, width=1280, height=720,
    )
    asset = service._record_asset(
        first, None, generated.outputs[0],
        AssetType.THUMBNAIL,
        generated, role="thumbnail",
    )
    with pytest.raises(ValueError, match="not found in this project"):
        service.thumbnails.queue_candidate(
            second.id, ThumbnailCandidateRequest(source_asset_id=asset.id),
        )


def test_candidate_file_delivery_is_project_scoped(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project = make_project(service)
    other = service.create_project(ProjectCreate(
        title="Other", topic="other", target_duration=10,
    ))
    candidate_dir = service.store.project_path(project) / "thumbnails" / "candidate-01"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1280, 720), "navy").save(candidate_dir / "composite.png")
    delivered = service.thumbnails.candidate_file(project.id, "candidate-01")
    assert delivered == candidate_dir / "composite.png"
    with pytest.raises(FileNotFoundError):
        service.thumbnails.candidate_file(other.id, "candidate-01")
    with pytest.raises(FileNotFoundError):
        service.thumbnails.candidate_file(project.id, "../project.json")


def test_candidate_run_attributes_media_processes_to_the_job(
    tmp_path: Path, monkeypatch,
) -> None:
    """run_candidate_job wraps its work in media_process_scope(job.id)."""
    from backend.rendering import process as media_process

    service = make_service(tmp_path)
    project = make_project(service)
    job = service.thumbnails.queue_candidate(
        project.id, ThumbnailCandidateRequest(candidate_id="candidate-01"),
    )
    captured: dict[str, object] = {}
    original_render = service.graphic_renderer.render_thumbnail

    def spy_render(*args, **kwargs):
        captured["job_id"] = getattr(media_process._current_job, "job_id", None)
        return original_render(*args, **kwargs)

    monkeypatch.setattr(service.graphic_renderer, "render_thumbnail", spy_render)

    service.thumbnails.run_candidate_job(job.id)

    assert captured["job_id"] == job.id
    # The thread-local attribution is restored once the job runner returns.
    assert getattr(media_process._current_job, "job_id", None) is None
