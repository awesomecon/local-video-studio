"""Fish Audio S2 Pro delivery-tag (performance cue) logic.

S2 Pro interprets ``[square bracket]`` cues in the text it is given: open-domain
natural-language delivery directions such as ``[calm, conversational narration]``
or ``[emphasis]``. Fish documents no closed tag vocabulary.

The cues must never leak into anything else:

- Captions align against the *authored* transcript (scene narration), while
  delivery cues remain in this separate artifact.
- Chunking must be cue-aware so a cue on its own line can never become a
  chunk containing only ``[curious]`` (garbage audio).
- Only the ``fish_s2_pro`` provider ever receives tagged text; every other
  provider would read the brackets aloud.

This module is pure logic: no model, LLM, or I/O dependencies.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas import utc_now


class NoNarrationTextError(RuntimeError):
    """No narration text (scene or override) exists to tag.

    Mapped to HTTP 409 by the API, matching the "nothing to narrate" state
    of ``resolve_narration_text``.
    """


#: An S2 delivery cue: nonempty, non-nested text inside square brackets. Fish
#: documents open-domain descriptions and does not publish a cue-length limit.
TAG_RE = re.compile(r"\[([^\[\]\n]+)\]")

#: A run of ALL-CAPS letters (>= 4) that the LLM may not introduce.
_CAPS_RUN_RE = re.compile(r"[A-Z]{4,}")

#: Unicode word tokens: punctuation and whitespace are not word characters, so
#: comparing token sequences tolerates "light punctuation adjustment" while
#: still rejecting any added, deleted, or reordered spoken word.
_WORD_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

def strip_performance_tags(text: str) -> str:
    """Remove every delivery cue and collapse the remaining whitespace."""
    cleaned = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_tagged_layout(text: str) -> str:
    """Glue cue-only lines to the next content line, preserving paragraphs.

    A model (or a hand edit) may place a cue on its own line::

        [calm narration]
        I thought everything was normal.

    Left alone, blank-line paragraph splitting would turn the cue into a
    standalone chunk of pure bracket text.  Gluing it to the following line
    keeps the cue attached to the sentence it directs.  Blank lines (real
    paragraph breaks) and untagged text are preserved unchanged, and a
    trailing cue-only line with nothing to attach to is dropped: a pause cue
    with no following sentence has no audible effect.
    """
    result: list[str] = []
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # A real paragraph break: keep it (chunking prefers paragraphs).
            result.append("")
            continue
        if not strip_performance_tags(stripped):
            # Cue-only line: buffer it to glue to the next content line.
            pending.append(stripped)
            continue
        if pending:
            result.append(" ".join([*pending, stripped]))
            pending = []
        else:
            result.append(stripped)
    # Trailing pending cues have nothing to attach to: drop them.
    return "\n".join(result)


def count_spoken_words(text: str) -> int:
    """Count words that will actually be spoken (cues excluded)."""
    return len(strip_performance_tags(text).split())


def count_tags(text: str) -> int:
    """Count delivery cues in a piece of tagged text."""
    return len(TAG_RE.findall(text))


def cue_ceiling(source: str) -> int:
    """Length-scaled anti-over-tagging ceiling for one segment.

    A short line can carry only a few cues before they compete with
    themselves; longer narration earns more.  The floor of 3 keeps a natural
    delivery shift (a style cue plus a [pause] plus an [emphasis]) from being
    rejected on a short but cue-worthy line.
    """
    return max(3, count_spoken_words(source) // 15)


def _source_aware_tagged_view(
    source: str, tagged: str,
) -> tuple[str, str, list[str]]:
    """Separate inserted cues from bracketed text already present in source.

    Exact source bracket expressions remain spoken text for validation. Only
    additional bracket expressions are treated as performance cues.
    """
    source_brackets = Counter(match.group(0) for match in TAG_RE.finditer(source))
    spoken: list[str] = []
    structure: list[str] = []
    cues: list[str] = []
    cursor = 0
    for match in TAG_RE.finditer(tagged):
        spoken.append(tagged[cursor:match.start()])
        structure.append(tagged[cursor:match.start()])
        literal = match.group(0)
        if source_brackets[literal] > 0:
            source_brackets[literal] -= 1
            spoken.append(match.group(1))
            structure.append(" " * len(literal))
        else:
            spoken.append(" ")
            structure.append(literal)
            cues.append(match.group(1))
        cursor = match.end()
    spoken.append(tagged[cursor:])
    structure.append(tagged[cursor:])
    return "".join(spoken), "".join(structure), cues


def count_inserted_tags(source: str, tagged: str) -> int:
    """Count cues added beyond bracket expressions already in clean source."""
    return len(_source_aware_tagged_view(source, tagged)[2])


def _spoken_tokens(text: str) -> list[str]:
    return [token.lower() for token in _WORD_TOKEN_RE.findall(text)]


def validate_tagged(source: str, tagged: str) -> list[str]:
    """Reject, don't trust: verify a tagged text against its clean source.

    Returns a list of human-readable problems (empty when the tagged text is
    safe to send to the model).  The word-sequence check is the safety core:
    it stops the LLM from rewriting the script while adding cues.
    """
    errors: list[str] = []
    source = source.strip()
    tagged = tagged.strip()

    if not tagged:
        return ["tagged text is empty"]

    # --- Bracket structure: no nested, unbalanced, empty, or multi-line cues.
    spoken_text, cue_structure, cues = _source_aware_tagged_view(source, tagged)
    depth = 0
    for char in cue_structure:
        if char == "\n" and depth:
            errors.append("multi-line cues are not allowed")
            break
        if char == "[":
            depth += 1
            if depth > 1:
                errors.append("nested cues are not allowed")
                break
        elif char == "]":
            depth -= 1
            if depth < 0:
                errors.append("unbalanced brackets: a ']' without a matching '['")
                break
    else:
        if depth > 0:
            errors.append("unbalanced brackets: a '[' without a matching ']'")
    if "[]" in cue_structure or re.search(r"\[\s*\]", cue_structure):
        errors.append("empty cues are not allowed")

    # --- Word sequence: identical spoken words, in order.  Punctuation drift
    # is tolerated (tokens are word characters only); anything else is a
    # rewrite of the script and is rejected.
    source_tokens = _spoken_tokens(source)
    tagged_tokens = _spoken_tokens(spoken_text)
    if tagged_tokens != source_tokens:
        if len(tagged_tokens) != len(source_tokens):
            errors.append(
                "spoken words were added or deleted "
                f"({len(tagged_tokens)} vs {len(source_tokens)} in the source)"
            )
        else:
            errors.append("spoken words were changed or reordered")

    # --- Cue content rules.
    # Length guard: growth may come from the open-domain cues themselves, with
    # a small allowance for the punctuation changes Fish recommends permitting.
    max_length = len(source) + sum(len(cue) + 2 for cue in cues) + 32
    if len(tagged) > max_length:
        errors.append(
            f"tagged text is {len(tagged)} characters but the source is "
            f"{len(source)} with {len(cues)} cue(s); the cues add too much text"
        )
    # Uppercase delivery descriptions such as Fish's documented
    # ``[NARRATOR, low and slow]`` are valid. Only reject capitalization newly
    # introduced into the words that will actually be spoken.
    for run in _CAPS_RUN_RE.findall(spoken_text):
        if run not in source:
            errors.append(f"new ALL-CAPS run {run!r} is not in the source text")

    # --- Anti-over-tagging: a length-scaled ceiling on inserted cues.
    ceiling = cue_ceiling(source)
    if len(cues) > ceiling:
        errors.append(
            f"{len(cues)} cues exceed the anti-over-tagging ceiling of "
            f"{ceiling} for this text"
        )

    return errors


class PerformanceSegment(BaseModel):
    """One tagged narration segment: the clean source plus its cue-annotated form."""

    model_config = ConfigDict(extra="forbid")

    #: Stable identity: ``scene:<scene id>`` or ``override`` for a script
    #: override run.  Chunk mapping matches on this key.
    key: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1)
    tagged: str = Field(min_length=1)
    scene_id: str | None = None
    scene_index: int | None = Field(default=None, ge=0)
    scene_title: str | None = None


class PerformanceScript(BaseModel):
    """Portable, human-readable delivery-tag artifact for one narration run."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    provider: Literal["fish_s2_pro"] = "fish_s2_pro"
    #: Local LLM model that authored the cues ("" when unknown).
    model: str = ""
    generated_at: datetime = Field(default_factory=utc_now)
    #: sha256 of the joined segment sources at generation time; powers the
    #: "script changed since tags were generated" staleness hint.
    source_sha256: str = ""
    intensity: Literal["subtle", "balanced", "expressive"] = "balanced"
    segments: list[PerformanceSegment] = Field(default_factory=list)

    @property
    def tag_count(self) -> int:
        return sum(
            count_inserted_tags(segment.source, segment.tagged)
            for segment in self.segments
        )

    @property
    def source_text(self) -> str:
        return "\n\n".join(segment.source for segment in self.segments)
