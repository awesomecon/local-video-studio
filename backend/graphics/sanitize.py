"""Explicit allowlist sanitizer for static HTML/CSS/inline-SVG screens."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Iterable

from .models import GraphicScreenResponse

SANITIZER_VERSION = "graphic-screen-sanitizer-v1"
MAX_ELEMENTS = 400
MAX_DEPTH = 32
MAX_PATH_LENGTH = 12_000
MAX_CSS_DECLARATIONS = 500

_HTML_TAGS = {
    "main", "section", "div", "header", "footer", "h1", "h2", "h3", "p", "span",
    "strong", "em", "ul", "ol", "li", "br",
    "table", "thead", "tbody", "tr", "th", "td",
}
_SVG_TAGS = {
    "svg", "g", "defs", "lineargradient", "radialgradient", "stop", "rect", "circle",
    "ellipse", "line", "polyline", "polygon", "path", "text", "tspan",
}
_GLOBAL_ATTRS = {"class", "id", "role", "aria-label"}
_HTML_ATTRS = _GLOBAL_ATTRS | {"style", "colspan", "rowspan"}
_SVG_ATTRS = _GLOBAL_ATTRS | {
    "viewbox", "width", "height", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy",
    "r", "rx", "ry", "points", "d", "fill", "fill-opacity", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "opacity", "transform", "text-anchor",
    "font-family", "font-size", "font-weight", "letter-spacing", "dominant-baseline",
    "offset", "stop-color", "stop-opacity", "gradientunits", "gradienttransform",
}
_VOID = {"br"}
_FORBIDDEN_CSS = re.compile(
    r"(?:url\s*\(|@import|expression\s*\(|javascript\s*:|data\s*:|file\s*:|blob\s*:)",
    re.IGNORECASE,
)
_CSS_SELECTOR_PART = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9_-]*|\*)?(?:[.#][A-Za-z][A-Za-z0-9_-]*)*$"
)
_CSS_PROPERTY = re.compile(r"^[a-z][a-z0-9-]*$")
_CSS_LENGTH = re.compile(r"^(?:0|(?:\d+(?:\.\d+)?|\.\d+)(?:px|em|rem|%|vh|vw))$", re.IGNORECASE)
ALLOWED_CSS_PROPERTIES = {
    "align-content", "align-items", "align-self", "aspect-ratio", "background",
    "background-clip", "background-color", "background-image", "background-position",
    "background-repeat", "background-size", "border", "border-bottom",
    "border-bottom-color", "border-bottom-style", "border-bottom-width",
    "border-collapse", "border-color", "border-left", "border-left-color",
    "border-left-style", "border-left-width", "border-radius", "border-right",
    "border-right-color", "border-right-style", "border-right-width", "border-spacing",
    "border-style", "border-top", "border-top-color", "border-top-style",
    "border-top-width", "border-width", "box-sizing",
    "bottom", "box-shadow", "color", "column-gap", "display", "flex", "flex-basis",
    "flex-direction", "flex-grow", "flex-shrink", "flex-wrap", "font-family", "font-size",
    "font-style", "font-weight", "gap", "grid-auto-columns", "grid-auto-flow",
    "grid-auto-rows", "grid-column", "grid-column-end", "grid-column-start",
    "grid-row", "grid-row-end", "grid-row-start", "grid-template-columns",
    "grid-template-rows", "height", "justify-content", "justify-items", "justify-self",
    "left", "letter-spacing", "line-height", "margin", "margin-bottom", "margin-left",
    "margin-right", "margin-top", "max-height", "max-width", "min-height", "min-width",
    "object-fit", "fill", "opacity", "overflow", "overflow-x", "overflow-y",
    "overflow-wrap", "padding", "padding-bottom",
    "padding-left", "padding-right", "padding-top", "position", "right", "row-gap",
    "place-content", "place-items", "stroke", "stroke-dasharray", "stroke-dashoffset",
    "stroke-width", "text-align", "text-decoration", "text-shadow",
    "text-overflow", "text-transform", "top", "transform", "transform-origin",
    "vertical-align", "white-space", "width", "word-break", "word-spacing", "z-index",
}
_SAFE_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_SAFE_ID = _SAFE_CLASS
_WHITESPACE = re.compile(r"\s+")
_URL_FUNCTION = re.compile(r"url\s*\(", re.IGNORECASE)
_LOCAL_SVG_PAINT = re.compile(r"^url\(\s*#[A-Za-z][A-Za-z0-9_-]{0,79}\s*\)$", re.IGNORECASE)


class GraphicScreenValidationError(ValueError):
    """Structural diagnostics for repair passes and attempt logs.

    Safe to surface: element/attribute/property names, selector fragments, and
    mismatch positions. Never included: text content or model-authored source.
    """


def normalize_visible_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


class _AllowlistParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.text: list[str] = []
        self.stack: list[str] = []
        self.elements = 0

    def handle_decl(self, decl: str) -> None:
        raise GraphicScreenValidationError("document declarations are not allowed")

    def handle_comment(self, data: str) -> None:
        # Comments are intentionally dropped so they cannot become a covert source channel.
        return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        allowed = _HTML_TAGS | _SVG_TAGS
        if tag not in allowed:
            raise GraphicScreenValidationError(f"unsupported HTML or SVG element '{tag}'")
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            raise GraphicScreenValidationError("too many document elements")
        if len(self.stack) >= MAX_DEPTH:
            raise GraphicScreenValidationError("document nesting is too deep")
        if tag in _SVG_TAGS and tag != "svg" and "svg" not in self.stack:
            raise GraphicScreenValidationError("SVG content must be inside an svg element")
        if tag == "svg" and "svg" in self.stack:
            raise GraphicScreenValidationError("nested SVG elements are not allowed")
        allowed_attrs = _SVG_ATTRS if tag in _SVG_TAGS else _HTML_ATTRS
        rendered: list[str] = []
        for name, value in attrs:
            name = name.lower()
            value = "" if value is None else value
            if name.startswith("on") or name not in allowed_attrs:
                raise GraphicScreenValidationError(f"unsupported or active attribute '{name}'")
            if len(value) > 12_000:
                raise GraphicScreenValidationError("attribute value is too large")
            if name in {"class", "id"}:
                tokens = value.split()
                if not tokens or any(not (_SAFE_CLASS if name == "class" else _SAFE_ID).match(t) for t in tokens):
                    raise GraphicScreenValidationError(
                        f"invalid {'class' if name == 'class' else 'id'} attribute tokens"
                    )
            if name == "d" and len(value) > MAX_PATH_LENGTH:
                raise GraphicScreenValidationError("SVG path is too long")
            if _URL_FUNCTION.search(value) and not (
                name in {"fill", "stroke"} and _LOCAL_SVG_PAINT.fullmatch(value)
            ):
                raise GraphicScreenValidationError("external SVG references are not allowed")
            if name == "style":
                _validate_css(value, inline=True)
            rendered.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(rendered)}>")
        # Void elements never go on the stack: browsers treat them as
        # self-closing, and HTMLParser reports plain <br> without an end tag.
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID and (not self.stack or self.stack[-1] != tag):
            # Stray void close tag (e.g. "</br>"); browsers ignore it, so do we.
            return
        if not self.stack or self.stack[-1] != tag:
            raise GraphicScreenValidationError("malformed document nesting")
        self.stack.pop()
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.stack:
            if data.strip():
                raise GraphicScreenValidationError("text must be inside an allowed element")
            return
        if len(data) > 2_000:
            raise GraphicScreenValidationError("text node is too large")
        normalized = normalize_visible_text(data)
        if normalized:
            self.text.append(normalized)
        self.parts.append(html.escape(data))


def _validate_css_declaration(name: str, value: str) -> None:
    name = name.strip().lower()
    value = value.strip()
    if not _CSS_PROPERTY.fullmatch(name) or name not in ALLOWED_CSS_PROPERTIES:
        raise GraphicScreenValidationError(f"unsupported CSS property '{name[:60]}'")
    if not value or "!important" in value.lower():
        raise GraphicScreenValidationError("unsupported CSS value")
    lowered = _WHITESPACE.sub("", value).lower()
    if name == "display" and lowered == "none":
        raise GraphicScreenValidationError("CSS may not hide manifest text")
    if name == "opacity":
        try:
            if float(value) <= 0:
                raise GraphicScreenValidationError("CSS may not hide manifest text")
        except ValueError as exc:
            raise GraphicScreenValidationError("invalid CSS opacity") from exc
    if name == "font-size" and (_CSS_LENGTH.fullmatch(value) is None or value == "0"):
        raise GraphicScreenValidationError("CSS font size must be a positive bounded length")
    if name == "transform" and re.search(
        r"scale(?:x|y)?\(\s*(?:0+(?:\.0+)?|\.0+)\s*\)", value, re.IGNORECASE,
    ):
        raise GraphicScreenValidationError("CSS may not hide manifest text")
    if name in {"color", "background", "background-color"} and (
        "transparent" in lowered or re.search(r"rgba?\([^)]*,0(?:\.0+)?\)", lowered)
    ):
        raise GraphicScreenValidationError("transparent CSS colors are not allowed")


def _validate_css_declarations(css: str) -> int:
    count = 0
    for raw in css.split(";"):
        declaration = raw.strip()
        if not declaration:
            continue
        if ":" not in declaration:
            raise GraphicScreenValidationError(
                f"malformed CSS declaration '{declaration[:60]}'"
            )
        name, value = declaration.split(":", 1)
        _validate_css_declaration(name, value)
        count += 1
    return count


def _validate_selector(selector: str) -> None:
    for branch in selector.split(","):
        parts = branch.strip().split()
        if not parts or any(_CSS_SELECTOR_PART.fullmatch(part) is None for part in parts):
            raise GraphicScreenValidationError(
                f"unsupported CSS selector '{branch.strip()[:120]}'"
            )


def _validate_css(css: str, *, inline: bool = False) -> None:
    if len(css) > 40_000:
        raise GraphicScreenValidationError("CSS source is too large")
    # CSS escapes and comments are decoded/removed by the browser before tokenization. Reject them
    # outright so forbidden properties such as `content` cannot be disguised from validation.
    if "\\" in css or "/*" in css or "*/" in css or any(ord(char) < 32 and char not in "\n\r\t" for char in css):
        raise GraphicScreenValidationError("CSS escapes, comments, and controls are not allowed")
    if _FORBIDDEN_CSS.search(css):
        raise GraphicScreenValidationError("active or external CSS is not allowed")
    if inline:
        if "{" in css or "}" in css:
            raise GraphicScreenValidationError("inline CSS must contain declarations only")
        declarations = _validate_css_declarations(css)
    else:
        declarations = 0
        position = 0
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            if css[position:match.start()].strip():
                raise GraphicScreenValidationError("malformed CSS")
            _validate_selector(match.group(1))
            declarations += _validate_css_declarations(match.group(2))
            position = match.end()
        if css[position:].strip():
            raise GraphicScreenValidationError("malformed CSS")
    if declarations > MAX_CSS_DECLARATIONS:
        raise GraphicScreenValidationError("too many CSS declarations")


def _owned_document(body: str, css: str, width: int, height: int) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'">
<style>
* {{ box-sizing: border-box; }} html, body {{ margin: 0; width: {width}px; height: {height}px; overflow: hidden; background: #10141c; color: #f8fafc; font-family: 'Noto Sans', sans-serif; }}
ul, ol {{ list-style: none; margin: 0; padding: 0; }}
body > main {{ width: {width}px; height: {height}px; }}
{css}
</style></head><body>{body}</body></html>"""


