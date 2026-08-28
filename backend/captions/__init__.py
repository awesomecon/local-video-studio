"""Local narration alignment and readable subtitle-cue construction."""

from .alignment import (
    AlignmentResult,
    CaptionWord,
    build_caption_cues,
    restore_authored_punctuation,
)

__all__ = [
    "AlignmentResult",
    "CaptionWord",
    "build_caption_cues",
    "restore_authored_punctuation",
]
