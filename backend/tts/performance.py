"""Delivery-tag (performance cue) logic for the tagged TTS providers.

Two providers consume in-band delivery markup, each with its own vocabulary:

- ``fish_s2_pro``: open-domain ``[square bracket]`` natural-language cues
  such as ``[calm, conversational narration]`` or ``[emphasis]``. Fish
  documents no closed tag vocabulary.
- ``higgs_tts_3``: a fixed set of ``<|category:value|>`` inline control
  tokens (emotion / style / sfx / prosody), verified from the official
  SGLang-Omni cookbook and passed to the engine verbatim.

Cues must never leak into anything else:

- Captions align against the *authored* transcript (scene narration), while
  delivery cues remain in this separate, provider-scoped artifact.
- Chunking must be cue-aware so a cue on its own line can never become a
  chunk containing only ``[curious]`` or ``<|prosody:pause|>`` (garbage
  audio).
- Only the provider a script was generated for ever receives tagged text;
  every other provider would read the markup aloud.

This module is pure logic: no model, LLM, or I/O dependencies.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas import utc_now


class NoNarrationTextError(RuntimeError):
    """No narration text (scene or override) exists to tag.

    Mapped to HTTP 409 by the API, matching the "nothing to narrate" state
    of ``resolve_narration_text``.
    """


#: Providers whose TTS engine interprets in-band delivery markup.
PERFORMANCE_TAG_PROVIDERS: tuple[str, ...] = ("fish_s2_pro", "higgs_tts_3")
PerformanceProvider = Literal["fish_s2_pro", "higgs_tts_3"]


def _require_supported_provider(provider: str) -> None:
    if provider not in PERFORMANCE_TAG_PROVIDERS:
        raise ValueError(f"unknown performance-tag provider: {provider!r}")


#: An S2 delivery cue: nonempty, non-nested text inside square brackets. Fish
#: documents open-domain descriptions and does not publish a cue-length limit.
TAG_RE = re.compile(r"\[([^\[\]\n]+)\]")

#: A run of ALL-CAPS letters (>= 4) that the LLM may not introduce.
_CAPS_RUN_RE = re.compile(r"[A-Z]{4,}")

#: Unicode word tokens: punctuation and whitespace are not word characters, so
#: comparing token sequences tolerates "light punctuation adjustment" while
#: still rejecting any added, deleted, or reordered spoken word.
_WORD_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# ---------------------------------------------------------------------------
# Higgs TTS 3 inline control tokens
# ---------------------------------------------------------------------------

#: Official Higgs Audio v3 inline control vocabulary, verified from the
#: SGLang-Omni cookbook (docs/cookbook/higgs_tts.md).
HIGGS_EMOTIONS = frozenset({
    "elation", "amusement", "enthusiasm", "determination", "pride",
    "contentment", "affection", "relief", "contemplation", "confusion",
    "surprise", "awe", "longing", "arousal", "anger", "fear", "disgust",
    "bitterness", "sadness", "shame", "helplessness",
})
HIGGS_STYLES = frozenset({"singing", "shouting", "whispering"})
HIGGS_SFX = frozenset({
    "cough", "laughter", "crying", "screaming", "burping",
    "humming", "sigh", "sniff", "sneeze",
})
HIGGS_PROSODY_SPEED = frozenset({
    "speed_very_slow", "speed_slow", "speed_fast", "speed_very_fast",
})
HIGGS_PROSODY_PITCH = frozenset({"pitch_low", "pitch_high"})
HIGGS_PROSODY_PAUSES = frozenset({"pause", "long_pause"})
HIGGS_PROSODY_DELIVERY = frozenset({"expressive_high", "expressive_low"})

HIGGS_CATEGORIES: dict[str, frozenset[str]] = {
    "emotion": HIGGS_EMOTIONS,
    "style": HIGGS_STYLES,
    "sfx": HIGGS_SFX,
    "prosody": (
        HIGGS_PROSODY_SPEED | HIGGS_PROSODY_PITCH
        | HIGGS_PROSODY_PAUSES | HIGGS_PROSODY_DELIVERY
    ),
}
HIGGS_CONTROL_TOKENS = frozenset(
    f"<|{category}:{value}|>"
    for category, values in HIGGS_CATEGORIES.items()
    for value in values
)

#: Cookbook rule 2: every sound effect lands best paired with the written
#: sound right after it.  The LLM may add exactly these onomatopoeia words,
#: immediately after their sfx token and nowhere else.
HIGGS_SFX_ONOMATOPOEIA: dict[str, frozenset[str]] = {
    "cough": frozenset({"ahem"}),
    "laughter": frozenset({"haha", "hehe"}),
    "crying": frozenset({"boohoo", "sob"}),
    "screaming": frozenset({"ahh", "aaah"}),
    "burping": frozenset({"burp"}),
    "humming": frozenset({"hmm", "mmm"}),
    "sigh": frozenset({"uh", "ahh"}),
    "sniff": frozenset({"sff"}),
    "sneeze": frozenset({"achoo"}),
}

#: A well-formed ``<|category:value|>`` token; anything else stays text and
#: is caught by the spoken-words check if it sneaks into a tagged segment.
HIGGS_TOKEN_RE = re.compile(r"<\|([a-z_]+):([a-z0-9_]+)\|>")

#: Every ``<|`` run, malformed included: strips token-shaped text out of
#: spoken-word counts and cue-only line checks.
HIGGS_BROAD_TOKEN_RE = re.compile(r"<\|[^|\n]*\|?>")


def strip_performance_tags(text: str, provider: str = "fish_s2_pro") -> str:
    """Remove every delivery cue and collapse the remaining whitespace.

    Fish cues are ``[square brackets]``; Higgs cues are ``<|...|>`` control
    tokens (bracket text is plain text for Higgs and stays spoken).
    """
    _require_supported_provider(provider)
    if provider == "higgs_tts_3":
        cleaned = HIGGS_BROAD_TOKEN_RE.sub(" ", text)
    else:
        cleaned = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_tagged_layout(text: str, provider: str = "fish_s2_pro") -> str:
    """Glue cue-only lines to the next content line, preserving paragraphs.

    A model (or a hand edit) may place a cue on its own line::

        [calm narration]
        I thought everything was normal.

    Left alone, blank-line paragraph splitting would turn the cue into a
    standalone chunk of pure cue text.  Gluing it to the following line
    keeps the cue attached to the sentence it directs.  Blank lines (real
    paragraph breaks) and untagged text are preserved unchanged, and a
    trailing cue-only line with nothing to attach to is dropped: a pause cue
    with no following sentence has no audible effect.
    """
    _require_supported_provider(provider)
    result: list[str] = []
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # A real paragraph break: keep it (chunking prefers paragraphs).
            result.append("")
            continue
        if not strip_performance_tags(stripped, provider):
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


def count_spoken_words(text: str, provider: str = "fish_s2_pro") -> int:
    """Count words that will actually be spoken (cues excluded)."""
    return len(strip_performance_tags(text, provider).split())


def count_tags(text: str, provider: str = "fish_s2_pro") -> int:
    """Count delivery cues in a piece of tagged text."""
    _require_supported_provider(provider)
    if provider == "higgs_tts_3":
        return len(HIGGS_TOKEN_RE.findall(text))
    return len(TAG_RE.findall(text))


def cue_ceiling(source: str, provider: str = "fish_s2_pro") -> int:
    """Length-scaled anti-over-tagging ceiling for one segment.

    A short line can carry only a few cues before they compete with
    themselves; longer narration earns more.  The floor of 3 keeps a natural
    delivery shift (a style cue plus a pause plus an emphasis) from being
    rejected on a short but cue-worthy line.  Higgs tokens are individually
    stronger (each one re-voices a stretch of audio), so its ceiling grows
    more slowly than Fish's.
    """
    words = count_spoken_words(source, provider)
    if provider == "higgs_tts_3":
        return max(3, words // 20)
    return max(3, words // 15)


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


def _higgs_inserted_tokens(
    source: str, tagged: str,
) -> tuple[list[tuple[str, str, str]], Counter[str]]:
    """Tokens inserted beyond control tokens already present in the source.

    Returns ``(inserted, source_counter)`` where ``inserted`` holds
    ``(literal, category, value)`` triples for every new occurrence and
    ``source_counter`` counts token literals still attributed to the source.
    """
    source_counter = Counter(
        match.group(0) for match in HIGGS_TOKEN_RE.finditer(source)
    )
    inserted: list[tuple[str, str, str]] = []
    for match in HIGGS_TOKEN_RE.finditer(tagged):
        literal = match.group(0)
        if source_counter[literal] > 0:
            source_counter[literal] -= 1
        else:
            inserted.append((literal, match.group(1), match.group(2)))
    return inserted, source_counter


def count_inserted_tags(
    source: str, tagged: str, provider: str = "fish_s2_pro",
) -> int:
    """Count cues added beyond markup already present in the clean source."""
    _require_supported_provider(provider)
    if provider == "higgs_tts_3":
        return len(_higgs_inserted_tokens(source, tagged)[0])
    return len(_source_aware_tagged_view(source, tagged)[2])


def _spoken_tokens(text: str) -> list[str]:
    return [token.lower() for token in _WORD_TOKEN_RE.findall(text)]


def _sequences_align(
    source: list[str], tagged: list[str], permitted: set[int],
) -> bool:
    """True when ``tagged`` equals ``source`` plus permitted extra words.

    ``permitted`` holds positions in ``tagged`` for words that are legal
    additions (Higgs onomatopoeia right after an sfx token).
    """
    i = j = 0
    while i < len(source) and j < len(tagged):
        if source[i] == tagged[j]:
            i += 1
            j += 1
        elif j in permitted and j + 1 < len(tagged) and source[i] == tagged[j + 1]:
            j += 1
        else:
            return False
    while j < len(tagged) and j in permitted:
        j += 1
    return i == len(source) and j == len(tagged)


def _validate_tagged_fish(source: str, tagged: str) -> list[str]:
    """Fish S2 Pro validation (see :func:`validate_tagged` for the rules)."""
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
    ceiling = cue_ceiling(source, "fish_s2_pro")
    if len(cues) > ceiling:
        errors.append(
            f"{len(cues)} cues exceed the anti-over-tagging ceiling of "
            f"{ceiling} for this text"
        )

    return errors


def _higgs_delivery_group(category: str, value: str) -> str | None:
    """The singleton delivery group a token belongs to, or ``None``.

    Delivery tokens (emotion, style, speed, pitch, expressive) re-voice the
    whole turn, so Higgs accepts at most one of each group per segment.
    Positional tokens (pauses, sfx) have no such limit.
    """
    if category == "emotion":
        return "emotion"
    if category == "style":
        return "style"
    if value in HIGGS_PROSODY_SPEED:
        return "speed"
    if value in HIGGS_PROSODY_PITCH:
        return "pitch"
    if value in HIGGS_PROSODY_DELIVERY:
        return "expressive"
    return None


def _validate_tagged_higgs(source: str, tagged: str) -> list[str]:
    errors: list[str] = []
    source = source.strip()
    tagged = tagged.strip()

    if not tagged:
        return ["tagged text is empty"]

    # --- Token structure: pieces alternate spoken text and well-formed
    # ``<|...|>`` tokens; tokens already in the clean source are authoring
    # markup, not insertions.
    pieces: list[dict[str, Any]] = []
    cursor = 0
    for match in HIGGS_TOKEN_RE.finditer(tagged):
        if match.start() > cursor:
            pieces.append({"kind": "text", "text": tagged[cursor:match.start()]})
        pieces.append({
            "kind": "token",
            "literal": match.group(0),
            "category": match.group(1),
            "value": match.group(2),
            "source": False,
        })
        cursor = match.end()
    if cursor < len(tagged):
        pieces.append({"kind": "text", "text": tagged[cursor:]})
    source_counter = Counter(
        match.group(0) for match in HIGGS_TOKEN_RE.finditer(source)
    )
    for piece in pieces:
        if piece["kind"] == "token" and source_counter[piece["literal"]] > 0:
            source_counter[piece["literal"]] -= 1
            piece["source"] = True

    # A '<|' that is not a well-formed token is a broken control token.
    well_formed = sum(1 for piece in pieces if piece["kind"] == "token")
    if tagged.count("<|") != well_formed or tagged.count("|>") != well_formed:
        errors.append("unbalanced control tokens: '<|' and '|>' must form complete tokens")

    # --- Vocabulary: every token, source or inserted, must be official.
    for piece in pieces:
        if piece["kind"] != "token":
            continue
        if piece["literal"] not in HIGGS_CONTROL_TOKENS:
            errors.append(
                f"unknown Higgs control token {piece['literal']} is not in "
                "the official vocabulary"
            )

    # --- Placement: delivery tokens set how the whole segment is delivered,
    # so they must lead the segment, before any spoken text.
    seen_word = False
    for piece in pieces:
        if piece["kind"] == "text":
            if _spoken_tokens(piece["text"]):
                seen_word = True
        elif not piece["source"] and _higgs_delivery_group(
            piece["category"], piece["value"]
        ) and seen_word:
            errors.append(
                f"delivery token {piece['literal']} must come before any "
                "spoken text"
            )

    # --- Spoken words: tokens are interpreted by the engine, never spoken.
    # The only legal word additions are onomatopoeia immediately after an
    # inserted sfx token (cookbook rule 2); they are permitted extras in the
    # alignment below.
    source_words = _spoken_tokens(strip_performance_tags(source, "higgs_tts_3"))
    tagged_words: list[str] = []
    gap_starts: dict[int, int] = {}
    permitted: set[int] = set()
    for index, piece in enumerate(pieces):
        if piece["kind"] != "text":
            continue
        gap_starts[index] = len(tagged_words)
        tagged_words.extend(_spoken_tokens(piece["text"]))
    for index, piece in enumerate(pieces):
        if (
            piece["kind"] != "token" or piece["source"]
            or piece["category"] != "sfx"
        ):
            continue
        onomatopoeia = HIGGS_SFX_ONOMATOPOEIA.get(piece["value"])
        if not onomatopoeia:
            continue  # unknown sfx value already reported above
        if (
            index + 1 >= len(pieces)
            or pieces[index + 1]["kind"] != "text"
        ):
            errors.append(
                f"{piece['literal']} must be immediately followed by its "
                f"onomatopoeia ({' / '.join(sorted(onomatopoeia))})"
            )
            continue
        gap_words = _spoken_tokens(pieces[index + 1]["text"])
        if not gap_words or gap_words[0] not in onomatopoeia:
            errors.append(
                f"{piece['literal']} must be immediately followed by its "
                f"onomatopoeia ({' / '.join(sorted(onomatopoeia))})"
            )
            continue
        if gap_starts[index + 1] < len(tagged_words):
            permitted.add(gap_starts[index + 1])

    if not _sequences_align(source_words, tagged_words, permitted):
        if len(tagged_words) != len(source_words):
            errors.append(
                "spoken words were added or deleted "
                f"({len(tagged_words)} vs {len(source_words)} in the source)"
            )
        else:
            errors.append("spoken words were changed or reordered")

    # --- Delivery multiplicity: at most one token per delivery group.
    groups: Counter[str] = Counter()
    for piece in pieces:
        if piece["kind"] != "token" or piece["source"]:
            continue
        group = _higgs_delivery_group(piece["category"], piece["value"])
        if group is not None:
            groups[group] += 1
    for group in sorted(groups):
        if groups[group] > 1:
            errors.append(
                f"only one {group} token per segment is supported "
                f"({groups[group]} inserted)"
            )

    # --- Length guard: growth may come from the inserted tokens themselves,
    # plus a small allowance for sfx onomatopoeia and punctuation drift.
    inserted = [piece for piece in pieces if piece["kind"] == "token" and not piece["source"]]
    sfx_count = sum(1 for piece in inserted if piece["category"] == "sfx")
    max_length = (
        len(source)
        + sum(len(piece["literal"]) for piece in inserted)
        + 16 * sfx_count
        + 32
    )
    if len(tagged) > max_length:
        errors.append(
            f"tagged text is {len(tagged)} characters but the source is "
            f"{len(source)} with {len(inserted)} token(s); the tokens add "
            "too much text"
        )

    # --- Capitalization newly introduced into the words that will be spoken.
    spoken_text = re.sub(
        r"\s+", " ",
        " ".join(piece["text"] for piece in pieces if piece["kind"] == "text"),
    ).strip()
    for run in _CAPS_RUN_RE.findall(spoken_text):
        if run not in source:
            errors.append(f"new ALL-CAPS run {run!r} is not in the source text")

    # --- Anti-over-tagging: a length-scaled ceiling on inserted tokens.
    ceiling = cue_ceiling(source, "higgs_tts_3")
    if len(inserted) > ceiling:
        errors.append(
            f"{len(inserted)} tokens exceed the anti-over-tagging ceiling of "
            f"{ceiling} for this text"
        )

    return errors


def validate_tagged(
    source: str, tagged: str, provider: str = "fish_s2_pro",
) -> list[str]:
    """Reject, don't trust: verify a tagged text against its clean source.

    Returns a list of human-readable problems (empty when the tagged text is
    safe to send to the model).  The word-sequence check is the safety core:
    it stops the LLM from rewriting the script while adding cues.  For
    ``higgs_tts_3`` the same core applies on top of the cookbook rules:
    official vocabulary only, delivery tokens before any spoken text, at most
    one token per delivery group, and every sfx token immediately followed by
    its onomatopoeia (the one legal word addition).
    """
    _require_supported_provider(provider)
    if provider == "higgs_tts_3":
        return _validate_tagged_higgs(source, tagged)
    return _validate_tagged_fish(source, tagged)


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
    """Portable, human-readable delivery-tag artifact for one narration run.

    The artifact is provider-scoped: a script generated for one provider is
    recorded with that provider and is only ever applied to runs of that same
    provider, so one model's cues can never leak into another's.  A project has
    one shared ``narration/performance-tags.json`` artifact; generating for a
    different provider replaces it.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    provider: PerformanceProvider = "fish_s2_pro"
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
            count_inserted_tags(
                segment.source, segment.tagged, self.provider,
            )
            for segment in self.segments
        )

    @property
    def source_text(self) -> str:
        return "\n\n".join(segment.source for segment in self.segments)
