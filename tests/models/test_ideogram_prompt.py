from __future__ import annotations

import json

import pytest

from backend.models.errors import BackendError, BackendErrorCode

from backend.models.ideogram_prompt import (
    IdeogramPromptError,
    build_ideogram_v4_prompt,
    build_magic_prompt_messages,
    export_kjnodes_ideogram_json,
    extract_protected_text,
    import_kjnodes_ideogram_json,
    serialize_ideogram_prompt_json,
    validate_ideogram_prompt_json,
)


def _caption(*, text: str = "NOIR", bbox: list[int] | None = None) -> dict:
    element = {
        "type": "text",
        "text": text,
        "desc": "Large elegant centered serif typography.",
        "color_palette": ["#ffffff"],
    }
    if bbox is not None:
        element = {"type": "text", "bbox": bbox, **{k: v for k, v in element.items() if k != "type"}}
    return {
        "high_level_description": "A luxury perfume advertisement.",
        "style_description": {
            "aesthetics": "minimal, premium, luxurious",
            "lighting": "dramatic controlled studio lighting",
            "photo": "high-end 85mm commercial product photography",
            "medium": "photograph",
            "color_palette": ["#080808", "#ffffff", "#c0c0c0"],
        },
        "compositional_deconstruction": {
            "background": "Black marble with subtle glossy reflections.",
            "elements": [
                element,
                {
                    "type": "obj",
                    "bbox": [280, 300, 760, 700],
                    "desc": "A centered black glass perfume bottle.",
                    "color_palette": ["#111111", "#c0c0c0"],
                },
            ],
        },
    }


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _magic_response(*texts: str, aspect_ratio: str = "16:9") -> dict:
    elements = [
        {
            "type": "text",
            "bbox": [70, 100, 230, 900] if index == 0 else [820, 180, 930, 820],
            "text": f"LVS_IDEOGRAM_TEXT_{index:03d}",
            "desc": "Bold vintage display lettering.",
        }
        for index, _ in enumerate(texts)
    ]
    elements.append({
        "type": "obj",
        "bbox": [270, 130, 790, 870],
        "desc": "A central subject matching the user idea.",
    })
    return {
        "aspect_ratio": aspect_ratio,
        "high_level_description": "A polished cinematic composition.",
        "compositional_deconstruction": {
            "background": "A coherent environment with controlled depth.",
            "elements": elements,
        },
    }


def test_basic_quick_prompt_produces_canonical_json() -> None:
    result = build_ideogram_v4_prompt(
        "A cinematic red sports car in downtown Miami at night.",
        aspect_ratio="16:9",
    )
    structured = validate_ideogram_prompt_json(result["structured_prompt"])
    assert list(structured) == [
        "high_level_description",
        "style_description",
        "compositional_deconstruction",
    ]
    assert json.loads(result["serialized_prompt"]) == structured


@pytest.mark.parametrize("literal", ["PALM BEACH", "OpenAI LABS", "OPEN 24\nHOURS", "CAFÉ NOIR"])
def test_quick_mode_preserves_exact_quoted_text(literal: str) -> None:
    llm = FakeLLM(_magic_response(literal))
    result = build_ideogram_v4_prompt(
        f'A neon sign saying "{literal}"',
        aspect_ratio="16:9",
        llm=llm,
    )
    text_elements = [
        item
        for item in result["structured_prompt"]["compositional_deconstruction"]["elements"]
        if item["type"] == "text"
    ]
    assert [item["text"] for item in text_elements] == [literal]
    assert literal in json.loads(result["serialized_prompt"])[
        "compositional_deconstruction"
    ]["elements"][0]["text"]


def test_two_quoted_regions_stay_separate_and_ordered() -> None:
    llm = FakeLLM(_magic_response("PALM BEACH", "OPEN ALL NIGHT"))
    result = build_ideogram_v4_prompt(
        'A poster with "PALM BEACH" at the top and "OPEN ALL NIGHT" at the bottom.',
        aspect_ratio="4:5",
        llm=llm,
    )
    text_elements = [
        item
        for item in result["structured_prompt"]["compositional_deconstruction"]["elements"]
        if item["type"] == "text"
    ]
    assert [item["text"] for item in text_elements] == ["PALM BEACH", "OPEN ALL NIGHT"]
    assert [item["bbox"] for item in text_elements] == [
        [70, 100, 230, 900],
        [820, 180, 930, 820],
    ]


