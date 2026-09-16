"""Tests for the final-render history (superseded renders and their deletion).

Covers the render-history endpoints and the storage behavior:

  - the first final render has no history;
  - a forced re-render preserves the previous ``renders/final.mp4`` in
    ``renders/history/`` and records a ``final_render_history`` asset;
  - ``GET /api/projects/{id}/render-history`` lists preserved renders
    newest first with local playback/download URLs;
  - ``DELETE /api/projects/{id}/render-history/{asset_id}`` removes the
    history file and its index row;
  - the live final render (``final_render``) is protected from deletion.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core import load_config
from backend.schemas import ProjectCreate


def _client(tmp_path: Path) -> tuple[object, TestClient, object]:
    app = create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    client = TestClient(app)
    return app, client, app.state.service


def _create_and_render(client: TestClient, service: object, title: str = "Render History") -> str:
    project = service.create_project(  # type: ignore[union-attr]
        ProjectCreate(
            title=title,
            topic="final render history",
            target_duration=1,
            resolution=(160, 90),
            fps=12,
        )
    )
    service.run_project(project.id)  # type: ignore[union-attr]
    return project.id


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_first_render_has_no_history(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)

    response = client.get(f"/api/projects/{project_id}/render-history")
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id
    assert body["renders"] == []

    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]
    assert (root / "renders" / "final.mp4").is_file()
    assert not (root / "renders" / "history").is_dir() or not any(
        (root / "renders" / "history").iterdir()
    )


def test_re_render_preserves_previous_final_in_history(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]
    final = root / "renders" / "final.mp4"
    first_hash = _sha256(final)

    service.run_render(project_id, force=True)  # type: ignore[union-attr]

    # The superseded export is preserved under renders/history/ with the
    # portable timestamped name, and the live final.mp4 was replaced.
    history_files = sorted((root / "renders" / "history").iterdir())
    assert len(history_files) == 1
    assert re.fullmatch(r"final-\d{8}T\d{6}-[0-9a-f]{8}\.mp4", history_files[0].name)
    assert _sha256(history_files[0]) == first_hash
    assert final.is_file()

    # The preserved render is indexed as a final_render_history asset and
    # listed by the endpoint with a local URL and its file size.
    snapshot = client.get(f"/api/projects/{project_id}").json()
    history_assets = [
        asset for asset in snapshot["assets"]
        if (asset.get("settings") or {}).get("role") == "final_render_history"
    ]
    assert len(history_assets) == 1
    assert history_assets[0]["filepath"].startswith("renders/history/")

    body = client.get(f"/api/projects/{project_id}/render-history").json()
    assert len(body["renders"]) == 1
    entry = body["renders"][0]
    assert entry["id"] == history_assets[0]["id"]
    assert entry["filename"] == history_files[0].name
    assert entry["filepath"] == f"renders/history/{history_files[0].name}"
    assert entry["hash"] == first_hash
    assert entry["available"] is True
    assert entry["size_bytes"] == history_files[0].stat().st_size
    assert entry["size_bytes"] > 0
    assert entry["url"] == f"/api/projects/{project_id}/assets/{history_assets[0]['id']}/file"
    assert entry["created_at"]


def test_history_is_newest_first_and_grows_with_re_renders(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]
    final = root / "renders" / "final.mp4"

    hashes = []
    for _ in range(3):
        hashes.append(_sha256(final))
        service.run_render(project_id, force=True)  # type: ignore[union-attr]
        # The just-superseded file must already be the newest history entry.
        entry = client.get(f"/api/projects/{project_id}/render-history").json()["renders"][0]
        assert entry["hash"] == hashes[-1]

    body = client.get(f"/api/projects/{project_id}/render-history").json()
    entries = body["renders"]
    assert [entry["hash"] for entry in entries] == list(reversed(hashes))
    assert [entry["created_at"] for entry in entries] == sorted(
        (entry["created_at"] for entry in entries), reverse=True,
    )


def test_failed_re_render_does_not_create_history_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]
    final = root / "renders" / "final.mp4"
    original_hash = _sha256(final)

    def fail_render(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic final-render failure")

    monkeypatch.setattr(service.renderer, "render_final", fail_render)  # type: ignore[union-attr]
    with pytest.raises(RuntimeError, match="synthetic final-render failure"):
        service._ensure_final(project, force=True)  # type: ignore[union-attr]

    assert final.is_file() and _sha256(final) == original_hash
    assert client.get(f"/api/projects/{project_id}/render-history").json()["renders"] == []
    assert list((root / "renders" / "history").glob("*.mp4")) == []


def test_failure_after_publication_keeps_superseded_final_in_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]
    final = root / "renders" / "final.mp4"
    original_hash = _sha256(final)

    def publish_then_fail(*_args: object, **_kwargs: object) -> None:
        final.write_bytes(b"replacement published before validation failed")
        raise RuntimeError("synthetic post-publication failure")

    monkeypatch.setattr(service.renderer, "render_final", publish_then_fail)  # type: ignore[union-attr]
    with pytest.raises(RuntimeError, match="synthetic post-publication failure"):
        service._ensure_final(project, force=True)  # type: ignore[union-attr]

    history = client.get(f"/api/projects/{project_id}/render-history").json()["renders"]
    assert len(history) == 1
    assert history[0]["hash"] == original_hash
    assert _sha256(root / history[0]["filepath"]) == original_hash
    assert _sha256(final) != original_hash


def test_history_is_reindexed_from_portable_project_after_database_recovery(
    tmp_path: Path,
) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    service.run_render(project_id, force=True)  # type: ignore[union-attr]
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]
    before = client.get(f"/api/projects/{project_id}/render-history").json()["renders"]
    history_file = root / before[0]["filepath"]
    history_hash = _sha256(history_file)

    # Simulate rebuilding the disposable SQLite index from the portable tree.
    with service.database.connection() as connection:  # type: ignore[union-attr]
        connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
    assert service.database.get_project(project_id) is None  # type: ignore[union-attr]
    assert service.database.list_assets(project_id) == []  # type: ignore[union-attr]

    response = client.get(f"/api/projects/{project_id}/render-history")
    assert response.status_code == 200
    recovered = response.json()["renders"]
    assert len(recovered) == 1
    assert recovered[0]["filepath"] == before[0]["filepath"]
    assert recovered[0]["hash"] == history_hash
    assert recovered[0]["available"] is True
    assert history_file.is_file()


def test_delete_render_history_entry_removes_file_and_record(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    service.run_render(project_id, force=True)  # type: ignore[union-attr]
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]
    final = root / "renders" / "final.mp4"
    final_hash_after = _sha256(final)

    entry = client.get(f"/api/projects/{project_id}/render-history").json()["renders"][0]
    history_file = root / entry["filepath"]
    assert history_file.is_file()

    response = client.delete(f"/api/projects/{project_id}/render-history/{entry['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["asset_id"] == entry["id"]
    assert body["filepath"] == entry["filepath"]
    assert body["removed_file"] is True

    # File and index row are gone; the live final render is untouched.
    assert not history_file.exists()
    assert not service.database.get_asset(entry["id"])  # type: ignore[union-attr]
    assert client.get(f"/api/projects/{project_id}/render-history").json()["renders"] == []
    assert final.is_file() and _sha256(final) == final_hash_after
    current = [
        asset for asset in client.get(f"/api/projects/{project_id}").json()["assets"]
        if (asset.get("settings") or {}).get("role") == "final_render"
    ]
    assert current, "the current final render's asset record survives"
    latest = max(current, key=lambda asset: asset["created_at"])
    assert latest["filepath"] == "renders/final.mp4"


def test_delete_current_final_render_is_protected(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    service.run_render(project_id, force=True)  # type: ignore[union-attr]
    project = service._project(project_id)  # type: ignore[union-attr]
    final = service.store.project_path(project) / "renders" / "final.mp4"  # type: ignore[union-attr]
    final_hash = _sha256(final)

    snapshot = client.get(f"/api/projects/{project_id}").json()
    current = [
        asset for asset in snapshot["assets"]
        if (asset.get("settings") or {}).get("role") == "final_render"
    ][-1]
    response = client.delete(f"/api/projects/{project_id}/render-history/{current['id']}")
    assert response.status_code == 409
    assert "current final" in response.json()["detail"]
    # The current file and its record survive.
    assert final.is_file() and _sha256(final) == final_hash
    assert service.database.get_asset(current["id"]) is not None  # type: ignore[union-attr]
    # The history entry is still deletable.
    entry = client.get(f"/api/projects/{project_id}/render-history").json()["renders"][0]
    assert client.delete(
        f"/api/projects/{project_id}/render-history/{entry['id']}"
    ).status_code == 200


def test_delete_unknown_history_entry_is_404(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    response = client.delete(
        f"/api/projects/{project_id}/render-history/does-not-exist"
    )
    assert response.status_code == 404


def test_delete_entry_of_another_project_is_404(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    first = _create_and_render(client, service, title="History A")
    second = _create_and_render(client, service, title="History B")
    service.run_render(second, force=True)  # type: ignore[union-attr]
    entry = client.get(f"/api/projects/{second}/render-history").json()["renders"][0]

    # Addressing B's entry through A's project must not touch it.
    response = client.delete(f"/api/projects/{first}/render-history/{entry['id']}")
    assert response.status_code == 404
    assert client.get(f"/api/projects/{second}/render-history").json()["renders"][0]["id"] == entry["id"]
    assert service.database.get_asset(entry["id"]) is not None  # type: ignore[union-attr]


def test_delete_entry_with_missing_file_removes_record(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    service.run_render(project_id, force=True)  # type: ignore[union-attr]
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]

    entry = client.get(f"/api/projects/{project_id}/render-history").json()["renders"][0]
    (root / entry["filepath"]).unlink()

    response = client.delete(f"/api/projects/{project_id}/render-history/{entry['id']}")
    assert response.status_code == 200
    assert response.json()["removed_file"] is False
    assert not service.database.get_asset(entry["id"])  # type: ignore[union-attr]


def test_history_lists_entry_without_url_when_file_is_missing(tmp_path: Path) -> None:
    _app, client, service = _client(tmp_path)
    project_id = _create_and_render(client, service)
    service.run_render(project_id, force=True)  # type: ignore[union-attr]
    project = service._project(project_id)  # type: ignore[union-attr]
    root = service.store.project_path(project)  # type: ignore[union-attr]

    entry = client.get(f"/api/projects/{project_id}/render-history").json()["renders"][0]
    (root / entry["filepath"]).unlink()

    listed = client.get(f"/api/projects/{project_id}/render-history").json()["renders"][0]
    assert listed["id"] == entry["id"]
    assert listed["available"] is False
    assert listed["url"] is None
    assert listed["size_bytes"] is None
