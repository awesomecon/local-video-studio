"""Local narration profiles, chunking, and generation orchestration."""

from .manager import TTSManager
from .models import NarrationRequest, VoiceProfile
from .performance import (
    NoNarrationTextError,
    PerformanceScript,
    PerformanceSegment,
    strip_performance_tags,
    validate_tagged,
)

__all__ = [
    "NarrationRequest",
    "NoNarrationTextError",
    "PerformanceScript",
    "PerformanceSegment",
    "TTSManager",
    "VoiceProfile",
    "strip_performance_tags",
    "validate_tagged",
]