@pytest.mark.parametrize("bbox", [
    [-1, 300, 780, 700],
    [800, 300, 200, 700],
    [100, 900, 200, 400],
    [0, 0, 1001, 1000],
])
def test_invalid_object_bbox_is_rejected(bbox: list[int]) -> None:
    payload = _caption(bbox=[70, 160, 220, 840])
    payload["compositional_deconstruction"]["elements"][1]["bbox"] = bbox
    with pytest.raises(IdeogramPromptError, match="bbox"):
        import_kjnodes_ideogram_json(payload)


def test_valid_object_bbox_is_unchanged() -> None:
    payload = _caption(bbox=[70, 160, 220, 840])
    model = import_kjnodes_ideogram_json(payload)
    exported = export_kjnodes_ideogram_json(model)
    assert exported["compositional_deconstruction"]["elements"][1]["bbox"] == [
        280, 300, 760, 700,
    ]


def test_kjnodes_round_trip_preserves_text_boxes_order_and_normalizes_colors() -> None:
    payload = _caption(text="CAFÉ\nNOIR", bbox=[70, 160, 220, 840])
    exported = export_kjnodes_ideogram_json(import_kjnodes_ideogram_json(payload))
    text_element = exported["compositional_deconstruction"]["elements"][0]
    assert text_element["text"] == "CAFÉ\nNOIR"
    assert text_element["bbox"] == [70, 160, 220, 840]
    assert text_element["color_palette"] == ["#FFFFFF"]
    assert exported["style_description"]["color_palette"] == [
        "#080808", "#FFFFFF", "#C0C0C0",
    ]
    assert list(text_element) == ["type", "bbox", "text", "desc", "color_palette"]


def test_kjnodes_optional_high_level_is_not_invented_on_round_trip() -> None:
    payload = {
        "compositional_deconstruction": {
            "background": "",
            "elements": [{"type": "obj", "desc": ""}],
        },
    }
    exported = export_kjnodes_ideogram_json(import_kjnodes_ideogram_json(payload))
    assert exported == payload


def test_photo_and_art_style_use_official_distinct_key_orders() -> None:
    photo = validate_ideogram_prompt_json(_caption())
    assert list(photo["style_description"]) == [
        "aesthetics", "lighting", "photo", "medium", "color_palette",
    ]
    art = _caption()
    art["style_description"] = {
        "aesthetics": "retro, tropical",
        "lighting": "neon nighttime glow",
        "medium": "graphic_design",
        "art_style": "1950s screen-printed poster",
    }
    normalized = validate_ideogram_prompt_json(art)
    assert list(normalized["style_description"]) == [
        "aesthetics", "lighting", "medium", "art_style",
    ]


def test_photo_and_art_style_cannot_be_combined() -> None:
    payload = _caption()
    payload["style_description"]["art_style"] = "poster"
    with pytest.raises(IdeogramPromptError, match="exactly one"):
        import_kjnodes_ideogram_json(payload)


def test_palette_limits_are_enforced() -> None:
    payload = _caption()
    payload["style_description"]["color_palette"] = ["#000000"] * 17
    with pytest.raises(IdeogramPromptError, match="at most 16"):
        import_kjnodes_ideogram_json(payload)
    payload = _caption()
    payload["compositional_deconstruction"]["elements"][0]["color_palette"] = [
        "#000000"
    ] * 6
    with pytest.raises(IdeogramPromptError, match="at most 5"):
        import_kjnodes_ideogram_json(payload)


def test_magic_prompt_gets_actual_aspect_ratio_and_official_user_template() -> None:
    llm = FakeLLM(_magic_response(aspect_ratio="4:5"))
    build_ideogram_v4_prompt("A quiet portrait", aspect_ratio="4:5", llm=llm)
    user = llm.calls[0]["messages"][1]["content"]
    assert user.startswith("TARGET IMAGE ASPECT RATIO: 4:5 (width:height).\nUser idea: ")
    assert llm.calls[0]["temperature"] == 1.0


def test_precise_mode_preserves_coordinates_and_exact_text() -> None:
    payload = _caption(text="NOIR", bbox=[70, 160, 220, 840])
    result = build_ideogram_v4_prompt(None, mode="precise", precise_json=payload)
    text_element = result["structured_prompt"]["compositional_deconstruction"]["elements"][0]
    assert text_element["bbox"] == [70, 160, 220, 840]
    assert text_element["text"] == "NOIR"
    assert result["protected_text"] == ["NOIR"]