def sanitize_graphic_screen(
    response: GraphicScreenResponse, *, width: int, height: int,
) -> tuple[str, list[str]]:
    """Validate, normalize, and reserialize a response into a CSP-owned document."""

    if width <= 0 or height <= 0:
        raise GraphicScreenValidationError("invalid project resolution")
    _validate_css(response.css)
    parser = _AllowlistParser()
    try:
        parser.feed(response.html_body)
        parser.close()
    except GraphicScreenValidationError:
        raise
    except Exception as exc:
        raise GraphicScreenValidationError("invalid HTML input") from exc
    if parser.stack:
        raise GraphicScreenValidationError("unclosed document elements")
    expected = [normalize_visible_text(item) for item in response.visible_text]
    if any(not item for item in expected):
        raise GraphicScreenValidationError("visible text contains an empty string")
    if parser.text != expected:
        index = next(
            (at for at, pair in enumerate(zip(parser.text, expected)) if pair[0] != pair[1]),
            min(len(parser.text), len(expected)),
        )
        # Structural diagnostics only: counts and the first diverging index,
        # never model-authored text.
        raise GraphicScreenValidationError(
            "visible text does not exactly match the manifest "
            f"({len(parser.text)} DOM text nodes vs {len(expected)} manifest entries; "
            f"first mismatch at index {index})"
        )
    return _owned_document("".join(parser.parts), response.css, width, height), expected
