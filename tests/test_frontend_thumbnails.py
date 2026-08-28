from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_thumbnail_studio_route_contract_and_safe_rendering() -> None:
    app = read("frontend/js/app.js")
    router = read("frontend/js/router.js")
    page = read("frontend/js/pages/thumbnails.js")
    api = read("frontend/js/api.js")
    assert 'hash: "#/thumbnails"' in app
    assert app.index('label: "Storyboard"') < app.index('label: "Thumbnails"') < app.index('label: "Export"')
    assert 'name: "thumbnails"' in router
    for field_name in (
        "proposed_title", "avoid_prompt", "subject_position", "text_placement",
        "font_preset", "layout_preset", "auto_derived_title", "source_candidate_id",
    ):
        assert field_name in page
    for endpoint in (
        "/thumbnails`,", "/thumbnails/plan", "/thumbnails/candidates",
        "/regenerate", "/select",
    ):
        assert endpoint in api
    assert "deleteThumbnailCandidate" in api
    assert 'method: "DELETE"' in api
    assert "innerHTML" not in page
    assert 'url.startsWith("/api/projects/")' in page
    assert "registerLiveUpdate(refreshCandidates)" in page
    assert "deleteThumbnailCandidate" in page
    assert "Delete" in page


def test_ideogram_model_keeps_exact_copy_and_save_controls_visible() -> None:
    page = read("frontend/js/pages/thumbnails.js")

    # Ideogram owns the lettering, so only Pillow-specific styling disappears.
    # The shared exact-copy fields and plan save action must remain mounted and
    # visible or the selected model can never be persisted before generation.
    assert 'avoidPromptHost.style.display = ideogram ? "none" : ""' in page
    assert "typPanelHost.style.display" not in page
    assert "typographyStyleHost.style.display" not in page
    assert 'panel("Artwork direction"' in page
    assert page.index('field({ label: "Exact title"') < page.index("typographyStyleHost,")
    assert page.index("typographyStyleHost,") < page.index('el("div", { class: "row mt" }, save, status)')


def test_export_surfaces_selected_thumbnail_download() -> None:
    export = read("frontend/js/pages/export.js")
    assert "getThumbnails" in export
    assert "candidate.selected" in export
    assert "Download thumbnail PNG" in export