def test_json_parse_failure_uses_deterministic_fallback() -> None:
    result = build_ideogram_v4_prompt(
        'A motel sign saying "PALM BEACH"',
        aspect_ratio="16:9",
        llm=FakeLLM("not json at all"),
    )
    assert result["warnings"]
    assert any(
        element.get("text") == "PALM BEACH"
        for element in result["structured_prompt"]["compositional_deconstruction"]["elements"]
    )
    prose = json.dumps(result["structured_prompt"], ensure_ascii=False)
    assert '"supplied wording"' not in prose


def test_quick_mode_retries_one_transient_local_llm_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransientLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise BackendError(
                    BackendErrorCode.SERVER_NOT_RUNNING,
                    "The configured local LLM server is not reachable.",
                    retryable=True,
                )
            return {
                "aspect_ratio": "16:9",
                "high_level_description": "A cinematic Mars documentary thumbnail.",
                "compositional_deconstruction": {
                    "background": "Deep black space behind a glowing red Mars.",
                    "elements": [
                        {
                            "type": "obj",
                            "desc": "A dramatic portrait beside Mars.",
                        }
                    ],
                },
            }

    monkeypatch.setattr("backend.models.ideogram_prompt.time.sleep", lambda _delay: None)
    llm = TransientLLM()

    result = build_ideogram_v4_prompt(
        "A dramatic portrait beside Mars.",
        mode="quick",
        aspect_ratio="16:9",
        llm=llm,
    )

    assert llm.calls == 2
    assert result["warnings"] == []
    assert result["structured_prompt"]["high_level_description"] == (
        "A cinematic Mars documentary thumbnail."
    )


def test_quick_mode_labels_persistent_connection_failure_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OfflineLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            raise BackendError(
                BackendErrorCode.SERVER_NOT_RUNNING,
                "The configured local LLM server is not reachable.",
                retryable=True,
            )

    monkeypatch.setattr("backend.models.ideogram_prompt.time.sleep", lambda _delay: None)
    llm = OfflineLLM()

    result = build_ideogram_v4_prompt(
        "A dramatic portrait beside Mars.",
        mode="quick",
        aspect_ratio="16:9",
        llm=llm,
    )

    assert llm.calls == 2
    assert "could not reach the local LLM after a retry" in result["warnings"][0]


def test_one_controlled_json_repair_attempt() -> None:
    raw = json.dumps(_magic_response("PALM BEACH"), ensure_ascii=False)
    malformed = f"```json\n{raw[:-1]},}}\n```"
    result = build_ideogram_v4_prompt(
        'A sign saying "PALM BEACH"',
        aspect_ratio="16:9",
        llm=FakeLLM(malformed),
    )
    assert result["warnings"] == []


def test_no_typography_prompt_does_not_invent_text_in_fallback() -> None:
    result = build_ideogram_v4_prompt(
        "A photorealistic thunderstorm over the Florida coast at sunset.",
        aspect_ratio="16:9",
    )
    elements = result["structured_prompt"]["compositional_deconstruction"]["elements"]
    assert all(item["type"] != "text" for item in elements)
    assert result["structured_prompt"]["style_description"]["medium"] == "photograph"


def test_declared_text_and_upstream_prompt_file_are_available() -> None:
    assert extract_protected_text("Title: CAFÉ NOIR") == ["CAFÉ NOIR"]
    messages = build_magic_prompt_messages("A red car", "3:2")
    assert "QUOTED SPAN FIDELITY" in messages[0]["content"]


def test_unknown_precise_fields_are_rejected_not_discarded() -> None:
    payload = _caption()
    payload["invented_schema"] = True
    with pytest.raises(IdeogramPromptError, match="Extra inputs are not permitted"):
        import_kjnodes_ideogram_json(payload)


def test_compact_serializer_preserves_unicode_and_key_order() -> None:
    serialized = serialize_ideogram_prompt_json(_caption(text="CAFÉ NOIR"))
    assert "CAFÉ NOIR" in serialized
    assert "\\u00c9" not in serialized
    assert serialized.startswith('{"high_level_description":')
    assert '"style_description":' in serialized
    assert serialized.index('"style_description"') < serialized.index('"compositional_deconstruction"')
