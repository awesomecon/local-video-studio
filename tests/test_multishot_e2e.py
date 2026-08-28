"""End-to-end CPU-only vertical slice: shots -> scene render -> project render.

Covers: image shot -> video shot -> HTML/title shot, two transitions, a timed
exact-text overlay, an initial scene render without force, a full cache hit on
re-render, single-shot invalidation after an edit (with regeneration), a scene
recompile, and project render consuming compiled scenes without generation.
"""

import json
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core import load_config
from backend.rendering.manifests import load_manifest
from backend.schemas import Scene


@pytest.fixture()
def studio(tmp_path: Path):
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    created = client.post("/api/projects", json={
        "title": "Vertical Slice", "topic": "multishot acceptance",
        "target_duration": 12, "resolution": [160, 90], "fps": 12,
    })
    assert created.status_code == 201
    project_id = created.json()["project"]["id"]
    return client, app.state.service, project_id


def add_scene(service, project_id: str) -> Scene:
    project = service.database.get_project(project_id)
    scene = Scene(project_id=project_id, index=0, duration=9,
                  narration="Slice.", visual_prompt="slice")
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)
    return scene


def test_vertical_slice_scene_and_project_render(studio) -> None:
    client, service, project_id = studio
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    slug = project["slug"]
    fps = project["fps"]

    scene = add_scene(service, project_id)
    client.post(f"/api/scenes/{scene.id}/shots", json={
        "title": "still beat", "duration_seconds": 3,
        "lane": "image", "visual_type": "krea2_still", "seed": 7,
        "visual_prompt": "a quiet canal",
    })
    video_shot = client.post(f"/api/scenes/{scene.id}/shots", json={
        "title": "moving beat", "duration_seconds": 3,
        "lane": "h3", "visual_type": "h3_audiovisual",
        "transition_in": {"kind": "crossfade", "duration_seconds": 0.5},
    }).json()
    client.post(f"/api/scenes/{scene.id}/shots", json={
        "title": "card beat", "duration_seconds": 3,
        "lane": "html", "visual_type": "graphic_screen",
        "transition_in": {"kind": "fade_through_black", "duration_seconds": 0.5},
        "overlays": [{
            "kind": "exact_text", "exact_text": "AUGUST 7, 1976",
            "template": "date_label",
            "start_seconds": 0.25, "duration_seconds": 1.0,
            "style": {"color": "#ffffff"},
        }],
    }).json()

    listed = client.get(f"/api/scenes/{scene.id}/shots").json()
    assert listed["count"] == 4
    for shot in listed["shots"]:
        generated = client.post(f"/api/shots/{shot['id']}/generate", json={})
        assert generated.status_code == 202, generated.text
        job = service.jobs.get(generated.json()["id"])
        assert job.status.value == "completed", job.error

    # Preflight blocks on nothing but the missing compiled render.
    preflight = client.get(f"/api/projects/{project_id}/render/preflight").json()
    assert preflight["scenes"][0]["ready"] is False
    codes = {issue["code"] for issue in preflight["scenes"][0]["issues"]}
    assert codes == {"scene_unrendered"}

    # The initial render needs no force: scene_unrendered is why we render.
    rendered = client.post(f"/api/scenes/{scene.id}/render", json={})
    assert rendered.status_code == 202, rendered.text
    first_job = service.jobs.get(rendered.json()["id"])
    assert first_job.status.value == "completed", first_job.error
    summary = first_job.parameters["result"]
    assert summary["cache_hit"] is False
    # implicit beat (9s=108f) + three 3s beats minus two 6-frame overlaps.
    assert summary["total_frames"] == 108 + 36 * 3 - 6 - 6

    root = service.store.project_path(slug)
    manifest = load_manifest(root / "scenes" / "001" / "render-manifest.json")
    assert manifest is not None and manifest["scene_id"] == scene.id
    assert len(manifest["shots"]) == 4
    assert [boundary["kind"] for boundary in manifest["boundaries"]] == [
        "cut", "crossfade", "fade_through_black",
    ]

    preflight = client.get(f"/api/projects/{project_id}/render/preflight").json()
    assert preflight["scenes"][0]["ready"] is True, preflight["scenes"][0]["issues"]

    # Re-render is a full cache hit: same plan and shot keys.
    again = client.post(f"/api/scenes/{scene.id}/render", json={})
    assert again.status_code == 202
    summary_again = service.jobs.get(again.json()["id"]).parameters["result"]
    assert summary_again["cache_hit"] is True
    assert summary_again["cache_key"] == summary["cache_key"]

    # Editing one shot invalidates only that shot's composite; the others hit.
    edited = client.patch(f"/api/shots/{video_shot['id']}", json={
        "duration_seconds": 4,
    })
    assert edited.status_code == 200
    assert not (root / "scenes" / "001" / "rendered.mp4").exists()

    # The longer duration needs fresh source media, so regenerate that beat
    # before recompiling (its 3s visual would be short by QC).
    regenerated = client.post(f"/api/shots/{video_shot['id']}/regenerate", json={})
    assert regenerated.status_code == 202
    regen_job = service.jobs.get(regenerated.json()["id"])
    assert regen_job.status.value == "completed", regen_job.error
    assert regen_job.attempt_count == 1

    recompiled = client.post(f"/api/scenes/{scene.id}/render?force=true", json={})
    assert recompiled.status_code == 202
    result_summary = service.jobs.get(recompiled.json()["id"]).parameters["result"]
    assert result_summary["cache_hit"] is False
    assert result_summary["total_frames"] == 108 + 36 + 48 + 36 - 6 - 6

    # Project render consumes the compiled scene render without generating.
    narration = root / "narration" / "master.wav"
    narration.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(narration), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * 9))
    render_job = client.post(f"/api/projects/{project_id}/render", json={"force": True})
    assert render_job.status_code == 202, render_job.text
    final = service.jobs.get(render_job.json()["id"])
    assert final is not None and final.status.value == "completed", final.error

    timeline_manifest = json.loads(
        (root / "timeline.json").read_text(encoding="utf-8")
    )
    sources = {Path(clip["path"]).name for clip in timeline_manifest["clips"]}
    assert sources == {"rendered.mp4"}
