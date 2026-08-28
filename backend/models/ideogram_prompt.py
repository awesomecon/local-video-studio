"""Ideogram 4 native structured-caption construction and validation.

The schema and key order mirror ideogram-oss/ideogram4 and the default
Ideogram4PromptBuilderKJ export from ComfyUI-KJNodes.  Quick mode uses the
vendored open-source Magic Prompt v1 instructions with the configured local
LLM; precise mode accepts the native caption JSON directly.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .errors import BackendError, BackendErrorCode


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ASPECT_RATIO = re.compile(r"^[1-9]\d*:[1-9]\d*$")
_MAGIC_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "prompts"
    / "ideogram4_magic_prompt_v1.txt"
)
_PLACEHOLDER_PREFIX = "LVS_IDEOGRAM_TEXT_"
_MAGIC_PROMPT_LLM_ATTEMPTS = 2
_MAGIC_PROMPT_RETRY_DELAY_SECONDS = 0.5


class IdeogramPromptError(ValueError):
    """A caption cannot be safely normalized to native Ideogram JSON."""


def _nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")
    return value


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _palette(value: list[str] | None, *, maximum: int) -> list[str] | None:
    if value is None:
        return None
    if len(value) > maximum:
        raise ValueError(f"color_palette supports at most {maximum} colors")
    normalized: list[str] = []
    for color in value:
        if not isinstance(color, str) or not _HEX_COLOR.fullmatch(color):
            raise ValueError("colors must use #RRGGBB hexadecimal form")
        normalized.append(color.upper())
    return normalized


def _bbox(value: list[int] | None) -> list[int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        raise ValueError("bbox must be four integers [y_min,x_min,y_max,x_max]")
    y_min, x_min, y_max, x_max = value
    if not (0 <= y_min < y_max <= 1000 and 0 <= x_min < x_max <= 1000):
        raise ValueError(
            "bbox must satisfy 0 <= y_min < y_max <= 1000 and "
            "0 <= x_min < x_max <= 1000"
        )
    return list(value)


class IdeogramStyle(BaseModel):
    """Official style block; declaration order yields both canonical variants."""

    model_config = ConfigDict(extra="forbid")

    aesthetics: str
    lighting: str
    photo: str | None = None
    medium: str
    art_style: str | None = None
    color_palette: list[str] | None = None

    @field_validator("aesthetics", "lighting", "medium")
    @classmethod
    def validate_required_strings(cls, value: str, info: Any) -> str:
        return _nonempty_string(value, f"style_description.{info.field_name}")

    @field_validator("photo", "art_style")
    @classmethod
    def validate_optional_strings(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _nonempty_string(value, f"style_description.{info.field_name}")

    @field_validator("color_palette")
    @classmethod
    def validate_palette(cls, value: list[str] | None) -> list[str] | None:
        return _palette(value, maximum=16)

    @model_validator(mode="after")
    def validate_style_kind(self) -> IdeogramStyle:
        if (self.photo is None) == (self.art_style is None):
            raise ValueError("style_description requires exactly one of photo or art_style")
        if self.photo is not None and self.medium.strip().lower() != "photograph":
            raise ValueError("photo style must use medium 'photograph'")
        if self.art_style is not None and self.medium.strip().lower() == "photograph":
            raise ValueError("photograph medium must use photo instead of art_style")
        return self


class IdeogramObjectElement(BaseModel):
    """Native object element in canonical key order."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["obj"] = "obj"
    bbox: list[int] | None = None
    desc: str
    color_palette: list[str] | None = None

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: list[int] | None) -> list[int] | None:
        return _bbox(value)

    @field_validator("desc")
    @classmethod
    def validate_desc(cls, value: str) -> str:
        return _string(value, "element.desc")

    @field_validator("color_palette")
    @classmethod
    def validate_palette(cls, value: list[str] | None) -> list[str] | None:
        return _palette(value, maximum=5)


