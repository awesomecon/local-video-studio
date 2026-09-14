"""Auto Score: proposed cues over a soundtrack, without touching the saved plan.

Phase 3 ships the deterministic fallback recipe the Studio can always rely on
(no local LLM required). Phase 4 layers a local-LLM path on top of the same
:func:`suggest_auto_score` contract: both return *suggestions* that the user
must explicitly accept before anything is persisted. The saved score plan is
never overwritten by an auto pass, and locked cues survive an accept because
:func:`apply_auto_score_suggestions` declines any proposal that would collide
with a locked manual cue.

Only project-local data may feed the suggestions (timed narration, scene and
editorial boundaries, caption emphasis, duration, the user's music direction
and intensity). Nothing is sent to a remote service.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from backend.schemas.models import DomainModel, new_id

from .score import (
    DEFAULT_CUE_TRANSITION_SECONDS,
    LOWPASS_HZ_MAX,
    LOWPASS_HZ_MIN,
    GAIN_DB_MAX,
    GAIN_DB_MIN,
    MAX_DURATION_SECONDS,
    ScoreAction,
    ScoreCue,
    ScoreCueSource,
    ScorePlan,
)

#: Intensity presets map to transition shaping. Subtle moves land gently and
#: broadly; expressive moves are short and punchy.
_INTENSITY_TRANSITION = {
    "subtle": 0.6,
    "balanced": 0.3,
    "expressive": 0.15,
}

#: Two cues within this window (same action) are considered a collision for
#: the purpose of protecting locked cues during an auto accept.
LOCKED_CUE_COLLISION_SECONDS = 0.5


class AutoScoreSuggestion(DomainModel):
    """One proposed cue. It is not a persisted cue until the user accepts it."""

    time_seconds: float = Field(ge=0, le=MAX_DURATION_SECONDS, allow_inf_nan=False)
    action: ScoreAction
    label: str = Field(default="", max_length=200)
    transition_seconds: float = Field(
        default=DEFAULT_CUE_TRANSITION_SECONDS,
        ge=0,
        le=5.0,
        allow_inf_nan=False,
    )
    reason: str = Field(default="", max_length=500)
    gain_db: float | None = Field(
        default=None, ge=GAIN_DB_MIN, le=GAIN_DB_MAX, allow_inf_nan=False
    )
    lowpass_hz: float | None = Field(
        default=None, ge=LOWPASS_HZ_MIN, le=LOWPASS_HZ_MAX, allow_inf_nan=False
    )

    def to_cue(self) -> ScoreCue:
        """Materialize this proposal as an ``auto``-sourced, unlocked cue."""
        return ScoreCue(
            id=new_id(),
            time_seconds=self.time_seconds,
            action=self.action,
            label=self.label,
            transition_seconds=self.transition_seconds,
            gain_db=self.gain_db,
            lowpass_hz=self.lowpass_hz,
            source=ScoreCueSource.AUTO,
            locked=False,
        )


def _transition_for(
    intensity: str, base: float = DEFAULT_CUE_TRANSITION_SECONDS,
) -> float:
    scale = _INTENSITY_TRANSITION.get(intensity, _INTENSITY_TRANSITION["balanced"])
    return round(max(0.0, min(5.0, base * scale / _INTENSITY_TRANSITION["balanced"])), 3)


def _clamp_time(value: float, duration: float) -> float:
    return round(max(0.0, min(value, duration)), 3)


def deterministic_auto_score(
    duration_seconds: float,
    *,
    scene_boundaries: list[tuple[float, float]] | None = None,
    emphasis_times: list[float] | None = None,
    locked_cue_times: list[float] | None = None,
    intensity: str = "balanced",
    music_direction: str = "",
) -> list[AutoScoreSuggestion]:
    """Produce the deterministic cue recipe for a soundtrack of ``duration_seconds``.

    ``scene_boundaries`` are ``(start, end)`` spans on the audio clock;
    ``emphasis_times`` are caption-emphasis start times; ``locked_cue_times``
    are existing locked cues that the recipe must not crowd. Times are clamped
    into the soundtrack, collisions with locked cues are dropped, and the
    result is sorted by time.
    """
    if not duration_seconds or duration_seconds <= 0:
        return []
    duration = float(duration_seconds)
    transition = _transition_for(intensity)
    locked = sorted(float(t) for t in (locked_cue_times or []))
    boundaries = [
        (float(s), float(e))
        for s, e in (scene_boundaries or [])
        if 0 <= s < e <= duration + 1e-3
    ]
    emphasis = sorted(float(t) for t in (emphasis_times or []) if 0 <= t <= duration)

    def near_locked(time: float) -> bool:
        return any(abs(time - lock) <= LOCKED_CUE_COLLISION_SECONDS for lock in locked)

    def nearest(times: list[float], target: float) -> float | None:
        if not times:
            return None
        return min(times, key=lambda value: abs(value - target))

    # A reveal anchor: the strongest caption emphasis in the back half, else a
    # scene boundary near 75%, else the fixed 75% mark.
    back_emphasis = [t for t in emphasis if t >= duration * 0.45]
    reveal_time = nearest(back_emphasis, duration * 0.75)
    if reveal_time is None:
        mids = [(s + e) / 2 for s, e in boundaries]
        reveal_time = nearest(mids, duration * 0.75)
        if reveal_time is None:
            reveal_time = duration * 0.75
    reveal_time = _clamp_time(reveal_time, duration)

    # A pullback anchor: a major emphasized phrase just before the reveal, else
    # a scene boundary near 50%, else the fixed 50% mark.
    front_emphasis = [t for t in emphasis if t < duration * 0.45]
    pullback_time = nearest(front_emphasis, duration * 0.5)
    if pullback_time is None:
        starts = [s for s, _ in boundaries if duration * 0.3 <= s <= duration * 0.7]
        pullback_time = nearest(starts, duration * 0.5)
        if pullback_time is None:
            pullback_time = duration * 0.5
    pullback_time = _clamp_time(pullback_time, duration)

    end_sting_time = _clamp_time(duration * 0.94, duration)

    raw: list[AutoScoreSuggestion] = [
        AutoScoreSuggestion(
            time_seconds=_clamp_time(min(0.6, duration * 0.05), duration),
            action=ScoreAction.BUILD,
            label="Opening hook",
            transition_seconds=transition,
            reason="Establish the bed as the piece opens.",
        ),
        AutoScoreSuggestion(
            time_seconds=_clamp_time(duration * 0.25, duration),
            action=ScoreAction.BUILD,
            label="Gentle build",
            transition_seconds=transition,
            reason="Lift energy as the first ideas land.",
            gain_db=2,
        ),
        AutoScoreSuggestion(
            time_seconds=pullback_time,
            action=ScoreAction.PULL_BACK,
            label="Pull back before the turn",
            transition_seconds=transition,
            reason=(
                "Sit the music under the shift in emphasis."
                if music_direction
                else "Leave room for the narration to lead."
            ),
            gain_db=-6,
        ),
        AutoScoreSuggestion(
            time_seconds=_clamp_time(max(0.0, reveal_time - 0.3), duration),
            action=ScoreAction.SILENCE,
            label="Hold before the reveal",
            transition_seconds=min(0.2, transition),
            reason="Cut to silence right before the strongest beat.",
        ),
        AutoScoreSuggestion(
            time_seconds=reveal_time,
            action=ScoreAction.RESTORE,
            label="Restore for the reveal",
            transition_seconds=min(0.2, transition),
            reason="Bring the score back in on the reveal.",
        ),
        AutoScoreSuggestion(
            time_seconds=end_sting_time,
            action=ScoreAction.END_STING,
            label="Closing sting",
            transition_seconds=min(0.2, transition),
            reason="Land the final beat near the call to action.",
        ),
    ]

    kept = [cue for cue in raw if not near_locked(cue.time_seconds)]
    kept.sort(key=lambda cue: cue.time_seconds)
    return kept


def apply_auto_score_suggestions(
    plan: ScorePlan,
    suggestions: list[AutoScoreSuggestion],
) -> tuple[ScorePlan, list[ScoreCue]]:
    """Merge accepted suggestions into ``plan`` as new ``auto`` cues.

    A proposal is dropped when it would collide (same action within
    :data:`LOCKED_CUE_COLLISION_SECONDS`) with a *locked* cue, so locking a
    manual cue protects it from auto passes. Existing cues are preserved in
    order and identity; the result is re-validated (sorted, unique ids).
    Returns the new plan and the cues that were actually added.
    """
    locked = [cue for cue in plan.cues if cue.locked]
    added: list[ScoreCue] = []
    for suggestion in suggestions:
        collides = any(
            lock.action is suggestion.action
            and abs(lock.time_seconds - suggestion.time_seconds)
            <= LOCKED_CUE_COLLISION_SECONDS
            for lock in locked
        )
        if collides:
            continue
        added.append(suggestion.to_cue())

    merged = plan.model_copy(deep=True)
    merged.cues = [*plan.cues, *added]
    merged = ScorePlan.model_validate(merged.model_dump())
    return merged, added


# ---------------------------------------------------------------------------
# Local-LLM auto score
#
# The deterministic recipe above is always available; this module layer adds
# the configured local LLM as the *preferred* author of suggestions. Only
# project-local data (timed narration, scene/composition boundaries, caption
# emphasis, duration, the user's music direction and intensity, and existing
# locked cues) is sent to the LLM, and the LLM is local by configuration —
# nothing leaves the machine. The response is grammar-constrained structured
# JSON, re-validated client-side, and sanitized (times clamped into the
# soundtrack, proposals crowding locked cues dropped). Any failure of the LLM
# path degrades to the deterministic recipe.
# ---------------------------------------------------------------------------

#: Suggestion budget: short enough to finish quickly, long enough for the
#: full cue vocabulary with labels and reasons.
AUTO_SCORE_MAX_TOKENS = 4096
AUTO_SCORE_TEMPERATURE = 0.2
#: Reasoning stays enabled (per the machine's LLM policy) inside a modest
#: per-request budget; cue placement does not need long deliberation.
AUTO_SCORE_THINKING_BUDGET_TOKENS = 2_000

#: How many timed context lines the prompt may carry per collection.
_MAX_PROMPT_SEGMENTS = 40


class AutoScoreProposal(DomainModel):
    """Structured response of the local LLM: the proposed cue list."""

    cues: list[AutoScoreSuggestion] = Field(min_length=1, max_length=24)


#: Grammar-safe wire schema. No string length bounds (llama.cpp turns them
#: into bounded repetitions that trip its sanity limits); pydantic re-checks
#: everything client-side via :func:`validate_auto_score_payload`.
AUTO_SCORE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cues": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "time_seconds": {"type": "number"},
                    "action": {"type": "string", "enum": [action.value for action in ScoreAction]},
                    "label": {"type": "string"},
                    "transition_seconds": {"type": "number"},
                    "reason": {"type": "string"},
                    "gain_db": {"type": "number"},
                    "lowpass_hz": {"type": "number"},
                },
                "required": ["time_seconds", "action", "label", "reason"],
            },
        },
    },
    "required": ["cues"],
    "additionalProperties": False,
}

_ACTION_GUIDANCE = {
    ScoreAction.BUILD: "lift the bed in level/energy by gain_db dB relative to its current level from its time onward (blank = +3 dB)",
    ScoreAction.PULL_BACK: "sit the bed down by gain_db dB relative to its current level so the narration leads (blank = -6 dB; optionally with a low-pass filter via lowpass_hz)",
    ScoreAction.SILENCE: "drop the bed to silence from its time",
    ScoreAction.RESTORE: "bring the bed back up to full level",
    ScoreAction.IMPACT: "punch in a one-shot hit/impact asset at this exact time",
    ScoreAction.RISER: "mix a rising one-shot asset under the moment",
    ScoreAction.END_STING: "land a short closing hit near the end",
}


def validate_auto_score_payload(payload: Any) -> AutoScoreProposal:
    """Client-side authority for the LLM's structured response."""
    return AutoScoreProposal.model_validate(payload)


