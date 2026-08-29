"""Portable, human-readable project directory persistence."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.editorial.models import EditPlan, EditPlanProvenance
from backend.schemas import (
    Project, ProjectPlan, Scene, Shot, ThumbnailPlan, ThumbnailSelection, VideoMode,
)

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or f"project-{uuid4().hex[:8]}"


class ProjectStore:
    DIRECTORY_NAMES = (
        "script", "references", "narration", "music", "scenes", "subtitles",
        "thumbnails", "renders", "variants/archive", "voices", "audio/qwen",
        "audio/step", "audio/chatterbox", "benchmark", "editorial/compositions",
    )

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()

    def project_path(self, project_or_slug: Project | str) -> Path:
        slug = project_or_slug.slug if isinstance(project_or_slug, Project) else project_or_slug
        if not _SLUG.fullmatch(slug):
            raise ValueError("invalid project slug")
        return self.root / slug

    def create_project(self, project: Project, scenes: list[Scene] | None = None) -> Path:
        directory = self.project_path(project)
        directory.mkdir(parents=True, exist_ok=False)
        try:
            for name in self.DIRECTORY_NAMES:
                (directory / name).mkdir(parents=True, exist_ok=True)
            self.save_project(project)
            for scene in scenes or []:
                self.save_scene(project.slug, scene)
        except Exception:
            # This directory was just created, so user content could not have preexisted.
            shutil.rmtree(directory)
            raise
        return directory

    def save_project(self, project: Project) -> Path:
        path = self.project_path(project) / "project.json"
        self._require_project_directory(path.parent)
        self._atomic_json(path, project.model_dump(mode="json"))
        return path

    def load_project(self, slug: str) -> Project:
        return Project.model_validate_json(
            (self.project_path(slug) / "project.json").read_text(encoding="utf-8")
        )

    def project_json_exists(self, slug: str) -> bool:
        return (self.project_path(slug) / "project.json").is_file()

    def list_project_slugs(self) -> list[str]:
        """Discover on-disk project directories (those holding project.json).

        Used by the recovery path so a SQLite entry and a portable directory
        can be reconciled without discarding either.
        """
        if not self.root.is_dir():
            return []
        slugs = []
        for entry in self.root.iterdir():
            if entry.is_dir() and _SLUG.fullmatch(entry.name) and self.project_json_exists(entry.name):
                slugs.append(entry.name)
        return slugs

    def delete_project(self, project_or_slug: Project | str) -> Path:
        """Permanently remove a project directory and everything inside it.

        Only a slug-validated directory directly under the store root can be
        removed, so an identifier can never widen into another path. The
        caller is responsible for the SQLite rows; this is the portable data.
        """
        path = self.project_path(project_or_slug)
        if not _SLUG.fullmatch(path.name) or path.parent != self.root:
            raise ValueError("invalid project slug")
        if not path.is_dir():
            raise FileNotFoundError(f"project directory does not exist: {path}")
        shutil.rmtree(path)
        return path

    def list_scenes(self, slug: str) -> list[Scene]:
        """Load every portable scene from ``scenes/<NNN>/scene.json``.

        Used by the recovery path to rebuild the SQLite scene index without
        touching or duplicating on-disk files. Missing or unreadable scene
        directories are skipped, not raised.
        """
        directory = self.project_path(slug) / "scenes"
        scenes: list[Scene] = []
        if not directory.is_dir():
            return scenes
        for entry in sorted(directory.iterdir()):
            match = re.fullmatch(r"(\d{3})", entry.name)
            if not match or not entry.is_dir():
                continue
            index = int(match.group(1)) - 1
            try:
                scenes.append(self.load_scene(slug, index))
            except Exception:
                continue
        return scenes

    def save_plan(self, slug: str, plan: ProjectPlan) -> Path:
        project = self.load_project(slug)
        if plan.project_id != project.id:
            raise ValueError("plan does not belong to this project")
        path = self.project_path(slug) / "plan.json"
        self._atomic_json(path, plan.model_dump(mode="json"))
        return path

    def load_plan(self, slug: str) -> ProjectPlan:
        return ProjectPlan.model_validate_json(
            (self.project_path(slug) / "plan.json").read_text(encoding="utf-8")
        )

    def save_edit_plan(self, slug: str, plan: EditPlan) -> Path:
        """Persist one validated, portable Editorial Mode plan atomically."""
        project = self.load_project(slug)
        if project.video_mode is not VideoMode.EDITORIAL:
            raise ValueError("edit plans can only be saved for Editorial Mode projects")
        if plan.project_id != project.id:
            raise ValueError("edit plan does not belong to this project")
        directory = self.project_path(slug) / "editorial"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "edit-plan.json"
        self._atomic_json(path, plan.model_dump(mode="json"))
        return path

    def load_edit_plan(self, slug: str) -> EditPlan:
        return EditPlan.model_validate_json(
            (self.project_path(slug) / "editorial" / "edit-plan.json").read_text(
                encoding="utf-8"
            )
        )

    def edit_plan_exists(self, slug: str) -> bool:
        return (self.project_path(slug) / "editorial" / "edit-plan.json").is_file()

    def save_edit_plan_provenance(
        self, slug: str, provenance: EditPlanProvenance,
    ) -> Path:
        project = self.load_project(slug)
        if provenance.project_id != project.id:
            raise ValueError("edit plan provenance does not belong to this project")
        path = self.project_path(slug) / "editorial" / "plan-provenance.json"
        self._atomic_json(path, provenance.model_dump(mode="json"))
        return path

    def load_edit_plan_provenance(self, slug: str) -> EditPlanProvenance:
        return EditPlanProvenance.model_validate_json(
            (self.project_path(slug) / "editorial" / "plan-provenance.json").read_text(
                encoding="utf-8"
            )
        )

    def save_thumbnail_plan(self, slug: str, plan: ThumbnailPlan) -> Path:
        project = self.load_project(slug)
        if plan.project_id != project.id:
            raise ValueError("thumbnail plan does not belong to this project")
        path = self.project_path(slug) / "thumbnails" / "thumbnail-plan.json"
        self._atomic_json(path, plan.model_dump(mode="json"))
        return path

    def load_thumbnail_plan(self, slug: str) -> ThumbnailPlan:
        return ThumbnailPlan.model_validate_json(
            (self.project_path(slug) / "thumbnails" / "thumbnail-plan.json").read_text(
                encoding="utf-8"
            )
        )

    def save_thumbnail_selection(self, slug: str, selection: ThumbnailSelection) -> Path:
        project = self.load_project(slug)
        if selection.project_id != project.id:
            raise ValueError("thumbnail selection does not belong to this project")
        path = self.project_path(slug) / "thumbnails" / "selected.json"
        self._atomic_json(path, selection.model_dump(mode="json"))
        return path

    def load_thumbnail_selection(self, slug: str) -> ThumbnailSelection | None:
        path = self.project_path(slug) / "thumbnails" / "selected.json"
        if not path.is_file():
            return None
        return ThumbnailSelection.model_validate_json(path.read_text(encoding="utf-8"))

    def save_scene(self, slug: str, scene: Scene) -> Path:
        project = self.load_project(slug)
        if scene.project_id != project.id:
            raise ValueError("scene does not belong to this project")
        directory = self.project_path(slug) / "scenes" / f"{scene.index + 1:03d}"
        directory.mkdir(parents=True, exist_ok=True)
        self._atomic_json(directory / "scene.json", scene.model_dump(mode="json"))
        prompt = {
            "visual_prompt": scene.visual_prompt,
            "negative_prompt": scene.negative_prompt,
            "seed": scene.seed,
            "backend": scene.selected_backend,
            "visual_type": scene.visual_type.value,
            "settings": scene.settings,
        }
        self._atomic_json(directory / "prompt.json", prompt)
        return directory

    def load_scene(self, slug: str, index: int) -> Scene:
        path = self.project_path(slug) / "scenes" / f"{index + 1:03d}" / "scene.json"
        return Scene.model_validate_json(path.read_text(encoding="utf-8"))

    def shot_directory(self, slug: str, scene_index: int) -> Path:
        return self.project_path(slug) / "scenes" / f"{scene_index + 1:03d}" / "shots"

    def save_shot(self, slug: str, scene_index: int, shot: Shot) -> Path:
        """Persist one shot as ``scenes/<NNN>/shots/<MMM>/shot.json``.

        The directory number derives from the shot index; identity is the id
        inside the file. Call :meth:`sync_scene_shots` after reorders or
        removals so no stale numbered directories remain.
        """
        directory = self.shot_directory(slug, scene_index) / f"{shot.index + 1:03d}"
        directory.mkdir(parents=True, exist_ok=True)
        self._atomic_json(directory / "shot.json", shot.model_dump(mode="json"))
        return directory

    def load_shot(self, slug: str, scene_index: int, shot_index: int) -> Shot:
        path = (
            self.shot_directory(slug, scene_index) / f"{shot_index + 1:03d}" / "shot.json"
        )
        return Shot.model_validate_json(path.read_text(encoding="utf-8"))

    def load_shots(self, slug: str, scene_index: int) -> list[Shot]:
        """Load every stored shot for a scene, ordered by index.

        Missing or unreadable shot files are skipped rather than raised so one
        damaged file cannot blank an otherwise valid scene.
        """
        directory = self.shot_directory(slug, scene_index)
        shots: list[Shot] = []
        if not directory.is_dir():
            return shots
        for entry in sorted(directory.iterdir()):
            match = re.fullmatch(r"(\d{3})", entry.name)
            if not match or not entry.is_dir():
                continue
            try:
                shots.append(self.load_shot(slug, scene_index, int(match.group(1)) - 1))
            except Exception:
                continue
        return sorted(shots, key=lambda shot: shot.index)

    def sync_scene_shots(self, slug: str, scene_index: int, shots: Sequence[Shot]) -> None:
        """Write every shot and remove numbered directories that no longer map.

        Reordering or deleting shots changes their directory numbers; this
        keeps the portable tree exactly consistent with the ordered list.
        """
        directory = self.shot_directory(slug, scene_index)
        live = {f"{shot.index + 1:03d}" for shot in shots}
        if directory.is_dir():
            for entry in directory.iterdir():
                if entry.is_dir() and re.fullmatch(r"\d{3}", entry.name) \
                        and entry.name not in live:
                    shutil.rmtree(entry)
        for shot in sorted(shots, key=lambda item: item.index):
            self.save_shot(slug, scene_index, shot)

    def delete_scene_shots(self, slug: str, scene_index: int) -> None:
        """Remove every shot directory of one scene (scene delete cleanup)."""
        directory = self.shot_directory(slug, scene_index)
        if directory.is_dir():
            shutil.rmtree(directory)

    def archive_variant(self, slug: str, relative_path: str | Path) -> Path:
        project_dir = self.project_path(slug).resolve()
        source = (project_dir / relative_path).resolve()
        if project_dir not in source.parents or not source.is_file():
            raise ValueError("variant must be an existing file inside the project")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        destination = project_dir / "variants" / "archive" / (
            f"{source.stem}-{stamp}-{uuid4().hex[:8]}{source.suffix}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        return Path(shutil.move(str(source), str(destination)))

    def copy_to_archive(self, slug: str, relative_path: str | Path) -> Path:
        """Preserve a variant in history without removing the live publication."""
        project_dir = self.project_path(slug).resolve()
        source = (project_dir / relative_path).resolve()
        if project_dir not in source.parents or not source.is_file():
            raise ValueError("variant must be an existing file inside the project")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        destination = project_dir / "variants" / "archive" / (
            f"{source.stem}-{stamp}-{uuid4().hex[:8]}{source.suffix}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    @staticmethod
    def _require_project_directory(directory: Path) -> None:
        if not directory.is_dir():
            raise FileNotFoundError(f"project directory does not exist: {directory}")

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
