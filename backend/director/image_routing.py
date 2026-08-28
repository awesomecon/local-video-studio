"""Image-model routing and model-specific prompt construction for scenes.

Why Ideogram 4 exists here
--------------------------
Qwen-Image-2512 renders embedded lettering better than Krea, but in practice its
spelling and layout of longer strings was still not strong enough for headlines,
posters, maps, and UI mockups. Ideogram 4 is being ADDED as a new local
image-generation path specifically for images that must contain readable words,
and it is being TESTED side-by-side against Qwen Image (comparison mode) before
any decision to fully replace it. Nothing in this module removes Qwen: it stays
registered as a fallback and A/B option.

Krea remains the preferred generator for ordinary cinematic scenes without
embedded text; routing only switches to Ideogram when a scene genuinely needs
words inside the picture.

No cloud services are involved: Quick prompts use Ideogram's open-source Magic
Prompt v1 instructions with the user's local LLM, Precise prompts use native
structured JSON directly, and generation runs through local ComfyUI.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from backend.models.ideogram_prompt import (
    build_ideogram_v4_prompt,
    serialize_ideogram_prompt_json as _serialize_ideogram_prompt_json,
    validate_ideogram_prompt_json as _validate_ideogram_prompt_json,
)


class ImageModelOption(StrEnum):
    """Image generators the routing layer may select."""

    KREA = "krea"
    QWEN_IMAGE = "qwen_image"
    IDEOGRAM4_LOCAL = "ideogram4_local"


# Registry keys used by BackendRegistry (kept here so routing stays modular and
# the rest of the pipeline never hardcodes adapter names).
IMAGE_MODEL_BACKENDS: dict[str, str] = {
    ImageModelOption.KREA.value: "krea2_comfyui",
    ImageModelOption.QWEN_IMAGE.value: "qwen_image_2512_comfyui",
    ImageModelOption.IDEOGRAM4_LOCAL.value: "ideogram4_local_comfyui",
}

# Short directory names for side-by-side comparison outputs:
# scenes/<NNN>/comparisons/{krea,qwen,ideogram}/visual.png
IMAGE_MODEL_DIRNAMES: dict[str, str] = {
    ImageModelOption.KREA.value: "krea",
    ImageModelOption.QWEN_IMAGE.value: "qwen",
    ImageModelOption.IDEOGRAM4_LOCAL.value: "ideogram",
}

_KNOWN_MODELS = tuple(ImageModelOption)

# Visual types that are still-image jobs eligible for model routing.
ROUTABLE_VISUAL_TYPES = {
    "image_motion",
    "text_overlay_still",
    "krea2_still",
    "ideogram4_still",
    "qwen_image_still",
    "flux_still",
}

# Keyword detector vocabulary. Deliberately conservative so scenic b-roll never
# flips into Ideogram: every term describes something that usually carries
# readable words inside the picture itself.
_TEXT_HINT_TERMS: tuple[str, ...] = (
    "thumbnail",
    "title card",
    "opening title",
    "closing title",
    "poster",
    "infographic",
    "map with labels",
    "labeled map",
    "sign",
    "signage",
    "billboard",
    "street sign",
    "neon sign",
    "storefront sign",
    "newspaper",
    "magazine cover",
    "document screenshot",
    "screenshot",
    "ui mockup",
    "user interface",
    "interface mockup",
    "app screen",
    "website mockup",
    "headline",
    "caption card",
    "lower third",
    "banner",
    "label",
    "diagram with",
    "annotated diagram",
    "flowchart",
    "timeline graphic",
    "chart with",
    "whiteboard",
    "blackboard",
    "menu board",
    "book cover",
    "license plate",
    "graffiti tag",
    "written word",
    "readable text",
    "embedded text",
    "on-screen text",
)


class SceneImageRouting(BaseModel):
    """Per-scene image-model routing decision plus authored metadata.

    These five fields mirror the scene schema columns added alongside this
    module; they are persisted on each Scene and in the storyboard artifact.
    """

    model_config = ConfigDict(extra="forbid")

    scene_number: int = Field(default=1, ge=1)
    needs_embedded_text: bool = False
    text_in_image: str = ""
    preferred_image_model: str = ImageModelOption.KREA.value
    test_generate_with_qwen: bool = False
    test_generate_with_ideogram: bool = False

    @property
    def comparison_pair(self) -> bool:
        """True when both variants should be rendered for side-by-side review."""
        return self.test_generate_with_qwen and self.test_generate_with_ideogram


def split_text_in_image(value: str | Iterable[str]) -> list[str]:
    """Normalize authored text-in-image metadata into distinct literal strings.

    Accepts either an iterable of strings or one string whose lines separate
    individual render targets ("HEADLINE\\nSUBTITLE").
    """

    items: list[str]
    if isinstance(value, str):
        items = value.splitlines()
    else:
        items = [str(item) for item in value]
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        literal = str(item)
        if literal and literal not in seen:
            seen.add(literal)
            result.append(literal)
    return result


def detect_needs_embedded_text(
    *,
    visual_type: str = "",
    title: str = "",
    visual_description: str = "",
    visual_type_description: str = "",
    text_in_image: str | Iterable[str] = (),
    authored_flag: bool | None = None,
) -> bool:
    """Decide whether a scene requires readable words inside the picture.

    A scene sets ``needs_embedded_text`` when any of the following hold:
    thumbnail scene, title card, poster, labeled map, infographic, sign,
    newspaper/document screenshot, UI mockup, exact text literals were authored
    for the image, or the director explicitly flagged it. Everything else
    defaults to False (plain cinematic imagery routes to Krea).
    """

    if authored_flag:
        return True
    literals = split_text_in_image(text_in_image)
    if literals:
        return True
    vt = (visual_type or "").strip().lower()
    if vt in {"title_card", "diagram"}:
        return True
    haystack = " ".join(
        part.lower()
        for part in (title, visual_description, visual_type_description)
        if part
    )
    return any(re.search(rf"\b{re.escape(term)}\b", haystack) for term in _TEXT_HINT_TERMS)


def resolve_preferred_image_model(
    *,
    needs_embedded_text: bool,
    authored_preference: str = "",
    visual_type: str = "image_motion",
    image_motion_source: str = "krea2",
) -> str:
    """Apply the documented routing rules for the primary image generator.

    Rules:
      * An explicit, valid authored preference always wins (user override).
      * ``needs_embedded_text`` scenes prefer Ideogram 4 (strongest local
        in-image text rendering under test).
      * Text-capable still types that already requested Qwen keep Qwen while
        the comparison window is open.
      * Everything else — normal cinematic coverage — prefers Krea.
    """

    preference = (authored_preference or "").strip()
    if preference in _KNOWN_MODELS:
        return preference
    vt = (visual_type or "").strip().lower()
    # Hybrid exact-text stills deliberately keep all lettering out of the
    # generated background. Their primary route is therefore Krea, not the
    # normal embedded-text Ideogram route.
    if vt == "text_overlay_still":
        return ImageModelOption.KREA.value
    if needs_embedded_text:
        return ImageModelOption.IDEOGRAM4_LOCAL.value
    if vt == "ideogram4_still":
        return ImageModelOption.IDEOGRAM4_LOCAL.value
    if vt == "qwen_image_still":
        return ImageModelOption.QWEN_IMAGE.value
    if vt == "image_motion" and image_motion_source == "qwen_image_2512":
        return ImageModelOption.QWEN_IMAGE.value
    return ImageModelOption.KREA.value


def build_scene_image_routing(
    *,
    scene_number: int,
    visual_type: str = "image_motion",
    image_motion_source: str = "krea2",
    title: str = "",
    visual_description: str = "",
    visual_type_description: str = "",
    text_in_image: str | Iterable[str] = (),
    authored_needs_embedded_text: bool | None = None,
    authored_preference: str = "",
    test_generate_with_qwen: bool = False,
    test_generate_with_ideogram: bool = False,
    comparison_mode: bool = False,
) -> SceneImageRouting:
    """Produce the full routing record for one scene, including test flags.

    ``comparison_mode`` (project/config level) forces both test flags on for
    any scene that needs embedded text, so Qwen and Ideogram versions are both
    rendered and saved separately for review.
    """

    needs_text = detect_needs_embedded_text(
        visual_type=visual_type,
        title=title,
        visual_description=visual_description,
        visual_type_description=visual_type_description,
        text_in_image=text_in_image,
        authored_flag=authored_needs_embedded_text,
    )
    preferred = resolve_preferred_image_model(
        needs_embedded_text=needs_text,
        authored_preference=authored_preference,
        visual_type=visual_type,
        image_motion_source=image_motion_source,
    )
    with_qwen = bool(test_generate_with_qwen)
    with_ideogram = bool(test_generate_with_ideogram)
    routable = (visual_type or "").strip().lower() in ROUTABLE_VISUAL_TYPES
    if routable and comparison_mode and needs_text:
        with_qwen = True
        with_ideogram = True
    return SceneImageRouting(
        scene_number=max(1, int(scene_number)),
        needs_embedded_text=needs_text,
        text_in_image="\n".join(split_text_in_image(text_in_image)),
        preferred_image_model=preferred,
        test_generate_with_qwen=with_qwen,
        test_generate_with_ideogram=with_ideogram,
    )


# ---------------------------------------------------------------------------
# Model-specific prompt builders (requirement 9 guidance lives in each builder)


def build_krea_prompt(
    visual_description: str,
    *,
    style: str = "documentary",
) -> str:
    """Krea prompt: cinematic natural-language imagery with NO embedded text.

    Krea is the preferred generator for general scenes precisely because it
    excels at cinematic realism; any words it tries to draw come out garbled,
    so the prompt steers toward text-free frames. Single-subject framing is
    enforced here even if the upstream visual_description was verbose: no
    collage, grid, or multi-panel wording survives.
    """

    description = visual_description.strip().rstrip(".")
    # Defensively strip any upstream collage/grid language that still slips
    # through (the director prompt now forbids it, but older plans remain).
    for banned in (
        "split-screen", "split screen", "diptych", "triptych", "collage",
        "grid", "multiple panels", "montage of",
    ):
        if banned in description.lower():
            description = description.lower().replace(banned, "single subject")
    style_part = style.strip() or "documentary"
    return (
        f"{description}. Cinematic {style_part} photography, strong single focal "
        "subject, one clear photoreal scene only, grounded realistic detail, deliberate composition, natural depth, "
        "frame entirely free of written words, lettering, signage, logos, labels, documents, captions, and watermarks; no collage, no grid, no split-screen."
    )


def build_qwen_prompt(
    visual_description: str,
    *,
    text_in_image: str | Iterable[str] = (),
) -> str:
    """Qwen prompt: current behavior preserved verbatim.

    Qwen Image stays available as a fallback and A/B test candidate, so its
    prompt recipe is intentionally unchanged: the scene description followed by
    the exact quoted literals it must render.
    """

    prompt = visual_description.strip()
    requested_text = split_text_in_image(text_in_image)
    if requested_text:
        literals = "\n".join(
            f"- {json.dumps(item, ensure_ascii=False)}" for item in requested_text
        )
        prompt = (
            f"{prompt}\nRender each of these quoted strings exactly once, with clear, "
            f"legible spelling:\n{literals}"
        )
    return prompt


def build_ideogram_prompt_json(
    visual_description: str,
    *,
    text_in_image: str | Iterable[str] = (),
    style: str = "documentary",
    title: str = "",
    prompt_mode: str = "quick",
    aspect_ratio: str | None = None,
    precise_json: str | dict[str, Any] | None = None,
    llm: Any | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper around the reusable two-mode prompt builder."""

    description = visual_description.strip() or title.strip() or "cinematic scene"
    idea = description
    if style.strip():
        idea = f"{idea}\nRequested visual style: {style.strip()}."
    result = build_ideogram_v4_prompt(
        idea if prompt_mode == "quick" else None,
        mode=prompt_mode,  # type: ignore[arg-type]
        aspect_ratio=aspect_ratio,
        precise_json=precise_json,
        llm=llm,
        text_literals=split_text_in_image(text_in_image),
    )
    return result["structured_prompt"]


