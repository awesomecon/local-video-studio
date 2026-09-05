"""Trusted HTML compiler and exact-time Chromium renderer for Editorial Mode."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable, Sequence
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Iterator

from backend.graphics.browser import discover_chromium
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.process import run_media_process_stream

from .models import (
    EditPlan, EditorialAsset, EditorialComposition, EditorialElement, EvidenceClass,
    EditorialTemplate,
)
from .passages import locate_passage


AssetURLResolver = Callable[[EditorialAsset], str | None]
EDITORIAL_STYLE_ID = "archiveDossier"
_FONT_ROOT = Path(__file__).with_name("fonts")
_FONT_FILES = (
    ("LVS Noto Sans", "NotoSans-Regular.ttf", 400),
    ("LVS Noto Sans", "NotoSans-Bold.ttf", 700),
    ("LVS Noto Serif", "NotoSerif-Regular.ttf", 400),
    ("LVS Noto Serif", "NotoSerif-Bold.ttf", 700),
)


@lru_cache(maxsize=1)
def editorial_font_manifest() -> tuple[dict[str, str | int], ...]:
    """Return the immutable identity of the renderer's bundled font files."""
    entries: list[dict[str, str | int]] = []
    for family, filename, weight in _FONT_FILES:
        payload = (_FONT_ROOT / filename).read_bytes()
        entries.append({
            "family": family,
            "file": filename,
            "weight": weight,
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    return tuple(entries)


def _font_bundle_digest() -> str:
    payload = "\n".join(
        f"{entry['file']}:{entry['sha256']}" for entry in editorial_font_manifest()
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


EDITORIAL_FONT_BUNDLE_SHA256 = _font_bundle_digest()
EDITORIAL_RENDER_WORKFLOW_VERSION = (
    f"editorial-archive-dossier-v16-{EDITORIAL_FONT_BUNDLE_SHA256[:12]}"
)


@lru_cache(maxsize=1)
def _font_face_css() -> str:
    rules: list[str] = []
    for family, filename, weight in _FONT_FILES:
        encoded = base64.b64encode((_FONT_ROOT / filename).read_bytes()).decode("ascii")
        rules.append(
            "@font-face{"
            f'font-family:"{family}";'
            f'src:url("data:font/ttf;base64,{encoded}") format("truetype");'
            f"font-style:normal;font-weight:{weight};font-display:block"
            "}"
        )
    return "".join(rules)


def _script_json(value: Any) -> str:
    """Serialize validated data without allowing a value to close the script tag."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _element(composition: EditorialComposition, role: str) -> EditorialElement | None:
    """Resolve a deterministic template slot without prescribing LLM-authored ids."""
    return next(
        (item for item in composition.elements if item.role == role),
        next((item for item in composition.elements if item.id == role), None),
    )


def _asset(composition: EditorialComposition, element: EditorialElement | None) -> EditorialAsset | None:
    if element is None or element.asset_id is None:
        return None
    return next((item for item in composition.assets if item.id == element.asset_id), None)


def _asset_image(
    composition: EditorialComposition,
    element: EditorialElement | None,
    resolver: AssetURLResolver | None,
    *,
    class_name: str,
) -> str:
    asset = _asset(composition, element)
    source = resolver(asset) if asset is not None and resolver is not None else None
    if not source:
        return ""
    evidence_class = (
        " evidence-image"
        if asset is not None and asset.evidence_class is EvidenceClass.EVIDENCE
        else ""
    )
    return (
        f'<img class="asset-image {escape(class_name)}{evidence_class}" '
        f'src="{escape(source, quote=True)}" alt="">'
    )


def _source_caption(asset: EditorialAsset | None) -> str:
    """Render only a meaningful label attached to protected factual media."""
    if (
        asset is None
        or asset.evidence_class is not EvidenceClass.EVIDENCE
        or not asset.label.strip()
    ):
        return ""
    return (
        f'<div class="source-caption fit-text editorial-type" '
        f'{_fit_attrs(minimum=10, maximum=20, height=48, lines=2)}>'
        f'{escape(asset.label.strip())}</div>'
    )


def _passage_media(
    composition: EditorialComposition,
    document: EditorialElement | None,
    mark: EditorialElement | None,
    resolver: AssetURLResolver | None,
) -> tuple[str, str]:
    asset = _asset(composition, document)
    source = resolver(asset) if asset is not None and resolver else None
    # A mark must name the actual source words. Legacy plans with no target
    # retain their document, but no longer underline an invented position.
    quote = mark.text.strip() if mark else ""
    if not quote and document and composition.template is EditorialTemplate.DOCUMENT_REVEAL:
        quote = document.text.strip()
    located = locate_passage(source, quote) if source and mark and quote else None
    boxes = ()
    if located:
        source, boxes = located
    image = _asset_image(composition, document, lambda _: source, class_name="document-image")
    if not boxes or mark is None:
        return image, ""
    image += (
        '<blockquote class="passage-quote editorial-type">'
        '<span class="passage-quote-label">SOURCE EXCERPT</span>'
        f'<span class="passage-quote-text fit-text" '
        f'{_fit_attrs(minimum=32, maximum=54, height=320, lines=6)}>'
        f'“{escape(quote)}”</span></blockquote>'
    )
    spans = "".join(
        '<span class="verified-word-line" data-box="'
        + ",".join(f"{value:.8f}" for value in box) + '"></span>'
        for box in boxes
    )
    return image, (
        f'<div id="{escape(mark.id, quote=True)}" '
        f'class="verified-passage draw editorial-element">{spans}</div>'
    )


def _portrait_fallback() -> str:
    return (
        '<div class="photo-art preview-placeholder" aria-hidden="true">'
        '<div class="portrait-head"></div><div class="portrait-body"></div></div>'
    )


def _fit_attrs(
    *,
    minimum: int,
    maximum: int,
    height: int,
    lines: int,
    landscape_minimum: int | None = None,
    landscape_maximum: int | None = None,
    landscape_height: int | None = None,
    landscape_lines: int | None = None,
) -> str:
    """Compile trusted fitting bounds consumed by the renderer runtime."""
    return (
        f'data-fit-min="{minimum}" data-fit-max="{maximum}" '
        f'data-fit-height="{height}" data-fit-lines="{lines}" '
        f'data-fit-landscape-min="{landscape_minimum or minimum}" '
        f'data-fit-landscape-max="{landscape_maximum or maximum}" '
        f'data-fit-landscape-height="{landscape_height or height}" '
        f'data-fit-landscape-lines="{landscape_lines or lines}"'
    )


def validate_export_assets(plan: EditPlan, asset_root: Path | None) -> None:
    """Reject unresolved authored media before a production render starts."""
    failures: list[str] = []
    for composition in plan.compositions:
        by_id = {asset.id: asset for asset in composition.assets}
        for element in composition.elements:
            if not element.asset_id:
                continue
            asset = by_id.get(element.asset_id)
            if asset is None:
                # The domain model normally catches this; keep the export gate
                # defensive because it is the last boundary before rendering.
                failures.append(
                    f"{composition.id}/{element.role}: unknown asset {element.asset_id}"
                )
                continue
            if asset.source and asset.source.startswith(("http://", "https://")):
                failures.append(
                    f"{composition.id}/{element.role}: remote media is not exportable"
                )
                continue
            if asset_root is None or EditorialRenderer._data_asset_url(asset_root, asset) is None:
                failures.append(
                    f"{composition.id}/{element.role}: local media is missing or unreadable"
                )
    if failures:
        raise ValueError(
            "Editorial export requires resolved local media; preview placeholders cannot be "
            "exported: " + "; ".join(failures)
        )


def _archive_markup(
    composition: EditorialComposition,
    resolver: AssetURLResolver | None,
) -> str:
    year = _element(composition, "year")
    photo = _element(composition, "archive-photo")
    document = _element(composition, "paper")
    passage = _element(composition, "document-mark")
    rulers = _element(composition, "ruler-grid")
    reveal = _element(composition, "reveal")
    photo_asset = _asset(composition, photo)
    document_asset = _asset(composition, document)
    document_copy = (
        document_asset.label
        if document_asset and document_asset.label
        else "Documentary source material arranged on the editorial canvas."
    )
    ruler_count = rulers.count if rulers else 10
    document_image, passage_markup = _passage_media(composition, document, passage, resolver)
    nodes = "".join(
        f'<div class="ruler-node" data-ruler="{index}"><span>{index + 1:02d}</span>'
        f'<b class="ruler-chief">CHIEF</b></div>'
        for index in range(ruler_count)
    )
    return f"""
      <section class="composition archive-canvas" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="{escape(year.id if year else 'year', quote=True)}" class="year fit-text editorial-element editorial-type" {_fit_attrs(minimum=72, maximum=250, height=250, lines=1, landscape_minimum=56, landscape_maximum=154, landscape_height=160)}>{escape(year.text if year else "")}</div>
          <div id="{escape(photo.id if photo else 'photo', quote=True)}" class="archive-photo editorial-element" data-rest-rotate="-2.4">
            {_asset_image(composition, photo, resolver, class_name="archive-photo-image")}
            {_portrait_fallback()}
            {_source_caption(photo_asset)}
          </div>
          <div id="{escape(document.id if document else 'document', quote=True)}" class="document editorial-element" data-rest-rotate="2">
            {document_image}
            <h2 id="{escape((document.id if document else 'document') + '-title', quote=True)}" class="fit-text editorial-type" {_fit_attrs(minimum=24, maximum=62, height=150, lines=2, landscape_minimum=24, landscape_maximum=62, landscape_height=140)}>{escape(document.text if document and document.text else "DOCUMENT")}</h2>
            <div class="rule"></div>
            <p id="{escape((document.id if document else 'document') + '-copy', quote=True)}" class="document-copy fit-text editorial-type" {_fit_attrs(minimum=18, maximum=27, height=220, lines=5)}>{escape(document_copy)}</p>
            {passage_markup}
          </div>
          <div id="{escape(rulers.id if rulers else 'rulers', quote=True)}" class="ruler-grid editorial-element">{nodes}</div>
        </div>
        <div class="blackout"></div>
        <div id="{escape(reveal.id if reveal else 'reveal', quote=True)}" class="elon editorial-element editorial-type"><span class="fit-text" {_fit_attrs(minimum=50, maximum=170, height=240, lines=1, landscape_minimum=60, landscape_maximum=230, landscape_height=280)}>{escape(reveal.text if reveal else "")}</span></div>
      </section>
    """


def _document_reveal_markup(
    composition: EditorialComposition,
    resolver: AssetURLResolver | None,
) -> str:
    document = _element(composition, "document")
    title = _element(composition, "title")
    mark = _element(composition, "passage-mark")
    annotation = _element(composition, "annotation")
    context = _element(composition, "context-image")
    connector = _element(composition, "connector")
    document_asset = _asset(composition, document)
    sheet_class = "source-sheet"
    section_class = "composition document-reveal"
    section_style = ""
    display_mode = document_asset.metadata.get("display_mode") if document_asset else None
    if display_mode == "cover":
        sheet_class += " source-sheet-cover"
    elif display_mode == "strip":
        try:
            aspect_ratio = float(document_asset.metadata.get("display_aspect_ratio", 0))
        except (TypeError, ValueError):
            aspect_ratio = 0
        if 1.0 <= aspect_ratio <= 8.0:
            sheet_class += " source-sheet-strip"
            section_class += " document-reveal-strip"
            try:
                passage_position = float(
                    document_asset.metadata.get("passage_mark_position", 0.45)
                )
            except (TypeError, ValueError):
                passage_position = 0.45
            passage_position = min(max(passage_position, 0.1), 0.9)
            section_style = (
                f' style="--source-sheet-aspect:{aspect_ratio:.6f};'
                f'--passage-mark-position:{passage_position * 100:.2f}%"'
            )
    document_text = document.text.strip() if document and document.text else ""
    document_copy_tag = (
        f'<p class="document-copy fit-text editorial-type" '
        f'{_fit_attrs(minimum=20, maximum=30, height=650, lines=10, landscape_minimum=18, landscape_maximum=30, landscape_height=650)}>'
        f'{escape(document_text)}</p>'
        if document_text
        else ""
    )
    context_asset = _asset(composition, context)
    document_image, passage_markup = _passage_media(composition, document, mark, resolver)
    return f"""
      <section class="{section_class}" data-composition="{escape(composition.id, quote=True)}"{section_style}>
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="{escape(title.id if title else 'title', quote=True)}" class="document-title fit-text editorial-element editorial-type" {_fit_attrs(minimum=46, maximum=104, height=230, lines=3, landscape_minimum=38, landscape_maximum=82, landscape_height=120, landscape_lines=2)}>{escape(title.text if title else "")}</div>
          <div id="{escape(document.id if document else 'document', quote=True)}" class="{sheet_class} editorial-element" data-rest-rotate="1">
            {document_image}
            {document_copy_tag}
            {passage_markup}
          </div>
          <div id="{escape(connector.id if connector else 'connector', quote=True)}" class="connector-line draw editorial-element"></div>
          <div id="{escape(annotation.id if annotation else 'annotation', quote=True)}" class="annotation fit-text editorial-element editorial-type" {_fit_attrs(minimum=19, maximum=28, height=230, lines=5, landscape_minimum=18, landscape_maximum=28, landscape_height=170)}>{escape(annotation.text if annotation else "")}</div>
          <div id="{escape(context.id if context else 'context', quote=True)}" class="context-photo editorial-element" data-rest-rotate="-1.6">
            {_asset_image(composition, context, resolver, class_name="context-photo-image")}
            {_portrait_fallback()}
            {_source_caption(context_asset)}
          </div>
        </div>
        <div class="blackout"></div>
      </section>
    """


def _comparison_markup(
    composition: EditorialComposition,
    resolver: AssetURLResolver | None,
) -> str:
    headline = _element(composition, "headline")
    left_image = _element(composition, "left-image")
    right_image = _element(composition, "right-image")
    left_label = _element(composition, "left-label")
    right_label = _element(composition, "right-label")
    divider = _element(composition, "divider")
    left_asset = _asset(composition, left_image)
    right_asset = _asset(composition, right_image)
    return f"""
      <section class="composition comparison-canvas" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="{escape(headline.id if headline else 'headline', quote=True)}" class="comparison-headline fit-text editorial-element editorial-type" {_fit_attrs(minimum=44, maximum=100, height=280, lines=3, landscape_minimum=36, landscape_maximum=76, landscape_height=135, landscape_lines=2)}>{escape(headline.text if headline else "")}</div>
          <div id="{escape(left_image.id if left_image else 'left-image', quote=True)}" class="comparison-card left-card editorial-element" data-rest-rotate="-1.2">
            {_asset_image(composition, left_image, resolver, class_name="comparison-left-image")}
            {_portrait_fallback()}
            {_source_caption(left_asset)}
          </div>
          <div id="{escape(right_image.id if right_image else 'right-image', quote=True)}" class="comparison-card right-card editorial-element" data-rest-rotate="1.2">
            {_asset_image(composition, right_image, resolver, class_name="comparison-right-image")}
            {_portrait_fallback()}
            {_source_caption(right_asset)}
          </div>
          <div id="{escape(left_label.id if left_label else 'left-label', quote=True)}" class="comparison-label left-label fit-text editorial-element editorial-type" {_fit_attrs(minimum=23, maximum=40, height=100, lines=2, landscape_minimum=20, landscape_maximum=32, landscape_height=70)}>{escape(left_label.text if left_label else "")}</div>
          <div id="{escape(right_label.id if right_label else 'right-label', quote=True)}" class="comparison-label right-label fit-text editorial-element editorial-type" {_fit_attrs(minimum=23, maximum=40, height=100, lines=2, landscape_minimum=20, landscape_maximum=32, landscape_height=70)}>{escape(right_label.text if right_label else "")}</div>
          <div id="{escape(divider.id if divider else 'divider', quote=True)}" class="divider-line draw editorial-element" data-draw-axis="y"></div>
        </div>
        <div class="blackout"></div>
      </section>
    """


def _illustration_markup(
    composition: EditorialComposition,
    resolver: AssetURLResolver | None,
) -> str:
    illustration = _element(composition, "illustration")
    headline = _element(composition, "headline")
    supporting = _element(composition, "supporting-text")
    rule = _element(composition, "technical-line")
    return f"""
      <section class="composition illustration-canvas" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="{escape(illustration.id if illustration else 'illustration', quote=True)}" class="illustration-frame editorial-element">
            {_asset_image(composition, illustration, resolver, class_name="illustration-image")}
            <div class="illustration-art preview-placeholder" aria-hidden="true"></div>
          </div>
          <div id="{escape(rule.id if rule else 'technical-line', quote=True)}" class="technical-rule draw editorial-element"></div>
          <div id="{escape(headline.id if headline else 'headline', quote=True)}" class="illustration-headline fit-text editorial-element editorial-type" {_fit_attrs(minimum=42, maximum=92, height=145, lines=3, landscape_minimum=34, landscape_maximum=82, landscape_height=175)}>{escape(headline.text if headline else "")}</div>
          <div id="{escape(supporting.id if supporting else 'supporting-text', quote=True)}" class="supporting-copy fit-text editorial-element editorial-type" {_fit_attrs(minimum=18, maximum=28, height=250, lines=5, landscape_minimum=18, landscape_maximum=28, landscape_height=300)}>{escape(supporting.text if supporting else "")}</div>
        </div>
        <div class="blackout"></div>
      </section>
    """


def _big_text_markup(
    composition: EditorialComposition,
    resolver: AssetURLResolver | None,
) -> str:
    headline = _element(composition, "headline")
    kicker = _element(composition, "kicker")
    cta = _element(composition, "cta")
    blackout = _element(composition, "blackout")
    if cta is not None:
        cta_tag = (
            f'<div id="{escape(cta.id, quote=True)}" '
            f'class="big-cta editorial-element editorial-type">'
            f'<span id="{escape(cta.id + "-text", quote=True)}" class="fit-text" '
            f'{_fit_attrs(minimum=22, maximum=44, height=110, lines=2, landscape_minimum=20, landscape_maximum=36, landscape_height=80)}>'
            f"{escape(cta.text)}</span></div>"
        )
    else:
        cta_tag = ""
    return f"""
      <section class="composition big-text-reveal" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="kicker-rule"></div>
          <div id="{escape(kicker.id if kicker else 'kicker', quote=True)}" class="big-kicker fit-text editorial-element editorial-type" {_fit_attrs(minimum=20, maximum=34, height=82, lines=2, landscape_minimum=18, landscape_maximum=34, landscape_height=62)}>{escape(kicker.text if kicker else "")}</div>
          <div id="{escape(headline.id if headline else 'headline', quote=True)}" class="big-headline fit-text editorial-element editorial-type" {_fit_attrs(minimum=50, maximum=250, height=570, lines=4, landscape_minimum=42, landscape_maximum=230, landscape_height=465)}>{escape(headline.text if headline else "")}</div>
          {cta_tag}
        </div>
        <div id="{escape(blackout.id if blackout else 'blackout', quote=True)}" class="blackout editorial-element"></div>
      </section>
    """


_TEMPLATE_MARKUP: dict[EditorialTemplate, Callable[[EditorialComposition, AssetURLResolver | None], str]] = {
    EditorialTemplate.ARCHIVE_CANVAS: _archive_markup,
    EditorialTemplate.DOCUMENT_REVEAL: _document_reveal_markup,
    EditorialTemplate.COMPARISON_CANVAS: _comparison_markup,
    EditorialTemplate.ILLUSTRATION_CANVAS: _illustration_markup,
    EditorialTemplate.BIG_TEXT_REVEAL: _big_text_markup,
}


def compile_edit_plan_html(
    plan: EditPlan,
    *,
    asset_url_resolver: AssetURLResolver | None = None,
    asset_root: Path | None = None,
    captions: Sequence[dict[str, Any]] = (),
    show_placeholders: bool = True,
) -> str:
    """Compile a validated plan into trusted, self-contained preview/render HTML.

    Every approved template renders from renderer-owned layout; plan content only
    supplies escaped text, resolved asset URLs, and element ids. ``captions``
    carries pre-computed caption beats (global or clip-local clock, matching the
    plan's composition clock) with renderer-assigned design-space positions;
    when empty the composition carries no caption layer.
    """
    unimplemented = sorted({
        item.template.value
        for item in plan.compositions
        if item.template not in _TEMPLATE_MARKUP
    })
    if unimplemented:
        raise ValueError(
            f"the deterministic renderer does not implement template: {', '.join(unimplemented)}"
        )
    if asset_root is not None:
        fallback_resolver = asset_url_resolver
        asset_url_resolver = lambda asset: (
            EditorialRenderer._data_asset_url(asset_root, asset)
            or (fallback_resolver(asset) if fallback_resolver else None)
        )
    markup = "".join(
        _TEMPLATE_MARKUP[item.template](item, asset_url_resolver)
        for item in plan.compositions
    )
    payload = _script_json(plan.model_dump(mode="json"))
    captions_payload = _script_json(list(captions))
    font_manifest_payload = _script_json(editorial_font_manifest())
    landscape = plan.width >= plan.height
    design_width, design_height = ((1920, 1080) if landscape else (1080, 1920))
    design_scale = min(plan.width / design_width, plan.height / design_height)
    stage_left = (plan.width - design_width * design_scale) / 2
    stage_top = (plan.height - design_height * design_scale) / 2
    orientation = "landscape" if landscape else "portrait"
    text_class = "editorial-text-enabled" if plan.editorial_text_enabled else "editorial-text-disabled"
    caption_class = f" caption-style-{plan.caption_style.value}" if captions else ""
    placeholder_class = "preview-placeholders" if show_placeholders else "production-render"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="lvs-editorial-style" content="{EDITORIAL_STYLE_ID}">
<meta name="lvs-editorial-font-bundle-sha256" content="{EDITORIAL_FONT_BUNDLE_SHA256}">
<style>
{_font_face_css()}
:root{{--charcoal:#111315;--charcoal-2:#1b1d1f;--ivory:#e9dfc6;--rust:#b9532f;--blue:#6f91a6;--ink:#25211d}}
*{{box-sizing:border-box}} html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#000}}
body{{font-family:"LVS Noto Sans",sans-serif}}
#stage{{position:absolute;left:{stage_left:.4f}px;top:{stage_top:.4f}px;width:{design_width}px;height:{design_height}px;overflow:hidden;background:#000;transform:scale({design_scale:.8f});transform-origin:top left}}
.composition{{position:absolute;inset:0;display:none;overflow:hidden;background:var(--charcoal);color:var(--ivory)}}
.research-layer{{position:absolute;inset:0;transform-origin:50% 50%}}
.fit-text{{overflow-wrap:normal;word-break:normal}}
.production-render .preview-placeholder{{display:none!important}}
.grain{{position:absolute;inset:0;z-index:80;pointer-events:none;opacity:.11;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.82' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.32'/%3E%3C/svg%3E")}}
.technical-line{{position:absolute;background:var(--blue);opacity:.28}} .line-a{{left:74px;top:120px;width:1px;height:1640px}} .line-b{{left:74px;right:74px;top:1704px;height:1px}}
.blackout{{position:absolute;inset:0;z-index:100;background:#050505;opacity:0}}
/* archiveCanvas */
.year{{position:absolute;left:72px;right:72px;top:105px;font-size:250px;font-weight:900;line-height:.84;letter-spacing:-10px;color:var(--ivory);text-shadow:0 8px 28px #0008}}
.archive-photo{{position:absolute;left:76px;top:420px;width:610px;height:610px;padding:18px;background:#c9bea4;box-shadow:0 30px 80px #0008;transform:rotate(-2.4deg)}}
.photo-art{{position:absolute;overflow:hidden;background:radial-gradient(circle at 50% 27%,#a59c88 0 12%,transparent 13%),linear-gradient(145deg,#625f57,#b3aa97 46%,#494943)}}
.archive-photo .photo-art{{inset:18px 18px 72px}}
.context-photo .photo-art,.comparison-card .photo-art{{inset:16px 16px 64px}}
.asset-image{{display:block;object-fit:cover}} .archive-photo-image{{position:absolute;inset:18px 18px 72px;width:calc(100% - 36px);height:520px;z-index:2;filter:grayscale(.76) sepia(.22) contrast(1.04)}}
.archive-photo:has(.archive-photo-image) .photo-art{{visibility:hidden}}
.portrait-head{{position:absolute;left:220px;top:100px;width:130px;height:165px;border-radius:48% 48% 42% 42%;background:#353735;box-shadow:24px 5px 0 #77756c}}
.portrait-body{{position:absolute;left:120px;top:245px;width:360px;height:340px;border-radius:48% 48% 0 0;background:#292c2c}}
.source-caption{{position:absolute;left:16px;right:16px;bottom:12px;color:#3b3731;font-size:20px;line-height:1.15;letter-spacing:1px;text-align:center}}
.document{{position:absolute;right:54px;top:630px;width:590px;height:760px;padding:64px 56px;overflow:hidden;background:var(--ivory);color:var(--ink);box-shadow:0 35px 95px #000b;transform:rotate(2deg)}}
.document-image{{position:absolute;inset:0;width:100%;height:100%;opacity:.2;filter:sepia(.35) contrast(.85)}}
.document-image.evidence-image{{object-fit:contain;opacity:.96;filter:sepia(.08) contrast(1.02);background:#e5ddca}}
.document:has(.document-image.evidence-image)>h2,.document:has(.document-image.evidence-image)>.rule,.document:has(.document-image.evidence-image)>.document-copy,.document:has(.document-image.evidence-image)>p{{display:none}}
.document>*:not(.document-image):not(.passage):not(.paper-stamp):not(.verified-passage):not(.passage-quote){{position:relative;z-index:2}}
.paper-index{{font:18px monospace;letter-spacing:2px;color:#6e675b}} .document h2{{margin:95px 0 6px;font:700 62px/1 "LVS Noto Serif",serif;letter-spacing:1px}} .document>p{{margin:0;font:22px "LVS Noto Serif",serif;letter-spacing:3px}}
.document .rule{{height:2px;background:#2b2925;margin:24px 0 42px}} .document .document-copy{{font:27px/1.65 "LVS Noto Serif",serif;letter-spacing:0}}
.passage{{position:absolute;left:54px;right:54px;top:394px;height:8px;border-bottom:5px solid var(--rust);background:transparent;transform-origin:left center}}
.paper-stamp{{position:absolute;right:38px;bottom:40px;padding:10px 16px;border:4px solid #9f4429;color:#9f4429;font-weight:800;letter-spacing:3px;transform:rotate(-8deg);opacity:.82}}
.ruler-grid{{position:absolute;left:82px;right:72px;bottom:170px;display:grid;grid-template-columns:repeat(5,1fr);gap:24px}}
.ruler-node{{height:86px;border:1px solid #6f91a6a8;position:relative;color:#c3d6e2;background:#212d38}}
.ruler-node:before{{content:"";position:absolute;left:12px;right:12px;top:50%;height:1px;background:#6f91a6a8}} .ruler-node span{{position:absolute;right:10px;top:8px;font:17px monospace}}
.ruler-node .ruler-chief{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:800;letter-spacing:5px;color:#241f19;opacity:0}}
.ruler-node.chief{{border-color:var(--rust);background:#f0e5c9;color:#241f19;box-shadow:0 20px 48px #000c,inset 0 0 0 2px #b9532f}}
.ruler-node.focus{{border-color:var(--rust);background:#4e271d;color:#ffd8bd;box-shadow:inset 0 0 0 3px #b9532f}}
.elon{{position:absolute;z-index:110;inset:0;display:flex;align-items:center;justify-content:center;padding:0 48px;text-align:center;white-space:nowrap;font-size:170px;font-weight:900;line-height:.92;letter-spacing:6px;color:var(--ivory);opacity:0}}
.elon span{{display:block;width:100%}}
/* documentReveal */
.document-title{{position:absolute;left:72px;right:72px;top:96px;font-size:104px;font-weight:900;line-height:1.02;letter-spacing:2px;color:var(--ivory);text-shadow:0 8px 28px #0008}}
.source-sheet{{position:absolute;left:64px;right:64px;top:352px;height:872px;padding:64px 56px;overflow:hidden;background:var(--ivory);color:var(--ink);box-shadow:0 35px 95px #000b;transform:rotate(1deg)}}
.source-sheet .document-image{{opacity:.18}}
.source-sheet .document-image.evidence-image{{object-fit:contain;opacity:.98;filter:contrast(1.03);background:#eee9df}}
.source-sheet-cover{{padding:0;background:var(--charcoal-2)}}
.source-sheet-cover .document-image.evidence-image{{object-fit:cover;opacity:1;filter:contrast(1.03);background:var(--charcoal-2)}}
.document-reveal-strip{{--source-sheet-height:clamp(300px,calc(952px / var(--source-sheet-aspect)),640px)}}
.source-sheet-strip{{height:var(--source-sheet-height);padding:0}}
.source-sheet-strip .passage-mark{{top:var(--passage-mark-position)}}
.document-reveal-strip .connector-line{{top:calc(396px + var(--source-sheet-height))}}
.document-reveal-strip .annotation{{top:calc(436px + var(--source-sheet-height))}}
.document-reveal-strip .context-photo{{top:calc(416px + var(--source-sheet-height))}}
.source-sheet:has(.document-image.evidence-image)>.document-copy{{display:none}}
.source-sheet>*:not(.document-image):not(.passage-mark):not(.paper-stamp):not(.verified-passage):not(.passage-quote){{position:relative;z-index:2}}
.source-sheet .paper-index{{margin-top:0}} .source-sheet .document-copy{{font:30px/1.72 "LVS Noto Serif",serif;margin:44px 0 0;letter-spacing:0}}
.passage-mark{{position:absolute;left:56px;right:56px;top:196px;height:34px;border-bottom:8px solid var(--rust);background:#b9532f26;transform-origin:left center}}
.document>.verified-passage,.source-sheet>.verified-passage{{position:absolute;inset:0;z-index:3;pointer-events:none;transform-origin:left center}}
.verified-word-line{{position:absolute;background:#eabd3840;border-bottom:3px solid #a33b1e}}
.document:has(.verified-passage) .document-image,.source-sheet:has(.verified-passage) .document-image{{object-fit:contain;opacity:1;filter:none}}
.source-sheet:not(.source-sheet-strip):has(.verified-passage) .document-image{{height:45%}}
.passage-quote{{display:none}}
.source-sheet:not(.source-sheet-strip)>.passage-quote{{display:block;position:absolute;left:48px;right:48px;top:48%;margin:0;color:#241f19;font:700 50px/1.3 "LVS Noto Serif",serif;text-align:left}}
.passage-quote-label{{display:block;margin-bottom:20px;font:700 18px/1.3 "LVS Noto Sans",sans-serif;letter-spacing:3px;color:#6d4b31}}
.passage-quote-text{{display:block}}
.connector-line{{position:absolute;left:72px;top:1268px;width:520px;height:2px;background:var(--blue);transform-origin:left center}}
.annotation{{position:absolute;left:72px;top:1308px;width:500px;font:28px/1.6 "LVS Noto Serif",serif;color:var(--ivory);border-left:3px solid var(--rust);padding-left:26px}}
.context-photo{{position:absolute;right:72px;top:1288px;width:380px;height:520px;padding:16px;background:#c9bea4;box-shadow:0 30px 80px #0008;transform:rotate(-1.6deg)}}
.context-photo-image{{position:absolute;inset:16px 16px 64px;width:calc(100% - 32px);height:440px;z-index:2;filter:grayscale(.72) sepia(.2) contrast(1.03)}}
.context-photo:has(.context-photo-image) .photo-art{{visibility:hidden}}
/* comparisonCanvas */
.comparison-headline{{position:absolute;left:72px;right:72px;top:110px;font-size:100px;font-weight:900;line-height:1.05;letter-spacing:1px;color:var(--ivory);text-shadow:0 8px 28px #0008}}
.comparison-card{{position:absolute;top:420px;width:440px;height:560px;padding:16px;background:#c9bea4;box-shadow:0 30px 80px #0008}}
.left-card{{left:72px;transform:rotate(-1.2deg)}} .right-card{{right:72px;transform:rotate(1.2deg)}}
.comparison-left-image{{position:absolute;inset:16px 16px 64px;width:calc(100% - 32px);height:480px;z-index:2;filter:grayscale(.72) sepia(.18) contrast(1.03)}}
.comparison-right-image{{position:absolute;inset:16px 16px 64px;width:calc(100% - 32px);height:480px;z-index:2;filter:grayscale(.72) sepia(.18) contrast(1.03)}}
.comparison-card:has(.comparison-left-image) .photo-art,.comparison-card:has(.comparison-right-image) .photo-art{{visibility:hidden}}
.comparison-label{{position:absolute;top:1032px;width:440px;font-size:40px;font-weight:900;letter-spacing:2px;text-transform:uppercase;color:var(--ivory);border-top:3px solid var(--rust);padding-top:18px}}
.left-label{{left:72px}} .right-label{{right:72px}}
.divider-line{{position:absolute;left:539px;top:470px;width:2px;height:660px;background:var(--blue);transform-origin:top center}}
/* illustrationCanvas */
.illustration-frame{{position:absolute;left:72px;right:72px;top:300px;height:980px;padding:18px;background:var(--charcoal-2);border:1px solid #6f91a659;box-shadow:0 35px 95px #000b}}
.illustration-art{{position:absolute;inset:18px;overflow:hidden;background:radial-gradient(circle at 50% 42%,#2e3d49 0 20%,transparent 42%),linear-gradient(155deg,#222a32,#3c4b57 55%,#171c21)}}
.illustration-image{{position:absolute;inset:18px;width:calc(100% - 36px);height:calc(100% - 36px);z-index:2}}
.illustration-frame:has(.illustration-image) .illustration-art{{display:none}}
.technical-rule{{position:absolute;left:72px;right:72px;top:1332px;height:2px;background:var(--blue);transform-origin:left center}}
.illustration-headline{{position:absolute;left:72px;right:72px;top:1396px;font-size:92px;font-weight:900;line-height:1;letter-spacing:1px;color:var(--ivory)}}
.supporting-copy{{position:absolute;left:72px;right:72px;top:1548px;font:28px/1.62 "LVS Noto Serif",serif;color:#d9d0b2}}
/* bigTextReveal */
.kicker-rule{{position:absolute;left:50%;top:560px;width:120px;height:2px;margin-left:-60px;background:var(--rust)}}
.big-kicker{{position:absolute;left:72px;right:72px;top:604px;text-align:center;font-size:34px;font-weight:700;letter-spacing:12px;text-transform:uppercase;color:var(--blue)}}
.big-headline{{position:absolute;left:40px;right:40px;top:712px;text-align:center;font-size:250px;font-weight:900;line-height:.95;letter-spacing:4px;color:var(--ivory);text-shadow:0 10px 40px #000a}}
.big-cta{{position:absolute;left:72px;right:72px;top:1330px;height:120px;display:flex;align-items:center;justify-content:center;text-align:center;font-size:44px;font-weight:800;letter-spacing:6px;text-transform:uppercase;color:var(--ivory);border:3px solid var(--rust);background:#b9532f17;box-shadow:0 18px 50px #000a}}
.big-cta span{{display:block;max-width:100%}}
.landscape .line-a{{left:70px;top:72px;height:920px}} .landscape .line-b{{left:70px;right:70px;top:1000px}}
.landscape .year{{left:70px;top:62px;font-size:154px}}
.landscape .archive-photo{{left:82px;top:270px;width:650px;height:610px}}
.landscape .document{{right:82px;top:170px;width:780px;height:720px}}
.landscape .ruler-grid{{left:780px;right:82px;bottom:74px;grid-template-columns:repeat(5,1fr);gap:14px}}
.landscape .ruler-node{{height:58px}}
.landscape .elon{{font-size:230px}}
.landscape .document-title{{left:80px;right:80px;top:52px;font-size:82px}}
.landscape .source-sheet{{left:90px;right:760px;top:180px;height:790px}}
.landscape .source-sheet-strip{{height:clamp(300px,calc(1070px / var(--source-sheet-aspect)),790px)}}
.landscape .connector-line{{left:1230px;top:300px;width:570px}}
.landscape .annotation{{left:1230px;top:340px;width:570px}}
.landscape .context-photo{{right:90px;top:540px;width:520px;height:420px}}
.landscape .context-photo-image{{height:340px}}
.landscape .comparison-headline{{top:52px;font-size:76px}}
.landscape .comparison-card{{top:210px;width:760px;height:650px}}
.landscape .left-card{{left:80px}} .landscape .right-card{{right:80px}}
.landscape .comparison-left-image,.landscape .comparison-right-image{{height:570px}}
.landscape .comparison-label{{top:890px;width:760px;font-size:32px}}
.landscape .left-label{{left:80px}} .landscape .right-label{{right:80px}}
.landscape .divider-line{{left:959px;top:230px;height:690px}}
.landscape .illustration-frame{{left:70px;right:730px;top:120px;height:850px}}
.landscape .technical-rule{{left:1270px;right:70px;top:330px}}
.landscape .illustration-headline{{left:1270px;right:70px;top:380px;font-size:82px}}
.landscape .supporting-copy{{left:1270px;right:70px;top:570px}}
.landscape .kicker-rule{{top:290px}} .landscape .big-kicker{{top:330px}}
.landscape .big-headline{{top:430px;font-size:230px}}
.landscape .big-cta{{top:930px;height:90px;font-size:36px;letter-spacing:4px}}
.focus-mark{{box-shadow:inset 0 0 0 3px #b9532f}}
.editorial-text-disabled .editorial-type{{visibility:hidden}}
.editorial-text-disabled .ruler-node span{{visibility:hidden}}
/* Caption layer: documentary phrase captions. Font sizes mirror
   backend/captions/editorial.py _FONT_SIZES (design space); keep in sync
   and bump EDITORIAL_RENDER_WORKFLOW_VERSION when either changes. */
#caption-layer{{position:absolute;inset:0;z-index:120;pointer-events:none}}
.cap{{position:absolute;display:none;opacity:0;will-change:opacity,transform}}
.caption-style-editorialPhrase .cap,.caption-style-quietDocumentary .cap,.caption-style-oneLine .cap,.caption-style-oneWord .cap{{
font-family:"LVS Noto Sans",sans-serif;
font-weight:600;color:var(--ivory);
text-shadow:0 1px 2px #000f,0 2px 14px #000d;
line-height:1.3;
}}
/* Ordinary beats keep a soft scrim so they stay comfortably readable over
   bright frames (documents, photographs) while the cream block stays the
   loud emphasis. */
.caption-style-editorialPhrase .cap:not(.highlight),.caption-style-quietDocumentary .cap:not(.highlight),.caption-style-oneLine .cap:not(.highlight),.caption-style-oneWord .cap:not(.highlight){{background:rgba(9,10,12,.9);padding:3px 12px;color:#fff}}
.caption-style-editorialPhrase .cap{{font-size:48px;padding-left:16px;border-left:3px solid var(--rust)}}
.caption-style-editorialPhrase .cap:not(.highlight){{padding-left:16px}}
.caption-style-quietDocumentary .cap{{font-size:44px;font-weight:500}}
.caption-style-oneLine .cap{{font-size:46px;font-weight:500}}
.caption-style-oneWord .cap{{font-size:44px;font-weight:500}}
.landscape .caption-style-editorialPhrase .cap{{font-size:40px}}
.landscape .caption-style-quietDocumentary .cap,.landscape .caption-style-oneWord .cap{{font-size:36px}}
.landscape .caption-style-oneLine .cap{{font-size:38px}}
/* Paper-block emphasis: cream rectangle, dark text, no rounded corners, no glow. */
.caption-style-editorialPhrase .cap.highlight{{
background:#f0e5c9;color:#241f19;border-left:none;padding:6px 14px;
font-size:58px;font-weight:700;text-shadow:none;
}}
.landscape .caption-style-editorialPhrase .cap.highlight{{font-size:46px;padding:4px 12px}}
</style></head><body class="archive-dossier {placeholder_class} {text_class} {orientation}{caption_class}"><main id="stage">{markup}<div id="caption-layer"></div></main>
<script>
"use strict";
const PLAN={payload};
const CAPTIONS={captions_payload};
const FONT_MANIFEST={font_manifest_payload};
const clamp=v=>Math.max(0,Math.min(1,v));
const ease=v=>{{v=clamp(v);return v*v*(3-2*v)}};
function fitSetting(el,name){{
  const landscape=document.body.classList.contains('landscape');
  const key=landscape?'fitLandscape'+name:'fit'+name;
  return Number(el.dataset[key]);
}}
function textMetrics(el){{
  const range=document.createRange();range.selectNodeContents(el);
  const fragments=[...range.getClientRects()].filter(rect=>rect.width>0&&rect.height>0);
  const style=getComputedStyle(el),fontSize=parseFloat(style.fontSize)||16;
  const parsedLineHeight=parseFloat(style.lineHeight);
  const lineHeight=Number.isFinite(parsedLineHeight)?parsedLineHeight:fontSize*1.2;
  const padding=(parseFloat(style.paddingTop)||0)+(parseFloat(style.paddingBottom)||0);
  const contentHeight=Math.max(0,el.scrollHeight-padding);
  return {{
    width:fragments.length?Math.max(...fragments.map(rect=>rect.width)):0,
    height:contentHeight,
    lines:Math.max(1,Math.round(contentHeight/lineHeight)),
    horizontalOverflow:el.scrollWidth>el.clientWidth+1,
  }};
}}
function fitOne(el){{
  const minimum=fitSetting(el,'Min'),maximum=fitSetting(el,'Max');
  const maxHeight=fitSetting(el,'Height'),maxLines=fitSetting(el,'Lines');
  const stage=document.getElementById('stage');
  const scale=stage.getBoundingClientRect().width/stage.offsetWidth;
  const fits=size=>{{
    el.style.fontSize=size+'px';
    const metrics=textMetrics(el),box=el.getBoundingClientRect();
    return {{
      ok:!metrics.horizontalOverflow&&metrics.height<=maxHeight+.75&&metrics.lines<=maxLines,
      metrics,box,
    }};
  }};
  let low=minimum,high=maximum,best=minimum,bestResult=fits(minimum);
  if(bestResult.ok){{
    for(let i=0;i<12;i++){{
      const middle=(low+high)/2,result=fits(middle);
      if(result.ok){{best=middle;bestResult=result;low=middle;}}else{{high=middle;}}
    }}
  }}
  const finalResult=fits(best);
  const stageRect=stage.getBoundingClientRect(),rect=finalResult.box;
  const withinStage=(
    rect.left>=stageRect.left-1&&rect.top>=stageRect.top-1&&
    rect.right<=stageRect.right+1&&rect.bottom<=stageRect.bottom+1
  );
  const report={{
    id:el.id||null,className:el.className,fontSize:Number(best.toFixed(2)),
    lines:finalResult.metrics.lines,width:Number((rect.width/scale).toFixed(2)),
    height:Number(finalResult.metrics.height.toFixed(2)),
    maxLines,maxHeight,
    x:Number(((rect.left-stageRect.left)/scale).toFixed(2)),
    y:Number(((rect.top-stageRect.top)/scale).toFixed(2)),
    fit:finalResult.ok&&withinStage,
  }};
  return report;
}}
function fitEditorialText(){{
  const report=[];
  document.querySelectorAll('.composition').forEach(root=>{{
    const priorDisplay=root.style.display,priorVisibility=root.style.visibility;
    const transformed=[...root.querySelectorAll('[data-rest-rotate]')].map(el=>[
      el,el.style.transform,
    ]);
    root.style.display='block';root.style.visibility='hidden';
    transformed.forEach(([el])=>{{el.style.transform='none';}});
    root.querySelectorAll('.fit-text').forEach(el=>report.push(fitOne(el)));
    transformed.forEach(([el,value])=>{{el.style.transform=value;}});
    root.style.display=priorDisplay;root.style.visibility=priorVisibility;
  }});
  window.__editorialLayoutReport=report;
  document.body.dataset.layoutReport=JSON.stringify(report);
  const failures=report.filter(item=>!item.fit);
  if(failures.length){{
    throw new Error('Editorial text does not fit its renderer-owned region: '+
      failures.map(item=>item.id||item.className).join(', '));
  }}
}}
function showEditorialError(message){{
  const node=document.createElement('div');node.id='editorial-render-error';
  node.style.cssText='position:absolute;inset:0;z-index:999;padding:64px;background:#111315;color:#e9dfc6;font:32px/1.4 sans-serif';
  node.textContent=message;document.body.appendChild(node);
}}
let capNode=null,capKey=null;
function captionEl(){{
  if(!capNode){{
    const layer=document.getElementById('caption-layer');
    if(!layer)return null;
    capNode=document.createElement('div');capNode.className='cap';layer.appendChild(capNode);
  }}
  return capNode;
}}
function updateCaptions(t){{
  let active=null;
  for(let i=0;i<CAPTIONS.length;i++){{
    const cue=CAPTIONS[i];
    if(t>=cue.start&&t<cue.end)active=cue;
  }}
  const key=active?active.start+'|'+active.text:null;
  if(key!==capKey){{
    capKey=key;
    const node=captionEl();
    if(!node)return;
    if(!active){{node.style.display='none';node.style.opacity='0';return;}}
    node.textContent=active.text;
    node.style.display='block';
    node.classList.toggle('highlight',!!active.highlight);
    node.style.left=active.x+'px';
    node.style.top=active.y+'px';
  }}
  if(!active||!capNode)return;
  const inP=clamp((t-active.start)/0.14);
  const outP=clamp((active.end-t)/0.14);
  capNode.style.opacity=String(Math.min(inP,outP));
  const dy=(1-inP)*8;
  const dx=active.highlight?(1-inP)*10:0;
  capNode.style.transform=`translate(${{dx}}px,${{dy}}px)`;
}}
function reset(root){{
  root.querySelectorAll('.verified-passage').forEach(mark=>{{
    const img=mark.parentElement.querySelector('.document-image');
    if(!img||!img.naturalWidth)return;
    const w=img.clientWidth,h=img.clientHeight;
    const scale=Math.min(w/img.naturalWidth,h/img.naturalHeight);
    const iw=img.naturalWidth*scale,ih=img.naturalHeight*scale;
    mark.querySelectorAll('[data-box]').forEach(line=>{{
      const [x,y,bw,bh]=line.dataset.box.split(',').map(Number);
      Object.assign(line.style,{{left:(img.offsetLeft+(w-iw)/2+x*iw)+'px',top:(img.offsetTop+(h-ih)/2+y*ih)+'px',width:(bw*iw)+'px',height:(bh*ih)+'px'}});
    }});
  }});
  root.querySelectorAll('.editorial-element').forEach(el=>{{el.style.opacity='0';el.style.filter='none';el.style.transform='none';el.classList.remove('focus-mark')}});
  root.querySelectorAll('.document,.source-sheet').forEach(el=>{{el.style.transformOrigin=''}});
  root.querySelectorAll('.ruler-node').forEach(el=>{{
    el.style.opacity='0';el.style.transform='';el.style.zIndex='';el.classList.remove('focus','chief');
    const number=el.querySelector('span');if(number)number.style.opacity='1';
    const label=el.querySelector('.ruler-chief');if(label)label.style.opacity='0';
  }});
  const layer=root.querySelector('.research-layer');if(layer)layer.style.cssText='';
  const blackout=root.querySelector('.blackout');if(blackout)blackout.style.opacity='0';
  root.querySelectorAll('.draw').forEach(el=>{{el.style.transform=el.dataset.drawAxis==='y'?'scaleY(0)':'scaleX(0)'}});
}}
function applyEvent(root,event,t){{
  if(t<event.time)return;
  const p=event.duration===0?1:ease((t-event.time)/event.duration);
  const target=event.target==='canvas'?root:root.querySelector('#'+CSS.escape(event.target));
  if(!target)return;
  const rest=Number(target.dataset.restRotate)||0;
  switch(event.action){{
    case 'fade': target.style.opacity=String(p);break;
    case 'fadeUp': target.style.opacity=String(p);target.style.transform=`translateY(${{(1-p)*70}}px)`;break;
    case 'slideInLeft': target.style.opacity=String(p);target.style.transform=`translateX(${{(p-1)*520}}px) rotate(${{rest}}deg)`;break;
    case 'slideInRight': target.style.opacity=String(p);target.style.transform=`translateX(${{(1-p)*520}}px)`;break;
    case 'scaleIn': target.style.opacity=String(p);target.style.transform=`scale(${{.78+.22*p}})`;break;
    case 'slowPush': {{
      const requested=Number(event.value);
      if(target.classList.contains('document')&&Number.isFinite(requested)&&requested>1){{
        const zoom=Math.max(1,Math.min(2,requested));
        target.style.opacity='1';target.style.transformOrigin='100% 46%';
        target.style.transform=`scale(${{1+(zoom-1)*p}}) rotate(${{rest}}deg)`;
      }}else{{target.style.opacity='1';target.style.transform=`scale(${{1+.04*p}})`;}}
      break;
    }}
    case 'paperSlide': target.style.opacity=String(p);target.style.transform=`translate(${{(1-p)*520}}px,${{(1-p)*90}}px) rotate(${{rest*(3.5-2.5*p)}}deg)`;break;
    case 'underline': case 'highlight': target.style.opacity=String(p);target.style.transform=`scaleX(${{p}})`;break;
    case 'drawLine': target.style.opacity=String(p);target.style.transform=(target.dataset.drawAxis==='y')?`scaleY(${{p}})`:`scaleX(${{p}})`;break;
    case 'staggerIn': {{const nodes=[...target.querySelectorAll('.ruler-node')];if(nodes.length){{nodes.forEach((node,i)=>{{const q=ease(clamp(p*1.65-i/nodes.length*.65));node.style.opacity=String(q);node.style.transform=`translateY(${{(1-q)*34}}px)`}});target.style.opacity='1';}}else{{target.style.opacity=String(p);target.style.transform=`translateY(${{(1-p)*40}}px)`;}}break;}}
    case 'dimOthers': {{const nodes=[...target.querySelectorAll('.ruler-node')];if(nodes.length){{const focus=Number(event.value)||0;nodes.forEach((node,i)=>{{node.style.opacity=String(i===focus?1:1-.78*p)}});target.style.opacity='1';}}else{{target.style.filter=`brightness(${{1-.2*p}})`;}}break;}}
    case 'focusOne': {{const nodes=[...target.querySelectorAll('.ruler-node')];if(nodes.length){{const focus=Number(event.value)||0;const node=nodes[focus];if(node&&p>.2)node.classList.add('focus');target.style.opacity='1';}}else{{target.style.opacity='1';if(p>.2)target.classList.add('focus-mark');}}break;}}
    case 'promoteNode': {{const nodes=[...target.querySelectorAll('.ruler-node')];if(nodes.length){{const chief=Number(event.value)||0;const rootRect=root.getBoundingClientRect();nodes.forEach((node,i)=>{{const isChief=i===chief;const number=node.querySelector('span');const label=node.querySelector('.ruler-chief');node.style.opacity=String(isChief?1:1-.72*p);if(isChief){{const nodeRect=node.getBoundingClientRect();const dx=rootRect.left+rootRect.width/2-(nodeRect.left+nodeRect.width/2);const dy=rootRect.top+rootRect.height*.36-(nodeRect.top+nodeRect.height/2);node.style.transform=`translate(${{dx*p}}px,${{dy*p}}px) scale(${{1+.32*p}})`;node.style.zIndex='5';node.classList.toggle('chief',p>.35);if(number)number.style.opacity=String(1-clamp(p/.45));if(label)label.style.opacity=String(clamp((p-.35)/.65));}}else{{node.style.transform='';node.style.zIndex='';node.classList.remove('chief');if(number)number.style.opacity='1';if(label)label.style.opacity='0';}}}});target.style.opacity='1';}}else{{target.style.opacity='1';}}break;}}
    case 'collapseToBlack': {{const layer=root.querySelector('.research-layer');if(layer){{layer.style.opacity=String(1-p);layer.style.transform=`scale(${{1-.08*p}})`;}}const blackout=root.querySelector('.blackout');if(blackout)blackout.style.opacity=String(p);break;}}
    case 'hardCut': target.style.opacity='1';break;
  }}
}}
window.renderAt=function(globalTime){{
  PLAN.compositions.forEach((composition,index)=>{{
    const root=document.querySelectorAll('.composition')[index];
    const active=globalTime>=composition.start&&globalTime<=composition.start+composition.duration;
    root.style.display=active?'block':'none'; if(!active)return;
    const t=globalTime-composition.start;reset(root);composition.events.forEach(event=>applyEvent(root,event,t));
  }});
  updateCaptions(globalTime);
  return true;
}};
window.__editorialReady=false;window.__editorialError=null;
const pinnedFontsReady=Promise.all(FONT_MANIFEST.map(entry=>
  document.fonts.load(`${{entry.weight}} 16px "${{entry.family}}"`)
));
const sourceImagesReady=Promise.all([...document.images].map(img=>
  img.decode().catch(()=>undefined)
));
Promise.all([pinnedFontsReady,sourceImagesReady]).then(()=>document.fonts.ready).then(()=>{{
  fitEditorialText();window.renderAt(0);window.__editorialReady=true;
}}).catch(error=>{{
  window.__editorialError=String(error&&error.message||error);
  showEditorialError(window.__editorialError);
}});
</script></body></html>"""


class _CDP:
    def __init__(self, url: str) -> None:
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Editorial rendering requires the existing dev dependency websocket-client"
            ) from exc
        self._websocket = websocket
        self.ws = websocket.create_connection(url, timeout=60)
        self._ids = itertools.count(1)

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = next(self._ids)
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result", {})

    def close(self) -> None:
        self.ws.close()


class EditorialRenderer:
    """Render exact timestamps from trusted composition HTML into an MP4."""

    def __init__(self, chromium: Path | None = None) -> None:
        self.chromium = chromium or discover_chromium()

    def render(
        self,
        plan: EditPlan,
        output: Path,
        *,
        preview_html: Path | None = None,
        asset_root: Path | None = None,
        captions: Sequence[dict[str, Any]] = (),
    ) -> Path:
        if self.chromium is None:
            raise RuntimeError("Chromium is required for Editorial Mode rendering")
        validate_export_assets(plan, asset_root)
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        html = compile_edit_plan_html(
            plan,
            asset_root=asset_root,
            asset_url_resolver=(
                lambda asset: self._data_asset_url(asset_root, asset)
                if asset_root is not None else None
            ),
            captions=captions,
            show_placeholders=False,
        )
        if preview_html is not None:
            preview_html.parent.mkdir(parents=True, exist_ok=True)
            preview_html.write_text(html, encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="lvs-editorial-", dir=output.parent) as temp_name:
            work = Path(temp_name)
            document = work / "composition.html"
            document.write_text(html, encoding="utf-8")
            temporary = work / "render.mp4"
            ffmpeg = require_ffmpeg()
            run_media_process_stream([
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "image2pipe", "-framerate", str(plan.fps),
                "-vcodec", "mjpeg", "-i", "pipe:0",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ], self._capture_frames(plan, document, work / "chrome-profile"),
                timeout=max(120.0, plan.duration * 20))
            publish = output.with_name(f".{output.name}.editorial.tmp")
            shutil.copyfile(temporary, publish)
            os.replace(publish, output)
        return output

    @staticmethod
    def _data_asset_url(asset_root: Path, asset: EditorialAsset) -> str | None:
        if not asset.source or asset.source.startswith(("http://", "https://")):
            return None
        root = asset_root.resolve()
        path = (root / asset.source).resolve()
        if root not in path.parents or not path.is_file():
            return None
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    def _capture_frames(
        self, plan: EditPlan, document: Path, profile: Path,
    ) -> Iterator[bytes]:
        profile.mkdir(parents=True)
        args = [
            str(self.chromium), "--headless=new", "--disable-extensions",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
            "--force-device-scale-factor=1", f"--window-size={plan.width},{plan.height}",
            "--remote-debugging-port=0", "--remote-allow-origins=*",
            "--allow-file-access-from-files", f"--user-data-dir={profile}", "about:blank",
        ]
        if os.environ.get("LVS_CHROMIUM_NO_SANDBOX", "").strip().lower() in {"1", "true", "yes"}:
            args.insert(2, "--no-sandbox")
        process = subprocess.Popen(
            args, env=dict(os.environ, HOME=str(profile)), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client: _CDP | None = None
        try:
            port_file = profile / "DevToolsActivePort"
            for _ in range(100):
                if port_file.is_file():
                    break
                if process.poll() is not None:
                    raise RuntimeError("Chromium exited before exposing Editorial renderer control")
                time.sleep(0.1)
            if not port_file.is_file():
                raise RuntimeError("Chromium did not expose Editorial renderer control")
            port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
            page_url = None
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                        targets = json.load(response)
                    page_url = next(item["webSocketDebuggerUrl"] for item in targets if item["type"] == "page")
                    break
                except Exception:
                    time.sleep(0.1)
            if not page_url:
                raise RuntimeError("Chromium did not create an Editorial render page")
            client = _CDP(page_url)
            client.command("Page.enable")
            client.command("Runtime.enable")
            client.command("Emulation.setDeviceMetricsOverride", {
                "width": plan.width, "height": plan.height,
                "deviceScaleFactor": 1, "mobile": False,
            })
            client.command("Page.navigate", {"url": document.resolve().as_uri()})
            for _ in range(100):
                state = client.command("Runtime.evaluate", {
                    "expression": (
                        "({ready:window.__editorialReady===true,"
                        "error:window.__editorialError||null})"
                    ),
                    "returnByValue": True,
                })
                value = state.get("result", {}).get("value", {})
                if isinstance(value, dict) and value.get("error"):
                    raise RuntimeError(f"Editorial composition layout failed: {value['error']}")
                if isinstance(value, dict) and value.get("ready") is True:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Editorial composition did not become ready")
            frame_count = round(plan.duration * plan.fps)
            for frame in range(frame_count):
                timestamp = frame / plan.fps
                client.command("Runtime.evaluate", {
                    "expression": f"window.renderAt({timestamp:.12f})", "returnByValue": True,
                })
                screenshot = client.command("Page.captureScreenshot", {
                    "format": "jpeg", "quality": 95,
                    "fromSurface": True, "captureBeyondViewport": False,
                    "optimizeForSpeed": True,
                })
                yield base64.b64decode(screenshot["data"])
        finally:
            if client is not None:
                client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
