"""Trusted HTML compiler and exact-time Chromium renderer for Editorial Mode."""

from __future__ import annotations

import base64
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
from html import escape
from pathlib import Path
from typing import Any

from backend.graphics.browser import discover_chromium
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.process import run_media_process

from .models import (
    EditPlan, EditorialAsset, EditorialComposition, EditorialElement,
    EditorialTemplate,
)


AssetURLResolver = Callable[[EditorialAsset], str | None]
EDITORIAL_RENDER_WORKFLOW_VERSION = "editorial-renderer-v6-responsive-type"


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
    return (
        f'<img class="asset-image {escape(class_name)}" '
        f'src="{escape(source, quote=True)}" alt="">'
    )


def _card_tag(asset: EditorialAsset | None, default: str) -> str:
    label = asset.label if asset and asset.label else default
    klass = asset.evidence_class.value.upper() if asset else "ILLUSTRATION"
    return f"{label.upper()} · {klass}"


def _portrait_fallback() -> str:
    return '<div class="photo-art"><div class="portrait-head"></div><div class="portrait-body"></div></div>'


def _big_headline_class(text: str) -> str:
    """Choose a deterministic size tier for exact on-screen headlines."""
    compact_length = len("".join(text.split()))
    if compact_length >= 18:
        return " big-headline-long"
    if compact_length >= 10:
        return " big-headline-medium"
    return ""


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
    photo_tag = _card_tag(photo_asset, "ARCHIVE PHOTOGRAPH")
    document_copy = (
        document_asset.label
        if document_asset and document_asset.label
        else "Documentary source material arranged on the editorial canvas."
    )
    ruler_count = rulers.count if rulers else 10
    nodes = "".join(
        f'<div class="ruler-node" data-ruler="{index}"><span>{index + 1:02d}</span></div>'
        for index in range(ruler_count)
    )
    return f"""
      <section class="composition archive-canvas" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="{escape(year.id if year else 'year', quote=True)}" class="year editorial-element editorial-type">{escape(year.text if year else "")}</div>
          <div id="{escape(photo.id if photo else 'photo', quote=True)}" class="archive-photo editorial-element" data-rest-rotate="-2.4">
            {_asset_image(composition, photo, resolver, class_name="archive-photo-image")}
            {_portrait_fallback()}
            <div class="asset-tag editorial-type">{escape(photo_tag)}</div>
          </div>
          <div id="{escape(document.id if document else 'document', quote=True)}" class="document editorial-element" data-rest-rotate="2">
            {_asset_image(composition, document, resolver, class_name="document-image")}
            <div class="paper-index editorial-type">ARCHIVE / SOURCE DOCUMENT</div>
            <h2 class="editorial-type">{escape(document.text if document and document.text else "DOCUMENT")}</h2>
            <p class="editorial-type">DOCUMENT EXCERPT</p>
            <div class="rule"></div>
            <p class="document-copy editorial-type">{escape(document_copy)}</p>
            <div id="{escape(passage.id if passage else 'passage', quote=True)}" class="passage draw editorial-element"></div>
            <div class="paper-stamp editorial-type">SOURCE</div>
          </div>
          <div id="{escape(rulers.id if rulers else 'rulers', quote=True)}" class="ruler-grid editorial-element">{nodes}</div>
          <div class="draft-label editorial-type">FIG. 01 · EVIDENCE MAP</div>
        </div>
        <div class="blackout"></div>
        <div id="{escape(reveal.id if reveal else 'reveal', quote=True)}" class="elon editorial-element editorial-type">{escape(reveal.text if reveal else "")}</div>
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
    document_text = document.text.strip() if document and document.text else ""
    document_copy_tag = (
        f'<p class="document-copy editorial-type">{escape(document_text)}</p>'
        if document_text
        else ""
    )
    context_tag = _card_tag(_asset(composition, context), "CONTEXT PHOTOGRAPH")
    return f"""
      <section class="composition document-reveal" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="{escape(title.id if title else 'title', quote=True)}" class="document-title editorial-element editorial-type">{escape(title.text if title else "")}</div>
          <div id="{escape(document.id if document else 'document', quote=True)}" class="source-sheet editorial-element" data-rest-rotate="1">
            {_asset_image(composition, document, resolver, class_name="document-image")}
            <div class="paper-index editorial-type">SOURCE / DOCUMENT</div>
            {document_copy_tag}
            <div id="{escape(mark.id if mark else 'passage-mark', quote=True)}" class="passage-mark draw editorial-element"></div>
            <div class="paper-stamp editorial-type">SOURCE</div>
          </div>
          <div id="{escape(connector.id if connector else 'connector', quote=True)}" class="connector-line draw editorial-element"></div>
          <div id="{escape(annotation.id if annotation else 'annotation', quote=True)}" class="annotation editorial-element editorial-type">{escape(annotation.text if annotation else "")}</div>
          <div id="{escape(context.id if context else 'context', quote=True)}" class="context-photo editorial-element" data-rest-rotate="-1.6">
            {_asset_image(composition, context, resolver, class_name="context-photo-image")}
            {_portrait_fallback()}
            <div class="asset-tag editorial-type">{escape(context_tag)}</div>
          </div>
          <div class="draft-label editorial-type">FIG. 02 · SOURCE READING</div>
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
    left_tag = _card_tag(_asset(composition, left_image), "LEFT SOURCE")
    right_tag = _card_tag(_asset(composition, right_image), "RIGHT SOURCE")
    return f"""
      <section class="composition comparison-canvas" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="{escape(headline.id if headline else 'headline', quote=True)}" class="comparison-headline editorial-element editorial-type">{escape(headline.text if headline else "")}</div>
          <div id="{escape(left_image.id if left_image else 'left-image', quote=True)}" class="comparison-card left-card editorial-element" data-rest-rotate="-1.2">
            {_asset_image(composition, left_image, resolver, class_name="comparison-left-image")}
            {_portrait_fallback()}
            <div class="asset-tag editorial-type">{escape(left_tag)}</div>
          </div>
          <div id="{escape(right_image.id if right_image else 'right-image', quote=True)}" class="comparison-card right-card editorial-element" data-rest-rotate="1.2">
            {_asset_image(composition, right_image, resolver, class_name="comparison-right-image")}
            {_portrait_fallback()}
            <div class="asset-tag editorial-type">{escape(right_tag)}</div>
          </div>
          <div id="{escape(left_label.id if left_label else 'left-label', quote=True)}" class="comparison-label left-label editorial-element editorial-type">{escape(left_label.text if left_label else "")}</div>
          <div id="{escape(right_label.id if right_label else 'right-label', quote=True)}" class="comparison-label right-label editorial-element editorial-type">{escape(right_label.text if right_label else "")}</div>
          <div id="{escape(divider.id if divider else 'divider', quote=True)}" class="divider-line draw editorial-element" data-draw-axis="y"></div>
          <div class="draft-label editorial-type">FIG. 03 · COMPARISON</div>
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
            <div class="illustration-art"></div>
          </div>
          <div id="{escape(rule.id if rule else 'technical-line', quote=True)}" class="technical-rule draw editorial-element"></div>
          <div id="{escape(headline.id if headline else 'headline', quote=True)}" class="illustration-headline editorial-element editorial-type">{escape(headline.text if headline else "")}</div>
          <div id="{escape(supporting.id if supporting else 'supporting-text', quote=True)}" class="supporting-copy editorial-element editorial-type">{escape(supporting.text if supporting else "")}</div>
          <div class="draft-label editorial-type">FIG. 04 · ILLUSTRATION</div>
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
    blackout = _element(composition, "blackout")
    return f"""
      <section class="composition big-text-reveal" data-composition="{escape(composition.id, quote=True)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="kicker-rule"></div>
          <div id="{escape(kicker.id if kicker else 'kicker', quote=True)}" class="big-kicker editorial-element editorial-type">{escape(kicker.text if kicker else "")}</div>
          <div id="{escape(headline.id if headline else 'headline', quote=True)}" class="big-headline{_big_headline_class(headline.text if headline else '')} editorial-element editorial-type">{escape(headline.text if headline else "")}</div>
          <div class="draft-label editorial-type">FIG. 05 · STATED</div>
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
    captions: Sequence[dict[str, Any]] = (),
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
    markup = "".join(
        _TEMPLATE_MARKUP[item.template](item, asset_url_resolver)
        for item in plan.compositions
    )
    payload = _script_json(plan.model_dump(mode="json"))
    captions_payload = _script_json(list(captions))
    landscape = plan.width >= plan.height
    design_width, design_height = ((1920, 1080) if landscape else (1080, 1920))
    design_scale = min(plan.width / design_width, plan.height / design_height)
    stage_left = (plan.width - design_width * design_scale) / 2
    stage_top = (plan.height - design_height * design_scale) / 2
    orientation = "landscape" if landscape else "portrait"
    text_class = "editorial-text-enabled" if plan.editorial_text_enabled else "editorial-text-disabled"
    caption_class = f" caption-style-{plan.caption_style.value}" if captions else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--charcoal:#111315;--charcoal-2:#1b1d1f;--ivory:#e9dfc6;--rust:#b9532f;--blue:#6f91a6;--ink:#25211d}}
