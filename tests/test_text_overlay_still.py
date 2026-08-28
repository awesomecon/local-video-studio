from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.core import load_config
from backend.models.lane_resolver import describe_lane_targets
from backend.pipeline import PipelineService
from backend.pipeline.service import PipelineError
from backend.schemas import ProjectCreate, Scene, VisualType, implicit_shot_from_scene


def service(tmp_path: Path) -> PipelineService:
    return PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )


def test_text_overlay_still_flattens_exact_unicode_over_mock_background(
    tmp_path: Path,
) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Exact composite", topic="test", target_duration=3,
        resolution=(360, 640), aspect_ratio="9:16",
    ))
    scene = Scene(
        project_id=project.id,
        index=0,
        duration=3,
        visual_type=VisualType.TEXT_OVERLAY_STILL,
        visual_prompt="A red planet in deep space.",
        needs_embedded_text=True,
        text_in_image="CAFÉ NOIR\nFULL VIDEO →",
        preferred_image_model="krea",
    )
    directory = pipeline.store.project_path(project) / "scenes" / "001"
    directory.mkdir(parents=True)

    result = pipeline._dispatch_text_overlay_still(project, scene, directory)

    assert result.metadata["settings"]["text_overlay_literals"] == [
        "CAFÉ NOIR", "FULL VIDEO →",
    ]
    assert result.metadata["settings"]["text_overlay_background_model"] == "krea"
    assert (directory / "generated-background.png").is_file()
    with Image.open(result.outputs[0]) as image:
        assert image.size == (360, 640)


def test_text_overlay_still_requires_authored_text(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Missing text", topic="test", target_duration=3,
    ))
    scene = Scene(
        project_id=project.id,
        index=0,
        duration=3,
        visual_type=VisualType.TEXT_OVERLAY_STILL,
        visual_prompt="Mars.",
    )
    with pytest.raises(PipelineError, match="requires at least one"):
        pipeline._dispatch_text_overlay_still(
            project, scene, pipeline.store.project_path(project) / "scenes" / "001",
        )


def test_text_overlay_still_is_a_wired_image_lane() -> None:
    scene = Scene(
        project_id="p", index=0, duration=3,
        visual_type=VisualType.TEXT_OVERLAY_STILL,
    )
    assert implicit_shot_from_scene(scene).lane.value == "image"
    assert describe_lane_targets()["image"]["text_overlay_still"]["available"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("THE PIONEER", "reveal"),
        ("A NEW SIGNAL...\nDECADES AGO?", "hook"),
        (
            "A WARNING ABOUT TECHNOLOGY\n"
            "AND THE COMPLEXITY OF THE HUMAN MIND",
            "quote",
        ),
        ("PROJECT HORIZON\nTHE DISCOVERY\nFULL VIDEO →", "cta"),
    ],
)
def test_text_overlay_layout_is_inferred_from_authored_regions(
    text: str, expected: str,
) -> None:
    scene = Scene(
        project_id="p", index=0, duration=3,
        visual_type=VisualType.TEXT_OVERLAY_STILL,
        text_in_image=text,
    )
    literals = PipelineService._text_overlay_literals(scene)
    assert PipelineService._text_overlay_layout(scene, literals) == expected


def test_text_overlay_layout_rejects_unknown_preset() -> None:
    scene = Scene(
        project_id="p", index=0, duration=3,
        visual_type=VisualType.TEXT_OVERLAY_STILL,
        text_in_image="TITLE",
        settings={"text_overlay_layout": "posterish"},
    )
    with pytest.raises(PipelineError, match="must be auto, hook, reveal, quote, or cta"):
        PipelineService._text_overlay_layout(scene, ["TITLE"])


def test_text_overlay_positions_keep_cta_inside_mobile_safe_area() -> None:
    assert PipelineService._text_overlay_positions(2, "cta") == [0.18, 0.74]
    assert PipelineService._text_overlay_positions(3, "cta") == [0.14, 0.46, 0.75]
    assert max(PipelineService._text_overlay_positions(3, "cta")) <= 0.75
