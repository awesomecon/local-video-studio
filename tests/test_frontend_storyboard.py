from pathlib import Path


STORYBOARD = (
    Path(__file__).resolve().parents[1] / "frontend" / "js" / "pages" / "storyboard.js"
)


def test_storyboard_guards_scene_visual_actions_against_double_clicks() -> None:
    source = STORYBOARD.read_text(encoding="utf-8")

    assert "const pendingVisualSceneIds = new Set();" in source
    assert source.count("pendingVisualSceneIds.has(scene.id)") >= 3
    assert source.count("pendingVisualSceneIds.add(scene.id)") == 2
    assert source.count("pendingVisualSceneIds.delete(scene.id)") == 2
    assert 'pendingLabel: "Generating…"' in source
    assert 'pendingLabel: "Regenerating…"' in source
    assert 'button.setAttribute("aria-busy", "true")' in source


API = Path(__file__).resolve().parents[1] / "frontend" / "js" / "api.js"


def test_storyboard_offers_cancel_all_while_jobs_are_active() -> None:
    """The header shows "Cancel all (N)" only while the project has active jobs.

    One call hits POST /api/projects/{id}/jobs/cancel-all, which cancels every
    active job (batch children cascade with their parent on the backend).
    """
    source = STORYBOARD.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    # The API wrapper exists and the page imports it.
    assert "export function cancelAllProjectJobs(config, projectId, opts = {})" in api
    assert "cancelAllProjectJobs," in source
    # Header button, hidden until the snapshot reports active jobs.
    assert "const cancelAllBtn = el(" in source
    assert "cancelAllBtn.hidden = count === 0;" in source
    assert "function activeJobs() {" in source
    assert 'cancelAllBtn.textContent = `Cancel all (${count})`;' in source
    # Refreshed on every snapshot load, next to the batch bar.
    assert "updateCancelAll();" in source
    # Confirm before the destructive call, then an honest reload.
    assert '"Cancel all jobs?"' in source
    assert "toast(" in source


def test_storyboard_batch_cancel_wording_matches_cascade() -> None:
    """Cancel batch tells the user every job the batch created will be canceled."""
    source = STORYBOARD.read_text(encoding="utf-8")
    assert "every job it created will be canceled" in source
    assert "every scene job it created" in source


def test_storyboard_offers_krea_and_ideogram_model_batches() -> None:
    source = STORYBOARD.read_text(encoding="utf-8")
    assert 'krea: "Krea 2 stills"' in source
    assert 'ideogram4_local: "Ideogram text images"' in source
    assert "function effectiveImageModel(scene)" in source
    assert "image_model: imageModel" in source
