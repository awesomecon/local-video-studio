"""Local-LLM orchestration for Fish S2 Pro delivery tags.

Mirrors :class:`backend.director.engine.DirectorEngine`: the tagger takes an
injectable LLM backend (``None`` in mock mode) and degrades per batch instead
of failing the whole run.  Reasoning stays enabled — AGENTS.md forbids
shrinking the thinking budget as a latency optimization.  The budget here is
deliberately smaller than the director's 10k because tagging is a local edit
of existing text, not plan authoring: 3k tokens of reasoning is ample for
deciding cue placement over at most ~800 spoken words.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.local_llm import LocalLLMBackend

from .performance import (
    PerformanceScript,
    PerformanceSegment,
    count_spoken_words,
    normalize_tagged_layout,
    validate_tagged,
)

logger = logging.getLogger(__name__)

INTENSITIES = ("subtle", "balanced", "expressive")

# Batching bounds: a 128-scene project must never truncate one completion, so
# each batch is capped by spoken words *and* segment count.
_BATCH_MAX_WORDS = 800
_BATCH_MAX_SEGMENTS = 12

THINKING_BUDGET_TOKENS = 3_000
RESPONSE_BASE_TOKENS = 512
RESPONSE_TOKENS_PER_WORD = 2


class PerformanceTaggedSegment(BaseModel):
    """One model-authored tagged segment; ``index`` echoes the input order."""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(default=0, ge=0)
    tagged: str = ""


class PerformanceTaggedBatch(BaseModel):
    """Schema-constrained response for one bounded tagging batch."""

    model_config = ConfigDict(extra="ignore")

    segments: list[PerformanceTaggedSegment] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_batch(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        raw = payload.get("segments")
        if isinstance(raw, dict):
            raw = [raw]
        segments: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    segments.append(dict(item))
                elif isinstance(item, str) and item.strip():
                    segments.append({"tagged": item.strip()})
        payload["segments"] = segments
        return payload


_SYSTEM_PROMPT = """You are a voice-delivery director for Fish Audio S2 Pro narration.
You receive numbered narration segments. For each one, return the exact same spoken words with [square bracket] delivery cues inserted.
{context_block}
S2 Pro cue syntax (verified from the Fish Audio docs):
- Tags are open-domain natural-language instructions in [square brackets], not a fixed vocabulary. Write what you would tell a voice actor.
- Place each tag immediately before the word or phrase it should affect. It applies from that point until the next tag or the end of the sentence.
- Write tags in the narration language when appropriate; S2 understands multilingual tag descriptions.
- Well-tested breathing/reaction tags: [sigh] [inhale] [exhale] [gasp] [panting] [clears throat].
- Well-tested vocal sounds: [laughing] [chuckling] [giggle] [sobbing] [crying] [groan].
- Well-tested pacing tags: [pause] [short pause] [long pause].
- Well-tested voice styles: [whispering] [soft voice] [loud voice] [shouting] [low voice].
- Well-tested emotions and emphasis: [excited] [angry] [sad] [surprised] [emphasis].
- Other useful controls include [rustling sound], [speaking slowly, almost hesitant], [professional broadcast tone], [pitch up], and other clear free-form descriptions.
- Physical delivery and emotion can be paired when useful: [panting] [tired] ..., [whispering] [scared] ..., [shouting] [angry] ...

Hard rules:
- Never change, add, delete, or reorder any spoken word. Only insert cues. Light punctuation adjustment is acceptable.
- One primary emotion at a time; never mix conflicting emotions.
- Place a tag exactly where its effect should begin, including mid-sentence for word-level emphasis or delivery shifts.
- Do not tag every sentence. Add a tag only where an intentional delivery shift or reaction improves the narration, and never repeat an identical cue back to back.
- Use [pause], [short pause], or [long pause] where the delivery needs silence.
- Always follow a descriptive tag with spoken text; never leave a descriptive tag by itself.
- Start simple and layer only when a single tag is not enough; over-tagging competes with itself.
- Vary question style (curiosity / confusion / suspicion / disbelief / rhetorical) per context.
- Target natural long-form YouTube narration, not cartoon acting.
- Keep free-form descriptions specific and readable.
- Intensity: {intensity} (subtle = a few cues, balanced = moderate, expressive = frequent but still natural).
{notes_line}
Return only the JSON object constrained by the provided response schema. Echo each segment's index unchanged."""


