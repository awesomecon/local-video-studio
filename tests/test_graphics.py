from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.graphics.browser import chromium_argv
from backend.graphics.generator import GraphicScreenGenerator
from backend.graphics.models import GraphicScreenResponse
from backend.graphics.renderer import GraphicScreenRenderer, _kicker_label
from backend.graphics.sanitize import GraphicScreenValidationError, sanitize_graphic_screen
from backend.models.errors import BackendError, BackendErrorCode
from backend.schemas import Project, Scene, ThumbnailTextLayout


def response(**changes: object) -> GraphicScreenResponse:
    payload = {
        "title": "Tokens",
        "design_summary": "A clear static explainer.",
        "visible_text": ["Tokens", "62%"],
        "html_body": "<main><h1>Tokens</h1><svg viewBox='0 0 10 10'><text>62%</text></svg></main>",
        "css": ".label { color: #fff; }",
    }
    payload.update(changes)
    return GraphicScreenResponse.model_validate(payload)


def test_sanitizer_reserializes_a_static_screen_with_owned_csp() -> None:
    document, visible = sanitize_graphic_screen(response(), width=320, height=180)

    assert visible == ["Tokens", "62%"]
    assert "default-src 'none'" in document
    assert "width: 320px" in document
    assert "Noto Sans" in document
    assert "list-style: none" in document
    assert "<script" not in document


@pytest.mark.parametrize("body, css", [
    ("<main><script>alert(1)</script></main>", ""),
    ("<main><img src='file:///etc/passwd'></main>", ""),
    ("<main onclick='alert(1)'>Tokens</main>", ""),
    ("<main><svg><foreignObject>Tokens</foreignObject></svg></main>", ""),
    ("<main><svg><rect fill='url(https://example.test/x)'/></svg></main>", ""),
    ("<main>Tokens</main>", "a { background: url(https://example.test/x); }"),
    ("<main>Tokens</main>", "@import 'https://example.test/x';"),
    ("<main>Tokens</main>", "a::before { content: 'unexpected'; }"),
    ("<main>Tokens</main>", r"main::before { \63 ontent: 'unexpected'; }"),
    ("<main>Tokens</main>", r"main::before { c\6f ntent: 'unexpected'; }"),
    ("<main>Tokens</main>", "main::before { con/**/tent: 'unexpected'; }"),
    ("<main>Tokens</main>", "main { font-size: 0; }"),
    ("<main>Tokens</main>", "main { display: none; }"),
    ("<main>Tokens</main>", "main { transform: scale(0); }"),
])
def test_sanitizer_rejects_active_or_external_content(body: str, css: str) -> None:
    with pytest.raises(GraphicScreenValidationError):
        sanitize_graphic_screen(
            response(html_body=body, css=css, visible_text=["Tokens"]), width=320, height=180,
        )


def test_sanitizer_requires_exact_visible_text_in_dom_order() -> None:
    with pytest.raises(GraphicScreenValidationError, match="visible text"):
        sanitize_graphic_screen(response(visible_text=["62%", "Tokens"]), width=320, height=180)


def test_chromium_argv_is_fixed_and_never_disables_sandbox(tmp_path: Path) -> None:
    command = chromium_argv(
        Path("/snap/bin/chromium"), document=tmp_path / "screen.html", output=tmp_path / "screen.png",
        profile=tmp_path / "profile", width=320, height=180,
    )

    assert "--no-sandbox" not in command
    assert "--headless=new" in command
    assert "--window-size=320,180" in command
    assert any(item.startswith("--user-data-dir=") for item in command)
    assert command[-1].startswith("file:")


def test_renderer_rejects_wrong_output_dimensions_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renderer = GraphicScreenRenderer(Path("/not-used"))

    def fake_run(command, **kwargs):
        output = Path(next(value.split("=", 1)[1] for value in command if value.startswith("--screenshot=")))
        Image.new("RGB", (1, 1), "black").save(output)
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return Result()

    monkeypatch.setattr("backend.graphics.renderer.subprocess.run", fake_run)
    output = tmp_path / "visual.png"
    with pytest.raises(RuntimeError, match="resolution"):
        renderer.render("<html></html>", output, width=320, height=180)
    assert not output.exists()


