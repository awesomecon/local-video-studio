"""Editorial Mode caption beats: phrase grouping, emphasis, placement, sizing.

This module is renderer-agnostic and deterministic: it turns word timings into
caption beats for the documentary caption styles, selects which phrases earn
the paper-block highlight, and chooses a safe position for each beat given the
Editorial template layout. The deterministic renderer only decides how the
approved beats look.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from backend.editorial.models import (
    EditorialCaptionCue,
    EditorialCaptionStyle,
    EditorialComposition,
    EditorialElementType,
    EditorialTemplate,
)

from .alignment import CaptionWord

# Grouping limits -----------------------------------------------------------
MAX_PHRASE_LINE_CHARS = 34
MAX_PHRASE_WORDS = 5
SOFT_PHRASE_WORDS = 7
PAUSE_BREAK_SECONDS = 0.55
MIN_DWELL_SECONDS = 0.35
MAX_TAIL_SECONDS = 1.2
LINE_CHARS = 30
MAX_LINE_WORDS = 8

_SENTENCE_END = re.compile(r"[.!?][\"')\]]?$")
_NUMERAL = re.compile(r"\d")

# Design-space typography ----------------------------------------------------
# Design space is 1080x1920 (portrait) / 1920x1080 (landscape); the renderer
# scales the stage to the actual render dimensions, so these design-space
# sizes scale responsively with the project resolution.
_FONT_SIZES: dict[str, dict[EditorialCaptionStyle, tuple[int, int]]] = {
    "portrait": {
        EditorialCaptionStyle.EDITORIAL_PHRASE: (48, 58),
        EditorialCaptionStyle.QUIET_DOCUMENTARY: (44, 44),
        EditorialCaptionStyle.ONE_LINE: (46, 46),
        EditorialCaptionStyle.ONE_WORD: (44, 44),
        EditorialCaptionStyle.STANDARD: (100, 100),
    },
    "landscape": {
        EditorialCaptionStyle.EDITORIAL_PHRASE: (40, 46),
        EditorialCaptionStyle.QUIET_DOCUMENTARY: (36, 36),
        EditorialCaptionStyle.ONE_LINE: (38, 38),
        EditorialCaptionStyle.ONE_WORD: (36, 36),
        EditorialCaptionStyle.STANDARD: (56, 56),
    },
}

# Renderer-owned layout regions in design space (x, y, w, h), mirroring the
# composition CSS in backend/editorial/renderer.py. ``draft-label`` is
# renderer-authored and always present. Thin rules and fullscreen moment
# overlays are intentionally not listed: moments hide captions instead.
CAPTION_REGIONS: dict[
    EditorialTemplate, dict[str, dict[str, tuple[int, int, int, int]]]
] = {
    EditorialTemplate.ARCHIVE_CANVAS: {
        "portrait": {
            "year": (72, 105, 560, 250),
            "archive-photo": (76, 420, 610, 610),
            "paper": (436, 630, 590, 760),
            "ruler-grid": (82, 1554, 926, 196),
            "draft-label": (80, 1856, 320, 30),
        },
        "landscape": {
            "year": (70, 62, 380, 180),
            "archive-photo": (82, 270, 650, 610),
            "paper": (1058, 170, 780, 720),
            "ruler-grid": (780, 876, 1058, 130),
            "draft-label": (82, 1046, 320, 28),
        },
    },
    EditorialTemplate.DOCUMENT_REVEAL: {
        "portrait": {
            "title": (72, 96, 936, 208),
            "document": (64, 352, 952, 872),
            "annotation": (72, 1308, 500, 120),
            "context-image": (628, 1288, 380, 520),
            "draft-label": (80, 1856, 320, 30),
        },
        "landscape": {
            "title": (80, 52, 1760, 100),
            "document": (90, 180, 1070, 790),
            "annotation": (1230, 340, 570, 110),
            "context-image": (1310, 540, 520, 420),
            "draft-label": (82, 1046, 320, 28),
        },
    },
    EditorialTemplate.COMPARISON_CANVAS: {
        "portrait": {
            "headline": (72, 110, 936, 112),
            "left-image": (72, 420, 440, 560),
            "right-image": (568, 420, 440, 560),
            "left-label": (72, 1032, 440, 96),
            "right-label": (568, 1032, 440, 96),
            "draft-label": (80, 1856, 320, 30),
        },
        "landscape": {
            "headline": (80, 52, 1760, 90),
            "left-image": (80, 210, 760, 650),
            "right-image": (1080, 210, 760, 650),
            "left-label": (80, 890, 760, 60),
            "right-label": (1080, 890, 760, 60),
            "draft-label": (82, 1046, 320, 28),
        },
    },
    EditorialTemplate.ILLUSTRATION_CANVAS: {
        "portrait": {
            "illustration": (72, 300, 936, 980),
            "headline": (72, 1396, 936, 100),
            "supporting-text": (72, 1548, 936, 100),
            "draft-label": (80, 1856, 320, 30),
        },
        "landscape": {
            "illustration": (70, 120, 1120, 850),
            "headline": (1270, 380, 580, 90),
            "supporting-text": (1270, 570, 580, 100),
            "draft-label": (82, 1046, 320, 28),
        },
    },
    EditorialTemplate.BIG_TEXT_REVEAL: {
        "portrait": {
            "kicker": (72, 604, 936, 50),
            # BigText headlines may wrap to several lines. Reserve the full
            # hero-title band so captions move above it instead of crossing
            # through long deterministic typography.
            "headline": (40, 712, 1000, 1050),
            "cta": (72, 1330, 936, 120),
            "draft-label": (80, 1856, 320, 30),
        },
        "landscape": {
            "kicker": (70, 330, 1780, 46),
            "headline": (70, 430, 1780, 500),
            "cta": (80, 930, 1760, 90),
            "draft-label": (82, 1046, 320, 28),
        },
    },
}

# Fullscreen "moment" elements that own the screen and suppress captions.
# A reveal must overlap a caption by at least this much to take the screen:
# a caption that merely lingers into the first moment of a reveal stays up.
REVEAL_OVERLAP_MIN_SECONDS = 0.25
# Roles whose on-screen text dominates the frame. Fullscreen moments (the ELON
# reveal, big-text headlines such as COINCIDENCE? or a closing CTA) hide every
# overlapping caption; the other dominant text roles hide captions that merely
# duplicate them (e.g. a "1949" cue under a giant 1949).
_REVEAL_ROLES: dict[EditorialTemplate, tuple[str, ...]] = {
    EditorialTemplate.ARCHIVE_CANVAS: ("reveal", "year"),
    EditorialTemplate.DOCUMENT_REVEAL: ("title",),
    EditorialTemplate.COMPARISON_CANVAS: ("headline",),
    EditorialTemplate.ILLUSTRATION_CANVAS: ("headline",),
    EditorialTemplate.BIG_TEXT_REVEAL: ("headline", "kicker"),
}
_FULL_SCREEN_REVEALS: dict[EditorialTemplate, frozenset[str]] = {
    EditorialTemplate.ARCHIVE_CANVAS: frozenset({"reveal"}),
    EditorialTemplate.BIG_TEXT_REVEAL: frozenset({"headline"}),
}


@dataclass(frozen=True)
class _Unit:
    """A spoken phrase: word index span plus the words themselves."""

    start_index: int
    end_index: int
    words: tuple[CaptionWord, ...]

    @property
    def text(self) -> str:
        return _words_text(self.words)


def _token(text: str) -> str:
    token = "".join(char for char in text.casefold() if char.isalnum())
    # Whisper commonly normalizes spoken number words to digits. Treat the
    # two forms as the same token so planner-authored emphasis remains stable.
    return {"10": "ten"}.get(token, token)


def _words_text(words: Sequence[CaptionWord]) -> str:
    text = " ".join(word.text.strip() for word in words)
    # Whisper can split a compound into ``multi`` and ``-planetary``. Preserve
    # its timestamps while presenting the authored compound naturally.
    return re.sub(r"\s+(-[\w])", r"\1", text)


def _validate_words(words: Iterable[CaptionWord]) -> list[CaptionWord]:
    ordered = list(words)
    if any(
        current.start_seconds < previous.start_seconds
        for previous, current in zip(ordered, ordered[1:])
    ):
        raise ValueError("caption words must be ordered by start time")
    coalesced: list[CaptionWord] = []
    for word in ordered:
        text = word.text.strip()
        if (
            text.startswith("-")
            and coalesced
            and word.start_seconds - coalesced[-1].end_seconds <= 0.08
        ):
            previous = coalesced[-1]
            coalesced[-1] = CaptionWord(
                previous.start_seconds,
                word.end_seconds,
                f"{previous.text.strip()}{text}",
            )
        else:
            coalesced.append(word)
    return coalesced


def find_emphasis_spans(
    words: Sequence[CaptionWord], emphasis_texts: Sequence[str],
) -> list[tuple[int, int]]:
    """Locate planner-emphasized phrases inside the word stream.

    Matching is punctuation/case-insensitive and quotes the narration
    verbatim, so a planner phrase that was not spoken is simply ignored.
    """
    tokens = [_token(word.text) for word in words]
    spans: list[tuple[int, int]] = []
    used = [False] * len(tokens)
    for phrase in sorted(emphasis_texts, key=len, reverse=True):
        needed = [_token(chunk) for chunk in phrase.split()]
        needed = [chunk for chunk in needed if chunk]
        if not needed:
            continue
        for start in range(len(tokens) - len(needed) + 1):
            if any(used[start:start + len(needed)]):
                continue
            if tokens[start:start + len(needed)] == needed:
                spans.append((start, start + len(needed)))
                for index in range(start, start + len(needed)):
                    used[index] = True
                break
    return sorted(spans)


def _phrase_units(
    words: list[CaptionWord], emphasis_spans: Sequence[tuple[int, int]],
) -> list[_Unit]:
    """Group words into 2-5 word spoken phrases (up to 7 on one line)."""
    emph_starts = {start for start, _end in emphasis_spans}
    emph_ends = {end for _start, end in emphasis_spans}
    units: list[_Unit] = []
    current: list[CaptionWord] = []
    start_index = 0

    def flush() -> None:
        nonlocal current, start_index
        if current:
            units.append(_Unit(start_index, start_index + len(current) - 1, tuple(current)))
            current = []

    for index, word in enumerate(words):
        if not current:
            start_index = index
            current.append(word)
            continue
        joined_chars = len(_words_text([*current, word]))
        pause = word.start_seconds - current[-1].end_seconds
        previous_text = current[-1].text.strip()
        sentence_ended = bool(_SENTENCE_END.search(previous_text))
        comma_ended = previous_text.endswith(",")
        next_starts_emphasis = index in emph_starts
        previous_ends_emphasis = index in emph_ends
        break_unit = (
            len(current) + 1 > SOFT_PHRASE_WORDS
            or (len(current) + 1 > MAX_PHRASE_WORDS and joined_chars > MAX_PHRASE_LINE_CHARS)
            or sentence_ended
            or next_starts_emphasis
            or previous_ends_emphasis
            or pause >= PAUSE_BREAK_SECONDS
            or (comma_ended and len(current) >= 2)
            or (len(current) == MAX_PHRASE_WORDS and joined_chars > MAX_PHRASE_LINE_CHARS)
        )
        if break_unit:
            flush()
            start_index = index
        current.append(word)
    flush()
    return units


def _line_units(words: list[CaptionWord]) -> list[_Unit]:
    """Group words into single-line beats for the one-line style."""
    units: list[_Unit] = []
    current: list[CaptionWord] = []
    start_index = 0

    def flush() -> None:
        nonlocal current, start_index
        if current:
            units.append(_Unit(start_index, start_index + len(current) - 1, tuple(current)))
            current = []

    for index, word in enumerate(words):
        if not current:
            start_index = index
            current.append(word)
            continue
        joined_chars = len(_words_text([*current, word]))
        previous_text = current[-1].text.strip()
        pause = word.start_seconds - current[-1].end_seconds
        break_unit = (
            bool(_SENTENCE_END.search(previous_text))
            or pause >= PAUSE_BREAK_SECONDS
            or joined_chars > LINE_CHARS
            or len(current) + 1 > MAX_LINE_WORDS
        )
        if break_unit:
            flush()
            start_index = index
        current.append(word)
    flush()
    return units


def _word_units(words: list[CaptionWord]) -> list[_Unit]:
    return [_Unit(index, index, (word,)) for index, word in enumerate(words)]


def _unit_flags_from_spans(units: list[_Unit], spans: Sequence[tuple[int, int]]) -> list[bool]:
    """Flag units that are exactly an emphasized span; never highlight back-to-back beats."""
    flags: list[bool] = [False] * len(units)
    for index, unit in enumerate(units):
        covered = any(
            start <= unit.start_index and unit.end_index < end
            for start, end in spans
        )
        if covered and (index == 0 or not flags[index - 1]):
            flags[index] = True
    return flags


def _fallback_emphasis_flags(units: list[_Unit]) -> list[bool]:
    """Deterministic emphasis for plans without planner metadata.

    Numeral phrases lead; all-caps phrases fill the remaining slots. The
    no-back-to-back rule keeps highlighting intentional (about one beat
    every two to three).
    """
    flags = [False] * len(units)
    numeral_indexes = {
        index for index, unit in enumerate(units)
        if any(_NUMERAL.search(word.text) for word in unit.words)
    }
    for index in sorted(numeral_indexes):
        if not flags[index] and (index == 0 or not flags[index - 1]):
            flags[index] = True
    for index, unit in enumerate(units):
        if index in numeral_indexes or flags[index] or (index and flags[index - 1]):
            continue
        if unit.words and all(
            word.text.strip().isupper() and any(c.isalpha() for c in word.text)
            for word in unit.words
        ):
            flags[index] = True
    return flags


def _cues_from_units(
    units: list[_Unit],
    style: EditorialCaptionStyle,
    flags: list[bool] | None = None,
) -> list[EditorialCaptionCue]:
    if flags is None:
        flags = [False] * len(units)
    cues: list[EditorialCaptionCue] = []
    for index, unit in enumerate(units):
        start = unit.words[0].start_seconds
        end = unit.words[-1].end_seconds
        next_start = (
            units[index + 1].words[0].start_seconds if index + 1 < len(units) else None
        )
        if next_start is not None and end < next_start:
            # A tail may fill silence, but must never overlap the next beat.
            # Overlap makes the renderer replace the earlier node before its
            # fade-out completes and produces visible snapping.
            end = min(next_start, end + MAX_TAIL_SECONDS)
        elif next_start is None:
            end = min(end + MIN_DWELL_SECONDS, start + 2.4)
        text = unit.text
        cues.append(EditorialCaptionCue(
            start=start,
            end=end,
            text=text,
            style=style,
            highlight=flags[index],
        ))
    return cues


def build_editorial_caption_cues(
    words: Iterable[CaptionWord],
    style: EditorialCaptionStyle,
    *,
    emphasis_texts: Sequence[str] = (),
) -> list[EditorialCaptionCue]:
    """Build deterministic caption beats for one Editorial caption style.

    ``STANDARD`` returns no beats here: that style keeps the pre-existing
    large centered subtitles on the shared ASS path.
    """
    if style is EditorialCaptionStyle.STANDARD:
        return []
    ordered = _validate_words(words)
    if not ordered:
        return []
    emphasis_texts = list(dict.fromkeys(emphasis_texts))
    spans = find_emphasis_spans(ordered, emphasis_texts) if emphasis_texts else []
    if style in (
        EditorialCaptionStyle.EDITORIAL_PHRASE,
        EditorialCaptionStyle.QUIET_DOCUMENTARY,
    ):
        units = _phrase_units(ordered, spans)
        if style is EditorialCaptionStyle.EDITORIAL_PHRASE:
            flags = (
                _unit_flags_from_spans(units, spans)
                if spans else _fallback_emphasis_flags(units)
            )
        else:
            flags = [False] * len(units)
        return _cues_from_units(units, style, flags)
    if style is EditorialCaptionStyle.ONE_LINE:
        return _cues_from_units(_line_units(ordered), style)
    if style is EditorialCaptionStyle.ONE_WORD:
        return _cues_from_units(_word_units(ordered), style)
    raise ValueError(f"unsupported Editorial caption style: {style!r}")


def caption_font_sizes(
    style: EditorialCaptionStyle, orientation: str,
) -> tuple[int, int]:
    """(normal, highlighted) design-space font sizes for a caption style."""
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("orientation must be 'portrait' or 'landscape'")
    return _FONT_SIZES[orientation][style]


def synthesize_word_timings(
    spans: Sequence[tuple[str, float, float]],
) -> list[CaptionWord]:
    """Evenly spaced word timings for narration without an alignment pass.

    ``spans`` are (text, start_seconds, duration_seconds) tuples in narration
    order; the result stays deterministic so preview and export agree.
    """
    words: list[CaptionWord] = []
    for text, start, duration in spans:
        tokens = text.split()
        if not tokens or duration <= 0 or start < 0:
            continue
        step = duration / len(tokens)
        for index, token in enumerate(tokens):
            word_start = start + index * step
            word_end = start + min((index + 1) * step, duration)
            if word_end <= word_start:
                word_end = word_start + max(step, 0.05) * 0.5
            words.append(CaptionWord(word_start, word_end, token))
    return words


def composition_present_roles(composition: EditorialComposition) -> set[str]:
    """Layout roles whose visual content occupies the frame for caption placement."""
    roles: set[str] = set()
    for element in composition.elements:
        if not element.role:
            continue
        if element.type is EditorialElementType.TEXT:
            if element.text.strip():
                roles.add(element.role)
        elif element.type in {
            EditorialElementType.IMAGE,
            EditorialElementType.DOCUMENT,
            EditorialElementType.RULER_NODES,
        }:
            roles.add(element.role)
    roles.add("draft-label")
    return roles


def estimate_caption_box(
    text: str,
    style: EditorialCaptionStyle,
    orientation: str,
    *,
    highlight: bool = False,
) -> tuple[int, int]:
    """Conservative (width, height) design-space estimate for a caption beat."""
    base, highlighted_font = caption_font_sizes(style, orientation)
    font = highlighted_font if highlight else base
    width_chars = min(
        MAX_PHRASE_LINE_CHARS if style is not EditorialCaptionStyle.ONE_LINE
        else LINE_CHARS,
        max((len(line) for line in _wrap_text(text, 64)), default=0),
    )
    lines = len(_wrap_text(text, 64))
    width = int(width_chars * font * 0.52)
    if style is EditorialCaptionStyle.EDITORIAL_PHRASE and not highlight:
        width += 19  # thin rust rule + padding
    if highlight:
        width += 28  # paper-block horizontal padding
        lines = min(lines, 1)
    height = int(lines * font * 1.3) + (12 if highlight else 0)
    return width, height


def _wrap_text(text: str, limit: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def choose_caption_position(
    composition: EditorialComposition,
    orientation: str,
    design_width: int,
    design_height: int,
    box_width: int,
    box_height: int,
    present_roles: set[str] | None = None,
) -> tuple[str, int, int]:
    """Pick the first safe caption anchor that avoids composition content.

    Vertical projects avoid the lower-right (Shorts UI) by only offering
    lower-left, lower-center, mid-left, and upper-left anchors. Falls back to
    lower-left when every candidate collides; the fallback band still sits
    clear of the bottom edge.
    """
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("orientation must be 'portrait' or 'landscape'")
    roles = present_roles if present_roles is not None else composition_present_roles(composition)
    regions = [
        box
        for role, box in CAPTION_REGIONS[composition.template][orientation].items()
        if role in roles
    ]
    margin = 12
    if orientation == "portrait":
        candidates = [
            ("lower-left", 72, design_height - 400 - box_height),
            ("lower-center", (design_width - box_width) // 2, design_height - 400 - box_height),
            ("mid-left", 72, design_height - 700 - box_height),
            ("upper-left", 72, 140),
        ]
    else:
        candidates = [
            ("lower-left", 70, design_height - 300 - box_height),
            ("lower-center", (design_width - box_width) // 2, design_height - 300 - box_height),
            ("mid-left", 70, design_height - 480 - box_height),
            ("upper-left", 70, 100),
        ]
    for name, x, y in candidates:
        if x < 40 or y < 40 or x + box_width > design_width - 40 or y + box_height > design_height - 40:
            continue
        if not any(
            x - margin < region_x + region_w and region_x < x + box_width + margin
            and y - margin < region_y + region_h and region_y < y + box_height + margin
            for region_x, region_y, region_w, region_h in regions
        ):
            return name, x, y
    fallback = candidates[0]
    return fallback[0], fallback[1], fallback[2]


def place_caption_cues(
    cues: Sequence[EditorialCaptionCue],
    composition: EditorialComposition,
    design_width: int,
    design_height: int,
) -> list[dict]:
    """Attach a safe design-space position to each beat for one composition."""
    landscape = design_width >= design_height
    orientation = "landscape" if landscape else "portrait"
    payload: list[dict] = []
    for cue in cues:
        box_width, box_height = estimate_caption_box(
            cue.text, cue.style, orientation, highlight=cue.highlight,
        )
        anchor, x, y = choose_caption_position(
            composition, orientation, design_width, design_height,
            box_width, box_height,
        )
        payload.append({
            "start": round(cue.start, 6),
            "end": round(cue.end, 6),
            "text": cue.text,
            "style": cue.style.value,
            "highlight": cue.highlight,
            "anchor": anchor,
            "x": x,
            "y": y,
        })
    return payload


@dataclass(frozen=True)
class _RevealWindow:
    start: float
    end: float
    fullscreen: bool
    tokens: frozenset[str]
    numeral: bool = False


def _reveal_windows(compositions: Sequence[EditorialComposition]) -> list[_RevealWindow]:
    windows: list[_RevealWindow] = []
    for composition in compositions:
        roles = _REVEAL_ROLES.get(composition.template, ())
        for role in roles:
            element = next(
                (item for item in composition.elements if item.role == role), None,
            )
            if element is None:
                continue
            targets = {element.id, role}
            events = [
                event for event in composition.events if event.target in targets
            ]
            if not events:
                continue
            from_time = max(0.0, min(event.time for event in events) - 0.15)
            to_time = composition.duration
            if (
                composition.template is EditorialTemplate.BIG_TEXT_REVEAL
                and element.role == "headline"
            ):
                blackout = next(
                    (item for item in composition.elements if item.role == "blackout"),
                    None,
                )
                if blackout is not None:
                    blackout_events = [
                        event for event in composition.events
                        if event.target in {blackout.id, "blackout"}
                    ]
                    if blackout_events:
                        complete = max(
                            event.time + event.duration for event in blackout_events
                        )
                        to_time = min(to_time, complete)
            windows.append(_RevealWindow(
                start=composition.start + from_time,
                end=min(composition.start + to_time, composition.start + composition.duration),
                fullscreen=role in _FULL_SCREEN_REVEALS.get(composition.template, frozenset()),
                tokens=frozenset(_token(chunk) for chunk in element.text.split() if _token(chunk)),
                numeral=(
                    composition.template is EditorialTemplate.ARCHIVE_CANVAS
                    and role == "year"
                    and element.text.strip().isdigit()
                    and len(element.text.strip()) == 4
                ),
            ))
    return windows


def _clip_cue_around_fullscreen_reveal(
    cue: EditorialCaptionCue,
    window: _RevealWindow,
) -> EditorialCaptionCue | None:
    """Keep the useful side of a cue that only grazes a fullscreen reveal."""
    overlap = min(cue.end, window.end) - max(cue.start, window.start)
    if overlap < REVEAL_OVERLAP_MIN_SECONDS:
        return cue
    before = max(0.0, window.start - cue.start)
    after = max(0.0, cue.end - window.end)
    if before < MIN_DWELL_SECONDS and after < MIN_DWELL_SECONDS:
        return None
    if before >= after:
        return cue.model_copy(update={"end": window.start})
    return cue.model_copy(update={"start": window.end})


def suppress_cues_for_reveals(
    cues: Sequence[EditorialCaptionCue],
    compositions: Sequence[EditorialComposition],
) -> list[EditorialCaptionCue]:
    """Drop captions that on-screen editorial text must own.

    Fullscreen moments (the ELON reveal, big-text headlines) hide any
    overlapping caption so the moment gets the whole screen. Dominant text
    that shares the frame (giant year numerals, card titles, headlines) hides
    captions that duplicate its words, so the on-screen text carries the
    information instead of a second, smaller caption.
    """
    windows = _reveal_windows(compositions)
    if not windows:
        return list(cues)
    surviving: list[EditorialCaptionCue] = []
    for cue in cues:
        candidate: EditorialCaptionCue | None = cue
        hidden = False
        for window in windows:
            if candidate is None:
                break
            overlap = min(candidate.end, window.end) - max(candidate.start, window.start)
            if overlap < REVEAL_OVERLAP_MIN_SECONDS:
                continue
            if window.fullscreen:
                candidate = _clip_cue_around_fullscreen_reveal(candidate, window)
                continue
            cue_tokens = frozenset(
                _token(chunk) for chunk in candidate.text.split() if _token(chunk)
            )
            if not window.tokens or not cue_tokens:
                continue
            if window.numeral and cue_tokens & window.tokens:
                # A giant year numeral owns the year fact: any beat that still
                # says the year out loud restates what dominates the frame.
                hidden = True
                break
            common = len(cue_tokens & window.tokens)
            if common / len(cue_tokens) >= 0.6:
                hidden = True
                break
        if not hidden and candidate is not None:
            surviving.append(candidate)
    return surviving


def slice_caption_cues(
    cues: Sequence[EditorialCaptionCue],
    window_start: float,
    window_end: float,
) -> list[EditorialCaptionCue]:
    """Re-time cues for one composition window (used for per-clip renders)."""
    if window_end <= window_start:
        raise ValueError("caption window must be positive")
    sliced: list[EditorialCaptionCue] = []
    for cue in cues:
        start = max(cue.start, window_start)
        end = min(cue.end, window_end)
        if end - start < 0.05:
            continue
        sliced.append(EditorialCaptionCue(
            start=start - window_start,
            end=end - window_start,
            text=cue.text,
            style=cue.style,
            highlight=cue.highlight,
        ))
    return sliced


__all__ = [
    "CAPTION_REGIONS",
    "build_editorial_caption_cues",
    "caption_font_sizes",
    "choose_caption_position",
    "composition_present_roles",
    "estimate_caption_box",
    "find_emphasis_spans",
    "place_caption_cues",
    "slice_caption_cues",
    "synthesize_word_timings",
    "suppress_cues_for_reveals",
]
