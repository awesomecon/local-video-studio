"""Lane/backend capability resolver coverage (mock backends only).

Verifies that resolution succeeds only for wired (lane, visual_type) pairs,
honors ``shot.selected_backend`` after validating declared capabilities, and
raises structured errors BEFORE any job would be queued — never falling
through to mock generation in real mode.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from backend.models.base import (
    BackendDescriptor,
    Capability,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from backend.models.lane_resolver import (
    LaneErrorCode,
    LaneResolutionError,
    describe_lane_targets,
    resolve_lane_target,
)
from backend.models.mock import MockGeneratorBackend
from backend.schemas.models import VisualType
from backend.schemas.shots import Shot, ShotLane


def make_shot(**overrides: Any) -> Shot:
    fields: dict[str, Any] = {
        "project_id": "proj-1",
        "scene_id": "scene-1",
        "index": 0,
        "duration_seconds": 5.0,
        "lane": ShotLane.IMAGE,
        "visual_type": VisualType.KREA2_STILL,
    }
    fields.update(overrides)
    return Shot(**fields)


class StubBackend(GeneratorBackend):
    """Configurable fake for capability/readiness checks (no network)."""

    def __init__(
        self,
        name: str,
        capabilities: frozenset[Capability],
        *,
        health_status: str = "healthy",
        heavyweight: bool = False,
    ) -> None:
        self._name = name
        self._capabilities = capabilities
        self._health_status = health_status
        self._heavyweight = heavyweight

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name=self._name,
            model_name="stub",
            capabilities=self._capabilities,
            heavyweight=self._heavyweight,
        )

    def health(self) -> Mapping[str, Any]:
        return {
            "status": self._health_status,
            "backend": self._name,
            **({} if self._health_status == "healthy" else {"install_guidance": "start it"}),
        }

    def load(self) -> None:
        raise AssertionError("stub backends are never loaded in these tests")

    def unload(self) -> None:
        raise AssertionError("stub backends are never unloaded in these tests")

    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise AssertionError("stub backends never generate")

    def cancel(self, job_id: str) -> bool:
        return False

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        return {}


class RecordingQueue:
    """Stands in for the job queue; anything enqueued fails the test."""

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, shot_id: str) -> str:
        self.enqueued.append(shot_id)
        raise AssertionError(f"a job was queued despite failed resolution: {shot_id}")


@pytest.fixture()
def queue() -> RecordingQueue:
    return RecordingQueue()


def real_registry() -> dict[str, GeneratorBackend]:
    return {
        "krea2_comfyui": StubBackend(
            "krea2_comfyui", frozenset({Capability.TEXT_TO_IMAGE}),
        ),
        "qwen_image_2512_comfyui": StubBackend(
            "qwen_image_2512_comfyui", frozenset({Capability.TEXT_TO_IMAGE}),
        ),
        "ideogram4_local_comfyui": StubBackend(
            "ideogram4_local_comfyui", frozenset({Capability.TEXT_TO_IMAGE}),
        ),
        "comfyui": StubBackend(
            "comfyui",
            frozenset({
                Capability.TEXT_TO_VIDEO,
                Capability.IMAGE_TO_VIDEO,
                Capability.REFERENCE_TO_VIDEO,
            }),
        ),
    }


def test_real_mode_resolves_every_wired_visual_type(queue: RecordingQueue) -> None:
    registry = real_registry()
    expectations = {
        (ShotLane.IMAGE, VisualType.KREA2_STILL): "krea2_comfyui",
        (ShotLane.IMAGE, VisualType.IDEOGRAM4_STILL): "ideogram4_local_comfyui",
        (ShotLane.IMAGE, VisualType.QWEN_IMAGE_STILL): "qwen_image_2512_comfyui",
        (ShotLane.IMAGE, VisualType.IMAGE_MOTION): "krea2_comfyui",
        (ShotLane.H3, VisualType.H3_AUDIOVISUAL): "comfyui",
        (ShotLane.H3, VisualType.H3_REFERENCE): "comfyui",
    }
    for (lane, visual_type), expected_backend in expectations.items():
        shot = make_shot(lane=lane, visual_type=visual_type)
        target = resolve_lane_target(
            shot, registry, check_readiness=False,
        )
        assert target.kind == "backend"
        assert target.backend_name == expected_backend
        assert target.capability is not None


def test_executable_deterministic_handlers_resolve_without_backends(
    queue: RecordingQueue,
) -> None:
    target = resolve_lane_target(
        make_shot(lane=ShotLane.HTML, visual_type=VisualType.TITLE_CARD),
        real_registry(),
        check_readiness=False,
    )
    assert target.kind == "deterministic"
    assert target.handler == "graphic_screen"
    assert target.backend is None


def test_real_imported_media_resolves_as_a_local_deterministic_handler(
    queue: RecordingQueue,
) -> None:
    shot = make_shot(lane=ShotLane.REAL, visual_type=VisualType.REUSED_MEDIA)
    target = resolve_lane_target(shot, real_registry(), check_readiness=False)
    assert target.kind == "deterministic"
    assert target.handler == "imported_media"
    assert queue.enqueued == []


def test_unbuilt_backend_targets_are_not_advertised(queue: RecordingQueue) -> None:
    # FLUX and WAN have registered adapters but no shot-scope request
    # builders, so their visual types must fail resolution instead of
    # pretending a dispatchable target exists.
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(
            make_shot(
                lane=ShotLane.IMAGE, visual_type=VisualType.FLUX_STILL,
            ),
            real_registry(),
            check_readiness=False,
        )
    assert excinfo.value.code == LaneErrorCode.LANE_VISUAL_TYPE_MISMATCH.value

    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(
            make_shot(
                lane=ShotLane.H3, visual_type=VisualType.WAN_VIDEO,
            ),
            real_registry(),
            check_readiness=False,
        )
    assert excinfo.value.code == LaneErrorCode.LANE_VISUAL_TYPE_MISMATCH.value
    assert queue.enqueued == []


def test_mock_mode_resolves_everything_to_mock_backend(queue: RecordingQueue) -> None:
    registry = {"mock": MockGeneratorBackend()}
    for lane, visual_type in [
        (ShotLane.HTML, VisualType.GRAPHIC_SCREEN),
        (ShotLane.REAL, VisualType.REUSED_MEDIA),
        (ShotLane.H3, VisualType.H3_AUDIOVISUAL),
        (ShotLane.IMAGE, VisualType.QWEN_IMAGE_STILL),
    ]:
        target = resolve_lane_target(
            make_shot(lane=lane, visual_type=visual_type),
            registry,
            mock_mode=True,
        )
        assert target.backend_name == "mock"


def test_lane_visual_mismatch_fails_before_queueing(queue: RecordingQueue) -> None:
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(
            make_shot(lane=ShotLane.REAL, visual_type=VisualType.KREA2_STILL),
            real_registry(),
            check_readiness=False,
        )
    assert excinfo.value.code == LaneErrorCode.LANE_VISUAL_TYPE_MISMATCH.value
    assert queue.enqueued == []


def test_unwired_visual_types_fail_instead_of_falling_through_to_mock(
    queue: RecordingQueue,
) -> None:
    registry: dict[str, GeneratorBackend] = {"mock": MockGeneratorBackend()}
    for lane, visual_type in [
        (ShotLane.REAL, VisualType.TRANSITION_ONLY),
        (ShotLane.IMAGE, VisualType.CUSTOM),
        (ShotLane.HTML, VisualType.FLUX_STILL),
    ]:
        with pytest.raises(LaneResolutionError) as excinfo:
            resolve_lane_target(
                make_shot(lane=lane, visual_type=visual_type),
                registry,
                check_readiness=False,
            )
        assert excinfo.value.code == LaneErrorCode.LANE_VISUAL_TYPE_MISMATCH.value
        # The failure names the supported alternatives instead of silently
        # producing a placeholder asset through the mock backend.
        assert "supported" in str(excinfo.value)
    assert queue.enqueued == []


def test_selected_backend_mismatch_is_structured(queue: RecordingQueue) -> None:
    shot = make_shot(
        visual_type=VisualType.QWEN_IMAGE_STILL,
        selected_backend="krea2_comfyui",
    )
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(shot, real_registry(), check_readiness=False)
    assert excinfo.value.code == LaneErrorCode.CAPABILITY_MISMATCH.value
    assert excinfo.value.details["required_backend"] == "qwen_image_2512_comfyui"
    assert queue.enqueued == []


def test_unknown_selected_backend_cannot_implement_the_visual_type(
    queue: RecordingQueue,
) -> None:
    shot = make_shot(selected_backend="does_not_exist")
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(shot, real_registry(), check_readiness=False)
    # A wired visual type has exactly one designated backend; anything else is
    # a capability mismatch, not a registry miss.
    assert excinfo.value.code == LaneErrorCode.CAPABILITY_MISMATCH.value
    assert queue.enqueued == []


def test_selected_backend_honored_when_it_matches(queue: RecordingQueue) -> None:
    shot = make_shot(selected_backend="krea2_comfyui")
    target = resolve_lane_target(shot, real_registry(), check_readiness=False)
    assert target.backend_name == "krea2_comfyui"


def test_declared_capabilities_are_validated(queue: RecordingQueue) -> None:
    registry = {
        "krea2_comfyui": StubBackend(
            "krea2_comfyui", frozenset({Capability.TEXT_TO_SPEECH}),
        ),
    }
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(
            make_shot(), registry, check_readiness=False,
        )
    assert excinfo.value.code == LaneErrorCode.CAPABILITY_MISMATCH.value
    assert "text_to_image" in str(excinfo.value)
    assert queue.enqueued == []


def test_missing_designated_backend_reports_not_found(queue: RecordingQueue) -> None:
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(make_shot(), {}, check_readiness=False)
    assert excinfo.value.code == LaneErrorCode.BACKEND_NOT_FOUND.value
    assert queue.enqueued == []


def test_unhealthy_backend_fails_readiness(queue: RecordingQueue) -> None:
    registry = {
        "krea2_comfyui": StubBackend(
            "krea2_comfyui",
            frozenset({Capability.TEXT_TO_IMAGE}),
            health_status="unhealthy",
        ),
    }
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(make_shot(), registry, check_readiness=True)
    assert excinfo.value.code == LaneErrorCode.BACKEND_NOT_READY.value
    assert excinfo.value.retryable
    assert queue.enqueued == []


def test_health_check_exception_becomes_retryable_readiness_error(
    queue: RecordingQueue,
) -> None:
    class FailingHealth(StubBackend):
        def health(self) -> Mapping[str, Any]:
            raise RuntimeError("connection refused")

    registry = {
        "krea2_comfyui": FailingHealth(
            "krea2_comfyui", frozenset({Capability.TEXT_TO_IMAGE}),
        ),
    }
    with pytest.raises(LaneResolutionError) as excinfo:
        resolve_lane_target(make_shot(), registry, check_readiness=True)
    assert excinfo.value.code == LaneErrorCode.BACKEND_NOT_READY.value
    assert excinfo.value.retryable


def test_gpu_flag_follows_descriptor_heaviness() -> None:
    registry = {
        "krea2_comfyui": StubBackend(
            "krea2_comfyui",
            frozenset({Capability.TEXT_TO_IMAGE}),
            heavyweight=True,
        ),
    }
    target = resolve_lane_target(
        make_shot(), registry, check_readiness=False,
    )
    assert target.uses_gpu


def test_describe_lane_targets_lists_all_lanes_with_availability() -> None:
    described = describe_lane_targets()
    assert set(described) == {"real", "image", "h3", "html"}
    assert described["html"]["title_card"] == {
        "kind": "deterministic",
        "handler": "graphic_screen",
        "available": True,
    }
    assert described["real"]["reused_media"] == {
        "kind": "deterministic",
        "handler": "imported_media",
        "available": True,
    }
    assert described["image"]["krea2_still"]["backend"] == "krea2_comfyui"
    assert described["image"]["krea2_still"]["available"] is True
    # Unbuilt backend targets are absent entirely.
    assert "flux_still" not in described["image"]
    assert "wan_video" not in described["h3"]


# ---------------------------------------------------------------------------
# Entry-point wiring against a live PipelineService (mock mode)


def test_resolver_and_builder_wire_into_a_live_mock_pipeline(tmp_path) -> None:
    from backend.core import load_config
    from backend.models.shot_requests import build_krea2_request
    from backend.pipeline import PipelineService

    config = load_config(environ={})
    service = PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )

    html_shot = make_shot(
        lane=ShotLane.HTML, visual_type=VisualType.GRAPHIC_SCREEN,
    )
    target = resolve_lane_target(
        html_shot, service.registry, mock_mode=service.mock_mode,
    )
    assert target.backend_name == "mock"
    assert not target.uses_gpu

    # Builders accept the exact identity dict a dispatcher would pull from
    # the live registry descriptor.
    descriptor = service.registry.get("krea2_comfyui").descriptor()
    identity = {
        "model": descriptor.model_name,
        "model_version": descriptor.model_version,
        "quantization": descriptor.quantization,
    }
    image_shot = make_shot(visual_prompt="canyon")
    plan = build_krea2_request(
        image_shot,
        _project(),
        tmp_path / "out",
        backend_identity=identity,
    )
    assert plan.cache_payload["model"] == descriptor.model_name
    assert plan.request.settings["workflow_version"] == "krea2-turbo-fp8-v1"


def _project():
    from backend.schemas.models import Project

    return Project(
        title="Mars",
        topic="documentary",
        target_duration=600.0,
        resolution=(1920, 1080),
        fps=24,
        slug="mars",
    )