class IdeogramTextElement(BaseModel):
    """Native text element; ``text`` is never stripped or case-normalized."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    bbox: list[int] | None = None
    text: str
    desc: str
    color_palette: list[str] | None = None

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: list[int] | None) -> list[int] | None:
        return _bbox(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not isinstance(value, str) or value == "":
            raise ValueError("text elements require a nonempty literal text field")
        return value

    @field_validator("desc")
    @classmethod
    def validate_desc(cls, value: str) -> str:
        return _string(value, "element.desc")

    @field_validator("color_palette")
    @classmethod
    def validate_palette(cls, value: list[str] | None) -> list[str] | None:
        return _palette(value, maximum=5)


IdeogramElement = IdeogramObjectElement | IdeogramTextElement


class IdeogramComposition(BaseModel):
    """Native composition in its required background/elements order."""

    model_config = ConfigDict(extra="forbid")

    background: str
    elements: list[IdeogramElement]

    @field_validator("background")
    @classmethod
    def validate_background(cls, value: str) -> str:
        return _string(value, "compositional_deconstruction.background")


class IdeogramPrompt(BaseModel):
    """Complete canonical Ideogram 4 caption."""

    model_config = ConfigDict(extra="forbid")

    high_level_description: str | None = None
    style_description: IdeogramStyle | None = None
    compositional_deconstruction: IdeogramComposition

    @field_validator("high_level_description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        return _string(value, "high_level_description") if value is not None else None

    def canonical_dict(self) -> dict[str, Any]:
        """Return native keys in model-training order, omitting optional nulls."""

        return self.model_dump(mode="python", exclude_none=True)

    def serialize(self) -> str:
        """Return the exact compact UTF-8 caption sent to Ideogram."""

        return json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )


class IdeogramLLM(Protocol):
    def complete(self, **kwargs: Any) -> Any: ...


class ProtectedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    literals: list[str]
    placeholders: list[str]


class IdeogramPromptBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    mode: Literal["quick", "precise"]
    structured_prompt: dict[str, Any]
    serialized_prompt: str
    protected_text: list[str]
    warnings: list[str]


def aspect_ratio_from_size(width: int, height: int) -> str:
    if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
        raise IdeogramPromptError("width and height must be positive integers")
    divisor = math.gcd(int(width), int(height)) or 1
    return f"{int(width) // divisor}:{int(height) // divisor}"


def validate_aspect_ratio(value: str | None) -> str:
    ratio = value or "1:1"
    if not isinstance(ratio, str) or not _ASPECT_RATIO.fullmatch(ratio):
        raise IdeogramPromptError("aspect_ratio must use positive integer W:H form")
    return ratio


def _quote_spans(prompt: str) -> list[tuple[int, int, str]]:
    """Find straight/curly quoted literals, including embedded newlines."""

    patterns = (
        re.compile(r'"((?:\\.|[^"\\])*)"', re.DOTALL),
        re.compile(r"'((?:\\.|[^'\\])*)'", re.DOTALL),
        re.compile(r"“(.*?)”", re.DOTALL),
        re.compile(r"‘(.*?)’", re.DOTALL),
    )
    candidates: list[tuple[int, int, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(prompt):
            candidates.append((match.start(1), match.end(1), match.group(1)))
    candidates.sort(key=lambda item: (item[0], item[1]))
    result: list[tuple[int, int, str]] = []
    last_end = -1
    for item in candidates:
        if item[0] >= last_end:
            result.append(item)
            last_end = item[1]
    return result


def extract_protected_text(prompt: str, explicit_text: Iterable[str] = ()) -> list[str]:
    """Extract exact quoted/declared strings without case or whitespace changes."""

    if not isinstance(prompt, str):
        raise IdeogramPromptError("prompt must be a string")
    values = [value for _, _, value in _quote_spans(prompt) if value != ""]
    declared = re.compile(
        r"(?im)^\s*(?:text|title|headline|subtitle|label)\s*:\s*(\S.*)$"
    )
    for match in declared.finditer(prompt):
        value = match.group(1)
        if len(value) >= 2 and (value[0], value[-1]) in {
            ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")
        }:
            value = value[1:-1]
        if value and value not in values:
            values.append(value)
    for item in explicit_text:
        value = str(item)
        if value and value not in values:
            values.append(value)
    return values


def protect_exact_text(prompt: str, explicit_text: Iterable[str] = ()) -> ProtectedPrompt:
    spans = _quote_spans(prompt)
    literals: list[str] = []
    placeholders: list[str] = []
    chunks: list[str] = []
    cursor = 0
    for start, end, literal in spans:
        placeholder = f"{_PLACEHOLDER_PREFIX}{len(literals):03d}"
        chunks.extend((prompt[cursor:start], placeholder))
        cursor = end
        literals.append(literal)
        placeholders.append(placeholder)
    chunks.append(prompt[cursor:])
    protected_prompt = "".join(chunks)
    for literal in extract_protected_text(prompt, explicit_text):
        if literal in literals:
            continue
        placeholder = f"{_PLACEHOLDER_PREFIX}{len(literals):03d}"
        literals.append(literal)
        placeholders.append(placeholder)
        protected_prompt += f'\nRequired literal text: "{placeholder}"'
    return ProtectedPrompt(
        prompt=protected_prompt,
        literals=literals,
        placeholders=placeholders,
    )


def _load_magic_prompt_sections() -> dict[str, str]:
    try:
        raw = _MAGIC_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdeogramPromptError(
            f"vendored Ideogram Magic Prompt is unavailable: {_MAGIC_PROMPT_PATH}"
        ) from exc
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and " " not in stripped:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = stripped[1:-1].lower()
            lines = []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    if "system" not in sections or "user" not in sections:
        raise IdeogramPromptError("vendored Magic Prompt must contain [SYSTEM] and [USER]")
    return sections


def build_magic_prompt_messages(prompt: str, aspect_ratio: str) -> list[dict[str, str]]:
    """Build messages exactly like upstream ``magic_prompt.build_messages``."""

    ratio = validate_aspect_ratio(aspect_ratio)
    sections = _load_magic_prompt_sections()
    user = sections["user"].replace("{{aspect_ratio}}", ratio)
    user = user.replace("{{original_prompt}}", prompt)
    return [
        {"role": "system", "content": sections["system"]},
        {"role": "user", "content": user},
    ]


def _parse_json_with_one_repair(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise IdeogramPromptError("LLM output must be a JSON object or JSON string")
    text = value.strip()
    attempts = [text]
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        repaired = re.sub(r",\s*([}\]])", r"\1", text[first : last + 1])
        if repaired != text:
            attempts.append(repaired)
    for candidate in attempts[:2]:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise IdeogramPromptError("Magic Prompt returned malformed JSON after one repair attempt")


def _invoke_llm_once(
    llm: IdeogramLLM | Any, messages: Sequence[Mapping[str, str]],
) -> Any:
    if hasattr(llm, "complete"):
        return llm.complete(
            messages=messages,
            structured=True,
            max_tokens=16_384,
            temperature=1.0,
            thinking_budget_tokens=0,
        )
    if callable(llm):
        return llm(messages=messages)
    raise IdeogramPromptError("llm must be callable or provide complete()")


def _invoke_llm(llm: IdeogramLLM | Any, messages: Sequence[Mapping[str, str]]) -> Any:
    """Invoke Magic Prompt, tolerating one brief local-server startup race.

    A connection refusal is safe to retry because no completion reached the
    externally managed server. Other backend failures and malformed model
    output keep their existing single-attempt behavior.
    """

    for attempt in range(_MAGIC_PROMPT_LLM_ATTEMPTS):
        try:
            return _invoke_llm_once(llm, messages)
        except BackendError as exc:
            should_retry = (
                exc.retryable
                and exc.code == BackendErrorCode.SERVER_NOT_RUNNING
                and attempt + 1 < _MAGIC_PROMPT_LLM_ATTEMPTS
            )
            if not should_retry:
                raise
            time.sleep(_MAGIC_PROMPT_RETRY_DELAY_SECONDS)
    raise AssertionError("Magic Prompt retry loop exhausted without returning or raising")


def _text_box(index: int, count: int) -> list[int]:
    if count == 1:
        return [70, 120, 230, 880]
    if index == 0:
        return [70, 100, 230, 900]
    if index == count - 1:
        return [820, 180, 930, 820]
    band = max(90, 500 // max(1, count - 2))
    top = min(700, 270 + (index - 1) * band)
    return [top, 160, min(790, top + 90), 840]


def _restore_protected_text(
    elements: list[dict[str, Any]], protected: ProtectedPrompt
) -> list[dict[str, Any]]:
    """Restore placeholders, then deterministically repair missing text regions."""

    repaired = [dict(element) for element in elements]
    text_elements = [item for item in repaired if item.get("type") == "text"]
    restored: set[int] = set()
    for item in text_elements:
        text = item.get("text")
        if not isinstance(text, str):
            continue
        for index, placeholder in enumerate(protected.placeholders):
            if placeholder in text:
                item["text"] = protected.literals[index]
                restored.add(index)
                break
    available = [item for item in text_elements if item.get("text") not in protected.literals]
    for index, literal in enumerate(protected.literals):
        if index in restored or any(item.get("text") == literal for item in text_elements):
            continue
        if available:
            item = available.pop(0)
            item["text"] = literal
            if not isinstance(item.get("desc"), str) or not item["desc"].strip():
                item["desc"] = "Clearly legible typography preserving the supplied text exactly."
        else:
            repaired.append({
                "type": "text",
                "bbox": _text_box(index, len(protected.literals)),
                "text": literal,
                "desc": "Clearly legible typography preserving the supplied text exactly.",
            })
    return repaired


def _infer_style(prompt: str, high_level: str) -> IdeogramStyle | None:
    """Add only a conservative medium/style block absent from Magic Prompt v1."""

    source = f"{prompt} {high_level}".lower()
    non_photo = any(term in source for term in (
        "poster", "graphic design", "illustration", "illustrated", "painting",
        "watercolor", "vector", "screen-printed", "screen printed", "book cover",
        "album cover", "magazine cover", "logo", "infographic",
    ))
    if non_photo:
        medium = "graphic_design" if any(term in source for term in (
            "poster", "graphic design", "cover", "logo", "infographic",
        )) else "illustration"
        return IdeogramStyle(
            aesthetics="polished, coherent, faithful to the requested visual direction",
            lighting="lighting consistent with the described scene",
            medium=medium,
            art_style="carefully composed non-photographic artwork matching the user idea",
        )
    return IdeogramStyle(
        aesthetics="polished, natural, faithful to the requested visual direction",
        lighting="lighting consistent with the described scene",
        photo="natural camera perspective with coherent depth and detail",
        medium="photograph",
    )


def _normalize_magic_output(
    raw: dict[str, Any],
    *,
    prompt: str,
    aspect_ratio: str,
    protected: ProtectedPrompt,
) -> IdeogramPrompt:
    allowed_top = {
        "aspect_ratio", "high_level_description", "style_description",
        "compositional_deconstruction",
    }
    unknown = [key for key in raw if key not in allowed_top]
    if unknown:
        raise IdeogramPromptError(f"Magic Prompt returned unsupported keys: {unknown}")
    returned_ratio = raw.get("aspect_ratio")
    if returned_ratio is not None and returned_ratio != aspect_ratio:
        raise IdeogramPromptError(
            f"Magic Prompt returned aspect ratio {returned_ratio!r}, expected {aspect_ratio!r}"
        )
    high_level = raw.get("high_level_description")
    composition = raw.get("compositional_deconstruction")
    if not isinstance(high_level, str) or not isinstance(composition, Mapping):
        raise IdeogramPromptError("Magic Prompt omitted required caption fields")
    background = composition.get("background")
    elements = composition.get("elements")
    if not isinstance(background, str) or not isinstance(elements, list):
        raise IdeogramPromptError("Magic Prompt returned an invalid composition")
    restored = _restore_protected_text(
        [dict(item) if isinstance(item, Mapping) else item for item in elements],
        protected,
    )
    style_data = raw.get("style_description")
    style = (
        IdeogramStyle.model_validate(style_data)
        if style_data is not None
        else _infer_style(prompt, high_level)
    )
    return IdeogramPrompt.model_validate({
        "high_level_description": high_level,
        "style_description": style.model_dump(exclude_none=True) if style else None,
        "compositional_deconstruction": {
            "background": background,
            "elements": restored,
        },
    })


def _fallback_prompt(prompt: str, protected: ProtectedPrompt) -> IdeogramPrompt:
    # Literal characters belong only in dedicated text elements. This also
    # prevents a malformed LLM response from leaving a rewritten spelling in
    # prose while the exact repair element is appended separately.
    chunks: list[str] = []
    cursor = 0
    for start, end, _literal in _quote_spans(prompt):
        quote_start = start - 1 if start > cursor else start
        quote_end = end + 1 if end < len(prompt) else end
        chunks.extend((prompt[cursor:quote_start], "the requested lettering"))
        cursor = quote_end
    chunks.append(prompt[cursor:])
    description = "".join(chunks).strip().rstrip(".") or "A carefully composed image"
    elements: list[dict[str, Any]] = []
    for index, literal in enumerate(protected.literals):
        elements.append({
            "type": "text",
            "bbox": _text_box(index, len(protected.literals)),
            "text": literal,
            "desc": "Clear, legible typography preserving the supplied text exactly.",
        })
    elements.append({
        "type": "obj",
        "bbox": [270, 130, 790, 870] if protected.literals else None,
        "desc": description,
    })
    style = _infer_style(prompt, description)
    return IdeogramPrompt.model_validate({
        "high_level_description": f"{description}.",
        "style_description": style.model_dump(exclude_none=True) if style else None,
        "compositional_deconstruction": {
            "background": "The environment and atmosphere specified by the user idea.",
            "elements": elements,
        },
    })


def import_kjnodes_ideogram_json(data: Any) -> IdeogramPrompt:
    """Load default KJNodes native JSON; no coordinate translation is needed."""

    parsed = _parse_json_with_one_repair(data) if isinstance(data, str) else data
    try:
        return IdeogramPrompt.model_validate(parsed)
    except Exception as exc:
        raise IdeogramPromptError(f"invalid KJNodes/Ideogram caption: {exc}") from exc


def export_kjnodes_ideogram_json(prompt: IdeogramPrompt | Mapping[str, Any]) -> dict[str, Any]:
    """Return canonical JSON accepted by KJNodes' default import_json input."""

    model = prompt if isinstance(prompt, IdeogramPrompt) else import_kjnodes_ideogram_json(prompt)
    return model.canonical_dict()


