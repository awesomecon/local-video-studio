"""Foreign path syntax tests plus real filesystem checks on the current OS.

PureWindowsPath exercises interpretation only; these are not native Windows
filesystem tests. No existing projects or external services are used.
"""

import json
import shutil
from pathlib import Path, PurePath, PureWindowsPath

import pytest
from pydantic import ValidationError

from backend.schemas import Asset, AssetType, Project, ThumbnailSelection
from backend.schemas.paths import portable_relative_path, resolve_project_path
from backend.storage import ProjectStore, StudioDatabase, slugify
from backend.storage.projects import MAX_PROJECT_SLUG_LENGTH


def make_project(slug: str = "portable") -> Project:
    return Project(title="Portable", topic="Storage", target_duration=10, slug=slug)


def make_asset(project_id: str, filepath: str | PurePath = "scenes/001/frame.png") -> Asset:
    return Asset(project_id=project_id, type=AssetType.IMAGE, filepath=filepath,
                 backend="mock", model="mock", seed=1)


@pytest.mark.parametrize("value", [
    "/tmp/frame.png", r"C:\media\frame.png", "C:/media/frame.png", "C:frame.png",
    r"\media\frame.png", r"\\server\share\frame.png", "//server/share/frame.png",
    r"\\?\C:\media\frame.png", r"\\.\NUL", "../frame.png", r"..\frame.png",
    r"scenes/..\frame.png", r"scenes\../frame.png", "scenes/../../frame.png",
    "", ".", "./", "scenes/frame.png:stream", "scenes/\x00frame.png",
])
def test_foreign_and_escaping_paths_are_rejected_on_every_host(value: str) -> None:
    with pytest.raises(ValueError):
        portable_relative_path(value)
    with pytest.raises(ValidationError):
        make_asset("project", value)


@pytest.mark.parametrize("value", [
    "scenes/001/frame.png", r"scenes\001\frame.png", r"scenes\001/frame.png",
    PureWindowsPath(r"scenes\001\frame.png"), Path("scenes/001/frame.png"),
    "./scenes//001/frame.png",
])
def test_relative_path_spelling_is_canonical(value: str | PurePath) -> None:
    assert portable_relative_path(value) == "scenes/001/frame.png"
    asset = make_asset("project", value)
    assert asset.filepath == Path("scenes/001/frame.png")
    assert asset.model_dump(mode="json")["filepath"] == "scenes/001/frame.png"
    assert json.loads(asset.model_dump_json())["filepath"] == "scenes/001/frame.png"


def test_asset_assignment_and_thumbnail_selection_normalize_paths() -> None:
    asset = make_asset("project")
    asset.filepath = r"renders\final.mp4"
    assert asset.filepath == Path("renders/final.mp4")
    selection = ThumbnailSelection(project_id="project", candidate_id="candidate-01",
                                   composite_path=r"thumbnails\selected.png",
                                   composite_hash="hash")
    assert selection.composite_path == "thumbnails/selected.png"
    with pytest.raises(ValidationError):
        selection.composite_path = r"C:\outside.png"


def test_native_resolution_and_archive_accept_foreign_relative_separators(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = make_project()
    root = store.create_project(project)
    source = root / "scenes" / "frame.png"
    source.write_bytes(b"frame")
    assert resolve_project_path(root, r"scenes\frame.png") == source.resolve()
    copied = store.copy_to_archive(project.slug, r"scenes\frame.png")
    assert copied.read_bytes() == b"frame"
    assert source.is_file()
    archived = store.archive_variant(project.slug, r"scenes\frame.png")
    assert archived.read_bytes() == b"frame"
    assert not source.exists()


def test_copied_project_resolves_media_under_its_new_root(tmp_path: Path) -> None:
    original_store = ProjectStore(tmp_path / "original")
    project = make_project()
    original_root = original_store.create_project(project)
    asset = make_asset(project.id, r"scenes\001\frame.png")
    media = resolve_project_path(original_root, asset.filepath)
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"portable frame")
    (original_root / "assets.json").write_text(asset.model_dump_json(), encoding="utf-8")
    relocated_store = ProjectStore(tmp_path / "copied")
    relocated_root = relocated_store.project_path(project)
    shutil.copytree(original_root, relocated_root)
    shutil.rmtree(original_root)
    loaded_project = relocated_store.load_project(project.slug)
    loaded_asset = Asset.model_validate_json((relocated_root / "assets.json").read_text())
    assert loaded_project.id == project.id
    assert loaded_project.slug == project.slug
    assert resolve_project_path(relocated_root, loaded_asset.filepath).read_bytes() == b"portable frame"