def test_thumbnail_composite_is_deterministic_and_dedupes_repeated_hook(tmp_path: Path) -> None:
    renderer = GraphicScreenRenderer(Path("/not-used"))
    artwork = tmp_path / "artwork.png"
    Image.new("RGB", (1280, 720), (96, 112, 130)).save(artwork)
    layout = ThumbnailTextLayout(title="How Local LLMs Work", hook="how local llms work")

    first = renderer.render_thumbnail(artwork, tmp_path / "a.png", layout, text_side="right")
    second = renderer.render_thumbnail(artwork, tmp_path / "b.png", layout, text_side="right")
    assert first[0] == second[0]

    distinct = layout.model_copy(update={"hook": "Run models offline"})
    third = renderer.render_thumbnail(artwork, tmp_path / "c.png", distinct, text_side="right")
    assert third[0] != first[0]

    with Image.open(tmp_path / "c.png") as composite:
        assert composite.size == (1280, 720)


def test_kicker_label_skips_empty_or_repeated_hooks() -> None:
    repeated = ThumbnailTextLayout(title="How Local LLMs Work", hook=" how local llms work! ")
    assert _kicker_label(repeated) == ""
    blank = ThumbnailTextLayout(title="Title", hook="   ")
    assert _kicker_label(blank) == ""
    distinct = ThumbnailTextLayout(title="Title", hook="Run models offline")
    assert _kicker_label(distinct) == "Run models offline"


class _GraphicLLM:
    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.replies = replies
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["validator"](self.replies.pop(0))


class _FailingGraphicLLM(_GraphicLLM):
    def __init__(self, errors: list[BackendError], replies: list[dict[str, object]]) -> None:
        super().__init__(replies)
        self.errors = errors

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return kwargs["validator"](self.replies.pop(0))


def test_generator_keeps_reasoning_and_repairs_only_once() -> None:
    invalid = response(html_body="<main><script>x</script></main>", visible_text=["x"]).model_dump()
    valid = response().model_dump()
    llm = _GraphicLLM([invalid, valid])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic", selected_llm_model="chosen-local")
    scene = Scene(project_id=project.id, index=0, duration=1, narration="Explain tokens.", settings={
        "graphic_screen": {"instructions": "Clean explainer", "exact_text": ["Tokens", "62%"]},
    })

    generated, document, visible, attempt = GraphicScreenGenerator(llm).generate(project, scene)

    assert generated.title == "Tokens"
    assert visible == ["Tokens", "62%"]
    assert "default-src 'none'" in document
    assert attempt == 2
    assert len(llm.calls) == 2
    assert all(call["model"] == "chosen-local" for call in llm.calls)
    assert all(call["thinking_budget_tokens"] == 16_384 for call in llm.calls)
    assert all(call["max_tokens"] == 32_768 for call in llm.calls)


def test_repair_turn_echoes_rejected_draft_and_reason() -> None:
    invalid = response(html_body="<main><script>x</script></main>", visible_text=["x"]).model_dump()
    valid = response().model_dump()
    llm = _GraphicLLM([invalid, valid])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(project_id=project.id, index=0, duration=1)

    generator = GraphicScreenGenerator(llm)
    _, _, _, attempt = generator.generate(project, scene)

    assert attempt == 2
    repair_messages = llm.calls[1]["messages"]
    # The rejected draft is echoed as an assistant turn so the repair is not blind...
    draft_turns = [m for m in repair_messages if m["role"] == "assistant"]
    assert draft_turns and "<script>x</script>" in draft_turns[-1]["content"]
    # ...and the final user turn names the concrete structural reason.
    assert "unsupported HTML or SVG element" in repair_messages[-1]["content"]
    assert generator.attempt_errors == ["unsupported HTML or SVG element 'script'"]


