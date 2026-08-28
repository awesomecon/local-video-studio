"""Multi-shot integration coverage: portable storage, service CRUD, API contracts."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core import load_config
from backend.models.provenance import current_visual_asset
from backend.pipeline import PipelineService
from backend.pipeline.service import PipelineError
from backend.schemas import (
    Asset,
    AssetType,
    ProjectCreate,
    Scene,
    SceneStatus,
    ShotStatus,
    ShotTransitionKind,
)


def make_service(tmp_path: Path) -> PipelineService:
    config = load_config(environ={})
    return PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )


def make_project_with_scene(service: PipelineService, *, duration: float = 10):
    project = service.create_project(ProjectCreate(
        title="Mars Slice", topic="multishot documentary", target_duration=duration * 2,
        resolution=(320, 180), fps=12,
    ))
    scene = Scene(project_id=project.id, index=0, duration=duration, narration="Water.",
                  visual_prompt="a quiet canal")
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    return project, scene


# ---------------------------------------------------------------------------
# Service-level behavior


def test_legacy_scene_lists_one_implicit_shot(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)

    view = service.list_scene_shots(scene.id)

    assert view["materialized"] is False
    assert view["count"] == 1
    assert view["rendered_duration_seconds"] == pytest.approx(10)
    shot_payload = view["shots"][0]
    assert shot_payload["implicit"] is True
    assert shot_payload["id"].endswith("-implicit")
    assert shot_payload["visual_prompt"] == "a quiet canal"


def test_first_edit_materializes_the_implicit_shot_and_attaches_visual_asset(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    asset = service.database.save_asset(Asset(
        project_id=project.id, scene_id=scene.id, type=AssetType.IMAGE,
        filepath=Path("scenes/001/visual.png"), backend="mock", model="mock-v1",
        seed=3, settings={"role": "visual"},
    ))

    created = service.create_shot(
        scene.id, {"title": "second beat", "duration_seconds": 4},
    )

    view = service.list_scene_shots(scene.id)
    assert view["materialized"] is True
    assert view["count"] == 2
    assert [item["index"] for item in view["shots"]] == [0, 1]
    implicit = view["shots"][0]
    assert implicit["implicit"] is False
    # The legacy current visual asset moved to the materialized shot, never deleted.
    reloaded_asset = service.database.get_asset(asset.id)
    assert reloaded_asset.shot_id == implicit["id"]
    assert reloaded_asset.settings.get("role") == "visual"
    assert created.index == 1
    shot_file = (
        service.store.project_path(project) / "scenes" / "001" / "shots" / "002"
        / "shot.json"
    )
    assert shot_file.is_file()
    assert json.loads(shot_file.read_text(encoding="utf-8"))["id"] == created.id


def test_update_shot_retimes_and_persists_portable_copy(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 5})

    updated = service.update_shot(shot.id, {"duration_seconds": 7})

    assert updated.duration_seconds == 7
    on_disk = service.store.load_shots(project.slug, scene.index)
    # The materialized implicit shot keeps the scene's original duration.
    assert [item.duration_seconds for item in on_disk] == [10.0, 7.0]


def test_update_shot_rejects_overlap_that_swallows_neighbors(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"duration_seconds": 2})
    second = service.create_shot(scene.id, {
        "duration_seconds": 6,
        "transition_in": {"kind": "crossfade", "duration_seconds": 1},
    })

    with pytest.raises(ValueError, match="shorter than both adjacent shots"):
        service.update_shot(second.id, {
            "transition_in": {"kind": "crossfade", "duration_seconds": 5},
        })


def test_delete_shot_archives_media_and_keeps_asset_record(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 4})
    media_relative = Path(f"scenes/001/shots/{shot.index + 1:03d}/visual.png")
    media_path = service.store.project_path(project) / media_relative
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"png")
    asset = service.database.save_asset(Asset(
        project_id=project.id, scene_id=scene.id, shot_id=shot.id,
        type=AssetType.IMAGE, filepath=media_relative,
        backend="local_import", model="imported", seed=0,
    ))

    result = service.delete_shot(shot.id)

    assert result["deleted_shot_id"] == shot.id
    assert len(result["archived_assets"]) == 1
    assert not media_path.exists()
    archived = service.store.project_path(project) / result["archived_assets"][0]
    assert archived.is_file() and archived.read_bytes() == b"png"
    reloaded = service.database.get_asset(asset.id)
    assert reloaded is not None
    assert str(reloaded.filepath) == result["archived_assets"][0]
    # Only the materialized implicit shot remains.
    assert result["remaining_shots"] == 1
    assert result["scene_reverted_to_implicit"] is False

    # Removing that last stored shot reverts the scene to its legacy view.
    reverted = service.delete_shot(f"{scene.id}-implicit")
    assert reverted["remaining_shots"] == 0
    assert reverted["scene_reverted_to_implicit"] is True
    view = service.list_scene_shots(scene.id)
    assert view["materialized"] is False
    orphan_dir = service.store.project_path(project) / "scenes" / "001" / "shots"
    if orphan_dir.exists():
        assert list(orphan_dir.iterdir()) == []


def test_delete_shot_repoints_duplicate_asset_rows_for_one_media_file(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 4})
    relative = Path(f"scenes/001/shots/{shot.id}/visual.png")
    path = service.store.project_path(project) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"legacy-shared-media")
    rows = [
        service.database.save_asset(Asset(
            project_id=project.id, scene_id=scene.id, shot_id=shot.id,
            type=AssetType.IMAGE, filepath=relative, backend="legacy",
            model="legacy", seed=index,
        ))
        for index in range(2)
    ]

    service.delete_shot(shot.id)

    reloaded = [service.database.get_asset(row.id) for row in rows]
    assert reloaded[0] is not None and reloaded[1] is not None
    assert reloaded[0].filepath == reloaded[1].filepath
    assert (service.store.project_path(project) / reloaded[0].filepath).is_file()


def test_delete_shot_without_archive_removes_discarded_asset_records(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 4})
    relative = Path(f"scenes/001/shots/{shot.id}/visual.png")
    path = service.store.project_path(project) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"discard-me")
    asset = service.database.save_asset(Asset(
        project_id=project.id, scene_id=scene.id, shot_id=shot.id,
        type=AssetType.IMAGE, filepath=relative, backend="local_import",
        model="imported", seed=0,
    ))

    service.delete_shot(shot.id, archive_media=False)

    assert not path.exists()
    assert service.database.get_asset(asset.id) is None


def test_approve_shot_on_legacy_scene_materializes_and_locks_state(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    implicit_id = f"{scene.id}-implicit"

    approved = service.approve_shot(implicit_id)

    assert approved.status is ShotStatus.APPROVED
    view = service.list_scene_shots(scene.id)
    assert view["materialized"] is True
    assert view["approved"] == 1


def test_locked_scene_blocks_shot_writes(tmp_path: Path) -> None:
    from backend.pipeline.service import PipelineError

    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    locked = scene.model_copy(update={"status": SceneStatus.LOCKED, "locked": True})
    service.database.save_scene(locked)

    with pytest.raises(PipelineError, match="unlock"):
        service.create_shot(scene.id, {"duration_seconds": 3})


def test_snapshot_includes_shots_and_summary(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"duration_seconds": 3})

    snapshot = service.project_snapshot(project.id)

    payload = snapshot["scenes"][0]
    assert len(payload["shots"]) == 2
    assert payload["shot_summary"]["count"] == 2
    assert payload["shot_summary"]["materialized"] is True


def test_recovery_reindexes_shots_from_portable_files(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"duration_seconds": 3})
    before_ids = {item.id for item in service.database.list_shots(project.id)}

    with service.database.connection() as connection:
        connection.execute("DELETE FROM projects WHERE id=?", (project.id,))
    assert service.database.list_shots(project.id) == []

    listed, recovery = service.list_projects()

    assert any(item.id == project.id for item in listed)
    recovered = service.database.list_shots(project.id)
    assert {item.id for item in recovered} == before_ids
    assert all(not entry.get("type") == "recovery_failed" for entry in recovery)


# ---------------------------------------------------------------------------
# API contracts


@pytest.fixture()
def api(tmp_path: Path):
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    service = app.state.service
    project, scene = make_project_with_scene(service)
    return client, service, project, scene


def test_api_shot_crud_flow(api) -> None:
    client, service, project, scene = api

    listed = client.get(f"/api/scenes/{scene.id}/shots")
    assert listed.status_code == 200
    assert listed.json()["materialized"] is False

    created = client.post(f"/api/scenes/{scene.id}/shots", json={
        "title": "archival rocket",
        "duration_seconds": 4,
        "lane": "real",
        "visual_type": "reused_media",
        "transition_in": {"kind": "crossfade", "duration_seconds": 0.5},
    })
    assert created.status_code == 201
    shot = created.json()
    assert shot["lane"] == "real"

    patched = client.patch(f"/api/shots/{shot['id']}", json={"duration_seconds": 4.5})
    assert patched.status_code == 200
    assert patched.json()["duration_seconds"] == 4.5

    approved = client.post(f"/api/shots/{shot['id']}/approve", json={"lock": True})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["locked"] is True

    # Locked shots refuse edits and guarded deletion.
    denied = client.patch(f"/api/shots/{shot['id']}", json={"title": "nope"})
    assert denied.status_code == 409
    denied_delete = client.delete(f"/api/shots/{shot['id']}")
    assert denied_delete.status_code == 409

    # Re-approving unlocked mirrors the scene workflow.
    unlocked = client.post(f"/api/shots/{shot['id']}/approve", json={"lock": False})
    assert unlocked.status_code == 200
    assert unlocked.json()["locked"] is False

    deleted = client.delete(f"/api/shots/{shot['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted_shot_id"] == shot["id"]
    assert deleted.json()["remaining_shots"] >= 0

    snapshot = client.get(f"/api/projects/{project.id}")
    assert snapshot.status_code == 200
    scene_payload = snapshot.json()["scenes"][0]
    assert "shots" in scene_payload and "shot_summary" in scene_payload


def test_api_imports_local_reused_media_with_provenance(api) -> None:
    client, service, project, scene = api
    service.update_scene(scene.id, {"visual_type": "reused_media"})

    imported = client.post(
        f"/api/scenes/{scene.id}/reused-media",
        files={"file": ("public-domain-mars.png", b"not-a-real-png", "image/png")},
        data={"source": json.dumps({
            "title": "Public-domain Mars reference",
            "source_url": "https://example.test/mars",
            "license_note": "Public domain; verified by producer.",
            "classification": "documentary_evidence",
        })},
    )

    assert imported.status_code == 201, imported.text
    asset = imported.json()
    assert asset["type"] == "image"
    assert asset["backend"] == "imported_local"
    assert asset["settings"]["role"] == "visual"
    assert asset["settings"]["source"]["license_note"] == "Public domain; verified by producer."
    assert (service.store.project_path(project) / asset["filepath"]).is_file()
    snapshot_asset = service.project_snapshot(project.id)["assets"][0]
    assert snapshot_asset["current"] is True

    shots = client.get(f"/api/scenes/{scene.id}/shots").json()
    assert shots["materialized"] is True
    assert shots["shots"][0]["visual_type"] == "reused_media"
    assert shots["shots"][0]["status"] == "ready"


def test_api_imports_local_reused_media_into_selected_shot(api) -> None:
    client, service, project, scene = api
    shot = client.post(
        f"/api/scenes/{scene.id}/shots",
        json={
            "duration_seconds": 4,
            "lane": "real",
            "visual_type": "reused_media",
            "title": "Archival insert",
        },
    ).json()

    imported = client.post(
        f"/api/shots/{shot['id']}/reused-media",
        files={"file": ("archive.webp", b"local-image", "image/webp")},
        data={"source": json.dumps({
            "title": "Producer-owned archive still",
            "classification": "documentary_evidence",
        })},
    )

    assert imported.status_code == 201, imported.text
    asset = imported.json()
    assert asset["shot_id"] == shot["id"]
    assert asset["type"] == "image"
    assert asset["prompt"] == ""
    assert (service.store.project_path(project) / asset["filepath"]).is_file()
    selected = client.get(f"/api/scenes/{scene.id}/shots").json()["shots"]
    selected = next(item for item in selected if item["id"] == shot["id"])
    assert selected["status"] == "ready"
    assert selected["source"]["title"] == "Producer-owned archive still"
    assert selected["source"]["license_note"] == ""


def test_api_imports_existing_ai_images_for_scene_and_selected_shot(api) -> None:
    client, service, project, scene = api
    service.update_scene(scene.id, {"visual_type": "ideogram4_still"})

    scene_import = client.post(
        f"/api/scenes/{scene.id}/imported-image",
        files={"file": ("ideogram-result.png", b"generated-scene-image", "image/png")},
    )
    assert scene_import.status_code == 201, scene_import.text
    scene_asset = scene_import.json()
    assert scene_asset["backend"] == "imported_ai_image"
    assert scene_asset["settings"]["visual_type"] == "ideogram4_still"
    assert scene_asset["settings"]["source"]["title"] == "ideogram-result.png"

    shot = client.post(
        f"/api/scenes/{scene.id}/shots",
        json={
            "duration_seconds": 4,
            "lane": "image",
            "visual_type": "krea2_still",
            "title": "Imported generated insert",
        },
    ).json()
    shot_import = client.post(
        f"/api/shots/{shot['id']}/imported-image",
        files={"file": ("krea-result.webp", b"generated-shot-image", "image/webp")},
    )
    assert shot_import.status_code == 201, shot_import.text
    shot_asset = shot_import.json()
    assert shot_asset["shot_id"] == shot["id"]
    assert shot_asset["settings"]["visual_type"] == "krea2_still"
    assert (service.store.project_path(project) / shot_asset["filepath"]).is_file()
    selected = client.get(f"/api/scenes/{scene.id}/shots").json()["shots"]
    selected = next(item for item in selected if item["id"] == shot["id"])
    assert selected["status"] == "ready"


def test_api_overlay_endpoints_manage_cues(api) -> None:
    client, _, _, scene = api

    shot = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 6, "lane": "html", "visual_type": "graphic_screen",
    }).json()

    added = client.post(f"/api/shots/{shot['id']}/overlays", json={
        "kind": "exact_text",
        "exact_text": "AUGUST 7, 1976",
        "start_seconds": 0.5,
        "duration_seconds": 1.5,
        "style": {"color": "#ffffff"},
    })
    assert added.status_code == 201
    overlay = added.json()["overlays"][0]
    assert overlay["exact_text"] == "AUGUST 7, 1976"

    patched = client.patch(
        f"/api/shots/{shot['id']}/overlays/{overlay['id']}",
        json={"opacity": 0.8},
    )
    assert patched.status_code == 200
    assert patched.json()["overlays"][0]["opacity"] == 0.8

    scoped = client.patch(
        f"/api/overlays/{overlay['id']}?project_id={scene.project_id}",
        json={"start_seconds": 1.0},
    )
    assert scoped.status_code == 200
    assert scoped.json()["overlays"][0]["start_seconds"] == 1.0

    missing_scope = client.patch(f"/api/overlays/{overlay['id']}", json={})
    assert missing_scope.status_code == 422

    removed = client.delete(f"/api/shots/{shot['id']}/overlays/{overlay['id']}")
    assert removed.status_code == 200
    assert removed.json()["overlays"] == []


def test_api_rejects_invalid_shot_payloads(api) -> None:
    client, _, _, scene = api

    bad_extra = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 2, "hacker_field": True,
    })
    assert bad_extra.status_code == 422

    bad_timing = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 0,
    })
    assert bad_timing.status_code == 422

    bad_overlay = client.post(
        f"/api/scenes/{scene.id}/shots",
        json={
            "duration_seconds": 2,
            "overlays": [{
                "kind": "exact_text", "exact_text": "", "start_seconds": 0,
                "duration_seconds": 1,
            }],
        },
    )
    assert bad_overlay.status_code == 422


def test_api_shot_generation_and_scene_render_endpoints(api) -> None:
    client, service, _, scene = api
    shot = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 4,
    }).json()

    generated = client.post(f"/api/shots/{shot['id']}/generate", json={})
    assert generated.status_code == 202
    payload = generated.json()
    assert payload["stage"] == "shot_generate"
    job = service.jobs.get(payload["id"])
    assert job is not None and job.status.value == "completed", job.error
    assert job.attempt_count == 1

    # Every beat needs media before the scene can compile.
    for other in client.get(f"/api/scenes/{scene.id}/shots").json()["shots"]:
        assert client.post(f"/api/shots/{other['id']}/generate", json={}).status_code == 202

    rendered = client.post(f"/api/scenes/{scene.id}/render", json={})
    assert rendered.status_code == 202
    assert rendered.json()["stage"] == "scene_render"
    render_job = service.jobs.get(rendered.json()["id"])
    assert render_job is not None and render_job.status.value == "completed"
    assert render_job.attempt_count == 1

    missing = client.post("/api/shots/does-not-exist/generate", json={})
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Regression coverage (integration review findings)


def test_scene_render_works_without_force(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 4})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)

    summary = service.render_scene(scene.id)

    assert summary["cache_hit"] is False
    assert (service.store.project_path(project) / "scenes" / "001"
            / "rendered.mp4").is_file()


def test_unchanged_exact_overlay_is_not_rerendered(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {
        "duration_seconds": 6, "lane": "html", "visual_type": "graphic_screen",
        "overlays": [{
            "kind": "exact_text", "exact_text": "AUGUST 7, 1976",
            "template": "date_label", "start_seconds": 0.25,
            "duration_seconds": 1.0,
        }],
    })
    project = service._project(scene.project_id)
    cue = shot.overlays[0]

    first = service._render_exact_text_overlay(project, shot, cue)
    stamp = first.stat().st_mtime_ns
    second = service._render_exact_text_overlay(project, shot, cue)

    assert second == first
    assert second.stat().st_mtime_ns == stamp


def test_shot_edit_invalidates_compiled_scene_media(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 4})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    service.render_scene(scene.id)
    compiled = root = service.store.project_path(project) / "scenes" / "001"
    assert (compiled / "rendered.mp4").is_file()

    service.update_shot(shot.id, {"title": "renamed beat"})

    assert not (compiled / "rendered.mp4").exists()
    assert not (compiled / "render-manifest.json").exists()
    assert service._compiled_scene_media(project, scene) is None
    preflight = service.render_preflight(project.id)
    codes = {issue["code"] for issue in preflight["scenes"][0]["issues"]}
    assert "scene_unrendered" in codes
    assert preflight["scenes"][0]["ready"] is False


def test_regeneration_archives_previous_variant_and_updates_record(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 4})
    first_asset = service.generate_shot(shot.id)

    service.generate_shot(shot.id, force=True)

    reloaded = service.database.get_asset(first_asset.id)
    project = service._project(scene.project_id)
    assert str(reloaded.filepath).startswith("variants/archive/")
    assert (service.store.project_path(project) / reloaded.filepath).is_file()


def test_job_attempts_are_counted_and_capped(tmp_path: Path) -> None:
    from backend.pipeline.service import PipelineError

    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 3})
    job = service.queue_shot_generation(shot.id, regenerate=False)

    counted = service._begin_job_attempt(job.id)
    assert counted.attempt_count == 1
    job_row = service.jobs.get(job.id)
    exhausted = job_row.model_copy(update={"attempt_count": job_row.max_attempts})
    service.database.save_job(exhausted)

    with pytest.raises(PipelineError, match="allowed attempts"):
        service._begin_job_attempt(job.id)


def test_exact_text_overlay_opacity_applies_only_at_composition(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {
        "duration_seconds": 5, "lane": "html", "visual_type": "graphic_screen",
        "overlays": [{
            "kind": "exact_text", "exact_text": "TITLE CARD",
            "template": "caption_line", "start_seconds": 0.0,
            "duration_seconds": 1.0, "opacity": 0.5,
        }],
    })

    document = service._exact_overlay_document(shot.overlays[0], 320, 180)
    assert "opacity" not in document


def test_exact_text_placement_fields_are_restricted(api) -> None:
    client, _, _, scene = api

    rejected_offset = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 4, "lane": "html", "visual_type": "graphic_screen",
        "overlays": [{
            "kind": "exact_text", "exact_text": "X", "template": "date_label",
            "start_seconds": 0.0, "duration_seconds": 1.0, "x": 12,
        }],
    })
    assert rejected_offset.status_code == 422

    rejected_anchor = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 4, "lane": "html", "visual_type": "graphic_screen",
        "overlays": [{
            "kind": "exact_text", "exact_text": "X", "template": "date_label",
            "start_seconds": 0.0, "duration_seconds": 1.0, "anchor": "top_left",
        }],
    })
    assert rejected_anchor.status_code == 422


def test_exact_text_rejects_unsafe_css_colors(api) -> None:
    client, _, _, scene = api

    bad_color = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 4, "lane": "html", "visual_type": "graphic_screen",
        "overlays": [{
            "kind": "exact_text", "exact_text": "X", "template": "date_label",
            "start_seconds": 0.0, "duration_seconds": 1.0,
            "style": {"color": "red; } body { background:url(http://x)"},
        }],
    })
    assert bad_color.status_code == 422

    good_color = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 4, "lane": "html", "visual_type": "graphic_screen",
        "overlays": [{
            "kind": "exact_text", "exact_text": "X", "template": "date_label",
            "start_seconds": 0.0, "duration_seconds": 1.0,
            "style": {"color": "#ffd700"},
        }],
    })
    assert good_color.status_code == 201


def test_shot_generation_runs_as_background_job(api) -> None:
    client, service, _, scene = api
    shot = client.post(f"/api/scenes/{scene.id}/shots", json={
        "duration_seconds": 3, "lane": "image", "visual_type": "krea2_still",
    }).json()

    response = client.post(f"/api/shots/{shot['id']}/generate", json={})
    assert response.status_code == 202
    payload = response.json()
    assert payload["stage"] == "shot_generate"
    # TestClient executes the background task before returning; the row must
    # already be terminal and the media recorded under the shot.
    job = service.jobs.get(payload["id"])
    assert job.status.value == "completed", job.error
    assets = [
        asset for asset in service.database.list_assets(scene.project_id)
        if asset.shot_id == shot["id"]
    ]
    assert assets and assets[-1].settings.get("role") == "visual"

    duplicate = client.post(f"/api/shots/{shot['id']}/regenerate", json={})
    assert duplicate.status_code == 202


# ---------------------------------------------------------------------------
# Stable shot-media storage across insertion, deletion, and reordering


def _shot_assets(service, project):
    return [
        asset for asset in service.database.list_assets(project.id)
        if asset.shot_id is not None
    ]


def _assert_all_asset_files_exist_and_are_owned(service, project) -> None:
    root = service.store.project_path(project)
    for asset in _shot_assets(service, project):
        path = root / asset.filepath
        assert path.is_file(), f"missing media for {asset.shot_id}: {asset.filepath}"
        parts = Path(asset.filepath).parts
        owned_now = len(parts) > 3 and parts[2] == "shots" and parts[3] == asset.shot_id
        archived = "variants" in parts
        assert owned_now or archived, (
            f"asset {asset.id} for shot {asset.shot_id} points outside its "
            f"stable directory: {asset.filepath}"
        )


def _media_hashes_by_shot(service, project) -> dict:
    root = service.store.project_path(project)
    state = {}
    for asset in _shot_assets(service, project):
        path = root / asset.filepath
        if path.is_file():
            state.setdefault(asset.shot_id, []).append(
                (str(asset.filepath), path.read_bytes()),
            )
    return state


def test_deleting_first_shot_preserves_surviving_media(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"title": "a", "duration_seconds": 3})
    service.create_shot(scene.id, {"title": "b", "duration_seconds": 4})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    before = _media_hashes_by_shot(service, project)
    before_ids = {}
    for asset in _shot_assets(service, project):
        before_ids.setdefault(asset.shot_id, []).append(asset.id)
    first = next(s for s in service.shots_for_scene(scene) if s.index == 0)

    result = service.delete_shot(first.id)

    assert result["deleted_shot_id"] == first.id
    _assert_all_asset_files_exist_and_are_owned(service, project)
    after = _media_hashes_by_shot(service, project)
    for shot_id, entries in before.items():
        if shot_id == first.id:
            continue
        assert after.get(shot_id) == entries, f"shot {shot_id} media disturbed"
    # The deleted beat's history lives in the archive with valid paths.
    # Ownership columns are nulled by the FK on delete, so track by asset id.
    root = service.store.project_path(project)
    for asset_id in before_ids.get(first.id, []):
        row = service.database.get_asset(asset_id)
        assert row is not None
        parts = Path(row.filepath).parts
        assert "variants" in parts and "archive" in parts
        assert (root / row.filepath).is_file()


def test_moving_last_shot_to_front_keeps_media_stable(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"title": "a", "duration_seconds": 3})
    moved = service.create_shot(scene.id, {"title": "b", "duration_seconds": 3})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    before = _media_hashes_by_shot(service, project)

    updated = service.update_shot(moved.id, {"index": 0})

    assert updated.index == 0
    ordered = service.shots_for_scene(scene)
    assert [item.title for item in ordered] == ["b", "", "a"]
    _assert_all_asset_files_exist_and_are_owned(service, project)
    assert _media_hashes_by_shot(service, project) == {
        shot_id: entries for shot_id, entries in before.items()
    }


def test_inserting_a_middle_shot_leaves_neighbors_untouched(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"title": "a", "duration_seconds": 3})
    service.create_shot(scene.id, {"title": "c", "duration_seconds": 3})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    before = _media_hashes_by_shot(service, project)

    middle = service.create_shot(scene.id, {
        "index": 1, "title": "b", "duration_seconds": 2,
    })
    service.generate_shot(middle.id)

    ordered = service.shots_for_scene(scene)
    assert [(item.index, item.title) for item in ordered] == [
        (0, ""), (1, "b"), (2, "a"), (3, "c"),
    ]
    _assert_all_asset_files_exist_and_are_owned(service, project)
    after = _media_hashes_by_shot(service, project)
    for shot_id, entries in before.items():
        assert after[shot_id] == entries, f"shot {shot_id} media disturbed"
    assert all("shots" in Path(filepath).parts and middle.id in Path(filepath).parts
               for filepath, _ in after[middle.id])


def test_regenerating_after_reorder_targets_the_moved_shot(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"title": "a", "duration_seconds": 3})
    moved = service.create_shot(scene.id, {"title": "b", "duration_seconds": 3})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    service.update_shot(moved.id, {"index": 0})

    regenerated = service.generate_shot(moved.id, force=True)

    project = service._project(scene.project_id)
    assert regenerated.shot_id == moved.id
    _assert_all_asset_files_exist_and_are_owned(service, project)
    current = current_visual_asset(
        service.database.get_shot(moved.id),
        service.database.list_assets(project.id),
    )
    assert current is not None and moved.id in Path(current.filepath).parts


def test_legacy_numbered_media_is_migrated_before_renumbering(tmp_path: Path) -> None:
    import shutil as _shutil

    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"title": "a", "duration_seconds": 3})
    survivor = service.create_shot(scene.id, {"title": "b", "duration_seconds": 3})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    root = service.store.project_path(project)
    asset = next(a for a in _shot_assets(service, project) if a.shot_id == survivor.id)
    # Drag one surviving asset back into the legacy numbered layout.
    legacy_dir = root / "scenes" / "001" / "shots" / f"{survivor.index + 1:03d}"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / "visual.png"
    _shutil.move(str(root / asset.filepath), str(legacy_file))
    service.database.save_asset(asset.model_copy(update={
        "filepath": legacy_file.relative_to(root),
    }))
    original_bytes = legacy_file.read_bytes()

    # Any persist (this edit) must migrate the media before renumbering.
    service.update_shot(survivor.id, {"title": "b renamed"})

    migrated = service.database.get_asset(asset.id)
    migrated_path = root / migrated.filepath
    assert survivor.id in migrated_path.parts
    assert migrated_path.is_file()
    assert migrated_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Compiled-scene history, strict timeline coupling, force semantics


def test_force_scene_render_bypasses_cache_and_archives_history(
    tmp_path: Path,
) -> None:
    from backend.rendering.manifests import sha256_file

    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 3})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    first = service.render_scene(scene.id)
    root = service.store.project_path(project)
    published = root / "scenes" / "001" / "rendered.mp4"
    first_bytes = published.read_bytes()
    scene_render_rows = [
        asset for asset in service.database.list_assets(project.id)
        if asset.settings.get("role") == "scene_render"
    ]
    assert len(scene_render_rows) == 1

    forced = service.render_scene(scene.id, force=True)

    # force bypasses the second-level cache even though inputs are identical.
    assert forced["cache_hit"] is False
    assert published.is_file()
    # The retired compile was archived and its asset row repointed, so no row
    # claims bytes that no longer match its stored hash.
    history = [
        asset for asset in service.database.list_assets(project.id)
        if asset.settings.get("role") == "scene_render"
    ]
    assert len(history) == 2
    by_path = {str(asset.filepath): asset for asset in history}
    archived_row = next(
        asset for asset in history
        if {"variants", "archive"} <= set(Path(asset.filepath).parts)
    )
    archived_file = root / archived_row.filepath
    assert archived_file.read_bytes() == first_bytes
    live_row = by_path[str(Path("scenes") / "001" / "rendered.mp4")]
    assert sha256_file(published) == live_row.hash

    cached = service.render_scene(scene.id)
    assert cached["cache_hit"] is True


def test_failed_force_scene_render_preserves_last_good_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    shot = service.create_shot(scene.id, {"duration_seconds": 3})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    service.render_scene(scene.id)
    root = service.store.project_path(project)
    published = root / "scenes" / "001" / "rendered.mp4"
    manifest = published.with_name("render-manifest.json")
    media_before = published.read_bytes()
    manifest_before = manifest.read_bytes()
    live_row = next(
        asset for asset in service.database.list_assets(project.id)
        if asset.settings.get("role") == "scene_render"
    )

    def fail_render(*args, **kwargs):
        raise RuntimeError("simulated encoder failure")

    monkeypatch.setattr("backend.pipeline.service.SceneAssembler.render", fail_render)
    with pytest.raises(RuntimeError, match="simulated encoder failure"):
        service.render_scene(scene.id, force=True)

    assert published.read_bytes() == media_before
    assert manifest.read_bytes() == manifest_before
    assert service.database.get_asset(live_row.id).filepath == live_row.filepath
    assert not list((root / "variants" / "archive").glob("rendered-*.mp4"))


def test_project_timeline_auto_compiles_multishot_scenes(tmp_path: Path) -> None:
    import wave as _wave

    service = make_service(tmp_path)
    project, scene = make_project_with_scene(service)
    service.create_shot(scene.id, {"duration_seconds": 3})
    for beat in service.shots_for_scene(scene):
        service.generate_shot(beat.id)
    narration = service.store.project_path(project) / "narration" / "master.wav"
    narration.parent.mkdir(parents=True, exist_ok=True)
    with _wave.open(str(narration), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8000)

    timeline = service._build_timeline(project)
    assert Path(timeline.clips[0].path).name == "rendered.mp4"
    assert (service.store.project_path(project) / "scenes" / "001" / "rendered.mp4").is_file()


def test_lane_resolution_failure_carries_structured_payload(tmp_path: Path) -> None:
    from backend.pipeline.service import LaneResolutionRejected

    service = make_service(tmp_path)
    _, scene = make_project_with_scene(service)
    # FLUX_STILL is deliberately absent from the wired image-lane targets, so
    # rejection is deterministic on any host.
    shot = service.create_shot(scene.id, {
        "duration_seconds": 3, "lane": "image", "visual_type": "flux_still",
    })
    service.mock_mode = False
    try:
        service.validate_shot_lane(shot.id)
    except LaneResolutionRejected as exc:
        assert isinstance(exc.payload, dict)
        assert "code" in exc.payload and "message" in exc.payload
        assert exc.payload["code"] != "readiness"
    else:
        raise AssertionError("expected lane resolution rejection off mock mode")
    finally:
        service.mock_mode = True