*{{box-sizing:border-box}} html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#000}}
body{{font-family:"DejaVu Sans Condensed","Liberation Sans Narrow",sans-serif}}
#stage{{position:absolute;left:{stage_left:.4f}px;top:{stage_top:.4f}px;width:{design_width}px;height:{design_height}px;overflow:hidden;background:#000;transform:scale({design_scale:.8f});transform-origin:top left}}
.composition{{position:absolute;inset:0;display:none;overflow:hidden;background:var(--charcoal);color:var(--ivory)}}
.research-layer{{position:absolute;inset:0;transform-origin:50% 50%}}
.grain{{position:absolute;inset:0;z-index:80;pointer-events:none;opacity:.11;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.82' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.32'/%3E%3C/svg%3E")}}
.technical-line{{position:absolute;background:var(--blue);opacity:.28}} .line-a{{left:74px;top:120px;width:1px;height:1640px}} .line-b{{left:74px;right:74px;top:1704px;height:1px}}
.blackout{{position:absolute;inset:0;z-index:100;background:#050505;opacity:0}}
/* archiveCanvas */
.year{{position:absolute;left:72px;top:105px;font-size:250px;font-weight:900;line-height:.84;letter-spacing:-10px;color:var(--ivory);text-shadow:0 8px 28px #0008}}
.archive-photo{{position:absolute;left:76px;top:420px;width:610px;height:610px;padding:18px;background:#c9bea4;box-shadow:0 30px 80px #0008;transform:rotate(-2.4deg)}}
.photo-art{{position:absolute;overflow:hidden;background:radial-gradient(circle at 50% 27%,#a59c88 0 12%,transparent 13%),linear-gradient(145deg,#625f57,#b3aa97 46%,#494943)}}
.archive-photo .photo-art{{inset:18px 18px 72px}}
.context-photo .photo-art,.comparison-card .photo-art{{inset:16px 16px 64px}}
.asset-image{{display:block;object-fit:cover}} .archive-photo-image{{position:absolute;inset:18px 18px 72px;width:calc(100% - 36px);height:520px;z-index:2;filter:grayscale(.76) sepia(.22) contrast(1.04)}}
.archive-photo:has(.archive-photo-image) .photo-art{{visibility:hidden}}
.portrait-head{{position:absolute;left:220px;top:100px;width:130px;height:165px;border-radius:48% 48% 42% 42%;background:#353735;box-shadow:24px 5px 0 #77756c}}
.portrait-body{{position:absolute;left:120px;top:245px;width:360px;height:340px;border-radius:48% 48% 0 0;background:#292c2c}}
.asset-tag{{position:absolute;color:#3b3731;font-size:22px;letter-spacing:3px;text-align:center}}
.archive-photo .asset-tag{{left:18px;right:18px;bottom:14px}}
.context-photo .asset-tag,.comparison-card .asset-tag{{left:16px;right:16px;bottom:10px}}
.document{{position:absolute;right:54px;top:630px;width:590px;height:760px;padding:64px 56px;background:var(--ivory);color:var(--ink);box-shadow:0 35px 95px #000b;transform:rotate(2deg)}}
.document-image{{position:absolute;inset:0;width:100%;height:100%;opacity:.2;filter:sepia(.35) contrast(.85)}} .document>*:not(.document-image):not(.passage):not(.paper-stamp){{position:relative;z-index:2}}
.paper-index{{font:18px monospace;letter-spacing:2px;color:#6e675b}} .document h2{{margin:95px 0 6px;font:700 62px/1 "DejaVu Serif",serif;letter-spacing:1px}} .document>p{{margin:0;font:22px "DejaVu Serif",serif;letter-spacing:3px}}
.document .rule{{height:2px;background:#2b2925;margin:24px 0 42px}} .document .document-copy{{font:27px/1.65 "DejaVu Serif",serif;letter-spacing:0}}
.passage{{position:absolute;left:54px;right:54px;top:508px;height:22px;border-bottom:8px solid var(--rust);background:#b9532f26;transform-origin:left center}}
.paper-stamp{{position:absolute;right:38px;bottom:40px;padding:10px 16px;border:4px solid #9f4429;color:#9f4429;font-weight:800;letter-spacing:3px;transform:rotate(-8deg);opacity:.82}}
.ruler-grid{{position:absolute;left:82px;right:72px;bottom:170px;display:grid;grid-template-columns:repeat(5,1fr);gap:24px}}
.ruler-node{{height:86px;border:1px solid #6f91a680;position:relative;color:#9db4c0;background:#182027}}
.ruler-node:before{{content:"";position:absolute;left:12px;right:12px;top:50%;height:1px;background:#6f91a680}} .ruler-node span{{position:absolute;right:10px;top:8px;font:17px monospace}}
.ruler-node.focus{{border-color:var(--rust);background:#4e271d;color:#ffd8bd;box-shadow:inset 0 0 0 3px #b9532f}}
.draft-label{{position:absolute;left:80px;bottom:64px;font:18px monospace;letter-spacing:3px;color:#6f91a6}}
.elon{{position:absolute;z-index:110;inset:0;display:flex;align-items:center;justify-content:center;padding:0 48px;text-align:center;white-space:nowrap;font-size:170px;font-weight:900;line-height:.92;letter-spacing:6px;color:var(--ivory);opacity:0}}
/* documentReveal */
.document-title{{position:absolute;left:72px;right:72px;top:96px;font-size:104px;font-weight:900;line-height:1.02;letter-spacing:2px;color:var(--ivory);text-shadow:0 8px 28px #0008}}
.source-sheet{{position:absolute;left:64px;right:64px;top:352px;height:872px;padding:64px 56px;background:var(--ivory);color:var(--ink);box-shadow:0 35px 95px #000b;transform:rotate(1deg)}}
.source-sheet .document-image{{opacity:.18}} .source-sheet>*:not(.document-image):not(.passage-mark):not(.paper-stamp){{position:relative;z-index:2}}
.source-sheet .paper-index{{margin-top:0}} .source-sheet .document-copy{{font:30px/1.72 "DejaVu Serif",serif;margin:44px 0 0;letter-spacing:0}}
.passage-mark{{position:absolute;left:56px;right:56px;top:196px;height:34px;border-bottom:8px solid var(--rust);background:#b9532f26;transform-origin:left center}}
.connector-line{{position:absolute;left:72px;top:1268px;width:520px;height:2px;background:var(--blue);transform-origin:left center}}
.annotation{{position:absolute;left:72px;top:1308px;width:500px;font:28px/1.6 "DejaVu Serif",serif;color:var(--ivory);border-left:3px solid var(--rust);padding-left:26px}}
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
.supporting-copy{{position:absolute;left:72px;right:72px;top:1548px;font:28px/1.62 "DejaVu Serif",serif;color:#d9d0b2}}
/* bigTextReveal */
.kicker-rule{{position:absolute;left:50%;top:560px;width:120px;height:2px;margin-left:-60px;background:var(--rust)}}
.big-kicker{{position:absolute;left:72px;right:72px;top:604px;text-align:center;font-size:34px;font-weight:700;letter-spacing:12px;text-transform:uppercase;color:var(--blue)}}
.big-headline{{position:absolute;left:40px;right:40px;top:712px;text-align:center;font-size:250px;font-weight:900;line-height:.95;letter-spacing:4px;color:var(--ivory);text-shadow:0 10px 40px #000a}}
.big-headline-medium{{font-size:130px;letter-spacing:1px}}
.big-headline-long{{font-size:170px;line-height:.92;letter-spacing:2px}}
.landscape .line-a{{left:70px;top:72px;height:920px}} .landscape .line-b{{left:70px;right:70px;top:1000px}}
.landscape .year{{left:70px;top:62px;font-size:154px}}
.landscape .archive-photo{{left:82px;top:270px;width:650px;height:610px}}
.landscape .document{{right:82px;top:170px;width:780px;height:720px}}
.landscape .ruler-grid{{left:780px;right:82px;bottom:74px;grid-template-columns:repeat(5,1fr);gap:14px}}
.landscape .ruler-node{{height:58px}} .landscape .draft-label{{left:82px;bottom:34px}}
.landscape .elon{{font-size:230px}}
.landscape .document-title{{left:80px;right:80px;top:52px;font-size:82px}}
.landscape .source-sheet{{left:90px;right:760px;top:180px;height:790px}}
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
.landscape .big-headline{{top:430px;font-size:230px}} .landscape .big-headline-medium{{font-size:170px}} .landscape .big-headline-long{{font-size:150px}}
.focus-mark{{box-shadow:inset 0 0 0 3px #b9532f}}
.editorial-text-disabled .editorial-type{{visibility:hidden}}
.editorial-text-disabled .ruler-node span{{visibility:hidden}}
/* Caption layer: documentary phrase captions. Font sizes mirror
   backend/captions/editorial.py _FONT_SIZES (design space); keep in sync
   and bump EDITORIAL_RENDER_WORKFLOW_VERSION when either changes. */
#caption-layer{{position:absolute;inset:0;z-index:120;pointer-events:none}}
.cap{{position:absolute;display:none;opacity:0;will-change:opacity,transform}}
.caption-style-editorialPhrase .cap,.caption-style-quietDocumentary .cap,.caption-style-oneLine .cap,.caption-style-oneWord .cap{{
font-family:"DejaVu Sans Condensed","Liberation Sans Narrow",sans-serif;
font-weight:600;color:var(--ivory);
text-shadow:0 2px 9px #000b,0 0 3px #0008;
line-height:1.3;
}}
.caption-style-editorialPhrase .cap{{font-size:48px;padding-left:16px;border-left:3px solid var(--rust)}}
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
</style></head><body class="{text_class} {orientation}{caption_class}"><main id="stage">{markup}<div id="caption-layer"></div></main>
<script>
"use strict";
const PLAN={payload};
const CAPTIONS={captions_payload};
const clamp=v=>Math.max(0,Math.min(1,v));
const ease=v=>{{v=clamp(v);return v*v*(3-2*v)}};
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
  root.querySelectorAll('.editorial-element').forEach(el=>{{el.style.opacity='0';el.style.filter='none';el.style.transform='none';el.classList.remove('focus-mark')}});
  root.querySelectorAll('.ruler-node').forEach(el=>{{el.style.opacity='0';el.classList.remove('focus')}});
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
    case 'slowPush': target.style.opacity='1';target.style.transform=`scale(${{1+.04*p}})`;break;
    case 'paperSlide': target.style.opacity=String(p);target.style.transform=`translate(${{(1-p)*520}}px,${{(1-p)*90}}px) rotate(${{rest*(3.5-2.5*p)}}deg)`;break;
    case 'underline': case 'highlight': target.style.opacity=String(p);target.style.transform=`scaleX(${{p}})`;break;
    case 'drawLine': target.style.opacity=String(p);target.style.transform=(target.dataset.drawAxis==='y')?`scaleY(${{p}})`:`scaleX(${{p}})`;break;
    case 'staggerIn': {{const nodes=[...target.querySelectorAll('.ruler-node')];if(nodes.length){{nodes.forEach((node,i)=>{{const q=ease(clamp(p*1.65-i/nodes.length*.65));node.style.opacity=String(q);node.style.transform=`translateY(${{(1-q)*34}}px)`}});target.style.opacity='1';}}else{{target.style.opacity=String(p);target.style.transform=`translateY(${{(1-p)*40}}px)`;}}break;}}
    case 'dimOthers': {{const nodes=[...target.querySelectorAll('.ruler-node')];if(nodes.length){{const focus=Number(event.value)||0;nodes.forEach((node,i)=>{{node.style.opacity=String(i===focus?1:1-.78*p)}});target.style.opacity='1';}}else{{target.style.filter=`brightness(${{1-.2*p}})`;}}break;}}
    case 'focusOne': {{const nodes=[...target.querySelectorAll('.ruler-node')];if(nodes.length){{const focus=Number(event.value)||0;const node=nodes[focus];if(node&&p>.2)node.classList.add('focus');target.style.opacity='1';}}else{{target.style.opacity='1';if(p>.2)target.classList.add('focus-mark');}}break;}}
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
window.renderAt(0);window.__editorialReady=true;
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
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        html = compile_edit_plan_html(
            plan,
            asset_url_resolver=(
                lambda asset: self._data_asset_url(asset_root, asset)
                if asset_root is not None else None
            ),
            captions=captions,
        )
        if preview_html is not None:
            preview_html.parent.mkdir(parents=True, exist_ok=True)
            preview_html.write_text(html, encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="lvs-editorial-", dir=output.parent) as temp_name:
            work = Path(temp_name)
            document = work / "composition.html"
            document.write_text(html, encoding="utf-8")
            frames = work / "frames"
            frames.mkdir()
            self._capture_frames(plan, document, frames, work / "chrome-profile")
            temporary = work / "render.mp4"
            ffmpeg = require_ffmpeg()
            run_media_process([
                str(ffmpeg), "-y", "-framerate", str(plan.fps),
                "-i", str(frames / "frame-%06d.png"),
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ], timeout=max(120.0, plan.duration * 20))
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

    def _capture_frames(self, plan: EditPlan, document: Path, frames: Path, profile: Path) -> None:
        profile.mkdir(parents=True)
        args = [
            str(self.chromium), "--headless=new", "--disable-gpu", "--disable-extensions",
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
                ready = client.command("Runtime.evaluate", {
                    "expression": "window.__editorialReady === true", "returnByValue": True,
                })
                if ready.get("result", {}).get("value") is True:
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
                    "format": "png", "fromSurface": True, "captureBeyondViewport": False,
                })
                (frames / f"frame-{frame:06d}.png").write_bytes(base64.b64decode(screenshot["data"]))
        finally:
            if client is not None:
                client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