def test_final_error_carries_the_concrete_reason() -> None:
    invalid = response(html_body="<main><script>x</script></main>", visible_text=["x"]).model_dump()
    llm = _GraphicLLM([invalid, invalid])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(project_id=project.id, index=0, duration=1)

    with pytest.raises(GraphicScreenValidationError) as failure:
        GraphicScreenGenerator(llm).generate(project, scene)
    assert "unsupported HTML or SVG element" in str(failure.value)
    assert getattr(failure.value, "attempt_count") == 2


def test_validator_recovers_html_body_alias_and_string_schema_version() -> None:
    payload = response().model_dump()
    payload["body"] = payload.pop("html_body")
    payload["schema_version"] = "1"

    recovered = GraphicScreenGenerator._validate_response(
        payload, title="t", summary="s", exact_text=[],
    )

    assert recovered.html_body.startswith("<main>")
    assert recovered.schema_version == 1


def test_sanitizer_accepts_br_and_tables() -> None:
    body = (
        "<main><h1>Tokens</h1><p>One<br>Two</p>"
        "<table><thead><tr><th>Name</th></tr></thead>"
        "<tbody><tr><td>62%</td></tr></tbody></table></main>"
    )
    document, visible = sanitize_graphic_screen(
        response(html_body=body, visible_text=["Tokens", "One", "Two", "Name", "62%"]),
        width=320, height=180,
    )
    assert "<br>" in document
    assert "<table>" in document
    assert visible == ["Tokens", "One", "Two", "Name", "62%"]


def test_sanitizer_reports_mismatch_position_without_leaking_source() -> None:
    with pytest.raises(GraphicScreenValidationError, match="index 1") as failure:
        sanitize_graphic_screen(
            response(visible_text=["Tokens", "61%"]), width=320, height=180,
        )
    message = str(failure.value)
    assert "2 DOM text nodes" in message
    assert "Tokens" not in message


def test_sanitizer_names_the_unsupported_element_attribute_and_property() -> None:
    with pytest.raises(GraphicScreenValidationError, match="'figure'"):
        sanitize_graphic_screen(
            response(html_body="<main><figure>Tokens</figure></main>", visible_text=["Tokens"]),
            width=320, height=180,
        )
    with pytest.raises(GraphicScreenValidationError, match="'data-value'"):
        sanitize_graphic_screen(
            response(html_body="<main data-value='3'>Tokens</main>"), width=320, height=180,
        )
    with pytest.raises(GraphicScreenValidationError, match="unsupported CSS property 'filter'"):
        sanitize_graphic_screen(response(css="main { filter: blur(2px); }"), width=320, height=180)
    with pytest.raises(
        GraphicScreenValidationError, match="unsupported CSS property '-webkit-background-clip'",
    ):
        sanitize_graphic_screen(
            response(css="h1 { -webkit-background-clip: text; }"), width=320, height=180,
        )


def test_sanitizer_accepts_safe_static_text_and_table_properties() -> None:
    css = (
        "h1 { color: #f7d774; text-shadow: 0 2px 6px rgba(0,0,0,0.55);"
        " background-image: linear-gradient(45deg,#f7d774,#b8860b); background-clip: text; }"
        "table { border-collapse: collapse; border-spacing: 4px; }"
        "td { overflow-wrap: anywhere; word-spacing: 2px; object-fit: contain; }"
    )
    document, _ = sanitize_graphic_screen(response(css=css), width=320, height=180)
    for fragment in ("text-shadow", "background-clip: text", "border-collapse", "object-fit"):
        assert fragment in document


def test_generator_stops_after_one_structural_repair() -> None:
    invalid = response(html_body="<main><script>x</script></main>", visible_text=["x"]).model_dump()
    llm = _GraphicLLM([invalid, invalid])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(project_id=project.id, index=0, duration=1)

    with pytest.raises(GraphicScreenValidationError) as failure:
        GraphicScreenGenerator(llm).generate(project, scene)
    assert getattr(failure.value, "attempt_count") == 2
    assert len(llm.calls) == 2