def validate_ideogram_prompt_json(payload: Any) -> dict[str, Any]:
    """Validate and return a canonical plain dictionary."""

    return import_kjnodes_ideogram_json(payload).canonical_dict()


def serialize_ideogram_prompt_json(payload: IdeogramPrompt | Mapping[str, Any]) -> str:
    model = payload if isinstance(payload, IdeogramPrompt) else import_kjnodes_ideogram_json(payload)
    return model.serialize()


def build_ideogram_v4_prompt(
    prompt: str | Mapping[str, Any] | None,
    mode: Literal["quick", "precise"] = "quick",
    aspect_ratio: str | None = None,
    precise_json: str | Mapping[str, Any] | None = None,
    llm: IdeogramLLM | Any | None = None,
    *,
    text_literals: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a canonical caption independently from image generation."""

    if mode not in {"quick", "precise"}:
        raise IdeogramPromptError("mode must be 'quick' or 'precise'")
    warnings: list[str] = []
    if mode == "precise":
        source = precise_json if precise_json is not None else prompt
        if source is None:
            raise IdeogramPromptError("precise mode requires precise_json or a JSON prompt")
        model = import_kjnodes_ideogram_json(source)
        protected_text = [
            element.text
            for element in model.compositional_deconstruction.elements
            if isinstance(element, IdeogramTextElement)
        ]
    else:
        if not isinstance(prompt, str) or not prompt.strip():
            raise IdeogramPromptError("quick mode requires a nonempty natural-language prompt")
        ratio = validate_aspect_ratio(aspect_ratio)
        protected = protect_exact_text(prompt, text_literals)
        model: IdeogramPrompt
        if llm is None:
            warnings.append("Local LLM unavailable; used deterministic Ideogram prompt fallback.")
            model = _fallback_prompt(prompt, protected)
        else:
            try:
                messages = build_magic_prompt_messages(protected.prompt, ratio)
                raw = _parse_json_with_one_repair(_invoke_llm(llm, messages))
                model = _normalize_magic_output(
                    raw,
                    prompt=prompt,
                    aspect_ratio=ratio,
                    protected=protected,
                )
            except Exception as exc:
                if (
                    isinstance(exc, BackendError)
                    and exc.code == BackendErrorCode.SERVER_NOT_RUNNING
                ):
                    warning = (
                        "Magic Prompt could not reach the local LLM after a retry; "
                        "used deterministic fallback"
                    )
                else:
                    warning = "Magic Prompt output was unusable; used deterministic fallback"
                warnings.append(f"{warning}: {type(exc).__name__}: {exc}")
                model = _fallback_prompt(prompt, protected)
        protected_text = list(protected.literals)
        final_text = [
            element.text
            for element in model.compositional_deconstruction.elements
            if isinstance(element, IdeogramTextElement)
        ]
        missing = [literal for literal in protected_text if literal not in final_text]
        if missing:
            raise IdeogramPromptError(f"exact-text restoration failed for: {missing!r}")
    structured = model.canonical_dict()
    return IdeogramPromptBuildResult(
        mode=mode,
        structured_prompt=structured,
        serialized_prompt=model.serialize(),
        protected_text=protected_text,
        warnings=warnings,
    ).model_dump(mode="python")


def preview_ideogram_prompt(
    prompt: IdeogramPrompt | Mapping[str, Any],
    *,
    mode: str,
    aspect_ratio: str | None = None,
) -> str:
    model = prompt if isinstance(prompt, IdeogramPrompt) else import_kjnodes_ideogram_json(prompt)
    lines = [f"Ideogram v4 Prompt Mode: {mode.title()}"]
    if aspect_ratio:
        lines.append(f"Aspect Ratio: {aspect_ratio}")
    if model.high_level_description is not None:
        lines.extend(("", "HIGH LEVEL", model.high_level_description))
    if model.style_description is not None:
        style = model.style_description
        lines.extend(("", "STYLE", f"Medium: {style.medium}", f"Aesthetics: {style.aesthetics}", f"Lighting: {style.lighting}"))
    for index, element in enumerate(model.compositional_deconstruction.elements, 1):
        lines.extend(("", f"ELEMENT {index} — {element.type.upper()}"))
        if isinstance(element, IdeogramTextElement):
            lines.append(f"Text: {json.dumps(element.text, ensure_ascii=False)}")
        else:
            lines.append(element.desc)
        if element.bbox is not None:
            lines.append(f"bbox: {json.dumps(element.bbox)}")
    return "\n".join(lines)