class PerformanceTagger:
    """Tag narration segments with S2 Pro delivery cues via the local LLM."""

    def __init__(self, llm: LocalLLMBackend | None = None) -> None:
        self.llm = llm

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _segment_label(segment: PerformanceSegment) -> str:
        if segment.scene_index is not None and segment.scene_title:
            return f"Scene {segment.scene_index + 1} · {segment.scene_title}"
        if segment.scene_index is not None:
            return f"Scene {segment.scene_index + 1}"
        return "Script override"

    @staticmethod
    def _system_prompt(intensity: str, notes: str, context: str = "") -> str:
        notes_line = (
            f"Focus notes from the creator: {notes.strip()}"
            if notes.strip() else ""
        )
        context_block = (
            f"About this video:\n{context.strip()}\n"
            if context.strip() else ""
        )
        return _SYSTEM_PROMPT.format(
            intensity=intensity if intensity in INTENSITIES else "balanced",
            notes_line=notes_line,
            context_block=context_block,
        )

    @staticmethod
    def build_messages(
        segments: list[PerformanceSegment],
        *,
        intensity: str = "balanced",
        notes: str = "",
        language: str = "en",
        context: str = "",
    ) -> list[dict[str, str]]:
        lines: list[str] = [
            f"Narration language: {language or 'en'}.",
            "Tag every segment below. Keep the spoken words exactly as written.",
        ]
        for position, segment in enumerate(segments):
            lines.append(
                f"Segment {position} "
                f"({PerformanceTagger._segment_label(segment)}):"
            )
            lines.append(segment.source)
            lines.append("")
        return [
            {
                "role": "system",
                "content": PerformanceTagger._system_prompt(intensity, notes, context),
            },
            {"role": "user", "content": "\n".join(lines).rstrip()},
        ]

    @staticmethod
    def _batch_schema() -> dict[str, Any]:
        schema = PerformanceTaggedBatch.model_json_schema()
        schema["required"] = list(schema["properties"])
        schema["additionalProperties"] = False
        return schema

    @staticmethod
    def _max_completion_tokens(batch: list[PerformanceSegment]) -> int:
        words = sum(count_spoken_words(segment.source) for segment in batch)
        return (
            THINKING_BUDGET_TOKENS
            + RESPONSE_BASE_TOKENS
            + RESPONSE_TOKENS_PER_WORD * words
        )

    @staticmethod
    def _batches(
        segments: list[PerformanceSegment],
    ) -> list[list[PerformanceSegment]]:
        batches: list[list[PerformanceSegment]] = []
        current: list[PerformanceSegment] = []
        words = 0
        for segment in segments:
            count = count_spoken_words(segment.source)
            if current and (
                words + count > _BATCH_MAX_WORDS
                or len(current) >= _BATCH_MAX_SEGMENTS
            ):
                batches.append(current)
                current, words = [], 0
            current.append(segment)
            words += count
        if current:
            batches.append(current)
        return batches

    # ------------------------------------------------------------------
    # Completion with per-batch degradation
    # ------------------------------------------------------------------

    def _complete_batch(
        self,
        messages: list[dict[str, str]],
        batch: list[PerformanceSegment],
        *,
        model: str,
    ) -> PerformanceTaggedBatch:
        assert self.llm is not None
        payload = self.llm.complete(
            messages=messages,
            structured=True,
            json_schema=self._batch_schema(),
            validator=PerformanceTaggedBatch.model_validate,
            max_tokens=self._max_completion_tokens(batch),
            temperature=0.4,
            thinking_budget_tokens=THINKING_BUDGET_TOKENS,
            model=model or None,
        )
        return (
            payload
            if isinstance(payload, PerformanceTaggedBatch)
            else PerformanceTaggedBatch.model_validate(payload)
        )

    @staticmethod
    def _map_candidates(
        batch: list[PerformanceSegment], draft: PerformanceTaggedBatch,
    ) -> list[str | None]:
        """Map the model's segments back to batch positions.

        Indices are trustworthy only if they form a permutation of 0..n-1;
        otherwise (e.g. the model omitted the field and every segment
        defaulted to 0) we map positionally instead.
        """
        by_index: dict[int, str] = {}
        indices = [item.index for item in draft.segments]
        if len(draft.segments) == len(batch) and sorted(indices) == list(range(len(batch))):
            by_index = {
                item.index: item.tagged for item in draft.segments
                if isinstance(item.tagged, str)
            }
        candidates: list[str | None] = []
        for position in range(len(batch)):
            candidate = by_index.get(position)
            if candidate is None and len(draft.segments) == len(batch):
                # Out-of-order or missing index: fall back positionally.
                candidate = draft.segments[position].tagged or None
            candidates.append(candidate)
        return candidates

    def _repair_segment(
        self,
        segment: PerformanceSegment,
        candidate: str | None,
        errors: list[str],
        *,
        intensity: str,
        notes: str,
        language: str,
        model: str,
        context: str = "",
    ) -> str | None:
        """Regenerate one failed segment, telling the LLM what failed last time.

        The re-tag prompt carries the full rule set, the previous attempt,
        and the exact validation errors, so the model corrects the specific
        problem instead of re-rolling the whole batch.  Returns the repaired
        tagged text when it validates, else ``None`` so the caller falls back
        to the clean source.
        """
        previous = (
            normalize_tagged_layout(candidate)
            if candidate is not None
            else "(the segment was missing from your response)"
        )
        user = (
            f"Narration language: {language or 'en'}.\n"
            "Re-tag the single segment below. Your previous attempt failed "
            "validation:\n"
            + "\n".join(f"- {error}" for error in errors)
            + f"\n\nPrevious attempt:\n{previous}\n\n"
            f"Segment 0 ({self._segment_label(segment)}):\n"
            f"{segment.source}\n\n"
            "Fix the problems above. Keep every spoken word exactly as "
            "written, insert only valid [square bracket] cues, and echo the "
            "segment index."
        )
        messages = [
            {"role": "system", "content": self._system_prompt(intensity, notes, context)},
            {"role": "user", "content": user},
        ]
        try:
            draft = self._complete_batch(messages, [segment], model=model)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the run
            logger.warning(
                "Performance-tag repair failed for segment %s: %s",
                segment.key, exc,
            )
            return None
        if not draft.segments:
            return None
        repaired = draft.segments[0].tagged
        if not isinstance(repaired, str) or not repaired:
            return None
        normalized = normalize_tagged_layout(repaired)
        if validate_tagged(segment.source, normalized):
            return None
        return normalized

    def _tag_batch(
        self,
        batch: list[PerformanceSegment],
        *,
        intensity: str,
        notes: str,
        language: str,
        model: str,
        context: str = "",
        warnings: list[str],
    ) -> list[str]:
        """Return tagged text per segment, degrading per segment on failure.

        Segments that fail validation are regenerated individually with the
        local LLM, which is told exactly what failed last time; a segment
        that still fails after the repair degrades to its clean source.
        """
        messages = self.build_messages(
            batch, intensity=intensity, notes=notes, language=language,
            context=context,
        )
        try:
            draft = self._complete_batch(messages, batch, model=model)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the run
            logger.warning("Performance-tag batch failed: %s", exc)
            warnings.append(
                f"Delivery tagging failed for {len(batch)} segment(s): {exc}"
            )
            return [segment.source for segment in batch]

        candidates = self._map_candidates(batch, draft)

        results: list[str] = []
        for position, (segment, candidate) in enumerate(zip(batch, candidates)):
            normalized = (
                normalize_tagged_layout(candidate)
                if candidate is not None
                else None
            )
            errors = (
                ["the segment was missing from the model response"]
                if normalized is None
                else validate_tagged(segment.source, normalized)
            )
            if not errors:
                results.append(normalized)
                continue
            repaired = self._repair_segment(
                segment, candidate, errors,
                intensity=intensity, notes=notes,
                language=language, model=model, context=context,
            )
            if repaired is not None:
                results.append(repaired)
                continue
            warnings.append(
                f"Delivery tags rejected for segment {position} "
                f"({'; '.join(errors[:2])}); the clean source text was used."
            )
            results.append(segment.source)
        return results

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def tag(
        self,
        segments: list[PerformanceSegment],
        *,
        intensity: str = "balanced",
        notes: str = "",
        language: str = "en",
        model: str = "",
        context: str = "",
    ) -> tuple[PerformanceScript, list[str]]:
        """Tag every segment; returns the script plus per-segment warnings."""
        if self.llm is None:
            raise RuntimeError("no local LLM is available for delivery tagging")
        warnings: list[str] = []
        tagged_by_position: list[str] = []
        for batch in self._batches(segments):
            if len(batch) == 1 and count_spoken_words(batch[0].source) > _BATCH_MAX_WORDS:
                warnings.append(
                    f"Delivery tagging skipped segment {batch[0].key}: it exceeds "
                    f"the {_BATCH_MAX_WORDS}-spoken-word batch limit."
                )
                tagged_by_position.append(batch[0].source)
                continue
            tagged_by_position.extend(
                self._tag_batch(
                    batch, intensity=intensity, notes=notes,
                    language=language, model=model, context=context,
                    warnings=warnings,
                )
            )
        script = PerformanceScript(
            model=model,
            intensity=intensity if intensity in INTENSITIES else "balanced",
            segments=[
                PerformanceSegment(
                    **{
                        "key": segment.key,
                        "source": segment.source,
                        "tagged": tagged,
                        "scene_id": segment.scene_id,
                        "scene_index": segment.scene_index,
                        "scene_title": segment.scene_title,
                    }
                )
                for segment, tagged in zip(segments, tagged_by_position)
            ],
        )
        return script, warnings