def _format_prompt_segment(segment: dict[str, Any], duration: float) -> str:
    start = float(segment.get("start_seconds", 0.0))
    end = float(segment.get("end_seconds", start))
    text = str(segment.get("text", "")).strip().replace("\n", " ")
    if len(text) > 140:
        text = text[:137] + "..."
    return f"[{start:6.2f}s - {min(end, duration):6.2f}s] {text}"


#: Project-local context handed to the local LLM. Every field is derived from
#: the project's own narration, scenes, captions, and saved cues.
AutoScoreContext = dict[str, Any]


def build_auto_score_prompt(
    context: AutoScoreContext,
) -> list[dict[str, str]]:
    """Compose the messages for the local LLM auto-score request.

    ``context`` keys: ``duration_seconds``, ``music_direction`` (str),
    ``intensity`` (str), ``narration_segments`` (list of dicts with
    start_seconds/end_seconds/text), ``scene_spans`` (list of dicts with
    start_seconds/end_seconds/title), ``emphasis_phrases`` (list of str or
    {text,start_seconds}), ``locked_cues`` (list of dicts with
    time_seconds/action/label).
    """
    duration = float(context.get("duration_seconds", 0.0))
    direction = str(context.get("music_direction") or "").strip()
    intensity = str(context.get("intensity") or "balanced")

    segments = [s for s in (context.get("narration_segments") or [])][:_MAX_PROMPT_SEGMENTS]
    spans = [s for s in (context.get("scene_spans") or [])][:_MAX_PROMPT_SEGMENTS]
    emphasis = [e for e in (context.get("emphasis_phrases") or [])][:_MAX_PROMPT_SEGMENTS]
    locked = [c for c in (context.get("locked_cues") or [])][:_MAX_PROMPT_SEGMENTS]

    parts: list[str] = [
        f"Soundtrack: {duration:.3f}s of generated instrumental music that cannot be re-composed.",
        f"Music direction: {direction or '(none given)'}",
        f"Desired intensity: {intensity}",
        "",
        "Timed narration (the audio clock these cues must serve):",
    ]
    if segments:
        parts.extend("  " + _format_prompt_segment(s, duration) for s in segments)
    else:
        parts.append("  (no narration available yet)")

    parts.append("")
    parts.append("Scene / composition boundaries:")
    if spans:
        for span in spans:
            index = span.get("index")
            number = str(int(index) + 1) if isinstance(index, (int, float)) else "?"
            start = float(span.get("start_seconds", 0.0))
            end = float(span.get("end_seconds", 0.0))
            title = str(span.get("title", "")).strip() or "untitled"
            parts.append(f"  [{start:6.2f}s - {end:6.2f}s] scene {number}: {title}")
    else:
        parts.append("  (no scene data)")

    if emphasis:
        parts.append("")
        parts.append("Emphasized caption phrases (weight cues around these):")
        for phrase in emphasis:
            if isinstance(phrase, dict):
                start = float(phrase.get("start_seconds") or 0.0)
                text = str(phrase.get("text", "")).strip()
                parts.append(f"  [at {start:.2f}s] {text}")
            else:
                parts.append(f"  {str(phrase).strip()}")

    if locked:
        parts.append("")
        parts.append("Locked manual cues (never propose anything within 0.5s of these):")
        for cue in locked:
            parts.append(
                f"  [{float(cue.get('time_seconds', 0.0)):.2f}s] {cue.get('action')} — {cue.get('label', '')}"
            )

    if context.get("effect_assets"):
        parts.append("")
        parts.append("Available effect assets (impacts/stings the mix can place at cue times):")
        parts.append("  " + ", ".join(str(item) for item in context["effect_assets"]))

    vocabulary = "; ".join(f"{action.value} = {text}" for action, text in _ACTION_GUIDANCE.items())
    system = (
        "You are the score editor for a short video. The instrumental background "
        "track was already composed by ACE-Step and is fixed; you can only place "
        "exact timed cues that an audio engine applies afterward. Propose a small "
        "set of cues (usually 4 to 9, at most 12) that make the music serve the "
        "narration. Rules: time_seconds are seconds into the soundtrack and must "
        f"not exceed {duration:.3f}; use short transitions (0.1-0.6s) for silence "
        "and restore moves; a silence cue should sit just before the strongest "
        "reveal with a restore at the reveal; only propose impact/riser/end_sting "
        "when one-shot effect assets are available; keep labels under 40 characters "
        "and give one concrete reason per cue. Never propose cues near locked cues. "
        f"Action vocabulary: {vocabulary}."
    )
    if not context.get("effect_assets"):
        system += " This project has no effect assets yet; stick to automation cues (build, pull_back, silence, restore)."

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts) + "\n\nReturn the cue plan as JSON."},
    ]