def test_symlink_source_and_output_cannot_escape_project(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = make_project()
    root = store.create_project(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "frame.png").write_bytes(b"outside")
    try:
        (root / "references" / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable on this filesystem: {exc}")
    with pytest.raises(ValueError, match="inside the project"):
        resolve_project_path(root, r"references\linked\frame.png")
    with pytest.raises(ValueError, match="inside the project"):
        store.archive_variant(project.slug, "references/linked/frame.png")
    archive = root / "variants" / "archive"
    archive.rmdir()
    archive.symlink_to(outside, target_is_directory=True)
    source = root / "scenes" / "frame.png"
    source.write_bytes(b"inside")
    with pytest.raises(ValueError, match="inside the project"):
        store.copy_to_archive(project.slug, "scenes/frame.png")
    assert (outside / "frame.png").read_bytes() == b"outside"
    assert source.read_bytes() == b"inside"


@pytest.mark.parametrize("title", ["CON", "Prn", "aux", "NUL", "COM1", "COM9", "LPT1", "LPT9"])
def test_new_slugs_avoid_windows_devices(title: str) -> None:
    assert slugify(title) == f"project-{title.lower()}"


def test_slug_allocation_handles_length_and_casefolded_collisions(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    (tmp_path / "PORTABLE").mkdir()
    (tmp_path / "portable-2").write_bytes(b"existing file")
    assert store.available_slug("Portable", ["portable-3"]) == "portable-4"
    title = "Long title " * 100
    base = slugify(title)
    assert len(base) <= MAX_PROJECT_SLUG_LENGTH
    (tmp_path / base).mkdir()
    next_slug = store.available_slug(title)
    assert next_slug.endswith("-2")
    assert len(next_slug) <= MAX_PROJECT_SLUG_LENGTH
    assert store.available_slug("portable") == "portable-3"


def test_legacy_project_identity_and_opaque_settings_are_not_rewritten(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    # Legacy slugs are deliberately not passed through new-name generation.
    project = make_project("legacy-" + "x" * 90)
    project.settings["legacy_path"] = r"C:\old-machine\unknown.png"
    directory = tmp_path / project.slug
    directory.mkdir()
    path = directory / "project.json"
    original = project.model_dump_json(indent=4)
    path.write_text(original, encoding="utf-8")
    loaded = store.load_project(project.slug)
    assert loaded == project
    assert store.project_path(loaded) == directory
    assert path.read_text(encoding="utf-8") == original
    assert store.available_slug(project.slug) != project.slug


def make_database(tmp_path: Path) -> tuple[StudioDatabase, Project]:
    database = StudioDatabase(tmp_path / "studio.sqlite3")
    database.initialize()
    project = database.create_project(make_project())
    return database, project


def test_database_writes_canonical_paths_even_after_unvalidated_copy(tmp_path: Path) -> None:
    database, project = make_database(tmp_path)
    asset = make_asset(project.id).model_copy(update={"filepath": Path(r"scenes\001\frame.png")})
    saved = database.save_asset(asset)
    with database.connection() as connection:
        row = connection.execute("SELECT filepath, payload_json FROM assets").fetchone()
    assert row["filepath"] == "scenes/001/frame.png"
    assert json.loads(row["payload_json"])["filepath"] == row["filepath"]
    assert saved.filepath == Path("scenes/001/frame.png")
    database.record_render_metadata(project.id, r"renders\final.mp4", {}, project.created_at)
    with database.connection() as connection:
        assert connection.execute("SELECT filepath FROM render_metadata").fetchone()[0] == "renders/final.mp4"
    unsafe = asset.model_copy(update={"filepath": Path(r"C:\outside.png")})
    with pytest.raises(ValueError):
        database.save_asset(unsafe)


@pytest.mark.parametrize("legacy_path", [r"scenes\001\frame.png", r"C:\old\frame.png"])
def test_legacy_asset_reads_never_rewrite_sqlite_payloads(tmp_path: Path, legacy_path: str) -> None:
    database, project = make_database(tmp_path)
    asset = database.save_asset(make_asset(project.id))
    payload = asset.model_dump(mode="json")
    payload["filepath"] = legacy_path
    original = json.dumps(payload, indent=4)
    with database.connection() as connection:
        connection.execute("UPDATE assets SET filepath=?, payload_json=? WHERE id=?",
                           (legacy_path, original, asset.id))
    if legacy_path.startswith("C:"):
        with pytest.raises(ValidationError):
            database.get_asset(asset.id)
    else:
        assert database.get_asset(asset.id).filepath == Path("scenes/001/frame.png")
    with database.connection() as connection:
        row = connection.execute("SELECT filepath, payload_json FROM assets").fetchone()
    assert row["filepath"] == legacy_path
    assert row["payload_json"] == original


def test_path_prefix_delete_matches_legacy_separators_without_deleting_siblings(tmp_path: Path) -> None:
    database, project = make_database(tmp_path)
    asset = database.save_asset(make_asset(project.id, "narration/takes/first.wav"))
    sibling = database.save_asset(make_asset(project.id, "narration/takes-extra/keep.wav"))
    with database.connection() as connection:
        connection.execute("UPDATE assets SET filepath=? WHERE id=?",
                           (r"narration\takes\first.wav", asset.id))
    assert database.delete_assets_for_path(project.id, "narration/takes/") == 1
    assert database.get_asset(sibling.id) is not None