def test_generator_recovers_metadata_and_uses_visual_prompt_for_blank_instructions() -> None:
    payload = response().model_dump()
    payload.pop("title")
    payload.pop("design_summary")
    llm = _GraphicLLM([payload])
    project = Project(title="Project title", topic="test", target_duration=1, slug="graphic")
    scene = Scene(
        project_id=project.id,
        index=0,
        title="Scene title",
        duration=1,
        visual_prompt="Diagram with exact labels: Tokens, 62%.",
        settings={"graphic_screen": {"instructions": "", "exact_text": []}},
    )

    generated, _, _, attempt = GraphicScreenGenerator(llm).generate(project, scene)

    assert generated.title == "Scene title"
    assert generated.design_summary == scene.visual_prompt
    assert scene.visual_prompt in llm.calls[0]["messages"][1]["content"]
    assert attempt == 1


def test_generator_repairs_nonretryable_structured_backend_error_once() -> None:
    mismatch = BackendError(
        BackendErrorCode.INVALID_RESPONSE,
        "The local model returned incomplete JSON.",
        retryable=False,
    )
    llm = _FailingGraphicLLM([mismatch], [response().model_dump()])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(project_id=project.id, index=0, duration=1)

    _, _, _, attempt = GraphicScreenGenerator(llm).generate(project, scene)

    assert attempt == 2
    assert len(llm.calls) == 2
    assert "incomplete JSON" in llm.calls[1]["messages"][-1]["content"]