def _sanitize_suggestions(
    suggestions: list[AutoScoreSuggestion],
    *,
    duration_seconds: float,
    locked_cues: list[dict[str, Any]],
) -> list[AutoScoreSuggestion]:
    """Clamp times into the soundtrack and drop proposals crowding locked cues."""
    locked = [
        (str(cue.get("action", "")), float(cue.get("time_seconds", 0.0)))
        for cue in locked_cues
    ]
    kept: list[AutoScoreSuggestion] = []
    for suggestion in suggestions:
        time = min(max(suggestion.time_seconds, 0.0), duration_seconds)
        time = round(time, 3)
        if any(
            action == suggestion.action.value and abs(time - when) <= LOCKED_CUE_COLLISION_SECONDS
            for action, when in locked
        ):
            continue
        if time != suggestion.time_seconds:
            suggestion = suggestion.model_copy(update={"time_seconds": time})
        kept.append(suggestion)
    kept.sort(key=lambda item: item.time_seconds)
    return kept


def llm_auto_score(
    backend: Any,
    *,
    context: AutoScoreContext,
    model: str | None = None,
) -> list[AutoScoreSuggestion]:
    """Ask the configured local LLM for score cues (structured JSON).

    ``backend`` is a ``LocalLLMBackend``. Raises (``BackendError``,
    ``ValueError``, network errors) on any failure; callers fall back to
    :func:`deterministic_auto_score`. Reasoning stays enabled inside the
    configured per-request budget.
    """
    duration = float(context.get("duration_seconds", 0.0))
    if duration <= 0:
        raise ValueError("auto score requires a soundtrack duration")
    messages = build_auto_score_prompt(context)
    payload = backend.complete(
        messages=messages,
        structured=True,
        json_schema=AUTO_SCORE_JSON_SCHEMA,
        validator=validate_auto_score_payload,
        max_tokens=AUTO_SCORE_MAX_TOKENS,
        temperature=AUTO_SCORE_TEMPERATURE,
        thinking_budget_tokens=AUTO_SCORE_THINKING_BUDGET_TOKENS,
        model=model,
    )
    proposal = payload if isinstance(payload, AutoScoreProposal) else validate_auto_score_payload(payload)
    return _sanitize_suggestions(
        proposal.cues,
        duration_seconds=duration,
        locked_cues=context.get("locked_cues") or [],
    )
