"""Dedicated, schema-constrained local-LLM Graphic Screen generator."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from backend.models.errors import BackendError, BackendErrorCode
from backend.schemas import Project, Scene
from backend.storage.generation_cache import GenerationCache

from .models import GraphicScreenResponse
from .sanitize import ALLOWED_CSS_PROPERTIES, GraphicScreenValidationError, sanitize_graphic_screen

logger = logging.getLogger(__name__)


class GraphicScreenGenerator:
    PROMPT_VERSION = "graphic-screen-prompt-v2"
    WORKFLOW_VERSION = "graphic-screen-v1"
    SANITIZER_VERSION = "graphic-screen-sanitizer-v1"
    # llama.cpp counts hidden reasoning and the schema-constrained response against
    # max_tokens. Graphic Screens need more final-response room than director plans
    # because the JSON contains the complete HTML/CSS/SVG source.
    REASONING_BUDGET_TOKENS = 16_384
    RESPONSE_BUDGET_TOKENS = 16_384
    MAX_COMPLETION_TOKENS = REASONING_BUDGET_TOKENS + RESPONSE_BUDGET_TOKENS
    # The rejected draft is fed back as an assistant turn so the repair prompt has
    # something concrete to fix; bound it so retries stay cheap.
    REPAIR_DRAFT_LIMIT = 24_000
    # Transient local-server failures (HTTP 5xx, timeouts, dropped connections) get one
    # in-process retry after a short delay. Observed beellama bursts fail instantly and
    # recover within minutes; failing the whole job made the user retry by hand.
    TRANSIENT_RETRY_DELAY_SECONDS = 20.0

    def __init__(self, llm: Any, cache: GenerationCache | None = None) -> None:
        self.llm = llm
        self.cache = cache
        self.selected_model = "auto"
        self.cache_hit = False
        self.cache_key: str | None = None
        # Safe per-attempt rejection reasons (no model-authored source) so the
        # pipeline can record why a repair pass was needed instead of a bare flag.
        self.attempt_errors: list[str] = []

    def generate(self, project: Project, scene: Scene) -> tuple[GraphicScreenResponse, str, list[str], int]:
        self.cache_hit = False
        self.cache_key = None
        settings = scene.settings.get("graphic_screen", {})
        settings = settings if isinstance(settings, dict) else {}
        configured_instructions = str(settings.get("instructions", "")).strip()
        instructions = (configured_instructions or scene.visual_prompt)[:8_000]
        exact_text = settings.get("exact_text", [])
        exact_text = [str(item)[:500] for item in exact_text] if isinstance(exact_text, list) else []
        schema = GraphicScreenResponse.model_json_schema()
        model = project.selected_llm_model if project.selected_llm_model != "auto" else None
        if model is None and hasattr(self.llm, "selected_model"):
            model = self.llm.selected_model()
        self.selected_model = model or "auto"
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._request_text(project, scene, instructions, exact_text)},
        ]
        cache_key = self._cache_key(messages, schema)
        self.cache_key = cache_key
        served = self._serve_cached_response(
            cache_key,
            exact_text=exact_text,
            width=project.resolution[0],
            height=project.resolution[1],
        )
        if served is not None:
            return served
        for attempt in (1, 2):
            draft: Any = None
            response: GraphicScreenResponse | None = None
            try:
                draft = self.llm.complete(
                    messages=messages, structured=True, json_schema=schema,
                    validator=lambda payload: self._validate_response(
                        payload,
                        title=scene.title or project.title or "Graphic Screen",
                        summary=instructions or scene.narration,
                        exact_text=exact_text,
                    ),
                    max_tokens=self.MAX_COMPLETION_TOKENS,
                    temperature=0.2,
                    model=model,
                    thinking_budget_tokens=self.REASONING_BUDGET_TOKENS,
                )
                response = draft if isinstance(draft, GraphicScreenResponse) else GraphicScreenResponse.model_validate(draft)
                if exact_text and response.visible_text != exact_text:
                    raise GraphicScreenValidationError(
                        "visible text does not exactly match the user-authored text manifest"
                    )
                document, visible = sanitize_graphic_screen(
                    response, width=project.resolution[0], height=project.resolution[1],
                )
                self._store_response(cache_key, response)
                return response, document, visible, attempt
            except (GraphicScreenValidationError, ValueError) as exc:
                reason = str(exc) or exc.__class__.__name__
                self.attempt_errors.append(reason[:200])
                if attempt == 2:
                    error = GraphicScreenValidationError(
                        "Graphic Screen response failed structural validation: "
                        f"{reason[:200]}"
                    )
                    error.attempt_count = attempt  # type: ignore[attr-defined]
                    raise error from exc
                self._append_repair_turns(messages, draft, reason)
            except BackendError as exc:
                # A syntactically valid but incomplete model payload is repairable in the
                # same bounded second attempt as a sanitizer rejection. Retryable
                # transport failures (HTTP 5xx, timeouts, dropped connections) instead
                # get one delayed retry of the untouched conversation; token-limit and
                # other non-retryable responses remain user-triggered retries.
                if not exc.retryable and exc.code is not BackendErrorCode.INVALID_RESPONSE:
                    raise
                self.attempt_errors.append(str(exc)[:200])
                if attempt == 2:
                    exc.attempt_count = attempt  # type: ignore[attr-defined]
                    raise
                if exc.retryable:
                    time.sleep(self.TRANSIENT_RETRY_DELAY_SECONDS)
                    continue
                self._append_repair_turns(messages, draft, str(exc))
        raise AssertionError("unreachable")

    def _cache_key(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str | None:
        if self.cache is None:
            return None
        return self.cache.key_hash({
            "kind": "graphic_screen_response",
            "prompt_version": self.PROMPT_VERSION,
            "workflow_version": self.WORKFLOW_VERSION,
            "sanitizer_version": self.SANITIZER_VERSION,
            "schema": schema,
            "model": self.selected_model,
            "temperature": 0.2,
            "max_tokens": self.MAX_COMPLETION_TOKENS,
            "messages": messages,
        })

    def _serve_cached_response(
        self,
        cache_key: str | None,
        *,
        exact_text: list[str],
        width: int,
        height: int,
    ) -> tuple[GraphicScreenResponse, str, list[str], int] | None:
        """Replay a cached validated response through the same safety checks.

        Returns None on any miss or rejection so generation falls back to the
        normal bounded LLM flow; the cache only ever replaces the model call.
        """
        if cache_key is None or self.cache is None:
            return None
        cached = self.cache.lookup("local_graphic", cache_key)
        if cached is None:
            return None
        try:
            response = GraphicScreenResponse.model_validate_json(
                cached.path.read_text(encoding="utf-8")
            )
            if exact_text and response.visible_text != exact_text:
                raise ValueError("cached response does not match the requested text manifest")
            document, visible = sanitize_graphic_screen(response, width=width, height=height)
        except Exception as exc:
            logger.warning(
                "Graphic Screen cache entry %.12s was rejected (%s); regenerating",
                cache_key, exc,
            )
            return None
        self.cache_hit = True
        return response, document, visible, 1

    def _store_response(self, cache_key: str | None, response: GraphicScreenResponse) -> None:
        if cache_key is None or self.cache is None or self.cache_hit:
            return
        self.cache.store(
            "local_graphic", cache_key, response.model_dump_json().encode("utf-8"),
            metadata={
                "prompt_version": self.PROMPT_VERSION,
                "workflow_version": self.WORKFLOW_VERSION,
                "sanitizer_version": self.SANITIZER_VERSION,
                "model": self.selected_model,
            },
        )

    def _append_repair_turns(self, messages: list[dict[str, str]], draft: Any, reason: str) -> None:
        """Echo the rejected draft back (bounded) and name the concrete failure.

        Without the assistant turn the model never sees what "the previous response"
        was, so repairs were blind retries that usually failed the same way.
        """

        draft_text = ""
        if draft is not None:
            try:
                draft_text = json.dumps(
                    draft.model_dump(mode="json") if isinstance(draft, GraphicScreenResponse) else draft,
                    ensure_ascii=False,
                )[: self.REPAIR_DRAFT_LIMIT]
            except (TypeError, ValueError):
                draft_text = ""
        if draft_text:
            messages.append({"role": "assistant", "content": draft_text})
        messages.append({
            "role": "user",
            "content": (
                f"That response was rejected: {reason[:400]}. "
                "Return one corrected complete JSON object with exactly the fields "
                "title, design_summary, visible_text, html_body, css. html_body is a "
                "body fragment only — no doctype, html, head, or body elements. Keep "
                "every visible text node exactly equal to the visible_text entries in "
                "DOM order. Return JSON only."
            ),
        })

    @staticmethod
    def _validate_response(
        payload: Any,
        *,
        title: str,
        summary: str,
        exact_text: list[str],
    ) -> GraphicScreenResponse:
        """Recover application metadata while leaving authored visual fields strict."""

        if not isinstance(payload, Mapping):
            return GraphicScreenResponse.model_validate(payload)
        candidate = dict(payload)
        # Local models occasionally rename the container field (observed: `body`).
        # Recover only unambiguous aliases; everything else stays strict.
        if "html_body" not in candidate:
            for alias in ("body", "html", "fragment"):
                value = candidate.pop(alias, None)
                if isinstance(value, str) and value.strip():
                    candidate["html_body"] = value
                    break
        candidate.setdefault("schema_version", 1)
        if candidate.get("schema_version") == "1":
            candidate["schema_version"] = 1
        if not str(candidate.get("title", "")).strip():
            candidate["title"] = title[:300]
        if not str(candidate.get("design_summary", "")).strip():
            candidate["design_summary"] = (summary or "Static Graphic Screen")[:2_000]
        if exact_text and not candidate.get("visible_text"):
            candidate["visible_text"] = exact_text
        return GraphicScreenResponse.model_validate(candidate)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "Design one static local Graphic Screen. Return only a JSON object with "
            "exactly these fields: title, design_summary, visible_text, html_body, css. "
            "html_body is a body fragment with only semantic layout elements (main, "
            "section, div, header, footer, h1-h3, p, span, strong, em, ul, ol, li, br, "
            "table, thead, tbody, tr, th, td) and optional inline SVG. "
            "No HTML document, JavaScript, animation, URLs, images, imports, forms, or external assets. "
            "CSS may use only ordinary element, class, ID, or descendant selectors and static "
            "layout/presentation rules; do not use escapes, comments, pseudo selectors, generated "
            "content, or text-hiding styles. Restrict CSS properties to exactly this list: "
            f"{', '.join(sorted(ALLOWED_CSS_PROPERTIES))}. Never use vendor-prefixed properties "
            "(such as -webkit-background-clip), custom properties (--*), @media, @keyframes, "
            "pseudo-elements, or the content property. "
            "Every visible text node, including chart labels and numbers, must appear in visible_text in exact DOM order. "
            "The finished design must fit at the requested canvas size without scrolling. Keep all text and important "
            "graphics inside an 8% top/bottom and 6% left/right mobile-safe area. Give every text region its own "
            "non-overlapping layout box; text must never cross another label, number, connector, icon, or canvas edge. "
            "Use box-sizing:border-box, bounded font sizes, generous line-height, and explicit grid or flex gaps. "
            "Do not place a subtitle over a larger number or use decorative lines through text. Prefer fewer, smaller "
            "elements and intentional negative space over filling the canvas."
        )

    @staticmethod
    def _request_text(project: Project, scene: Scene, instructions: str, exact_text: list[str]) -> str:
        return (
            f"Canvas: {project.resolution[0]}x{project.resolution[1]}\n"
            f"Style: {project.style}\nAudience: {project.audience}\nNarration: {scene.narration[:8_000]}\n"
            f"Graphic instructions: {instructions}\n"
            f"Required exact on-screen text (use these when supplied): {exact_text}"
        )
