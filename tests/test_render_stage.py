"""Tests for single deterministic-stage re-run.

Covers the new ``POST /api/projects/{id}/render/stages/{stage}`` endpoint and
the ``render_stage`` job: queueing each stage, unknown-stage rejection, the
single-flight 409, execution updating ``stage_state`` (and reusing the cache
without force), retryability via ``execute_job``, and the ``editorial_visual``
mode gate.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core import load_config
from backend.pipeline import PipelineService
from backend.schemas import GenerationJob, JobStatus, ProjectCreate

CLASSIC_STAGES = (
    "timeline",
    "render_preview",
    "quality_control",
    "render_final",
    "thumbnails",
)


def _api(tmp_path: Path, video_mode: str | None = None,
         resolution: tuple[int, int] = (160, 90)) -> tuple[object, TestClient, str]:
    payload = {
        "title": "Stage Rerun",
        "topic": "single deterministic stage",
        "target_duration": 1,
        "resolution": list(resolution),
        "fps": 12,
    }
    if video_mode is not None:
        payload["video_mode"] = video_mode
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post("/api/projects", json=payload).json()["project"]["id"]
    return app, client, project_id


def _rendered_pipeline(tmp_path: Path) -> tuple[PipelineService, str]:
    pipeline = PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )
    project = pipeline.create_project(
        ProjectCreate(
            title="Stage Rerun",
            topic="single deterministic stage",
            target_duration=1,
            resolution=(160, 90),
            fps=12,
        )
    )
    pipeline.run_project(project.id)
    return pipeline, project.id


def test_queue_render_stage_api(tmp_path: Path) -> None:
    app, client, project_id = _api(tmp_path)
    service = app.state.service
    # A fully rendered project so validate_render_inputs passes for every stage.
    service.run_project(project_id)

    for stage in CLASSIC_STAGES:
        response = client.post(
            f"/api/projects/{project_id}/render/stages/{stage}", json={"force": True},
        )
        assert response.status_code == 202, (stage, response.text)
        body = response.json()
        assert body["project_id"] == project_id
        assert body["stage"] == "render_stage"
        assert body["backend"] == "ffmpeg"
        assert body["status"] == "queued"
        assert body["parameters"]["stage"] == stage
        assert body["parameters"]["force"] is True
        # Each POST is queued behind the previous (still queued) job -> the
        # next stage must be rejected as a conflict until one completes.
        client.post(f"/api/jobs/{body['id']}/cancel")

    # Unknown stage -> 404 with a clear message (no job enqueued).
    unknown = client.post(
        f"/api/projects/{project_id}/render/stages/llm", json={"force": True},
    )
    assert unknown.status_code == 404
    assert "unknown render stage" in unknown.json()["detail"]
    assert "llm" in unknown.json()["detail"]


def test_render_stage_unknown_project_is_404(tmp_path: Path) -> None:
    _app, client, _project_id = _api(tmp_path)
    response = client.post("/api/projects/does-not-exist/render/stages/thumbnails", json={})
    assert response.status_code == 404


def test_render_stage_rejects_missing_inputs_before_queueing(tmp_path: Path) -> None:
    # Fresh project with no scenes: validate_render_inputs fails with a 409,
    # mirroring POST /render, and no job row is created.
    _app, client, project_id = _api(tmp_path)
    response = client.post(f"/api/projects/{project_id}/render/stages/thumbnails", json={})
    assert response.status_code == 409
    assert "no scenes" in response.json()["detail"]
    assert client.get(f"/api/projects/{project_id}").json()["jobs"] == []


def test_render_stage_conflicts_with_inflight_render(tmp_path: Path) -> None:
    app, client, project_id = _api(tmp_path)
    service = app.state.service
    service.run_project(project_id)

    # A full render in flight blocks a stage re-run (and vice versa).
    render_job = service.jobs.enqueue(GenerationJob(project_id=project_id, stage="render"))
    blocked = client.post(
        f"/api/projects/{project_id}/render/stages/thumbnails", json={"force": True},
    )
    assert blocked.status_code == 409
    assert "already running" in blocked.json()["detail"]

    # The reverse: an in-flight stage re-run blocks a new full render.
    service.jobs.cancel(render_job.id)
    service.jobs.enqueue(
        GenerationJob(project_id=project_id, stage="render_stage",
                      parameters={"stage": "thumbnails"}),
    )
    render_blocked = client.post(f"/api/projects/{project_id}/render", json={})
    assert render_blocked.status_code == 409

    # A second stage re-run is also rejected while one is in flight.
    stage_blocked = client.post(
        f"/api/projects/{project_id}/render/stages/render_final", json={},
    )
    assert stage_blocked.status_code == 409


def test_render_stage_editorial_visual_gating(tmp_path: Path) -> None:
    # Classic project: editorial_visual is inapplicable -> 400.
    app, client, classic_id = _api(tmp_path, video_mode="classic")
    service = app.state.service
    service.run_project(classic_id)
    response = client.post(
        f"/api/projects/{classic_id}/render/stages/editorial_visual", json={},
    )
    assert response.status_code == 400
    assert "Editorial Mode" in response.json()["detail"]

    # Editorial project: editorial_visual is addressable -> 202. (Editorial
    # Edit Plans require a >= 320px canvas, so use a larger resolution here.)
    app2, client2, editorial_id = _api(tmp_path / "ed", video_mode="editorial",
                                       resolution=(640, 360))
    app2.state.service.run_project(editorial_id)
    ok = client2.post(f"/api/projects/{editorial_id}/render/stages/editorial_visual", json={})
    assert ok.status_code == 202
    assert ok.json()["stage"] == "render_stage"
    assert ok.json()["parameters"]["stage"] == "editorial_visual"


def test_render_stage_execution_updates_stage_state(tmp_path: Path) -> None:
    pipeline, project_id = _rendered_pipeline(tmp_path)
    root = pipeline.store.project_path(pipeline._project(project_id))

    before = pipeline.project_snapshot(project_id)["stage_state"]["stages"]
    final_before = before["render_final"]
    thumbnails_before = before["thumbnails"]

    job = pipeline.queue_render_stage(project_id, "thumbnails", force=True)
    pipeline.run_render_stage(
        project_id, "thumbnails", force=True, parent_job_id=job.id,
    )

    assert pipeline.jobs.get(job.id).status is JobStatus.COMPLETED
    after = pipeline.project_snapshot(project_id)["stage_state"]["stages"]
    assert after["thumbnails"]["status"] == "completed"
    # The re-run rebuilt the stage: a new bookkeeping job id and a fresh
    # completion timestamp, with the same three output files present on disk.
    assert after["thumbnails"]["job_id"] != thumbnails_before["job_id"]
    assert after["thumbnails"]["completed_at"] != thumbnails_before["completed_at"]
    assert len(after["thumbnails"]["outputs"]) == 3
    assert all((root / path).is_file() and (root / path).stat().st_size > 0
               for path in after["thumbnails"]["outputs"])
    # Only the requested stage ran: the final render record is untouched.
    assert after["render_final"] == final_before
    assert pipeline._project(project_id).status.value == "completed"


def test_render_stage_without_force_reuses_completed_cache(tmp_path: Path) -> None:
    pipeline, project_id = _rendered_pipeline(tmp_path)
    before = pipeline.project_snapshot(project_id)["stage_state"]["stages"]

    job = pipeline.queue_render_stage(project_id, "thumbnails", force=False)
    pipeline.run_render_stage(project_id, "thumbnails", force=False, parent_job_id=job.id)

    assert pipeline.jobs.get(job.id).status is JobStatus.COMPLETED
    after = pipeline.project_snapshot(project_id)["stage_state"]["stages"]
    # Cache hit: the completed stage is reused, so its record is not rewritten.
    assert after["thumbnails"] == before["thumbnails"]


def test_render_stage_is_executable_and_retryable(tmp_path: Path) -> None:
    pipeline, project_id = _rendered_pipeline(tmp_path)
    job = pipeline.queue_render_stage(project_id, "render_final", force=True)

    # The render_stage row is a top-level executable job (hence retryable),
    # like other ffmpeg jobs, and cancelable while non-terminal.
    assert pipeline.job_is_executable(job) is True

    # execute_job is the restart/retry dispatcher: it must run the single
    # stage (not the whole chain) and complete the row.
    pipeline.execute_job(job.id)
    executed = pipeline.jobs.get(job.id)
    assert executed.status is JobStatus.COMPLETED

    # A stage record was (re)written by the re-run.
    stages = pipeline.project_snapshot(project_id)["stage_state"]["stages"]
    assert stages["render_final"]["status"] == "completed"
