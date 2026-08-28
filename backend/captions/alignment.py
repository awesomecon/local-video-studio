"""Portable word timings and deterministic cue grouping.

This module intentionally has no speech-model dependency.  The optional model backend
serializes its result here, while tests and renderers can consume the portable JSON.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

from backend.timeline import SubtitleCue, SubtitleWord


@dataclass(frozen=True, slots=True)
class CaptionWord:
    """A word spoken between two audio-clock positions."""

    start_seconds: float
    end_seconds: float
    text: str

    def __post_init__(self) -> None:
        if self.start_seconds < 0 or self.end_seconds <= self.start_seconds:
            raise ValueError("caption word timestamps are invalid")
        if not self.text.strip():
            raise ValueError("caption words must not be blank")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    """Audio-derived timing data retained with each portable project."""

    words: tuple[CaptionWord, ...]
    language: str | None
    language_probability: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "language": self.language,
            "language_probability": self.language_probability,
            "words": [word.to_dict() for word in self.words],
        }


def restore_authored_punctuation(
    words: Iterable[CaptionWord], transcript: str,
) -> tuple[CaptionWord, ...]:
    """Apply the authored transcript text to audio-aligned word timestamps.

    Speech recognition supplies the clock positions, but it can omit commas,
    periods, quotation marks, and capitalization. The narration transcript is
    authoritative for those details because it is the text that produced the
    audio. When token counts differ, only confidently aligned words are
    replaced so a recognition mismatch cannot shift the rest of the captions.
    """

    aligned = tuple(words)
    authored = _authored_tokens(transcript)
    if not aligned or not authored:
        return aligned

    # Narration generated from the stored script normally has exactly one
    # timestamp per authored token. In that common case, using every authored
    # token also restores punctuation on a word Whisper happened to misspell.
    if len(aligned) == len(authored):
        return tuple(
            CaptionWord(word.start_seconds, word.end_seconds, text)
            for word, (text, _normalized) in zip(aligned, authored, strict=True)
        )

    restored = list(aligned)
    matcher = SequenceMatcher(
        a=[_normalize_token(word.text) for word in aligned],
        b=[normalized for _text, normalized in authored],
        autojunk=False,
    )
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            aligned_index = block.a + offset
            authored_text = authored[block.b + offset][0]
            word = aligned[aligned_index]
            restored[aligned_index] = CaptionWord(
                word.start_seconds, word.end_seconds, authored_text,
            )
    return tuple(restored)


def _normalize_token(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


def _authored_tokens(transcript: str) -> list[tuple[str, str]]:
    """Return spoken tokens while retaining adjacent standalone punctuation."""

    tokens: list[tuple[str, str]] = []
    pending_prefix = ""
    opening_marks = frozenset("([{\u2018\u201c")
    for chunk in transcript.split():
        normalized = _normalize_token(chunk)
        if normalized:
            text = f"{pending_prefix}{chunk}" if pending_prefix else chunk
            tokens.append((text, normalized))
            pending_prefix = ""
        elif not tokens or all(character in opening_marks for character in chunk):
            pending_prefix += f"{chunk} "
        else:
            text, previous_normalized = tokens[-1]
            tokens[-1] = (f"{text} {chunk}", previous_normalized)
    if pending_prefix and tokens:
        text, normalized = tokens[-1]
        tokens[-1] = (f"{text} {pending_prefix.rstrip()}", normalized)
    return tokens


def build_caption_cues(
    words: Iterable[CaptionWord],
    *,
    max_line_characters: int = 36,
    max_lines: int = 2,
    max_cue_seconds: float = 3.5,
    pause_break_seconds: float = 0.55,
    max_words: int = 10,
) -> list[SubtitleCue]:
    """Turn word timings into short captions without changing their clock positions.

    Words only join an existing cue when the resulting text still fits the reading
    constraints. Sentence boundaries and a word cap keep phrases scannable. This makes
    the output deterministic and keeps the last cue on the actual end of speech rather
    than a planned scene boundary.
    """
    if max_line_characters < 1 or max_lines < 1 or max_cue_seconds <= 0 or max_words < 1:
        raise ValueError("caption grouping limits must be positive")
    if pause_break_seconds < 0:
        raise ValueError("pause break must not be negative")

    ordered = list(words)
    if any(current.start_seconds < previous.start_seconds for previous, current in zip(ordered, ordered[1:])):
        raise ValueError("caption words must be ordered")

    cues: list[SubtitleCue] = []
    current: list[CaptionWord] = []

    def flush() -> None:
        if not current:
            return
        lines = _wrap_words(current, max_line_characters=max_line_characters, max_lines=max_lines)
        cues.append(SubtitleCue(
            current[0].start_seconds,
            current[-1].end_seconds,
            "\n".join(lines),
            words=tuple(
                SubtitleWord(word.start_seconds, word.end_seconds, word.text.strip())
                for word in current
            ),
        ))
        current.clear()

    for word in ordered:
        if not current:
            current.append(word)
            continue
        joined = [*current, word]
        previous = current[-1]
        pause = word.start_seconds - previous.end_seconds
        too_long = word.end_seconds - current[0].start_seconds > max_cue_seconds
        too_many_words = len(joined) > max_words
        sentence_ended = bool(re.search(r"[.!?][\"')\]]?$", previous.text.strip()))
        does_not_fit = _wrap_words(
            joined, max_line_characters=max_line_characters, max_lines=max_lines,
        ) is None
        if pause >= pause_break_seconds or too_long or too_many_words or sentence_ended or does_not_fit:
            flush()
        current.append(word)
    flush()
    return cues


def _wrap_words(
    words: list[CaptionWord], *, max_line_characters: int, max_lines: int,
) -> list[str] | None:
    """Wrap words into lines, or return None when they cannot share one cue.

    A single word longer than ``max_line_characters`` (a URL or an unbroken CJK
    run) is emitted as its own overflowed line rather than dropped or crashing;
    subtitle overflow is reported separately by QC.
    """

    lines: list[str] = []
    for word in words:
        text = word.text.strip()
        candidate = f"{lines[-1]} {text}" if lines else text
        if len(candidate) <= max_line_characters:
            if lines:
                lines[-1] = candidate
            else:
                lines.append(candidate)
            continue
        if len(lines) >= max_lines:
            return None
        lines.append(text)
    return lines
