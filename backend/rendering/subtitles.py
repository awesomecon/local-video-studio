"""SRT and ASS subtitle serialization from audio-derived timestamps."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from backend.timeline.models import SubtitleCue

_ACTIVE_WORD_TAG = r"{\c&H0000D7FF&\b1}"
_RESET_STYLE_TAG = r"{\r}"


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    whole_seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours:d}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _validate_cues(cues: Iterable[SubtitleCue]) -> list[SubtitleCue]:
    result = list(cues)
    for cue in result:
        if cue.start_seconds < 0 or cue.end_seconds <= cue.start_seconds:
            raise ValueError("Subtitle cue timestamps are invalid")
        if not cue.text.strip():
            raise ValueError("Subtitle cue text must not be blank")
    return result


def write_srt(cues: Iterable[SubtitleCue], destination: str | Path) -> Path:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for index, cue in enumerate(_validate_cues(cues), start=1):
        text = cue.text.replace("\r\n", "\n").replace("\r", "\n")
        blocks.append(
            f"{index}\n{_srt_timestamp(cue.start_seconds)} --> "
            f"{_srt_timestamp(cue.end_seconds)}\n{text}"
        )
    output.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return output


def _escape_ass(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\r", r"\N")
        .replace("\n", r"\N")
    )


def _highlighted_text(cue: SubtitleCue, active_index: int) -> str | None:
    """Return cue text with exactly one word highlighted, preserving line breaks."""
    cursor = 0
    spans: list[tuple[int, int]] = []
    for word in cue.words:
        token = word.text.strip()
        start = cue.text.find(token, cursor)
        if start < 0:
            return None
        end = start + len(token)
        spans.append((start, end))
        cursor = end
    start, end = spans[active_index]
    return (
        _escape_ass(cue.text[:start])
        + _ACTIVE_WORD_TAG
        + _escape_ass(cue.text[start:end])
        + _RESET_STYLE_TAG
        + _escape_ass(cue.text[end:])
    )


def _dialogue(start: float, end: float, text: str) -> str:
    return (
        f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},"
        f"Default,,0,0,0,,{text}"
    )


def _cue_events(cue: SubtitleCue) -> list[str]:
    """Split a cue into active-word and neutral-gap ASS events."""
    if not cue.words:
        return [_dialogue(cue.start_seconds, cue.end_seconds, _escape_ass(cue.text))]
    highlighted = [_highlighted_text(cue, index) for index in range(len(cue.words))]
    if any(text is None for text in highlighted):
        return [_dialogue(cue.start_seconds, cue.end_seconds, _escape_ass(cue.text))]

    events: list[str] = []
    cursor = cue.start_seconds
    neutral_text = _escape_ass(cue.text)
    for index, word in enumerate(cue.words):
        word_start = max(cursor, word.start_seconds)
        word_end = min(cue.end_seconds, word.end_seconds)
        if word_start > cursor:
            events.append(_dialogue(cursor, word_start, neutral_text))
        if word_end > word_start:
            events.append(_dialogue(word_start, word_end, highlighted[index] or neutral_text))
            cursor = word_end
    if cursor < cue.end_seconds:
        events.append(_dialogue(cursor, cue.end_seconds, neutral_text))
    return events


def write_ass(
    cues: Iterable[SubtitleCue],
    destination: str | Path,
    *,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    # Scale typography with the target frame.  The old fixed 52 px style was
    # enormous in previews and undersized at higher resolutions.
    font_size = max(18, round(height * 0.052))
    outline = max(2, round(font_size * 0.075))
    shadow = max(1, round(font_size * 0.035))
    horizontal_margin = max(24, round(width * 0.07))
    vertical_margin = max(20, round(height * 0.065))
    default_style = (
        f"Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00101010,&H90000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,"
        f"{horizontal_margin},{horizontal_margin},{vertical_margin},1"
    )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
{style_format}
{default_style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [event for cue in _validate_cues(cues) for event in _cue_events(cue)]
    output.write_text(header + "\n".join(events) + ("\n" if events else ""), encoding="utf-8")
    return output