def validate_ideogram_prompt_json(payload: Any) -> dict[str, Any]:
    """Validate and normalize with the typed native-caption model."""

    return _validate_ideogram_prompt_json(payload)


def serialize_ideogram_prompt_json(payload: dict[str, Any]) -> str:
    """Compact JSON string ready to substitute into the ComfyUI workflow."""

    return _serialize_ideogram_prompt_json(payload)


def scene_text_literals(scene_settings: dict[str, Any], text_in_image: str = "") -> list[str]:
    """Collect exact in-image strings from scene settings plus routing metadata.

    Reads the pre-existing sources (graphic_text for graphic screens,
    on_screen_text for Qwen stills/motion) and merges the new text_in_image
    field, preserving order and de-duplicating case-insensitively.
    """

    collected: list[str] = list(split_text_in_image(text_in_image))
    for key in ("on_screen_text", "graphic_text"):
        raw = scene_settings.get(key, [])
        if isinstance(raw, str):
            collected.extend(split_text_in_image(raw))
        elif isinstance(raw, list):
            collected.extend(
                str(item) for item in raw if str(item) != ""
            )
    seen: set[str] = set()
    unique: list[str] = []
    for item in collected:
        marker = item
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def storyboard_entry(scene: Any, *, style: str = "documentary") -> dict[str, Any]:
    """Build the per-scene storyboard record emitted by the script stage.

    Output fields per scene: scene_number, narration, visual_description,
    needs_embedded_text, text_in_image, preferred_image_model, qwen_prompt,
    krea_prompt, ideogram_prompt_mode, ideogram_prompt_json. Prompts stored on the scene
    (settings.image_prompts) are reused; missing ones are derived locally with
    the same builders so deterministic/mock plans produce complete storyboards.
    """

    settings = scene.settings if isinstance(scene.settings, dict) else {}
    stored_prompts = settings.get("image_prompts")
    stored_prompts = stored_prompts if isinstance(stored_prompts, dict) else {}
    literals = scene_text_literals(settings, getattr(scene, "text_in_image", ""))
    visual_description = getattr(scene, "visual_prompt", "")
    preferred = getattr(scene, "preferred_image_model", "automatic")
    preferred_value = (
        preferred.value if isinstance(preferred, ImageModelOption) else str(preferred)
    )
    if preferred_value == "automatic":
        motion_source = str(settings.get("image_motion_source", "krea2"))
        preferred_value = resolve_preferred_image_model(
            needs_embedded_text=bool(getattr(scene, "needs_embedded_text", False)),
            visual_type=str(getattr(getattr(scene, "visual_type", ""), "value", "")),
            image_motion_source=motion_source,
        )
    ideogram_json = stored_prompts.get("ideogram_prompt_json")
    if not isinstance(ideogram_json, dict):
        ideogram_json = build_ideogram_prompt_json(
            visual_description,
            text_in_image=literals,
            style=style,
            title=getattr(scene, "title", ""),
        )
        validate_ideogram_prompt_json(ideogram_json)
    return {
        "scene_number": int(getattr(scene, "index", 0)) + 1,
        "narration": getattr(scene, "narration", ""),
        "visual_description": visual_description,
        "needs_embedded_text": bool(getattr(scene, "needs_embedded_text", False)),
        "text_in_image": getattr(scene, "text_in_image", ""),
        "preferred_image_model": preferred_value,
        "ideogram_prompt_mode": str(settings.get("ideogram_prompt_mode", "quick")),
        "qwen_prompt": str(
            stored_prompts.get(
                "qwen_prompt",
                build_qwen_prompt(visual_description, text_in_image=literals),
            )
        ),
        "krea_prompt": str(
            stored_prompts.get(
                "krea_prompt",
                build_krea_prompt(visual_description, style=style),
            )
        ),
        "ideogram_prompt_json": ideogram_json,
    }
