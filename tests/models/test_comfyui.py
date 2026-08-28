from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from backend.models import ComfyUIBackend, GenerationRequest, Ideogram4LocalBackend
from backend.models.comfyui import substitute_workflow
from backend.models.errors import BackendError, BackendErrorCode


def test_recursive_workflow_substitution_preserves_native_types():
    workflow = {"1": {"inputs": {"seed": "{{seed}}", "text": "x {{prompt}}"}}}
    assert substitute_workflow(workflow, {"seed": 123, "prompt": "hello"}) == {
        "1": {"inputs": {"seed": 123, "text": "x hello"}}
    }


def test_unload_releases_comfyui_models_and_allocator_memory():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.url.path == "/free"
        return httpx.Response(200)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    ComfyUIBackend(client_factory=factory).unload()

    assert captured == {"unload_models": True, "free_memory": True}


def test_submit_poll_and_retrieve(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "prompt-1"})
        if request.url.path == "/history/prompt-1":
            return httpx.Response(
                200,
                json={
                    "prompt-1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "result.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"fake-png")
        raise AssertionError(request.url)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    backend = ComfyUIBackend(
        workflow={"1": {"inputs": {"text": "{{prompt}}", "seed": "{{seed}}"}}},
        poll_interval=0,
        client_factory=factory,
    )
    result = backend.generate(
        GenerationRequest(job_id="job", output_dir=tmp_path, prompt="hello", seed=9)
    )
    assert result.outputs[0].read_bytes() == b"fake-png"
    assert result.metadata["prompt_id"] == "prompt-1"


def test_ideogram_rejects_rendered_content_filter_card(tmp_path, monkeypatch):
    placeholder = tmp_path / "filtered.png"
    placeholder.write_bytes(b"placeholder")
    monkeypatch.setattr("backend.models.adapters.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "backend.models.adapters.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="Image blocked by safety filter\n", stderr="",
        ),
    )

    assert Ideogram4LocalBackend._is_content_filter_placeholder(placeholder)


def test_ideogram_keeps_ordinary_text_image(tmp_path, monkeypatch):
    image = tmp_path / "ordinary.png"
    image.write_bytes(b"placeholder")
    monkeypatch.setattr("backend.models.adapters.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "backend.models.adapters.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="SCIENCE MEDICINE ENGINEERING\n", stderr="",
        ),
    )

    assert not Ideogram4LocalBackend._is_content_filter_placeholder(image)


def test_cancel_deletes_only_owned_pending_prompt():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(200, json={
                "queue_running": [],
                "queue_pending": [[1, "owned-prompt", {}], [2, "other-prompt", {}]],
            })
        if request.url.path == "/queue" and request.method == "POST":
            assert json.loads(request.content) == {"delete": ["owned-prompt"]}
            return httpx.Response(200)
        raise AssertionError(request.url)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ComfyUIBackend(client_factory=factory)
    backend._jobs["job"] = "owned-prompt"
    assert backend.cancel("job") is True
    assert not any(path == "/interrupt" for _method, path, _content in calls)


def test_cancel_interrupts_only_when_owned_prompt_is_sole_running_prompt():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/queue":
            return httpx.Response(200, json={
                "queue_running": [[1, "owned-prompt", {}]],
                "queue_pending": [],
            })
        if request.url.path == "/interrupt":
            return httpx.Response(200)
        raise AssertionError(request.url)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ComfyUIBackend(client_factory=factory)
    backend._jobs["job"] = "owned-prompt"
    assert backend.cancel("job") is True
    assert paths == ["/queue", "/interrupt"]


def test_cancel_never_interrupts_unowned_running_prompt():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={
            "queue_running": [[1, "other-prompt", {}]],
            "queue_pending": [],
        })

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ComfyUIBackend(client_factory=factory)
    backend._jobs["job"] = "owned-prompt"
    assert backend.cancel("job") is False
    assert paths == ["/queue"]


