"""Natural-boundary script chunking for long-form narration.

The chunker is cue-aware: Fish S2 Pro delivery tags (``[square bracket]``
cues) are never counted as spoken words, are kept glued to the sentence they
direct, and can never become a chunk of their own.  Text without cues chunks
byte-identically to the original word-counting behavior.
"""

from __future__ import annotations

import re

from .performance import count_spoken_words, normalize_tagged_layout

# Sentence boundary: after terminal punctuation, before the next sentence's
# first character.  The ``\[`` in the lookahead keeps a sentence that starts
# with a delivery cue (``[emphasis] This is...``) attached to its cue instead
# of splitting between the bracket and the sentence.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")
_TAGGED_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[\[A-Z0-9\"'])")

# Tokens for hard-splitting an oversized sentence: a whole cue stays one
# token so it is never separated from the word it annotates.
_CUE_OR_WORD_RE = re.compile(r"\[[^\[\]\n]+\]|\S+")


def chunk_narration(text: str, target_seconds: float, *, words_per_second: float = 2.5) -> list[str]:
    """Keep paragraphs intact when possible, splitting only oversized paragraphs."""
    target_words = max(10, round(target_seconds * words_per_second))
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph.split()) <= target_words:
            units.append(paragraph)
            continue
        units.extend(_split_large_paragraph(paragraph, target_words))

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for unit in units:
        count = len(unit.split())
        if current and current_words + count > target_words:
            chunks.append("\n\n".join(current))
            current, current_words = [], 0
        current.append(unit)
        current_words += count
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_narration_tagged(
    text: str, target_seconds: float, *, words_per_second: float = 2.5,
) -> list[str]:
    """Chunk Fish S2 Pro delivery-tagged narration.

    Layout is normalized first so a cue placed on its own line glues to the
    following sentence; the cue-aware :func:`chunk_narration` then sizes
    chunks by spoken words only.
    """
    normalized = normalize_tagged_layout(text)
    target_words = max(10, round(target_seconds * words_per_second))
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()
    ]
    units: list[str] = []
    for paragraph in paragraphs:
        if count_spoken_words(paragraph) <= target_words:
            units.append(paragraph)
        else:
            units.extend(_split_large_paragraph_tagged(paragraph, target_words))

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    leading: list[str] = []
    for unit in units:
        count = count_spoken_words(unit)
        if count == 0 and "[" in unit:
            if current:
                current.append(unit)
            else:
                leading.append(unit)
            continue
        if current and current_words + count > target_words:
            chunks.append("\n\n".join(leading + current))
            current, current_words, leading = [], 0, []
        current.append(unit)
        current_words += count
    if current or leading:
        chunks.append("\n\n".join(leading + current))
    return chunks


def _split_large_paragraph(paragraph: str, target_words: int) -> list[str]:
    sentences = _SENTENCE.split(paragraph)
    result: list[str] = []
    current: list[str] = []
    words = 0
    for sentence in sentences:
        sentence_words = sentence.split()
        if len(sentence_words) > target_words:
            if current:
                result.append(" ".join(current))
                current, words = [], 0
            result.extend(
                " ".join(sentence_words[index:index + target_words])
                for index in range(0, len(sentence_words), target_words)
            )
        elif current and words + len(sentence_words) > target_words:
            result.append(" ".join(current))
            current, words = [sentence], len(sentence_words)
        else:
            current.append(sentence)
            words += len(sentence_words)
    if current:
        result.append(" ".join(current))
    return result


def _split_large_paragraph_tagged(paragraph: str, target_words: int) -> list[str]:
    sentences = _TAGGED_SENTENCE.split(paragraph)
    result: list[str] = []
    current: list[str] = []
    words = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence_words = count_spoken_words(sentence)
        if sentence_words == 0 and "[" in sentence:
            # A cue-only fragment (e.g. a trailing ``[pause]``): glue it to
            # the previous sentence so it can never end a chunk dangling or
            # become a chunk of pure bracket text.
            if current:
                current[-1] = f"{current[-1]} {sentence}"
            else:
                result.append(sentence)
            continue
        if sentence_words > target_words:
            if current:
                result.append(" ".join(current))
                current, words = [], 0
            result.extend(_hard_split_tagged(sentence, target_words))
        elif current and words + sentence_words > target_words:
            result.append(" ".join(current))
            current, words = [sentence], sentence_words
        else:
            current.append(sentence)
            words += sentence_words
    if current:
        result.append(" ".join(current))
    return result


def _hard_split_tagged(sentence: str, target_words: int) -> list[str]:
    """Split an oversized sentence on spoken words, keeping cues attached.

    Cues are whole tokens that never count toward the word budget and never
    trigger a split, so a cue always travels with the word it annotates.
    """
    tokens = _CUE_OR_WORD_RE.findall(sentence)
    groups: list[str] = []
    current: list[str] = []
    pending_cues: list[str] = []
    words = 0
    for token in tokens:
        is_cue = token.startswith("[")
        if is_cue:
            pending_cues.append(token)
            continue
        if current and words + 1 > target_words:
            groups.append(" ".join(current))
            current, words = [], 0
        current.extend(pending_cues)
        pending_cues = []
        current.append(token)
        words += 1
    # A trailing pause/tone cue has no following word to direct, so keep it
    # with the final spoken group rather than emitting it alone.
    if pending_cues:
        if current:
            current.extend(pending_cues)
        elif groups:
            groups[-1] = " ".join([groups[-1], *pending_cues])
    if current:
        groups.append(" ".join(current))
    return groups