@pytest.mark.parametrize("code", [BackendErrorCode.INVALID_RESPONSE, BackendErrorCode.REQUEST_TIMEOUT])
def test_generator_retries_transient_server_errors_once(
    code: BackendErrorCode, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(GraphicScreenGenerator, "TRANSIENT_RETRY_DELAY_SECONDS", 0.0)
    hiccup = BackendError(code, "The local LLM returned HTTP 500.", retryable=True)
    llm = _FailingGraphicLLM([hiccup], [response().model_dump()])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(project_id=project.id, index=0, duration=1)

    generator = GraphicScreenGenerator(llm)
    _, _, _, attempt = generator.generate(project, scene)

    assert attempt == 2
    assert len(llm.calls) == 2
    # A recovered server retries the untouched original conversation, not a repair turn.
    assert llm.calls[1]["messages"] == llm.calls[0]["messages"]
    assert generator.attempt_errors == ["The local LLM returned HTTP 500."]


def test_generator_raises_immediately_for_nonretryable_backend_errors() -> None:
    unavailable = BackendError(
        BackendErrorCode.MODEL_UNAVAILABLE, "Configured model is not available.", retryable=False,
    )
    llm = _FailingGraphicLLM([unavailable], [response().model_dump()])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(project_id=project.id, index=0, duration=1)

    with pytest.raises(BackendError, match="not available"):
        GraphicScreenGenerator(llm).generate(project, scene)
    assert len(llm.calls) == 1


def test_system_prompt_states_the_css_property_contract() -> None:
    prompt = GraphicScreenGenerator._system_prompt()
    assert "text-shadow" in prompt
    assert "-webkit-background-clip" in prompt
    assert "custom properties" in prompt
    assert "mobile-safe area" in prompt
    assert "non-overlapping layout box" in prompt


def test_generator_reuses_cached_response_for_identical_prompts(tmp_path: Path) -> None:
    from backend.storage.generation_cache import GenerationCache

    cache = GenerationCache(tmp_path / "cache")
    first_llm = _GraphicLLM([response().model_dump()])
    second_llm = _GraphicLLM([])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(
        project_id=project.id, index=0, duration=1, narration="Explain tokens.",
        settings={"graphic_screen": {"instructions": "Clean explainer"}},
    )

    first = GraphicScreenGenerator(first_llm, cache=cache).generate(project, scene)
    generator = GraphicScreenGenerator(second_llm, cache=cache)
    second = generator.generate(project, scene)

    assert len(first_llm.calls) == 1
    assert second_llm.calls == []
    assert generator.cache_hit is True
    assert second[0].model_dump() == first[0].model_dump()
    assert second[1] == first[1]
    assert second[3] == 1


def test_generator_cache_misses_when_any_request_input_differs(tmp_path: Path) -> None:
    from backend.storage.generation_cache import GenerationCache

    cache = GenerationCache(tmp_path / "cache")
    llm = _GraphicLLM([
        response().model_dump(),
        response().model_dump(),
        response().model_dump(),
        response().model_dump(),
    ])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    base = Scene(
        project_id=project.id, index=0, duration=1, narration="Explain tokens.",
        visual_prompt="Diagram of tokens.",
        settings={"graphic_screen": {"instructions": "Clean explainer"}},
    )
    different_narration = base.model_copy(update={"narration": "Something else."})
    different_model = Project(
        title="Graphic", topic="test", target_duration=1, slug="graphic",
        selected_llm_model="other-local-model",
    )
    different_resolution = Project(
        title="Graphic", topic="test", target_duration=1, slug="graphic",
        resolution=(1280, 720),
    )

    GraphicScreenGenerator(llm, cache=cache).generate(project, base)
    GraphicScreenGenerator(llm, cache=cache).generate(project, different_narration)
    GraphicScreenGenerator(llm, cache=cache).generate(different_model, base)
    GraphicScreenGenerator(llm, cache=cache).generate(different_resolution, base)

    assert len(llm.calls) == 4


def test_generator_serves_cached_response_per_resolved_model(tmp_path: Path) -> None:
    from backend.storage.generation_cache import GenerationCache

    cache = GenerationCache(tmp_path / "cache")
    llm = _GraphicLLM([response().model_dump(), response().model_dump()])
    project = Project(
        title="Graphic", topic="test", target_duration=1, slug="graphic",
        selected_llm_model="chosen-local",
    )
    scene = Scene(project_id=project.id, index=0, duration=1)

    GraphicScreenGenerator(llm, cache=cache).generate(project, scene)
    replay = GraphicScreenGenerator(llm, cache=cache)

    assert replay.generate(project, scene)[0].title == "Tokens"
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == "chosen-local"


def test_generator_falls_back_to_llm_when_exact_text_manifest_differs(tmp_path: Path) -> None:
    from backend.storage.generation_cache import GenerationCache

    cache = GenerationCache(tmp_path / "cache")
    llm = _GraphicLLM([
        response().model_dump(),
        response(visible_text=["Tokens", "63%"], html_body=(
            "<main><h1>Tokens</h1><svg viewBox='0 0 10 10'><text>63%</text></svg></main>"
        )).model_dump(),
    ])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")

    def scene_with(text: list[str]) -> Scene:
        return Scene(
            project_id=project.id, index=0, duration=1,
            settings={"graphic_screen": {"exact_text": text}},
        )

    cached_scene = scene_with(["Tokens", "62%"])
    requested_scene = scene_with(["Tokens", "63%"])
    GraphicScreenGenerator(llm, cache=cache).generate(project, cached_scene)
    generator = GraphicScreenGenerator(llm, cache=cache)
    generated = generator.generate(project, requested_scene)

    assert len(llm.calls) == 2
    assert generated[0].visible_text == ["Tokens", "63%"]
    assert generator.cache_hit is False


def test_generator_regenerates_cleanly_from_corrupt_cache_entry(tmp_path: Path) -> None:
    from backend.storage.generation_cache import GenerationCache

    cache = GenerationCache(tmp_path / "cache")
    llm = _GraphicLLM([response().model_dump(), response().model_dump()])
    project = Project(title="Graphic", topic="test", target_duration=1, slug="graphic")
    scene = Scene(project_id=project.id, index=0, duration=1)

    GraphicScreenGenerator(llm, cache=cache).generate(project, scene)
    generator = GraphicScreenGenerator(llm, cache=cache)
    key = generator._cache_key(
        [
            {"role": "system", "content": generator._system_prompt()},
            {"role": "user", "content": generator._request_text(project, scene, "", [])},
        ],
        GraphicScreenResponse.model_json_schema(),
    )
    entry = tmp_path / "cache" / "v1" / "local_graphic" / key / "artifact.bin"
    entry.write_bytes(b'{"broken": ')

    regenerated = generator.generate(project, scene)

    assert len(llm.calls) == 2
    assert generator.cache_hit is False
    assert regenerated[0].title == "Tokens"
    assert generator.cache.store("local_graphic", key, b"refreshed") is True