def _queue_and_interrupt_handler(state: dict, holder: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "prompt-1"})
        if request.url.path.startswith("/history/"):
            state["polls"] += 1
            if state["polls"] >= state.get("cancel_after_polls", 3):
                assert holder["backend"].cancel("job") is True
            return httpx.Response(200, json={})
        if request.url.path == "/queue" and request.method == "GET":
            running = [] if state.get("interrupted") else [[1, "prompt-1", {}]]
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if request.url.path == "/interrupt":
            state["interrupted"] = True
            return httpx.Response(200)
        raise AssertionError(request.url)

    return handler


def test_cancel_during_generation_stops_polling_with_canceled_error(tmp_path):
    """A cancel must unblock the poll loop instead of stalling until the timeout."""
    state = {"polls": 0}
    holder: dict = {}
    handler = _queue_and_interrupt_handler(state, holder)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ComfyUIBackend(
        workflow={"1": {"inputs": {"text": "{{prompt}}", "seed": "{{seed}}"}}},
        poll_interval=0.01,
        generation_timeout=30,
        client_factory=factory,
    )
    holder["backend"] = backend
    with pytest.raises(BackendError) as excinfo:
        backend.generate(
            GenerationRequest(job_id="job", output_dir=tmp_path, prompt="hello", seed=9)
        )
    assert excinfo.value.code is BackendErrorCode.CANCELED
    assert state["polls"] <= 4


def test_cancel_beats_timeout_when_history_never_returns_a_record(tmp_path):
    """A canceled prompt never appears in /history; it must not surface as a timeout."""
    state = {"polls": 0, "cancel_after_polls": 1}
    holder: dict = {}
    handler = _queue_and_interrupt_handler(state, holder)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ComfyUIBackend(
        workflow={"1": {"inputs": {"text": "{{prompt}}", "seed": "{{seed}}"}}},
        poll_interval=0.01,
        generation_timeout=0.05,
        client_factory=factory,
    )
    holder["backend"] = backend
    with pytest.raises(BackendError) as excinfo:
        backend.generate(
            GenerationRequest(job_id="job", output_dir=tmp_path, prompt="hello", seed=9)
        )
    assert excinfo.value.code is BackendErrorCode.CANCELED
    assert not excinfo.value.retryable


def test_cancel_without_active_run_does_not_flag_or_block_id_reuse(tmp_path):
    """Canceling with nothing in flight flags nothing, so reused ids stay usable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "prompt-1"})
        if request.url.path == "/history/prompt-1":
            return httpx.Response(
                200,
                json={
                    "prompt-1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "result.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"fake-png")
        raise AssertionError(request.url)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ComfyUIBackend(
        workflow={"1": {"inputs": {"text": "{{prompt}}", "seed": "{{seed}}"}}},
        poll_interval=0,
        client_factory=factory,
    )
    backend._jobs["job"] = "stale-prompt"
    assert backend.cancel("job") is False
    assert backend._canceled == set()
    result = backend.generate(
        GenerationRequest(job_id="job", output_dir=tmp_path, prompt="hello", seed=9)
    )
    assert result.outputs[0].read_bytes() == b"fake-png"


def test_validation_rejection_surfaces_comfyui_error(tmp_path):
    """A 400 prompt-validation rejection names the real cause, not just HTTP 400."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/prompt"
        return httpx.Response(400, json={
            "error": {
                "type": "prompt_outputs_failed_validation",
                "message": "Prompt outputs failed validation",
            },
            "node_errors": {
                "11": {
                    "errors": [{
                        "message": "Value not in list",
                        "details": "unet_name: 'acestep_v1.5_xl_sft_bf16.safetensors' not in ['acestep_v1.5_xl_turbo_bf16.safetensors']",
                    }],
                },
            },
        })

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ComfyUIBackend(
        workflow={"1": {"inputs": {"text": "{{prompt}}", "seed": "{{seed}}"}}},
        client_factory=factory,
    )
    with pytest.raises(BackendError) as excinfo:
        backend.generate(
            GenerationRequest(job_id="job", output_dir=tmp_path, prompt="hello", seed=9)
        )
    message = str(excinfo.value)
    assert "rejected the request" in message
    assert "Value not in list" in message
    assert "acestep_v1.5_xl_sft_bf16.safetensors" in message


def test_http_error_message_falls_back_without_json():
    response = httpx.Response(500, text="boom")
    assert ComfyUIBackend._http_error_message(response) == "ComfyUI returned HTTP 500."
