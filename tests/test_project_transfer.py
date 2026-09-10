"""Native project-transfer acceptance for portable paths and filenames.

Everything here runs against the real filesystem of the current runner, so the
native CI matrix executes the transfer on each OS with true separators, case
rules, and reserved-name behavior. The PureWindowsPath unit coverage in
``tests/test_portable_storage.py`` complements but does not replace this file:
only a real copy across roots proves a project reopens, recovers, and renders.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.core import load_config
from backend.pipeline import PipelineService
from backend.rendering.binaries import discover_binaries
from backend.rendering.commands import RenderOptions
from backend.rendering.mock_media import create_placeholder_video
from backend.rendering.renderer import FFmpegRenderer
from backend.schemas import Asset, AssetType, Project, ProjectCreate, Scene
from backend.schemas.paths import resolve_asset_path, safe_portable_filename
from backend.timeline.models import SubtitleCue, Timeline, TimelineClip


def _service(base: Path, name: str) -> PipelineService:
    return PipelineService(
        load_config(environ={}),
        database_path=base / name / "studio.sqlite3",
        project_root=base / name / "projects",
        temp_root=base / name / "tmp",
        mock_mode=True,
    )


def _build_source_tree(base: Path) -> tuple[PipelineService, Project, Path]:
    """Create a realistic project directory as the 'first machine' would."""
    service = _service(base, "machine-a")
    project = service.create_project(ProjectCreate(
        title="Transfer Me", topic="ports", target_duration=2,
    ))
    root = service.store.project_path(project)
    scene = Scene(project_id=project.id, index=0, title="One", duration=2.0,
                  narration="hello world")
    service.store.save_scene(project.slug, scene)
    service.database.save_scene(scene)
    binaries = discover_binaries()
    clip = create_placeholder_video(root / "scenes" / "001" / "clip.mp4",
                                    duration_seconds=0.5, width=160, height=90,
                                    binaries=binaries)
    assert clip.is_file()
    asset = Asset(project_id=project.id, scene_id=scene.id, type=AssetType.VIDEO,
                  filepath="scenes/001/clip.mp4", backend="mock", model="mock", seed=7)
    service.database.save_asset(asset)
    candidate = root / "thumbnails" / "candidate-01"
    candidate.mkdir(parents=True)
    (candidate / "artwork.png").write_bytes(b"artwork")
    (candidate / "composite.png").write_bytes(b"composite")
    (candidate / "manifest.json").write_text(json.dumps({
        # Provenance is free-form; Windows-authored spellings may appear here
        # without affecting resolution, which uses the canonical fields above.
        "output_hashes": {"artwork": "a" * 64, "composite": "b" * 64},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stale": False,
        "origin": {"workstation": "MACHINE-A", "path": "D:\\studio\\candidate-01"},
    }), encoding="utf-8")
    # timeline.json is an opaque human-readable artifact: whatever spelling a
    # previous host wrote must survive a move byte-identical.
    (root / "timeline.json").write_text(json.dumps({
        "clips": [{"scene_id": scene.id, "path": "scenes\\001\\clip.mp4"}],
    }, indent=2), encoding="utf-8")
    return service, project, root


def _move_to_machine_b(tmp_path: Path, project: Project, root: Path) -> tuple[PipelineService, Project, Path]:
    relocated = tmp_path / "machine-b" / "projects" / project.slug
    relocated.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root, relocated)
    shutil.rmtree(root)
    service = _service(tmp_path, "machine-b")
    return service, project, relocated


def test_transferred_project_recovers_renders_and_resolves(tmp_path: Path) -> None:
    _, project, root = _build_source_tree(tmp_path)
    project_bytes = (root / "project.json").read_bytes()
    timeline_bytes = (root / "timeline.json").read_text(encoding="utf-8")
    service, project, relocated = _move_to_machine_b(tmp_path, project, root)

    projects, recovery = service.list_projects()
    assert [item.id for item in projects] == [project.id]
    assert project.slug == "transfer-me"
    recovered = [item for item in recovery if item["type"] == "recovered"]
    assert any(item["slug"] == project.slug and item["project_id"] == project.id
               for item in recovered)
    # Scenes come back from the portable scene.json files, not the old index.
    assert [scene.id for scene in service.database.list_scenes(project.id)] != []
    # Nothing was rewritten by the move or the recovery.
    assert (relocated / "project.json").read_bytes() == project_bytes
    assert (relocated / "timeline.json").read_text(encoding="utf-8") == timeline_bytes
    # Windows-authored spellings in newly parsed references resolve canonically.
    foreign = Asset(project_id=project.id, type=AssetType.VIDEO,
                    filepath="scenes\\001\\clip.mp4", backend="mock", model="mock", seed=7)
    assert foreign.model_dump(mode="json")["filepath"] == "scenes/001/clip.mp4"
    media = resolve_asset_path(relocated, foreign.filepath)
    assert media.is_file() and media.stat().st_size > 0

    binaries = discover_binaries()
    timeline = Timeline(
        clips=[TimelineClip("scene-1", media, 0, 0.5)], width=160, height=90,
        subtitles=[SubtitleCue(0, 0.4, "Transferred captions")],
    )
    renderer = FFmpegRenderer(binaries, temp_root=relocated)
    output = relocated / "renders" / "transferred.mp4"
    info = renderer.render(timeline, output, RenderOptions(burn_subtitles=True))
    assert output.is_file() and info.duration_seconds > 0


def test_unportable_stage_state_fails_loudly_without_rewriting(tmp_path: Path) -> None:
    service = _service(tmp_path, "machine-a")
    project = service.create_project(ProjectCreate(
        title="Broken Move", topic="ports", target_duration=1,
    ))
    state_path = service.store.project_path(project) / "stage-state.json"
    payload = {"version": 1, "stages": {"timeline": {
        "status": "completed", "job_id": "job",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        # Legacy absolute spelling from another machine: ambiguous, so report
        # instead of guessing a rebase or silently treating it as complete.
        "outputs": ["D:\\studio\\timeline.json"],
    }}}
    original = json.dumps(payload, indent=2)
    state_path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="project-relative|resolve stored media"):
        service._stage_complete(project, "timeline")
    assert state_path.read_text(encoding="utf-8") == original


def test_unportable_takes_reference_falls_back_without_escaping(tmp_path: Path) -> None:
    service = _service(tmp_path, "machine-a")
    project = service.create_project(ProjectCreate(
        title="Takes Move", topic="ports", target_duration=1,
    ))
    root = service.store.project_path(project)
    (root / "narration").mkdir(parents=True, exist_ok=True)
    (root / "narration" / "master.wav").write_bytes(b"RIFF-fake")
    (root / "narration" / "takes.json").write_text(json.dumps({
        "active_file": "\\\\server\\share\\take.wav",
    }), encoding="utf-8")
    assert service._narration_scene_bounds(project) is None


def test_generated_archive_names_survive_windows_rules(tmp_path: Path) -> None:
    service = _service(tmp_path, "machine-a")
    project = service.create_project(ProjectCreate(
        title="Names", topic="ports", target_duration=1,
    ))
    root = service.store.project_path(project)
    hostile = ["CON.png", "trailing-dot..png", "trailing-space .png", f"{'n' * 200}.png", "odd.pn<g"]
    expected_stems = ["_CON", "trailing-dot", "trailing-space", "n" * 100, "odd"]
    expected_suffixes = [".png", ".png", ".png", ".png", ".pn_g"]
    for name, expected, suffix in zip(hostile, expected_stems, expected_suffixes):
        source = root / "scenes" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"data")
        archived = service.store.archive_variant(project.slug, f"scenes/{name}")
        assert not source.exists()
        assert archived.is_file() and archived.read_bytes() == b"data"
        # The full history name carries a timestamp/hash suffix, so stability
        # is checked per segment: the sanitized stem plus safe generated parts.
        assert archived.name.startswith(expected + "-")
        assert all(
            part == safe_portable_filename(part)
            for part in archived.name.replace(".", "-").split("-")
            if part
        )
        assert not archived.name.endswith((".", " "))
        assert archived.suffix == suffix
        assert len(archived.name) <= 160


@pytest.mark.parametrize("name, expected", [
    ("clip.mp4", "clip.mp4"),
    ("trailing. ", "trailing"),
    ("dots....", "dots"),
    ("CON.png", "_CON.png"),
    ("con .png", "_con .png"),
    ("com1", "_com1"),
    ("a<b>|c?.png", "a_b__c_.png"),
    ("ok name.png", "ok name.png"),
])
def test_safe_portable_filename_cases(name: str, expected: str) -> None:
    assert safe_portable_filename(name) == expected
    assert len(safe_portable_filename("n" * 500 + ".png")) <= 100
    wide = safe_portable_filename("é" * 60 + ".png")
    assert len(wide.encode("utf-8")) <= 100 and len(wide) > 0
    assert safe_portable_filename("conXYZ", max_length=3) == "_con"


def test_timeline_relative_path_resolves_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real" / "proj"
    (real / "scenes").mkdir(parents=True)
    media = real / "scenes" / "a.mp4"
    media.write_bytes(b"0")
    link = tmp_path / "link" / "proj"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real, target_is_directory=True)
    # The stored spelling is resolved while the root still carries the link:
    # without resolving both sides this looks like an outside-project path.
    assert PipelineService._timeline_relative_path(
        link, str(media.resolve()), scope="clip",
    ) == "scenes/a.mp4"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "a:b", "x\x00y"])
def test_safe_portable_filename_rejects(name: str) -> None:
    with pytest.raises(ValueError):
        safe_portable_filename(name)
