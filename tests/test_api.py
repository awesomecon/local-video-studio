from pathlib import Path
from io import BytesIO
import json

import yaml
from fastapi.testclient import TestClient
from PIL import Image

from backend.api.main import create_app
from backend.core import load_config
from backend.core.config import DEFAULT_CONFIG_PATH
from backend.models import LocalLLMBackend
from backend.models.errors import BackendError, BackendErrorCode
from backend.schemas import GenerationJob, JobStatus


def test_project_plan_and_status_api(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    created = client.post(
        "/api/projects",
        json={
            "title": "Aqueducts",
            "topic": "Roman aqueducts",
            "target_duration": 3,
            "resolution": [160, 90],
            "fps": 12,
        },
    )
    assert created.status_code == 201
    project_id = created.json()["project"]["id"]
    planned = client.post(f"/api/projects/{project_id}/plan", json={})
    assert planned.status_code == 200
    assert len(planned.json()["scenes"]) == 3
    snapshot = client.get(f"/api/projects/{project_id}")
    assert snapshot.status_code == 200
    assert len(snapshot.json()["scenes"]) == 3
    model_list = client.get("/api/models")
    assert model_list.status_code == 200
    assert model_list.json()["runtime"]["mock"]["state"] == "mock"
    assert client.get("/api/jobs").status_code == 200


def test_editorial_plan_api_requires_script_then_persists_mock_plan(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    created = client.post("/api/projects", json={
        "title": "Editorial Mars", "topic": "Project Mars", "target_duration": 14,
        "resolution": [1080, 1920], "fps": 24, "video_mode": "editorial",
    })
    project_id = created.json()["project"]["id"]

    missing_script = client.post(f"/api/projects/{project_id}/editorial/plan")
    assert missing_script.status_code == 409
    assert "script" in missing_script.json()["detail"]

    assert client.post(f"/api/projects/{project_id}/plan", json={}).status_code == 200
    generated = client.post(f"/api/projects/{project_id}/editorial/plan")
    assert generated.status_code == 200
    assert generated.json()["project_id"] == project_id
    assert generated.json()["compositions"][0]["template"] == "archiveCanvas"

    snapshot = client.get(f"/api/projects/{project_id}").json()
    assert snapshot["editorial"]["has_edit_plan"] is True
    assert snapshot["editorial"]["plan_status"] == "current"
    assert snapshot["editorial"]["stale"] is False
    assert snapshot["editorial"]["stale_reasons"] == []
    assert snapshot["editorial"]["generate_url"].endswith("/editorial/plan")
    assert client.get(f"/api/projects/{project_id}/editorial/edit-plan").json() == generated.json()
    downloaded = client.get(
        f"/api/projects/{project_id}/editorial/edit-plan?download=true"
    )
    assert downloaded.status_code == 200
    assert downloaded.json() == generated.json()
    assert downloaded.headers["content-disposition"] == (
        'attachment; filename="edit-plan.json"'
    )
    # The idempotent endpoint returns the existing plan rather than replacing it.
    assert client.post(f"/api/projects/{project_id}/editorial/plan").json() == generated.json()


def test_editorial_composition_regeneration_is_explicit_and_scoped(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post("/api/projects", json={
        "title": "Partial Editorial", "topic": "One composition",
        "target_duration": 14, "video_mode": "editorial",
    }).json()["project"]["id"]
    assert client.post(f"/api/projects/{project_id}/plan", json={}).status_code == 200
    plan = client.post(f"/api/projects/{project_id}/editorial/plan").json()
    composition_id = plan["compositions"][0]["id"]

    regenerated = client.post(
        f"/api/projects/{project_id}/editorial/compositions/{composition_id}/regenerate"
    )

    assert regenerated.status_code == 200
    assert regenerated.json()["compositions"] == plan["compositions"]
    assert client.post(
        f"/api/projects/{project_id}/editorial/compositions/missing/regenerate"
    ).status_code == 404


def test_editorial_asset_lock_and_local_replacement_are_protected(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post("/api/projects", json={
        "title": "Editorial Asset", "topic": "Protected local media",
        "target_duration": 4, "video_mode": "editorial",
    }).json()["project"]["id"]
    script = client.post(f"/api/projects/{project_id}/plan", json={}).json()
    scene_id = script["scenes"][0]["id"]
    plan = {
        "project_id": project_id,
        "compositions": [{
            "id": "asset-comp", "start": 0, "duration": 4,
            "template": "archiveCanvas",
            "assets": [{
                "id": "hero-asset", "type": "generated_image",
                "evidence_class": "illustration", "locked": False,
            }],
            "elements": [{
                "id": "hero", "type": "image", "asset_id": "hero-asset",
                "role": "archive-photo",
            }],
            "events": [{"time": 0, "action": "fade", "target": "hero"}],
            "narration_refs": [scene_id],
        }],
    }
    assert client.put(
        f"/api/projects/{project_id}/editorial/edit-plan", json=plan,
    ).status_code == 200
    asset_url = (
        f"/api/projects/{project_id}/editorial/compositions/asset-comp/assets/hero-asset"
    )
    locked = client.patch(asset_url, json={"locked": True})
    assert locked.status_code == 200
    assert locked.json()["compositions"][0]["assets"][0]["locked"] is True

    unreadable = client.post(
        asset_url + "/replace",
        files={"file": ("fake.png", b"not-an-image", "image/png")},
    )
    assert unreadable.status_code == 422
    image_bytes = BytesIO()
    Image.new("RGB", (8, 8), "#a44b2a").save(image_bytes, format="PNG")
    replaced = client.post(
        asset_url + "/replace",
        files={"file": ("replacement.png", image_bytes.getvalue(), "image/png")},
        data={"evidence": "true"},
    )
    assert replaced.status_code == 200
    planned = replaced.json()["compositions"][0]["assets"][0]
    assert planned["id"] == "hero-asset"
    assert planned["type"] == "user_uploaded_image"
    assert planned["evidence_class"] == "evidence"
    assert planned["locked"] is True
    assert planned["metadata"]["manual_replacement"] is True
    registered = next(
        item for item in client.get(f"/api/projects/{project_id}").json()["assets"]
        if item["id"] == planned["asset_id"]
    )
    assert registered["backend"] == "imported_local"
    assert client.patch(asset_url, json={"locked": False}).status_code == 409


def test_editorial_composition_narrow_edits_retime_followers_and_retarget_template(
    tmp_path: Path,
) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post("/api/projects", json={
        "title": "Editable", "topic": "Composition controls",
        "target_duration": 8, "video_mode": "editorial",
    }).json()["project"]["id"]
    script = client.post(f"/api/projects/{project_id}/plan", json={}).json()
    scene_id = script["scenes"][0]["id"]
    plan = {
        "project_id": project_id,
        "compositions": [
            {
                "id": "first", "start": 0, "duration": 4,
                "template": "bigTextReveal",
                "elements": [{
                    "id": "title", "type": "text", "text": "BEFORE", "role": "headline",
                }],
                "events": [{"time": 0, "action": "fadeUp", "target": "title"}],
                "narration_refs": [scene_id],
            },
            {
                "id": "second", "start": 4, "duration": 4,
                "template": "bigTextReveal",
                "elements": [{
                    "id": "end", "type": "text", "text": "AFTER", "role": "headline",
                }],
                "events": [{"time": 0, "action": "fadeUp", "target": "end"}],
                "narration_refs": [scene_id],
            },
        ],
    }
    assert client.put(
        f"/api/projects/{project_id}/editorial/edit-plan", json=plan,
    ).status_code == 200
    url = f"/api/projects/{project_id}/editorial/compositions/first"
    edited = client.patch(url, json={
        "duration": 5,
        "text_updates": {"title": "REVISED"},
        "event_actions": {"0": "scaleIn"},
    })
    assert edited.status_code == 200
    compositions = edited.json()["compositions"]
    assert compositions[0]["duration"] == 5
    assert compositions[0]["elements"][0]["text"] == "REVISED"
    assert compositions[0]["events"][0]["action"] == "scaleIn"
    assert compositions[1]["start"] == 5
    assert compositions[1]["duration"] == 3

    retargeted = client.patch(url, json={"template": "documentReveal"})
    assert retargeted.status_code == 200
    first = retargeted.json()["compositions"][0]
    assert first["template"] == "documentReveal"
    assert first["elements"][0]["role"] == "document"
    assert first["elements"][0]["text"] == "REVISED"
    assert client.patch(url, json={"template": "illustrationCanvas"}).status_code == 409
    assert client.patch(url, json={"duration": 120}).status_code == 409
    assert client.patch(url, json={}).status_code == 409


def test_editorial_plan_staleness_is_additive_and_non_destructive(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    created = client.post("/api/projects", json={
        "title": "Editorial Clock", "topic": "Narration", "target_duration": 14,
        "resolution": [1080, 1920], "fps": 24, "video_mode": "editorial",
    }).json()
    project_id = created["project"]["id"]
    assert client.post(f"/api/projects/{project_id}/plan", json={}).status_code == 200
    generated = client.post(f"/api/projects/{project_id}/editorial/plan").json()

    project = app.state.service.database.get_project(project_id)
    root = app.state.service.store.project_path(project)
    timings = root / "subtitles" / "word-timings.json"
    timings.parent.mkdir(parents=True, exist_ok=True)
    timings.write_text(json.dumps({
        "words": [{"start_seconds": 0, "end_seconds": 1, "text": "Narration"}],
    }), encoding="utf-8")

    stale = client.get(f"/api/projects/{project_id}").json()["editorial"]
    assert stale["plan_status"] == "stale"
    assert stale["stale"] is True
    assert stale["stale_reasons"] == ["word_timings"]
    # Staleness never deletes or silently replaces the user's plan.
    assert client.get(f"/api/projects/{project_id}/editorial/edit-plan").json() == generated

    # Explicitly saving the plan records the current clock without changing it.
    saved = client.put(
        f"/api/projects/{project_id}/editorial/edit-plan", json=generated,
    )
    assert saved.status_code == 200
    current = client.get(f"/api/projects/{project_id}").json()["editorial"]
    assert current["plan_status"] == "current"
    assert current["stale"] is False

    assert client.patch(
        f"/api/projects/{project_id}", json={"style": "restrained archival"},
    ).status_code == 200
    changed = client.get(f"/api/projects/{project_id}").json()["editorial"]
    assert changed["plan_status"] == "stale"
    assert changed["stale_reasons"] == ["project"]

    assert client.put(
        f"/api/projects/{project_id}/editorial/edit-plan", json=generated,
    ).status_code == 200
    script = app.state.service.store.load_plan(project.slug)
    first = script.scenes[0].model_copy(update={"narration": "Revised narration."})
    app.state.service.store.save_plan(
        project.slug, script.model_copy(update={"scenes": [first, *script.scenes[1:]]}),
    )
    narration_changed = client.get(
        f"/api/projects/{project_id}"
    ).json()["editorial"]
    assert narration_changed["plan_status"] == "stale"
    assert narration_changed["stale_reasons"] == ["script"]


def test_editorial_display_settings_are_narrow_and_selectively_invalidate(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post("/api/projects", json={
        "title": "Editorial Switches", "topic": "Independent display layers",
        "target_duration": 14, "resolution": [1080, 1920], "fps": 24,
        "video_mode": "editorial",
    }).json()["project"]["id"]
    assert client.post(f"/api/projects/{project_id}/plan", json={}).status_code == 200
    original = client.post(f"/api/projects/{project_id}/editorial/plan").json()
    snapshot = client.get(f"/api/projects/{project_id}").json()["editorial"]
    assert snapshot["captions_enabled"] is True
    assert snapshot["editorial_text_enabled"] is True
    assert snapshot["settings_url"].endswith("/editorial/settings")

    empty = client.patch(f"/api/projects/{project_id}/editorial/settings", json={})
    assert empty.status_code == 409
    extra = client.patch(
        f"/api/projects/{project_id}/editorial/settings", json={"arbitrary": True},
    )
    assert extra.status_code == 422

    service = app.state.service
    project = service._project(project_id)
    root = service.store.project_path(project)
    tracked = {
        "editorial_visual": "editorial/master.mp4",
        "timeline": "timeline.json",
        "render_preview": "renders/preview.mp4",
        "quality_control": "renders/qc.json",
        "render_final": "renders/final.mp4",
        "thumbnails": "thumbnails/candidate-01/thumbnail.png",
    }
    for relative in tracked.values():
        output = root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"recorded")
    service._atomic_json(root / "stage-state.json", {
        "version": 1,
        "stages": {
            stage: {"status": "completed", "outputs": [relative]}
            for stage, relative in tracked.items()
        },
    })

    captions = client.patch(
        f"/api/projects/{project_id}/editorial/settings",
        json={"captions_enabled": False},
    )
    assert captions.status_code == 200
    assert captions.json()["captions_enabled"] is False
    assert captions.json()["editorial_text_enabled"] is True
    assert captions.json()["compositions"] == original["compositions"]
    stages = client.get(f"/api/projects/{project_id}").json()["stage_state"]["stages"]
    assert "editorial_visual" in stages
    assert all(stage not in stages for stage in set(tracked) - {"editorial_visual"})

    service._atomic_json(root / "stage-state.json", {
        "version": 1,
        "stages": {
            stage: {"status": "completed", "outputs": [relative]}
            for stage, relative in tracked.items()
        },
    })
    typography = client.patch(
        f"/api/projects/{project_id}/editorial/settings",
        json={"editorial_text_enabled": False},
    )
    assert typography.status_code == 200
    assert typography.json()["editorial_text_enabled"] is False
    stages = client.get(f"/api/projects/{project_id}").json()["stage_state"]["stages"]
    assert all(stage not in stages for stage in tracked)
    editorial = client.get(f"/api/projects/{project_id}").json()["editorial"]
    assert editorial["captions_enabled"] is False
    assert editorial["editorial_text_enabled"] is False
    assert editorial["plan_status"] == "current"


def test_legacy_editorial_plan_without_provenance_is_untracked(tmp_path: Path) -> None:
    from backend.editorial import build_project_mars_prototype

    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={
        "title": "Existing Editorial", "topic": "Compatibility",
        "target_duration": 14, "video_mode": "editorial",
    }).json()["project"]
    # Simulate a plan saved by the earlier release, before provenance existed.
    app.state.service.store.save_edit_plan(
        project["slug"], build_project_mars_prototype(project_id=project["id"]),
    )

    editorial = client.get(f"/api/projects/{project['id']}").json()["editorial"]
    assert editorial["has_edit_plan"] is True
    assert editorial["plan_status"] == "untracked"
    assert editorial["stale"] is None
    assert editorial["stale_reasons"] == []


def test_editorial_plan_api_rejects_classic_projects(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post("/api/projects", json={
        "title": "Classic", "topic": "Classic workflow", "target_duration": 5,
    }).json()["project"]["id"]

    response = client.post(f"/api/projects/{project_id}/editorial/plan")

    assert response.status_code == 409
    assert "not in Editorial Mode" in response.json()["detail"]


def test_unload_ideogram_releases_cache_and_stops_only_owned_worker(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    service = app.state.service
    calls: list[str] = []

    class FakeBackend:
        def unload(self) -> None:
            calls.append("backend")

    class FakeWorker:
        def stop(self) -> bool:
            calls.append("worker")
            return True

    monkeypatch.setattr(service.registry, "get", lambda name: FakeBackend())
    service.ideogram_worker = FakeWorker()
    service._resident_comfy_backend = "ideogram4_local_comfyui"

    response = TestClient(app).post("/api/ideogram4/unload")

    assert response.status_code == 200
    assert response.json() == {
        "status": "unloaded",
        "previous_backend": "ideogram4_local_comfyui",
        "stopped_owned_worker": True,
    }
    assert calls == ["backend", "worker"]
    assert service.resident_comfy_backend is None


def test_render_rejects_missing_existing_media_before_queueing(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post(
        "/api/projects",
        json={
            "title": "Not Ready",
            "topic": "missing render inputs",
            "target_duration": 1,
            "resolution": [160, 90],
            "fps": 12,
        },
    ).json()["project"]["id"]

    response = client.post(f"/api/projects/{project_id}/render", json={"force": True})

    assert response.status_code == 409
    assert "no scenes" in response.json()["detail"]
    assert app.state.service.jobs.list(project_id) == []


def test_render_rejects_second_job_while_one_is_active(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post(
        "/api/projects",
        json={
            "title": "Racey Render",
            "topic": "concurrent renders",
            "target_duration": 1,
            "resolution": [160, 90],
            "fps": 12,
        },
    ).json()["project"]["id"]
    planned = client.post(f"/api/projects/{project_id}/plan", json={})
    assert planned.status_code == 200

    # Simulate an in-flight render/pipeline job, as if a first POST were still
    # running its background task.
    service = app.state.service
    service.jobs.enqueue(GenerationJob(project_id=project_id, stage="render"))

    response = client.post(f"/api/projects/{project_id}/render", json={})

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]
    # The API must not enqueue a competing render job.
    active = [
        job for job in service.jobs.list(project_id) if job.stage == "render"
    ]
    assert len(active) == 1


def test_delete_project_removes_directory_and_index(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post(
        "/api/projects",
        json={
            "title": "Doomed",
            "topic": "deletion",
            "target_duration": 1,
            "resolution": [160, 90],
            "fps": 12,
        },
    ).json()["project"]["id"]
    planned = client.post(f"/api/projects/{project_id}/plan", json={})
    assert planned.status_code == 200

    response = client.delete(f"/api/projects/{project_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["project_id"] == project_id
    assert not (tmp_path / "projects" / body["slug"]).exists()
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert client.get("/api/projects").json()["projects"] == []
    service = app.state.service
    assert service.database.list_scenes(project_id) == []
    assert service.database.list_jobs(project_id) == []
    assert service.database.list_assets(project_id) == []
    # A repeat delete reports the missing project instead of failing oddly.
    assert client.delete(f"/api/projects/{project_id}").status_code == 404


def test_delete_project_refuses_active_jobs_until_canceled(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    created = client.post(
        "/api/projects",
        json={
            "title": "Busy",
            "topic": "active jobs",
            "target_duration": 1,
            "resolution": [160, 90],
            "fps": 12,
        },
    )
    project_id = created.json()["project"]["id"]
    service = app.state.service
    service.jobs.enqueue(GenerationJob(project_id=project_id, stage="narration"))

    refused = client.delete(f"/api/projects/{project_id}")
    assert refused.status_code == 409
    assert "jobs" in refused.json()["detail"]

    job = service.jobs.list(project_id)[0]
    service.jobs.cancel(job.id)
    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 200
    assert not (tmp_path / "projects" / deleted.json()["slug"]).exists()


def test_llm_models_returns_sanitized_unavailable_error(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    response = TestClient(app).get("/api/llm/models")
    assert response.status_code in {200, 503}
    assert "Authorization" not in response.text


def test_frontend_project_settings_and_asset_delivery(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    assert client.get("/").status_code == 200
    created = client.post(
        "/api/projects",
        json={
            "title": "Local UI",
            "topic": "test",
            "target_duration": 1,
            "resolution": [160, 90],
            "fps": 12,
        },
    ).json()
    project_id = created["project"]["id"]
    patched = client.patch(
        f"/api/projects/{project_id}",
        json={
            "narrator_preference": "calm",
            "settings": {"voice": {"language": "en", "speed": 1.25}},
        },
    )
    assert patched.status_code == 200
    assert patched.json()["project"]["narrator_preference"] == "calm"
    assert patched.json()["project"]["settings"]["voice"]["speed"] == 1.25

    service = app.state.service
    project = service._project(project_id)
    plan = service.ensure_plan(project_id)
    service._ensure_references(project, force=False)
    asset = service.generate_scene(plan.scenes[0].id)
    snapshot = client.get(f"/api/projects/{project_id}").json()
    delivered = client.get(snapshot["assets"][-1]["url"])
    assert asset.id == snapshot["assets"][-1]["id"]
    assert delivered.status_code == 200
    assert delivered.content
    assert client.get(f"/api/projects/not-this-project/assets/{asset.id}/file").status_code == 404


def test_llm_model_selection_validates_discovery_and_persists_project(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        LocalLLMBackend,
        "discover_models",
        lambda self: ({"id": "local-model", "object": "model"},),
    )
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    project_id = client.post(
        "/api/projects",
        json={"title": "Model Choice", "topic": "test", "target_duration": 1},
    ).json()["project"]["id"]
    selected = client.put(
        "/api/llm/models",
        json={"model": "local-model", "project_id": project_id},
    )
    assert selected.status_code == 200
    assert selected.json()["selected_model"] == "local-model"
    snapshot = client.get(f"/api/projects/{project_id}").json()
    assert snapshot["project"]["selected_llm_model"] == "local-model"
    assert client.put("/api/llm/models", json={"model": "missing"}).status_code == 409


def test_real_script_generation_requires_an_explicit_project_model(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=False,
    )
    client = TestClient(app)
    project_id = client.post(
        "/api/projects",
        json={"title": "Choose Model", "topic": "test", "target_duration": 1},
    ).json()["project"]["id"]

    response = client.post(f"/api/projects/{project_id}/plan")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "model_selection_required"
    assert "Choose a local LLM model" in response.json()["detail"]["message"]


def test_plan_endpoint_returns_structured_backend_error(tmp_path: Path, monkeypatch) -> None:
    def broken_completion(*args, **kwargs):
        raise BackendError(
            BackendErrorCode.INVALID_RESPONSE,
            "The local model returned JSON that does not match the expected "
            "structure: scenes.0.visual_prompt: Field required.",
        )

    monkeypatch.setattr(LocalLLMBackend, "complete", broken_completion)
    monkeypatch.setattr(LocalLLMBackend, "selected_model", lambda self: self.model)
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=False,
    )
    client = TestClient(app)
    project_id = client.post(
        "/api/projects",
        json={"title": "Broken LLM", "topic": "test", "target_duration": 1},
    ).json()["project"]["id"]
    app.state.service.select_project_llm_model(project_id, "local-model")
    planned = client.post(f"/api/projects/{project_id}/plan", json={})
    assert planned.status_code == 502
    detail = planned.json()["detail"]
    assert detail["code"] == "invalid_response"
    assert "scenes.0.visual_prompt: Field required" in detail["message"]
    # The /script alias returns the same structured body.
    scripted = client.post(f"/api/projects/{project_id}/script", json={})
    assert scripted.status_code == 502
    assert scripted.json()["detail"]["code"] == "invalid_response"


def test_captions_models_reports_whisper_readiness(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    response = TestClient(app).get("/api/captions/models")
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "whisper"
    assert body["descriptor"]["backend_name"] == "whisper"
    assert body["descriptor"]["model_name"] == "Whisper large-v3-turbo"
    assert body["mock_mode"] is True
    assert body["enabled"] is True  # config/default.yaml ships whisper enabled
    # faster_whisper is an optional extra and the model directory may be absent
    # in the test environment, so assert only machine-independent invariants.
    assert body["health"]["status"] in {"healthy", "incompatible", "not_configured"}
    if body["health"]["status"] != "healthy":
        assert body["health"]["install_guidance"]


def test_captions_models_reports_not_configured_model_path(tmp_path: Path) -> None:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["backends"]["whisper"]["model_path"] = str(tmp_path / "absent-whisper")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    app = create_app(
        load_config(config_file, environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    body = TestClient(app).get("/api/captions/models").json()
    assert body["health"]["status"] == "not_configured"
    assert "backends.whisper.model_path" in body["health"]["install_guidance"]


def test_generate_captions_endpoint_builds_caption_stage(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    created = client.post(
        "/api/projects",
        json={"title": "Caption action", "topic": "test", "target_duration": 2},
    ).json()
    project_id = created["project"]["id"]
    service = app.state.service
    service.ensure_plan(project_id)
    service._ensure_narration(service._project(project_id), force=False)

    response = client.post(f"/api/projects/{project_id}/captions/generate")

    assert response.status_code == 202
    assert response.json()["stage"] == "caption_alignment"
    snapshot = client.get(f"/api/projects/{project_id}").json()
    assert snapshot["stage_state"]["stages"]["subtitles"]["status"] == "completed"


def test_music_models_returns_readiness_shape(tmp_path: Path) -> None:
    config = load_config(environ={})
    config.backends.ace_step.enabled = True
    app = create_app(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    import backend.models.ace_step_comfyui as ace_mod
    original_readiness = ace_mod.ACEStepComfyUIBackend.readiness
    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {"language": ["en"], "key_scale": ["C major"], "time_signature": ["4"]},
            "duration_range": {"min": 10, "max": 600},
            "error": None,
        }
    ace_mod.ACEStepComfyUIBackend.readiness = fake_readiness
    try:
        response = TestClient(app).get("/api/music/models")
    finally:
        ace_mod.ACEStepComfyUIBackend.readiness = original_readiness
    assert response.status_code == 200
    body = response.json()
    assert body["backend"]["backend_name"] == "ace_step_comfyui"
    assert body["provider"] == "comfyui"
    assert "readiness" in body
    assert "turbo" in body["readiness"]
    assert "sft" in body["readiness"]
    assert "combo_choices" in body["readiness"]
    assert "duration_range" in body["readiness"]


def test_generate_music_creates_job(tmp_path: Path) -> None:
    config = load_config(environ={})
    config.backends.ace_step.enabled = True
    app = create_app(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    import backend.models.ace_step_comfyui as ace_mod
    original_readiness = ace_mod.ACEStepComfyUIBackend.readiness
    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {"language": ["en"], "key_scale": ["C major"], "time_signature": ["4"]},
            "duration_range": {"min": 10, "max": 600},
            "error": None,
        }
    ace_mod.ACEStepComfyUIBackend.readiness = fake_readiness
    try:
        client = TestClient(app)
        project_id = client.post(
            "/api/projects",
            json={"title": "Music Gen", "topic": "test", "target_duration": 2, "resolution": [160, 90]},
        ).json()["project"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            json={"settings": {"music": {"backend": "ace_step_comfyui", "model": "xl_turbo"}}},
        )
        response = client.post(f"/api/projects/{project_id}/music/generate", json={})
    finally:
        ace_mod.ACEStepComfyUIBackend.readiness = original_readiness
    assert response.status_code == 202
    body = response.json()
    assert body["stage"] == "music"
    assert body["backend"] == "ace_step_comfyui"


def test_generate_music_deduplicates_active_job(tmp_path: Path) -> None:
    config = load_config(environ={})
    config.backends.ace_step.enabled = True
    app = create_app(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    import backend.models.ace_step_comfyui as ace_mod
    original_readiness = ace_mod.ACEStepComfyUIBackend.readiness
    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {"language": ["en"], "key_scale": ["C major"], "time_signature": ["4"]},
            "duration_range": {"min": 10, "max": 600},
            "error": None,
        }
    ace_mod.ACEStepComfyUIBackend.readiness = fake_readiness
    try:
        client = TestClient(app)
        project_id = client.post(
            "/api/projects",
            json={"title": "Dedup", "topic": "test", "target_duration": 2, "resolution": [160, 90]},
        ).json()["project"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            json={"settings": {"music": {"backend": "ace_step_comfyui", "model": "xl_turbo"}}},
        )
        first = client.post(f"/api/projects/{project_id}/music/generate", json={})
        assert first.status_code == 202
        second = client.post(f"/api/projects/{project_id}/music/generate", json={})
    finally:
        ace_mod.ACEStepComfyUIBackend.readiness = original_readiness
    assert second.status_code == 202


def test_generate_music_force_conflicts_with_active_job(tmp_path: Path) -> None:
    """An active music job dedupes plain requests and rejects force with 409.

    The stuck job is enqueued directly (no background execution) so the
    endpoint's active-job branches are exercised deterministically.
    """
    from backend.schemas import GenerationJob

    config = load_config(environ={})
    config.backends.ace_step.enabled = True
    app = create_app(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    import backend.models.ace_step_comfyui as ace_mod
    original_readiness = ace_mod.ACEStepComfyUIBackend.readiness
    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {"language": ["en"], "key_scale": ["C major"], "time_signature": ["4"]},
            "duration_range": {"min": 10, "max": 600},
            "error": None,
        }
    ace_mod.ACEStepComfyUIBackend.readiness = fake_readiness
    try:
        client = TestClient(app)
        project_id = client.post(
            "/api/projects",
            json={"title": "Force", "topic": "test", "target_duration": 2, "resolution": [160, 90]},
        ).json()["project"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            json={"settings": {"music": {"backend": "ace_step_comfyui", "model": "xl_turbo"}}},
        )
        stuck = app.state.service.jobs.enqueue(
            GenerationJob(project_id=project_id, stage="music", backend="ace_step_comfyui")
        )
        deduped = client.post(f"/api/projects/{project_id}/music/generate", json={})
        forced = client.post(f"/api/projects/{project_id}/music/generate", json={"force": True})
    finally:
        ace_mod.ACEStepComfyUIBackend.readiness = original_readiness
    assert deduped.status_code == 202
    assert deduped.json()["id"] == stuck.id
    assert forced.status_code == 409


def _studio(tmp_path: Path):
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    return app, TestClient(app)


def test_thumbnail_candidate_delete_endpoint_maps_errors(tmp_path: Path) -> None:
    _, client = _studio(tmp_path)
    project_id = client.post(
        "/api/projects",
        json={"title": "Thumb Delete", "topic": "test", "target_duration": 1},
    ).json()["project"]["id"]
    created = client.post(
        f"/api/projects/{project_id}/thumbnails/candidates",
        json={"candidate_id": "candidate-01"},
    )
    assert created.status_code == 202
    snapshot = client.get(f"/api/projects/{project_id}/thumbnails")
    assert snapshot.status_code == 200
    assert len(snapshot.json()["candidates"]) == 1

    deleted = client.delete(f"/api/projects/{project_id}/thumbnails/candidates/candidate-01")
    assert deleted.status_code == 200
    assert deleted.json()["candidate_id"] == "candidate-01"
    assert client.get(f"/api/projects/{project_id}/thumbnails").json()["candidates"] == []

    missing = client.delete(f"/api/projects/{project_id}/thumbnails/candidates/candidate-01")
    assert missing.status_code == 404
    invalid = client.delete(f"/api/projects/{project_id}/thumbnails/candidates/nope")
    assert invalid.status_code == 404


def test_thumbnail_magic_prompt_endpoint_persists_preview_before_image(
    tmp_path: Path,
) -> None:
    app, client = _studio(tmp_path)
    project_id = client.post(
        "/api/projects",
        json={"title": "Prompt Preview", "topic": "local prompt", "target_duration": 1},
    ).json()["project"]["id"]
    snapshot = client.get(f"/api/projects/{project_id}/thumbnails").json()
    plan = {**snapshot["plan"], "image_model": "ideogram4_local"}
    saved_plan = client.put(
        f"/api/projects/{project_id}/thumbnails/plan", json=plan,
    )
    assert saved_plan.status_code == 200

    generated = client.post(
        f"/api/projects/{project_id}/thumbnails/magic-prompt/regenerate",
        json={},
    )
    assert generated.status_code == 200, generated.text
    payload = generated.json()
    assert payload["status"] == "saved"
    assert payload["serialized_prompt"].startswith('{"high_level_description":')
    assert payload["structured_prompt"]["compositional_deconstruction"]
    prompt_path = (
        app.state.service.store.project_path(
            app.state.service._project(project_id)
        ) / payload["path"]
    )
    assert prompt_path.is_file()
    after = client.get(f"/api/projects/{project_id}/thumbnails").json()
    assert after["magic_prompt"]["stale"] is False
    assert after["candidates"] == []


def test_startup_recovery_fails_interrupted_jobs(tmp_path: Path) -> None:
    from backend.schemas import GenerationJob, JobStatus, Project
    from backend.storage import PersistentJobQueue, StudioDatabase

    database_path = tmp_path / "studio.sqlite3"
    database = StudioDatabase(database_path)
    database.initialize()
    project = database.create_project(Project(
        title="Interrupted", topic="restart", target_duration=1, slug="interrupted"))
    jobs = PersistentJobQueue(database)
    job = jobs.enqueue(GenerationJob(project_id=project.id, stage="pipeline"))
    jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
    jobs.transition(job.id, JobStatus.GENERATING, progress=0.4)

    _, client = _studio(tmp_path)
    row = next(
        item for item in client.get("/api/jobs").json()["jobs"] if item["id"] == job.id
    )
    assert row["status"] == "failed"
    assert "backend restarted" in row["error"]


def test_retry_reexecutes_top_level_job(tmp_path: Path) -> None:
    from backend.schemas import GenerationJob, JobStatus

    app, client = _studio(tmp_path)
    service = app.state.service
    project_id = client.post(
        "/api/projects",
        json={"title": "Retry", "topic": "test", "target_duration": 1,
              "resolution": [160, 90], "fps": 12},
    ).json()["project"]["id"]
    # A render job that died with the previous process:
    job = service.jobs.enqueue(GenerationJob(
        project_id=project_id, stage="render", backend="ffmpeg",
        parameters={"force": False}))
    service.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
    service.jobs.transition(job.id, JobStatus.GENERATING, progress=0.2)
    service.jobs.fail(job.id, "backend restarted before this job finished")

    response = client.post(f"/api/jobs/{job.id}/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    final = next(
        item for item in client.get("/api/jobs").json()["jobs"] if item["id"] == job.id
    )
    # The retry actually ran: it hit missing-media validation and failed anew.
    assert final["status"] == "failed"
    assert final["error"] != "backend restarted before this job finished"


def test_retry_rejects_child_stage_jobs(tmp_path: Path) -> None:
    from backend.schemas import GenerationJob, JobStatus

    app, client = _studio(tmp_path)
    service = app.state.service
    project_id = client.post(
        "/api/projects",
        json={"title": "Child", "topic": "test", "target_duration": 1,
              "resolution": [160, 90], "fps": 12},
    ).json()["project"]["id"]
    job = service.jobs.enqueue(GenerationJob(project_id=project_id, stage="plan"))
    service.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
    service.jobs.fail(job.id, "backend restarted before this job finished")

    response = client.post(f"/api/jobs/{job.id}/retry")

    assert response.status_code == 409
    assert "parent pipeline" in response.json()["detail"]
    row = next(
        item for item in client.get("/api/jobs").json()["jobs"] if item["id"] == job.id
    )
    assert row["status"] == "failed"


def _make_app(tmp_path: Path, mock_mode: bool = True):
    return create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=mock_mode,
    )


def _create_project(client: TestClient, **overrides) -> dict:
    body = {
        "title": "Edit Me",
        "topic": "a topic",
        "target_duration": 1,
        "resolution": [160, 90],
        "fps": 12,
    }
    body.update(overrides)
    return client.post("/api/projects", json=body).json()["project"]


def test_project_edit_accepts_every_editable_field(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    edits = {
        "title": "Renamed",
        "topic": "new topic",
        "target_duration": 90,
        "aspect_ratio": "9:16",
        "fps": 30,
        "resolution": [1080, 1920],
        "narrator_preference": "calm",
        "style": "cinematic",
        "audience": "technical",
        "visual_quality": "high",
        "instructions": "be concise",
    }
    patched = client.patch(f"/api/projects/{pid}", json=edits)
    assert patched.status_code == 200
    p = patched.json()["project"]
    assert p["title"] == "Renamed"
    assert p["topic"] == "new topic"
    assert p["target_duration"] == 90
    assert p["aspect_ratio"] == "9:16"
    assert p["fps"] == 30
    assert p["resolution"] == [1080, 1920]
    assert p["narrator_preference"] == "calm"
    assert p["style"] == "cinematic"
    assert p["audience"] == "technical"
    assert p["visual_quality"] == "high"
    assert p["instructions"] == "be concise"


def test_editorial_edit_plan_persists_and_previews(tmp_path: Path) -> None:
    from backend.editorial import build_project_mars_prototype

    client = TestClient(_make_app(tmp_path))
    project = _create_project(
        client, title="Project Mars", video_mode="editorial",
        resolution=[1080, 1920], fps=24,
    )
    plan = build_project_mars_prototype(project_id=project["id"])
    payload = plan.model_dump(mode="json")

    saved = client.put(
        f"/api/projects/{project['id']}/editorial/edit-plan", json=payload,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == payload
    snapshot = client.get(f"/api/projects/{project['id']}").json()
    assert snapshot["editorial"]["has_edit_plan"] is True

    restarted = TestClient(_make_app(tmp_path))
    loaded = restarted.get(
        f"/api/projects/{project['id']}/editorial/edit-plan"
    )
    assert loaded.status_code == 200
    assert loaded.json() == payload

    preview = restarted.get(f"/api/projects/{project['id']}/editorial/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("text/html")
    assert preview.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in preview.headers["content-security-policy"]
    assert "window.renderAt" in preview.text
    assert "1949" in preview.text


def test_classic_project_rejects_editorial_plan(tmp_path: Path) -> None:
    from backend.editorial import build_project_mars_prototype

    client = TestClient(_make_app(tmp_path))
    project = _create_project(client)
    plan = build_project_mars_prototype(project_id=project["id"])

    response = client.put(
        f"/api/projects/{project['id']}/editorial/edit-plan",
        json=plan.model_dump(mode="json"),
    )
    assert response.status_code == 409
    assert "Editorial Mode" in response.json()["detail"]


def test_project_edit_persists_across_restart(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client, title="Before Restart")["id"]
    assert client.patch(f"/api/projects/{pid}", json={"title": "After Restart"}).status_code == 200

    # A second app (fresh service) over the same on-disk state: the edit must
    # survive because both SQLite and the portable directory are durable.
    restarted = TestClient(_make_app(tmp_path))
    reloaded = restarted.get(f"/api/projects/{pid}").json()["project"]
    assert reloaded["title"] == "After Restart"


def test_project_edit_validation_failure_leaves_project_intact(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client, title="Kept")["id"]
    bad = client.patch(f"/api/projects/{pid}", json={"title": ""})  # min_length 1
    assert bad.status_code == 422
    assert client.get(f"/api/projects/{pid}").json()["project"]["title"] == "Kept"


def test_project_edit_invalidation_matrix(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    # Plan so scenes + a "plan" stage exist.
    client.post(f"/api/projects/{pid}/plan", json={})
    planned = client.get(f"/api/projects/{pid}").json()
    assert planned["stage_state"]["stages"].get("plan", {}).get("status") == "completed"
    assert len(planned["scenes"]) >= 1

    # A brief-field edit invalidates the plan stage (and downstream work).
    brief = client.patch(f"/api/projects/{pid}", json={"title": "New Title"})
    assert brief.status_code == 200
    assert "plan" in brief.json()["invalidated_stages"]
    after_brief = client.get(f"/api/projects/{pid}").json()
    assert "plan" not in after_brief["stage_state"]["stages"]

    # A dimension-field edit does NOT invalidate the plan stage, only timeline /
    # render / thumbnails / metadata.
    dim = client.patch(f"/api/projects/{pid}", json={"aspect_ratio": "1:1"})
    assert dim.status_code == 200
    invalidated = dim.json()["invalidated_stages"]
    assert "plan" not in invalidated
    assert "timeline" in invalidated
    assert "thumbnails" in invalidated


def test_project_edit_mark_scenes_stale(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    project = client.get(f"/api/projects/{pid}").json()["project"]
    scene_id = client.get(f"/api/projects/{pid}").json()["scenes"][0]["id"]
    service = client.app.state.service
    from backend.schemas import Scene, SceneStatus

    scene = service.database.get_scene(scene_id)
    service.database.save_scene(
        scene.model_copy(update={"status": SceneStatus.GENERATED, "locked": False})
    )
    # A locked scene must survive the stale-marking.
    locked = service.database.list_scenes(pid)[-1]
    service.database.save_scene(
        locked.model_copy(update={"status": SceneStatus.LOCKED, "locked": True})
    )

    res = client.patch(
        f"/api/projects/{pid}",
        json={"target_duration": 60, "mark_scenes_stale": True},
    )
    assert res.status_code == 200
    scenes = client.get(f"/api/projects/{pid}").json()["scenes"]
    by_id = {s["id"]: s for s in scenes}
    assert by_id[scene_id]["status"] == "draft"
    assert by_id[locked.id]["status"] == "locked"
    # The project's own fields are still updated.
    assert client.get(f"/api/projects/{pid}").json()["project"]["target_duration"] == 60


def test_project_list_recovers_directory_without_database_row(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    # Simulate a portable directory present but its database row missing.
    db = client.app.state.service.database
    with db.connection() as c:
        c.execute("DELETE FROM projects WHERE id = ?", (pid,))

    listed = client.get("/api/projects").json()
    ids = [p["id"] for p in listed["projects"]]
    assert pid in ids
    recovery_types = [r["type"] for r in listed["recovery"]]
    assert "recovered" in recovery_types
    # The recovered project is now resolvable directly (re-indexed, no discard).
    assert client.get(f"/api/projects/{pid}").status_code == 200
    # Files are never discarded.
    slug = client.get(f"/api/projects/{pid}").json()["project"]["slug"]
    assert (tmp_path / "projects" / slug / "project.json").is_file()


def test_project_list_reports_orphaned_database_row(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    slug = client.get(f"/api/projects/{pid}").json()["project"]["slug"]
    # Remove the portable directory but keep the database row.
    import shutil

    shutil.rmtree(tmp_path / "projects" / slug)

    listed = client.get("/api/projects").json()
    ids = [p["id"] for p in listed["projects"]]
    assert pid in ids  # orphaned rows are kept, not dropped
    assert any(r["type"] == "orphaned" for r in listed["recovery"])


def test_recovery_reindexes_portable_scenes(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    assert len(client.get(f"/api/projects/{pid}").json()["scenes"]) >= 1
    # Drop the database rows but keep the portable directories (project.json +
    # scenes/<n>/scene.json), simulating a SQLite index loss.
    db = client.app.state.service.database
    with db.connection() as c:
        c.execute("DELETE FROM scenes WHERE project_id = ?", (pid,))
        c.execute("DELETE FROM projects WHERE id = ?", (pid,))

    listed = client.get("/api/projects").json()
    assert any(p["id"] == pid for p in listed["projects"])
    snap = client.get(f"/api/projects/{pid}").json()
    # Scenes are rebuilt from disk, not reported as zero.
    assert len(snap["scenes"]) >= 1


def test_project_edit_title_mark_scenes_stale_writes_portable(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    service = client.app.state.service
    from backend.schemas import Scene, SceneStatus

    scene_id = client.get(f"/api/projects/{pid}").json()["scenes"][0]["id"]
    scene = service.database.get_scene(scene_id)
    service.database.save_scene(
        scene.model_copy(update={"status": SceneStatus.GENERATED, "locked": False})
    )
    slug = client.get(f"/api/projects/{pid}").json()["project"]["slug"]

    res = client.patch(
        f"/api/projects/{pid}",
        json={"title": "Renamed Title", "mark_scenes_stale": True},
    )
    assert res.status_code == 200
    # The scene is marked stale in SQLite ...
    scenes = client.get(f"/api/projects/{pid}").json()["scenes"]
    assert next(s for s in scenes if s["id"] == scene_id)["status"] == "draft"
    # ... and the portable scene.json is kept consistent.
    scene_path = tmp_path / "projects" / slug / "scenes" / "001" / "scene.json"
    import json as _json

    portable = _json.loads(scene_path.read_text(encoding="utf-8"))
    assert portable["status"] == "draft"


def test_unchanged_project_edit_does_not_invalidate(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client, title="Same Title")["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    planned = client.get(f"/api/projects/{pid}").json()
    assert planned["stage_state"]["stages"].get("plan", {}).get("status") == "completed"

    noop = client.patch(f"/api/projects/{pid}", json={"title": "Same Title"})
    assert noop.status_code == 200
    assert noop.json()["invalidated_stages"] == []
    after = client.get(f"/api/projects/{pid}").json()
    assert "plan" in after["stage_state"]["stages"]


def test_project_edit_outer_rollback_on_stage_failure(tmp_path: Path, monkeypatch) -> None:
    app = _make_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        pid = _create_project(client)["id"]
        original_title = client.get(f"/api/projects/{pid}").json()["project"]["title"]

        def boom(*_args, **_kwargs):
            raise OSError("disk full during stage write")

        monkeypatch.setattr(client.app.state.service, "_invalidate_stages", boom)
        response = client.patch(f"/api/projects/{pid}", json={"title": "Should Roll Back"})
        assert response.status_code == 500
        assert client.get(f"/api/projects/{pid}").json()["project"]["title"] == original_title


def test_recovery_reports_content_divergence(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client, title="Original")["id"]
    slug = client.get(f"/api/projects/{pid}").json()["project"]["slug"]
    # Edit the portable project.json directly to diverge from SQLite under the
    # same id/slug.
    project_json = tmp_path / "projects" / slug / "project.json"
    import json as _json

    payload = _json.loads(project_json.read_text(encoding="utf-8"))
    payload["title"] = "Disk Diverged"
    project_json.write_text(_json.dumps(payload, sort_keys=True), encoding="utf-8")

    listed = client.get("/api/projects").json()
    assert any(r["type"] == "diverged" and r["slug"] == slug for r in listed["recovery"])
    # The database row is kept, so the listed project still has the SQLite title.
    assert next(p for p in listed["projects"] if p["id"] == pid)["title"] == "Original"


def test_recovery_reports_partial_scene_reindex(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    assert len(client.get(f"/api/projects/{pid}").json()["scenes"]) >= 1
    # Corrupt one portable scene.json so reindexing it fails.
    slug = client.get(f"/api/projects/{pid}").json()["project"]["slug"]
    bad_scene = next(tmp_path.glob(f"projects/{slug}/scenes/*/scene.json"))
    bad_scene.write_text("not json", encoding="utf-8")

    db = client.app.state.service.database
    with db.connection() as c:
        c.execute("DELETE FROM scenes WHERE project_id = ?", (pid,))
        c.execute("DELETE FROM projects WHERE id = ?", (pid,))

    listed = client.get("/api/projects").json()
    assert any(p["id"] == pid for p in listed["projects"])
    assert any(r["type"] == "partial_recovery" and r["slug"] == slug for r in listed["recovery"])


def test_project_edit_bounds_reject_oversize_text(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    bad = client.patch(
        f"/api/projects/{pid}",
        json={
            "topic": "x" * 501,
            "style": "y" * 101,
            "audience": "z" * 101,
            "narrator_preference": "a" * 301,
            "visual_quality": "b" * 101,
            "instructions": "c" * 20_001,
        },
    )
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert any("topic" in str(item).lower() for item in detail) or "topic" in str(detail).lower()


def test_unchanged_nested_settings_are_a_true_noop(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    service = client.app.state.service
    voice = {"language": "en", "speed": 1.0}
    service.update_project(pid, {"settings": {"voice": voice}})
    service.run_project(pid)
    before = client.get(f"/api/projects/{pid}").json()

    noop = client.patch(f"/api/projects/{pid}", json={"settings": {"voice": voice}})

    assert noop.status_code == 200
    assert noop.json()["invalidated_stages"] == []
    after = client.get(f"/api/projects/{pid}").json()
    assert after["project"]["updated_at"] == before["project"]["updated_at"]
    assert after["stage_state"] == before["stage_state"]


def test_scene_stale_failure_rolls_back_entire_project_edit(
    tmp_path: Path, monkeypatch,
) -> None:
    app = _make_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        pid = _create_project(client, title="Before stale failure")["id"]
        client.post(f"/api/projects/{pid}/plan", json={})
        service = client.app.state.service
        from backend.schemas import SceneStatus

        originals = service.database.list_scenes(pid)
        for scene in originals:
            generated = scene.model_copy(update={"status": SceneStatus.GENERATED})
            service.store.save_scene(service._project(pid).slug, generated)
            service.database.save_scene(generated)

        original_save = service.database.save_scene

        def fail_draft(scene):
            if scene.status is SceneStatus.DRAFT:
                raise OSError("simulated SQLite scene failure")
            return original_save(scene)

        monkeypatch.setattr(service.database, "save_scene", fail_draft)
        response = client.patch(
            f"/api/projects/{pid}",
            json={"title": "Must roll back", "mark_scenes_stale": True},
        )

        assert response.status_code == 500
        snapshot = client.get(f"/api/projects/{pid}").json()
        assert snapshot["project"]["title"] == "Before stale failure"
        assert snapshot["stage_state"]["stages"]["plan"]["status"] == "completed"
        assert all(scene["status"] == "generated" for scene in snapshot["scenes"])
        slug = snapshot["project"]["slug"]
        assert all(
            service.store.load_scene(slug, scene["index"]).status is SceneStatus.GENERATED
            for scene in snapshot["scenes"]
        )


def test_thumbnail_invalidation_failure_restores_manifests_and_project(
    tmp_path: Path, monkeypatch,
) -> None:
    app = _make_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        pid = _create_project(client, title="Before thumbnail failure")["id"]
        service = client.app.state.service
        project = service._project(pid)
        manifest = (
            service.store.project_path(project)
            / "thumbnails" / "candidate-01" / "manifest.json"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        service._atomic_json(manifest, {"stale": False, "candidate_id": "candidate-01"})

        def fail_archive(_project):
            raise OSError("simulated selection archive failure")

        monkeypatch.setattr(service.thumbnails, "_archive_selection", fail_archive)
        response = client.patch(f"/api/projects/{pid}", json={"title": "Must roll back"})

        assert response.status_code == 500
        assert client.get(f"/api/projects/{pid}").json()["project"]["title"] == "Before thumbnail failure"
        import json as _json

        assert _json.loads(manifest.read_text(encoding="utf-8"))["stale"] is False


def test_direct_recovery_reports_scene_ownership_mismatch(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    service = client.app.state.service
    project = service._project(pid)
    scene_path = service.store.project_path(project) / "scenes" / "001" / "scene.json"
    import json as _json

    scene_payload = _json.loads(scene_path.read_text(encoding="utf-8"))
    scene_payload["project_id"] = "another-project"
    scene_path.write_text(_json.dumps(scene_payload), encoding="utf-8")
    with service.database.connection() as connection:
        connection.execute("DELETE FROM projects WHERE id = ?", (pid,))

    recovered = client.get(f"/api/projects/{pid}")

    assert recovered.status_code == 200
    body = recovered.json()
    assert any(item["type"] == "partial_recovery" for item in body["recovery"])
    assert all(scene["index"] != 0 for scene in body["scenes"])


def test_scene_edit_rejects_unknown_h3_quality_with_422(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    pid = _create_project(client)["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    scene_id = client.get(f"/api/projects/{pid}").json()["scenes"][0]["id"]

    bad = client.patch(f"/api/scenes/{scene_id}", json={"h3_quality": "ultra"})

    assert bad.status_code == 422
    detail = bad.json()["detail"]
    for value in ("fast_safe", "standard", "high", "custom"):
        assert value in detail

    ok = client.patch(f"/api/scenes/{scene_id}", json={"h3_quality": "standard"})
    assert ok.status_code == 200
    assert ok.json()["settings"]["h3_quality"] == "standard"


def test_cancel_terminal_job_returns_409_and_unknown_job_404(tmp_path: Path) -> None:
    from backend.schemas import GenerationJob, JobStatus

    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client)["id"]
    job = service.jobs.enqueue(GenerationJob(project_id=pid, stage="render"))
    service.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
    service.jobs.transition(job.id, JobStatus.GENERATING, progress=0.4)
    service.jobs.complete(job.id)

    terminal = client.post(f"/api/jobs/{job.id}/cancel")

    assert terminal.status_code == 409
    assert "terminal" in terminal.json()["detail"]

    missing = client.post("/api/jobs/does-not-exist/cancel")
    assert missing.status_code == 404


def test_plan_and_script_map_director_value_errors_to_422(
    tmp_path: Path, monkeypatch,
) -> None:
    app = _make_app(tmp_path)
    client = TestClient(app)
    pid = _create_project(client)["id"]

    def oversized(*_args, **_kwargs):
        raise ValueError("Director materialization would exceed the 128-scene project limit.")

    monkeypatch.setattr(app.state.service.director, "plan_with_draft", oversized)

    planned = client.post(f"/api/projects/{pid}/plan", json={})
    assert planned.status_code == 422
    assert "128-scene" in planned.json()["detail"]
    # The /script alias shares plan_project's error mapping.
    scripted = client.post(f"/api/projects/{pid}/script", json={})
    assert scripted.status_code == 422
    assert "128-scene" in scripted.json()["detail"]


def test_scene_generation_maps_h3_policy_errors_to_422(tmp_path: Path, monkeypatch) -> None:
    from backend.core.h3_policy import H3PolicyError

    app = _make_app(tmp_path)
    client = TestClient(app)
    pid = _create_project(client)["id"]
    client.post(f"/api/projects/{pid}/plan", json={})
    scene_id = client.get(f"/api/projects/{pid}").json()["scenes"][0]["id"]

    def policy_reject(_scene_id, *, force: bool = False):
        del force
        raise H3PolicyError(
            "Duration 30s exceeds the preset cap of 8 s for Standard.",
            "duration_out_of_range",
        )

    monkeypatch.setattr(app.state.service, "generate_scene", policy_reject)

    generated = client.post(f"/api/scenes/{scene_id}/generate")
    assert generated.status_code == 422
    assert "preset cap of 8 s" in generated.json()["detail"]

    regenerated = client.post(f"/api/scenes/{scene_id}/regenerate")
    assert regenerated.status_code == 422


def test_generate_music_force_requires_a_json_boolean(tmp_path: Path) -> None:
    """The string "false" must not act as force=True; booleans still work."""
    from backend.schemas import GenerationJob

    config = load_config(environ={})
    config.backends.ace_step.enabled = True
    app = create_app(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    import backend.models.ace_step_comfyui as ace_mod
    original_readiness = ace_mod.ACEStepComfyUIBackend.readiness

    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {"language": ["en"], "key_scale": ["C major"], "time_signature": ["4"]},
            "duration_range": {"min": 10, "max": 600},
            "error": None,
        }

    ace_mod.ACEStepComfyUIBackend.readiness = fake_readiness
    try:
        client = TestClient(app)
        project_id = client.post(
            "/api/projects",
            json={"title": "Force Bool", "topic": "test", "target_duration": 2,
                  "resolution": [160, 90]},
        ).json()["project"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            json={"settings": {"music": {"backend": "ace_step_comfyui", "model": "xl_turbo"}}},
        )
        stuck = app.state.service.jobs.enqueue(
            GenerationJob(project_id=project_id, stage="music", backend="ace_step_comfyui")
        )
        string_force = client.post(
            f"/api/projects/{project_id}/music/generate", json={"force": "false"},
        )
        deduped = client.post(
            f"/api/projects/{project_id}/music/generate", json={"force": False},
        )
    finally:
        ace_mod.ACEStepComfyUIBackend.readiness = original_readiness

    assert string_force.status_code == 422
    assert deduped.status_code == 202
    assert deduped.json()["id"] == stuck.id


def test_generate_music_movement_index_validation(tmp_path: Path) -> None:
    """movement_index must be a JSON integer; valid values reach the job row."""
    from backend.schemas import GenerationJob

    config = load_config(environ={})
    config.backends.ace_step.enabled = True
    app = create_app(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    import backend.models.ace_step_comfyui as ace_mod
    original_readiness = ace_mod.ACEStepComfyUIBackend.readiness

    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {},
            "duration_range": {"min": 10, "max": 600},
            "error": None,
        }

    ace_mod.ACEStepComfyUIBackend.readiness = fake_readiness
    try:
        client = TestClient(app)
        project_id = client.post(
            "/api/projects",
            json={"title": "Movement Index", "topic": "test", "target_duration": 2,
                  "resolution": [160, 90]},
        ).json()["project"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            json={"settings": {"music": {"backend": "ace_step_comfyui", "model": "xl_turbo"}}},
        )
        bad_string = client.post(
            f"/api/projects/{project_id}/music/generate", json={"movement_index": "1"},
        )
        bad_negative = client.post(
            f"/api/projects/{project_id}/music/generate", json={"movement_index": -2},
        )
        accepted = client.post(
            f"/api/projects/{project_id}/music/generate", json={"movement_index": 0},
        )
    finally:
        ace_mod.ACEStepComfyUIBackend.readiness = original_readiness

    assert bad_string.status_code == 422
    assert bad_negative.status_code == 422
    assert accepted.status_code == 202


def test_generate_narration_rejects_concurrent_narration_jobs(tmp_path: Path) -> None:
    """A second narration POST gets 409; after completion a new run is accepted."""
    from backend.schemas import GenerationJob, JobStatus

    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client)["id"]
    assert client.post(f"/api/projects/{pid}/plan", json={}).status_code == 200
    stuck = service.jobs.enqueue(GenerationJob(project_id=pid, stage="narration"))
    service.jobs.transition(stuck.id, JobStatus.PREPARING, progress=0.05)
    service.jobs.transition(stuck.id, JobStatus.GENERATING, progress=0.4)

    second = client.post(f"/api/projects/{pid}/tts/generate", json={})

    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    active = [job for job in service.jobs.list(pid) if job.stage == "narration"]
    assert len(active) == 1

    # Terminal jobs no longer block a fresh narration run. The executor is
    # stubbed so the accepted request's background task stays a no-op.
    service.jobs.complete(stuck.id)
    service.run_narration_job = lambda _job_id: None
    accepted = client.post(f"/api/projects/{pid}/tts/generate", json={})
    assert accepted.status_code == 202
    assert accepted.json()["id"] != stuck.id


def _planned_scenes(client: TestClient, project_id: str) -> list[dict]:
    assert client.post(f"/api/projects/{project_id}/plan", json={}).status_code == 200
    scenes = client.get(f"/api/projects/{project_id}").json()["scenes"]
    return sorted(scenes, key=lambda scene: scene["index"])


def _batch_jobs(client: TestClient) -> dict[str, dict]:
    return {job["id"]: job for job in client.get("/api/jobs").json()["jobs"]}


def test_visual_batch_generates_missing_scenes_in_type_order(
    tmp_path: Path, monkeypatch,
) -> None:
    """Generate-all queues one child job per scene, ordered by visual type."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)
    assert len(scenes) == 3
    # Board order S1 graphic screen, S2 Krea still, S3 image motion; the batch
    # must run stills first and graphic screens last.
    for scene, value in zip(scenes, ["graphic_screen", "krea2_still", "image_motion"]):
        patched = client.patch(f"/api/scenes/{scene['id']}", json={"visual_type": value})
        assert patched.status_code == 200

    calls: list[str] = []
    real_generate = service.generate_scene

    def spy(scene_id: str, **kwargs):
        calls.append(scene_id)
        return real_generate(scene_id, **kwargs)

    monkeypatch.setattr(service, "generate_scene", spy)

    response = client.post(f"/api/projects/{pid}/visuals/batch", json={})
    assert response.status_code == 202
    queued = response.json()
    assert queued["stage"] == "visual_batch"
    assert len(queued["parameters"]["scene_ids"]) == 3
    assert calls == [scenes[1]["id"], scenes[2]["id"], scenes[0]["id"]]

    jobs = _batch_jobs(client)
    assert jobs[queued["id"]]["status"] == "completed"
    children = [j for j in jobs.values() if j["stage"] == "scene_visual"]
    assert len(children) == 3
    assert all(child["status"] == "completed" for child in children)

    snapshot = client.get(f"/api/projects/{pid}").json()
    assert len(snapshot["assets"]) == 3
    assert all(scene["status"] == "generated" for scene in snapshot["scenes"])


def test_visual_batch_filters_by_type_and_skips_locked_and_existing(
    tmp_path: Path,
) -> None:
    app, client = _studio(tmp_path)
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)

    # Scene 0 already has a visual, scene 1 is locked without one.
    generated = client.post(f"/api/scenes/{scenes[0]['id']}/generate")
    assert generated.status_code == 201
    locked = client.post(f"/api/scenes/{scenes[1]['id']}/approve", json={"lock": True})
    assert locked.status_code == 200
    pinned = client.patch(
        f"/api/scenes/{scenes[2]['id']}", json={"visual_type": "flux_still"},
    )
    assert pinned.status_code == 200

    # A type filter only selects unlocked scenes of that type; scene 1 is
    # locked, so the krea2_still filter has nothing to do.
    empty = client.post(
        f"/api/projects/{pid}/visuals/batch", json={"visual_type": "krea2_still"},
    )
    assert empty.status_code == 409
    assert "No unlocked scenes of type krea2_still" in empty.json()["detail"]

    invalid = client.post(
        f"/api/projects/{pid}/visuals/batch", json={"visual_type": "nonsense"},
    )
    assert invalid.status_code == 422

    filtered = client.post(
        f"/api/projects/{pid}/visuals/batch", json={"visual_type": "flux_still"},
    )
    assert filtered.status_code == 202
    assert filtered.json()["parameters"]["scene_ids"] == [scenes[2]["id"]]
    assert filtered.json()["parameters"]["visual_type"] == "flux_still"

    # Everything unlocked now has a visual; a full batch is a no-op.
    snapshot = client.get(f"/api/projects/{pid}").json()
    statuses = {scene["id"]: scene["status"] for scene in snapshot["scenes"]}
    assert statuses[scenes[2]["id"]] == "generated"
    nothing = client.post(f"/api/projects/{pid}/visuals/batch", json={})
    assert nothing.status_code == 409
    assert "No unlocked scenes" in nothing.json()["detail"]


def test_visual_batch_filters_and_groups_by_effective_image_model(tmp_path: Path) -> None:
    """Krea source-frame jobs batch together even across visual types."""
    app, client = _studio(tmp_path)
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)
    for scene, changes in zip(scenes, [
        {
            "visual_type": "image_motion", "needs_embedded_text": False,
            "preferred_image_model": "krea",
        },
        {
            "visual_type": "krea2_still", "needs_embedded_text": False,
            "preferred_image_model": "krea",
        },
        {
            "visual_type": "image_motion",
            "preferred_image_model": "ideogram4_local",
            "needs_embedded_text": True,
            "text_in_image": "SHORT LABEL",
        },
    ]):
        patched = client.patch(f"/api/scenes/{scene['id']}", json=changes)
        assert patched.status_code == 200, patched.text

    krea = client.post(f"/api/projects/{pid}/visuals/batch", json={"image_model": "krea"})
    assert krea.status_code == 202, krea.text
    assert krea.json()["parameters"]["image_model"] == "krea"
    # Krea remains resident across both; stills precede Image Motion sources.
    assert krea.json()["parameters"]["scene_ids"] == [scenes[1]["id"], scenes[0]["id"]]

    ideogram = client.post(
        f"/api/projects/{pid}/visuals/batch", json={"image_model": "ideogram4_local"},
    )
    assert ideogram.status_code == 202, ideogram.text
    assert ideogram.json()["parameters"]["scene_ids"] == [scenes[2]["id"]]


def test_scene_edit_validates_and_persists_precise_ideogram_json(tmp_path: Path) -> None:
    _app, client = _studio(tmp_path)
    pid = _create_project(client, target_duration=3)["id"]
    scene = _planned_scenes(client, pid)[0]
    precise = {
        "high_level_description": "A precise sign layout.",
        "compositional_deconstruction": {
            "background": "A plain dark wall.",
            "elements": [{
                "type": "text", "bbox": [70, 100, 230, 900],
                "text": "OpenAI LABS", "desc": "Centered white serif lettering.",
            }],
        },
    }
    updated = client.patch(f"/api/scenes/{scene['id']}", json={
        "preferred_image_model": "ideogram4_local",
        "needs_embedded_text": True,
        "ideogram_prompt_mode": "precise",
        "ideogram_prompt_json": precise,
    })
    assert updated.status_code == 200, updated.text
    settings = updated.json()["settings"]
    assert settings["ideogram_prompt_mode"] == "precise"
    assert settings["ideogram_prompt_json"] == precise

    invalid = json.loads(json.dumps(precise))
    invalid["compositional_deconstruction"]["elements"][0]["bbox"] = [0, 0, 1001, 1000]
    rejected = client.patch(f"/api/scenes/{scene['id']}", json={
        "ideogram_prompt_json": invalid,
    })
    assert rejected.status_code == 422
    assert "bbox" in rejected.text


def test_visual_batch_rejects_second_batch_while_one_is_active(tmp_path: Path) -> None:
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    _planned_scenes(client, pid)

    # Simulate an in-flight batch, as if a first POST were still executing.
    service.jobs.enqueue(
        GenerationJob(project_id=pid, stage="visual_batch", parameters={"scene_ids": []}),
    )

    response = client.post(f"/api/projects/{pid}/visuals/batch", json={})

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]
    active = [job for job in service.jobs.list(pid) if job.stage == "visual_batch"]
    assert len(active) == 1


def test_visual_batch_isolates_scene_failures_and_retry_completes(
    tmp_path: Path, monkeypatch,
) -> None:
    """One broken scene fails its own child job; retrying finishes the rest."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)
    flaky_id = scenes[1]["id"]

    real_generate = service.generate_scene

    def flaky(scene_id: str, **kwargs):
        if scene_id == flaky_id and not getattr(flaky, "healed", False):
            flaky.healed = True
            raise RuntimeError("transient backend failure")
        return real_generate(scene_id, **kwargs)

    monkeypatch.setattr(service, "generate_scene", flaky)

    first = client.post(f"/api/projects/{pid}/visuals/batch", json={})
    assert first.status_code == 202

    jobs = _batch_jobs(client)
    parent = jobs[first.json()["id"]]
    assert parent["status"] == "failed"
    assert "1 of 3" in parent["error"]
    flaky_child = next(
        j for j in jobs.values()
        if j["stage"] == "scene_visual" and j["scene_id"] == flaky_id
    )
    assert flaky_child["status"] == "failed"
    assert "transient backend failure" in flaky_child["error"]

    # The healthy scenes were generated despite the failure...
    snapshot = client.get(f"/api/projects/{pid}").json()
    statuses = {scene["id"]: scene["status"] for scene in snapshot["scenes"]}
    assert statuses[flaky_id] != "generated"
    assert sum(status == "generated" for status in statuses.values()) == 2

    # ...and retrying the batch from the Job Monitor finishes the rest.
    retried = client.post(f"/api/jobs/{parent['id']}/retry")
    assert retried.status_code == 200
    jobs = _batch_jobs(client)
    assert jobs[parent["id"]]["status"] == "completed"
    snapshot = client.get(f"/api/projects/{pid}").json()
    assert all(scene["status"] == "generated" for scene in snapshot["scenes"])


def test_single_scene_generate_creates_scene_visual_job_row(tmp_path: Path) -> None:
    """A standalone Generate/Regenerate must be visible in the Job Monitor."""
    _, client = _studio(tmp_path)
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)

    response = client.post(f"/api/scenes/{scenes[0]['id']}/generate")
    assert response.status_code == 201

    rows = [job for job in _batch_jobs(client).values() if job["stage"] == "scene_visual"]
    assert len(rows) == 1
    row = rows[0]
    assert row["project_id"] == pid
    assert row["scene_id"] == scenes[0]["id"]
    assert row["status"] == "completed"
    assert row["progress"] == 1
    # transition() records when the job actually started running.
    assert row["started_at"] is not None
    assert row["started_at"] >= row["created_at"]
    # A completed standalone row: still retriable, no longer cancelable.
    assert row["executable"] is True
    assert row["cancelable"] is False


def test_single_scene_generate_no_op_still_leaves_a_job_row(tmp_path: Path) -> None:
    """A Generate that finds the visual already present must be visible too."""
    _, client = _studio(tmp_path)
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)
    scene_id = scenes[0]["id"]

    first = client.post(f"/api/scenes/{scene_id}/generate")
    assert first.status_code == 201
    rows = [j for j in _batch_jobs(client).values() if j["stage"] == "scene_visual"]
    assert len(rows) == 1

    # Asking again (visual already current) is a no-op, but the monitor must
    # still see the request as a completed row.
    again = client.post(f"/api/scenes/{scene_id}/generate")
    assert again.status_code == 201
    rows = [j for j in _batch_jobs(client).values() if j["stage"] == "scene_visual"]
    assert len(rows) == 2
    assert all(row["status"] == "completed" for row in rows)
    assert all(row["scene_id"] == scene_id for row in rows)
    assert all(row["executable"] is True for row in rows)


def test_standalone_scene_visual_retry_reruns_the_scene(tmp_path: Path) -> None:
    """A failed standalone scene_visual re-runs its scene from the Job Monitor."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)
    scene = scenes[1]

    # As if a restart had killed a standalone generation mid-flight.
    failed = service.jobs.enqueue(GenerationJob(
        project_id=pid,
        scene_id=scene["id"],
        stage="scene_visual",
        parameters={"force": False},
    ))
    service.jobs.transition(failed.id, JobStatus.PREPARING)
    service.jobs.fail(failed.id, "synthetic failure")

    listed = _batch_jobs(client)
    assert listed[failed.id]["executable"] is True
    assert listed[failed.id]["cancelable"] is False

    retried = client.post(f"/api/jobs/{failed.id}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    assert retried.json()["cancelable"] is True
    # Re-queueing resets the timing; the re-run records a fresh start.
    assert retried.json()["started_at"] is None

    jobs = _batch_jobs(client)
    assert jobs[failed.id]["status"] == "completed"
    assert jobs[failed.id]["started_at"] is not None
    snapshot = client.get(f"/api/projects/{pid}").json()
    assert next(s for s in snapshot["scenes"] if s["id"] == scene["id"])["status"] == "generated"


def test_parented_scene_visual_child_rejects_retry(tmp_path: Path) -> None:
    """Child rows are re-run by retrying their parent batch, not standalone."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)

    child = service.jobs.enqueue(GenerationJob(
        project_id=pid,
        scene_id=scenes[0]["id"],
        stage="scene_visual",
        parameters={"parent_job_id": "batch-parent"},
    ))
    service.jobs.transition(child.id, JobStatus.PREPARING)
    service.jobs.fail(child.id, "synthetic failure")

    assert _batch_jobs(client)[child.id]["executable"] is False

    response = client.post(f"/api/jobs/{child.id}/retry")
    assert response.status_code == 409
    assert "runs inside its parent pipeline" in response.json()["detail"]


def test_job_payload_annotations_follow_stage_management(tmp_path: Path) -> None:
    """executable/cancelable tell the Job Monitor which buttons to offer."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]

    bookkeeping = service.jobs.enqueue(GenerationJob(
        project_id=pid, stage="timeline", parameters={"managed_by": "pipeline"},
    ))
    standalone = service.jobs.enqueue(GenerationJob(
        project_id=pid, stage="narration",
    ))

    listed = _batch_jobs(client)
    # Pipeline bookkeeping: not retriable, and canceling it mid-operation is
    # a tolerated no-op, so the UI offers no button at all.
    assert listed[bookkeeping.id]["executable"] is False
    assert listed[bookkeeping.id]["cancelable"] is False
    # A queued standalone top-level row: both actions make sense.
    assert listed[standalone.id]["executable"] is True
    assert listed[standalone.id]["cancelable"] is True

    # The backend still accepts the bookkeeping cancel (the stage guard keeps
    # the row canceled without failing the pipeline); the row goes terminal.
    canceled = client.post(f"/api/jobs/{bookkeeping.id}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["executable"] is False
    assert canceled.json()["cancelable"] is False


def _queued_batch_with_inflight_child(service, pid: str):
    """Queue a visual batch without running it, with one child mid-flight.

    Returns (parent, children in batch order, in_flight_child_id). Calling
    queue_visual_batch directly (instead of the API) keeps the rows queued so
    the test can inspect the cancel cascade deterministically.
    """
    parent = service.queue_visual_batch(pid)
    order = {scene_id: i for i, scene_id in enumerate(parent.parameters["scene_ids"])}
    children = sorted(
        (
            job for job in service.jobs.list(pid)
            if job.stage == "scene_visual"
            and job.parameters.get("parent_job_id") == parent.id
        ),
        key=lambda job: order[job.scene_id],
    )
    service.jobs.transition(parent.id, JobStatus.PREPARING, progress=0.05)
    service.jobs.transition(parent.id, JobStatus.GENERATING, progress=0.2)
    service.jobs.transition(children[0].id, JobStatus.PREPARING, progress=0.05)
    service.jobs.transition(children[0].id, JobStatus.GENERATING, progress=0.4)
    return parent, children, children[0].id


def test_cancel_visual_batch_cancels_every_job_it_created(tmp_path: Path) -> None:
    """Canceling the batch parent cascades to queued and in-flight children."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    _planned_scenes(client, pid)
    parent, children, inflight_id = _queued_batch_with_inflight_child(service, pid)

    response = client.post(f"/api/jobs/{parent.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    jobs = {job.id: job for job in service.jobs.list(pid)}
    assert jobs[parent.id].status is JobStatus.CANCELED
    for child in children:
        assert jobs[child.id].status is JobStatus.CANCELED, (
            f"child {child.id} (was {child.status}) was not canceled with its batch"
        )
        # Tagged so retrying the batch re-queues them instead of treating
        # them as individually opted out.
        assert jobs[child.id].parameters.get("canceled_with_parent") is True
    # The in-flight child is canceled too, not allowed to finish as completed.
    assert jobs[inflight_id].status is JobStatus.CANCELED


def test_cancel_visual_batch_leaves_unrelated_jobs_alone(tmp_path: Path) -> None:
    """The batch cascade touches only its own children, not other project jobs."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)
    parent, children, _ = _queued_batch_with_inflight_child(service, pid)

    # A standalone scene job and a music job share the project, not the batch.
    standalone = service.jobs.enqueue(GenerationJob(
        project_id=pid, scene_id=scenes[0]["id"], stage="scene_visual",
    ))
    music = service.jobs.enqueue(GenerationJob(project_id=pid, stage="music"))
    service.jobs.transition(music.id, JobStatus.PREPARING, progress=0.1)

    response = client.post(f"/api/jobs/{parent.id}/cancel")
    assert response.status_code == 200

    jobs = {job.id: job for job in service.jobs.list(pid)}
    assert jobs[parent.id].status is JobStatus.CANCELED
    assert all(jobs[child.id].status is JobStatus.CANCELED for child in children)
    assert jobs[standalone.id].status is JobStatus.QUEUED
    assert jobs[music.id].status is JobStatus.PREPARING


def test_cancel_all_project_jobs_cancels_every_active_job(tmp_path: Path) -> None:
    """The storyboard's Cancel all stops every active job, batch first."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    scenes = _planned_scenes(client, pid)
    parent, children, inflight_id = _queued_batch_with_inflight_child(service, pid)
    standalone = service.jobs.enqueue(GenerationJob(
        project_id=pid, scene_id=scenes[1]["id"], stage="scene_visual",
    ))
    music = service.jobs.enqueue(GenerationJob(project_id=pid, stage="music"))
    service.jobs.transition(music.id, JobStatus.PREPARING, progress=0.1)
    # A completed job must survive Cancel all untouched.
    generated = client.post(f"/api/scenes/{scenes[2]['id']}/generate")
    assert generated.status_code == 201

    response = client.post(f"/api/projects/{pid}/jobs/cancel-all")

    assert response.status_code == 200
    assert response.json()["count"] == 1 + len(children) + 2
    canceled_ids = {job["id"] for job in response.json()["canceled"]}
    assert canceled_ids == {parent.id, standalone.id, music.id, *[c.id for c in children]}

    jobs = {job.id: job for job in service.jobs.list(pid)}
    assert jobs[parent.id].status is JobStatus.CANCELED
    for child in children:
        assert jobs[child.id].status is JobStatus.CANCELED
        assert jobs[child.id].parameters.get("canceled_with_parent") is True
    assert jobs[inflight_id].status is JobStatus.CANCELED
    # The standalone job was canceled on its own, not tagged as batch fallout.
    assert jobs[standalone.id].status is JobStatus.CANCELED
    assert jobs[standalone.id].parameters.get("canceled_with_parent") is None
    assert jobs[music.id].status is JobStatus.CANCELED

    # Terminal rows are never re-canceled: a second call is an empty no-op.
    again = client.post(f"/api/projects/{pid}/jobs/cancel-all")
    assert again.status_code == 200
    assert again.json()["count"] == 0
    assert again.json()["canceled"] == []
    generated_row = next(
        job for job in service.jobs.list(pid)
        if job.scene_id == scenes[2]["id"] and job.stage == "scene_visual"
        and not job.parameters.get("parent_job_id")
    )
    assert generated_row.status is JobStatus.COMPLETED


def test_cancel_all_project_jobs_unknown_project_404(tmp_path: Path) -> None:
    app, client = _studio(tmp_path)

    response = client.post("/api/projects/does-not-exist/jobs/cancel-all")

    assert response.status_code == 404


def test_retried_batch_requeues_children_canceled_with_parent(tmp_path: Path) -> None:
    """A batch canceled at the parent re-queues all its children on retry."""
    app, client = _studio(tmp_path)
    service = app.state.service
    pid = _create_project(client, target_duration=3)["id"]
    _planned_scenes(client, pid)
    parent, children, _ = _queued_batch_with_inflight_child(service, pid)

    assert client.post(f"/api/jobs/{parent.id}/cancel").status_code == 200
    assert client.post(f"/api/jobs/{parent.id}/retry").status_code == 200

    jobs = _batch_jobs(client)
    assert jobs[parent.id]["status"] == "completed"
    assert all(jobs[c.id]["status"] == "completed" for c in children)
    snapshot = client.get(f"/api/projects/{pid}").json()
    assert all(scene["status"] == "generated" for scene in snapshot["scenes"])
