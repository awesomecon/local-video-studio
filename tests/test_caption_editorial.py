"""Editorial documentary caption styles: grouping, emphasis, placement, separation.

These tests pin the contract between the deterministic caption module
(backend/captions/editorial.py), the renderer's caption layer, and the
pipeline: Editorial Mode renders phrase-level documentary captions inside the
composition while Classic Mode keeps burning the shared ASS subtitles, and
the legacy "standard" style keeps the classic large-caption look.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.captions import CaptionWord
from backend.captions.editorial import (
    build_editorial_caption_cues,
    caption_font_sizes,
    choose_caption_position,
    find_emphasis_spans,
    place_caption_cues,
    slice_caption_cues,
    suppress_cues_for_reveals,
)
from backend.core import load_config
from backend.editorial import (
    EditorialAsset,
    EditorialAssetType,
    EditorialCaptionCue,
    EditorialCaptionEmphasis,
    EditorialCaptionStyle,
    EditorialComposition,
    EditorialElement,
    EditorialElementType,
    EditorialEvent,
    EditorialPlanner,
    EditorialTemplate,
    EditPlan,
    MotionPrimitive,
    compile_edit_plan_html,
)
from backend.pipeline import PipelineService
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.process import run_media_process
from backend.schemas import (
    Project,
    ProjectCreate,
    ProjectPlan,
    Scene,
    VideoMode,
)

MARS_NARRATION = (
    "Mars is governed by ten men. Their chief ruler has one unusual title."
)


def _words(text: str, *, start: float = 0.0, pace: float = 0.3) -> list[CaptionWord]:
    """Evenly paced word timings for a narration sentence."""
    words: list[CaptionWord] = []
    cursor = start
    for token in text.split():
        words.append(CaptionWord(cursor, cursor + 0.22, token))
        cursor += pace
    return words


def _archive_composition(
    *, reveal_at: float | None = 5.0, duration: float = 10.0,
) -> EditorialComposition:
    elements = [
        EditorialElement(
            id="year", type=EditorialElementType.TEXT, text="1949", role="year",
        ),
    ]
    events = [EditorialEvent(time=0.0, action=MotionPrimitive.FADE_UP, target="year")]
    if reveal_at is not None:
        elements.append(EditorialElement(
            id="elon", type=EditorialElementType.TEXT, text="ELON", role="reveal",
        ))
        events.append(EditorialEvent(
            time=reveal_at, action=MotionPrimitive.FADE_UP, target="elon",
        ))
    return EditorialComposition(
        id="opening", start=0.0, duration=duration,
        template=EditorialTemplate.ARCHIVE_CANVAS,
        elements=elements, events=events,
    )


def _cue(start: float, end: float, text: str, *, highlight: bool = False) -> EditorialCaptionCue:
    return EditorialCaptionCue(
        start=start, end=end, text=text,
        style=EditorialCaptionStyle.EDITORIAL_PHRASE, highlight=highlight,
    )


# --- Phrase grouping ---------------------------------------------------------


def test_phrase_grouping_matches_documentary_beats() -> None:
    words = _words(MARS_NARRATION)
    cues = build_editorial_caption_cues(
        words, EditorialCaptionStyle.EDITORIAL_PHRASE,
        emphasis_texts=["ten men", "one unusual title"],
    )
    assert [cue.text for cue in cues] == [
        "Mars is governed by",
        "ten men.",
        "Their chief ruler has",
        "one unusual title.",
    ]
    assert [cue.highlight for cue in cues] == [False, True, False, True]
    assert all(cue.style is EditorialCaptionStyle.EDITORIAL_PHRASE for cue in cues)
    assert " ".join(cue.text for cue in cues) == MARS_NARRATION
    for cue in cues:
        assert cue.start >= 0 and cue.end > cue.start


def test_phrase_grouping_without_emphasis_stays_within_limits() -> None:
    cues = build_editorial_caption_cues(
        _words(MARS_NARRATION), EditorialCaptionStyle.EDITORIAL_PHRASE,
    )
    assert cues
    seen: list[str] = []
    previous_start = -1.0
    for cue in cues:
        assert len(cue.text.split()) <= 7, cue.text
        assert cue.start >= previous_start
        previous_start = cue.start
        seen.append(cue.text)
    assert " ".join(seen) == MARS_NARRATION


def test_quiet_documentary_style_never_highlights() -> None:
    cues = build_editorial_caption_cues(
        _words(MARS_NARRATION), EditorialCaptionStyle.QUIET_DOCUMENTARY,
        emphasis_texts=["ten men"],
    )
    assert cues
    assert all(cue.highlight is False for cue in cues)


def test_one_line_and_one_word_styles() -> None:
    words = _words(MARS_NARRATION)
    one_line = build_editorial_caption_cues(words, EditorialCaptionStyle.ONE_LINE)
    assert all(len(cue.text.split()) <= 8 for cue in one_line)
    assert " ".join(cue.text for cue in one_line) == MARS_NARRATION
    one_word = build_editorial_caption_cues(words, EditorialCaptionStyle.ONE_WORD)
    assert [cue.text for cue in one_word] == [word.text for word in words]
    assert all(cue.highlight is False for cue in one_word)


def test_standard_style_returns_no_beats() -> None:
    assert build_editorial_caption_cues(
        _words(MARS_NARRATION), EditorialCaptionStyle.STANDARD,
    ) == []


def test_emphasis_span_matching_is_insensitive_and_non_overlapping() -> None:
    words = [
        CaptionWord(0.0, 0.2, "Ten"),
        CaptionWord(0.3, 0.5, "men."),
        CaptionWord(0.8, 1.0, "ten"),
        CaptionWord(1.1, 1.3, "men"),
    ]
    spans = find_emphasis_spans(words, ["TEN MEN", "NEURALINK"])
    assert spans == [(0, 2)]  # case-insensitive; unspoken phrase ignored; no reuse

    cues = build_editorial_caption_cues(
        words, EditorialCaptionStyle.EDITORIAL_PHRASE, emphasis_texts=["TEN MEN"],
    )
    assert [cue.highlight for cue in cues] == [True, False]


def test_numeric_whisper_token_matches_authored_number_word_emphasis() -> None:
    words = [
        CaptionWord(0.0, 0.3, "10"),
        CaptionWord(0.3, 0.7, "men."),
    ]
    cues = build_editorial_caption_cues(
        words, EditorialCaptionStyle.EDITORIAL_PHRASE,
        emphasis_texts=["ten men"],
    )
    assert len(cues) == 1 and cues[0].highlight is True


def test_split_hyphenated_compound_is_presented_without_a_false_space() -> None:
    words = [
        CaptionWord(0.0, 0.4, "multi"),
        CaptionWord(0.4, 0.9, "-planetary."),
    ]
    cues = build_editorial_caption_cues(
        words, EditorialCaptionStyle.EDITORIAL_PHRASE,
    )
    assert [cue.text for cue in cues] == ["multi-planetary."]


def test_emphasis_phrase_isolated_at_both_boundaries() -> None:
    cues = build_editorial_caption_cues(
        _words("They called Neuralink the voice of Mars."),
        EditorialCaptionStyle.EDITORIAL_PHRASE,
        emphasis_texts=["Neuralink"],
    )
    emphasized = [cue for cue in cues if cue.highlight]
    assert [cue.text for cue in emphasized] == ["Neuralink"]
    assert " ".join(cue.text for cue in cues) == "They called Neuralink the voice of Mars."


def test_editorial_caption_beats_never_overlap() -> None:
    cues = build_editorial_caption_cues(
        _words("One phrase ends. Another phrase begins."),
        EditorialCaptionStyle.EDITORIAL_PHRASE,
    )
    assert all(
        current.end <= following.start
        for current, following in zip(cues, cues[1:])
    )


def test_fallback_emphasis_prefers_numerals_without_back_to_back() -> None:
    cues = build_editorial_caption_cues(
        _words("In 1949 the rulers arrived. Their power grew."),
        EditorialCaptionStyle.EDITORIAL_PHRASE,
    )
    flags = [cue.highlight for cue in cues]
    assert any(flags)
    assert not any(flags[index] and flags[index - 1] for index in range(1, len(flags)))
    numeral_cue = next(cue for cue in cues if "1949" in cue.text)
    assert numeral_cue.highlight is True


# --- Emphasis metadata from the planner --------------------------------------


class _EmphasisLLM:
    """Planner stub returning a fixed validated payload."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["validator"](self.payload)


def test_planner_emphasis_metadata_reaches_plan_as_metadata_only() -> None:
    project = Project(
        title="Mars", topic="Project Mars", target_duration=14, slug="mars-emphasis",
        video_mode=VideoMode.EDITORIAL, resolution=(1080, 1920), fps=24,
    )
    script = ProjectPlan(
        project_id=project.id, title=project.title, outline=["Opening"],
        target_duration=14,
        scenes=[Scene(
            project_id=project.id, index=0, title="Opening", duration=14,
            narration="Mars is governed by ten men. Their chief ruler has one unusual title.",
        )],
    )
    payload = {
        "compositions": [{
            "id": "opening", "start": 0, "duration": 14,
            "template": "archiveCanvas",
            "elements": [
                {"id": "date", "type": "text", "text": "1949", "role": "year"},
                {"id": "name", "type": "text", "text": "ELON", "role": "reveal"},
            ],
            "events": [
                {"time": 0, "action": "fadeUp", "target": "date"},
                {"time": 12.5, "action": "fadeUp", "target": "name"},
            ],
            "narration_refs": [script.scenes[0].id],
        }],
        "caption_emphasis": [
            {"text": "ten men", "emphasis": "keyPhrase"},
            {"text": "one unusual title", "emphasis": "keyPhrase"},
        ],
    }
    llm = _EmphasisLLM(payload)
    plan = EditorialPlanner(llm).plan(project, script)

    assert [item.text for item in plan.caption_emphasis] == [
        "ten men", "one unusual title",
    ]
    assert all(
        item.emphasis is EditorialCaptionEmphasis.model_fields["emphasis"].default
        or item.emphasis.value == "keyPhrase"
        for item in plan.caption_emphasis
    )
    # The planner prompt asks for metadata only: no styling, no CSS.
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "caption_emphasis" in system_prompt
    assert "metadata only" in system_prompt
    assert "never return styling for captions" in system_prompt


def test_emphasis_metadata_selects_highlights_end_to_end() -> None:
    emphasis = [
        EditorialCaptionEmphasis(text="ten men"),
        EditorialCaptionEmphasis(text="one unusual title"),
    ]
    cues = build_editorial_caption_cues(
        _words(MARS_NARRATION), EditorialCaptionStyle.EDITORIAL_PHRASE,
        emphasis_texts=[item.text for item in emphasis],
    )
    assert [cue.highlight for cue in cues] == [False, True, False, True]


# --- Responsive font scaling --------------------------------------------------


def test_caption_font_sizes_scale_by_orientation() -> None:
    styles = [
        EditorialCaptionStyle.EDITORIAL_PHRASE,
        EditorialCaptionStyle.QUIET_DOCUMENTARY,
        EditorialCaptionStyle.ONE_LINE,
        EditorialCaptionStyle.ONE_WORD,
    ]
    for style in styles:
        portrait_normal, portrait_high = caption_font_sizes(style, "portrait")
        landscape_normal, landscape_high = caption_font_sizes(style, "landscape")
        # Spec: 1080x1920 normal ~44-54px, highlighted ~52-64px; 16:9 smaller.
        assert 40 <= portrait_normal <= 54, (style, portrait_normal)
        assert portrait_normal <= portrait_high <= 64, (style, portrait_high)
        assert landscape_normal < portrait_normal
        assert landscape_high <= portrait_high


def test_caption_font_scaling_rejects_unknown_orientation() -> None:
    try:
        caption_font_sizes(EditorialCaptionStyle.EDITORIAL_PHRASE, "square")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown orientation")


# --- Safe positioning ---------------------------------------------------------


def _minimal_content(template: EditorialTemplate) -> tuple[list, list]:
    """Smallest valid (elements, assets) set per template (required roles)."""
    if template is EditorialTemplate.DOCUMENT_REVEAL:
        document = EditorialAsset(
            id="doc-asset", type=EditorialAssetType.DOCUMENT,
        )
        return (
            [
                EditorialElement(
                    id="doc", type=EditorialElementType.DOCUMENT,
                    asset_id="doc-asset", role="document",
                ),
                EditorialElement(
                    id="title", type=EditorialElementType.TEXT, text="Title",
                    role="title",
                ),
            ],
            [document],
        )
    if template is EditorialTemplate.COMPARISON_CANVAS:
        left = EditorialAsset(id="left-asset", type=EditorialAssetType.COMPARISON)
        right = EditorialAsset(id="right-asset", type=EditorialAssetType.COMPARISON)
        return (
            [
                EditorialElement(
                    id="left", type=EditorialElementType.IMAGE,
                    asset_id="left-asset", role="left-image",
                ),
                EditorialElement(
                    id="right", type=EditorialElementType.IMAGE,
                    asset_id="right-asset", role="right-image",
                ),
                EditorialElement(
                    id="head", type=EditorialElementType.TEXT, text="MARS",
                    role="headline",
                ),
            ],
            [left, right],
        )
    if template is EditorialTemplate.ILLUSTRATION_CANVAS:
        image = EditorialAsset(id="ill-asset", type=EditorialAssetType.GENERATED_IMAGE)
        return (
            [EditorialElement(
                id="ill", type=EditorialElementType.IMAGE,
                asset_id="ill-asset", role="illustration",
            )],
            [image],
        )
    if template is EditorialTemplate.BIG_TEXT_REVEAL:
        return (
            [EditorialElement(
                id="head", type=EditorialElementType.TEXT, text="TEN MEN",
                role="headline",
            )],
            [],
        )
    return (
        [EditorialElement(
            id="year", type=EditorialElementType.TEXT, text="1949", role="year",
        )],
        [],
    )


def _position_for(
    template: EditorialTemplate,
    orientation: str,
    *,
    box_width: int = 300,
    box_height: int = 60,
) -> tuple[str, int, int]:
    design_width, design_height = (
        (1920, 1080) if orientation == "landscape" else (1080, 1920)
    )
    elements, assets = _minimal_content(template)
    composition = EditorialComposition(
        id="c", start=0.0, duration=5.0, template=template,
        elements=elements, assets=assets,
    )
    return choose_caption_position(
        composition, orientation, design_width, design_height, box_width, box_height,
    )


def test_positioning_prefers_lower_left_and_never_lower_right() -> None:
    for template in EditorialTemplate:
        for orientation in ("portrait", "landscape"):
            anchor, x, y = _position_for(template, orientation)
            assert anchor in {"lower-left", "lower-center", "mid-left", "upper-left"}
            assert anchor != "lower-right"
            design_width, design_height = (
                (1920, 1080) if orientation == "landscape" else (1080, 1920)
            )
            assert x >= 40 and y >= 40
            assert x + 300 <= design_width - 40
            assert y + 60 <= design_height - 40


def test_positioning_escalates_anchors_when_lower_band_is_blocked() -> None:
    # Illustration canvas fills the lower frame: lower-left, lower-center and
    # mid-left all collide, so the first safe anchor is upper-left.
    image = EditorialAsset(id="ill-asset", type=EditorialAssetType.GENERATED_IMAGE)
    composition = EditorialComposition(
        id="c", start=0.0, duration=5.0,
        template=EditorialTemplate.ILLUSTRATION_CANVAS,
        assets=[image],
        elements=[
            EditorialElement(
                id="ill", type=EditorialElementType.IMAGE,
                asset_id="ill-asset", role="illustration",
            ),
            EditorialElement(
                id="head", type=EditorialElementType.TEXT, text="MARS",
                role="headline",
            ),
        ],
    )
    anchor, x, y = choose_caption_position(
        composition, "portrait", 1080, 1920, 300, 60,
    )
    assert anchor == "upper-left"
    assert (x, y) == (72, 140)


def test_positioning_falls_back_to_lower_left_on_total_overflow() -> None:
    anchor, _x, _y = _position_for(
        EditorialTemplate.ARCHIVE_CANVAS, "portrait", box_width=2000, box_height=2000,
    )
    assert anchor == "lower-left"


def test_place_caption_cues_emits_deterministic_payload() -> None:
    composition = _archive_composition(reveal_at=None)
    cues = build_editorial_caption_cues(
        _words("Mars is governed by ten men."),
        EditorialCaptionStyle.EDITORIAL_PHRASE, emphasis_texts=["ten men"],
    )
    first = place_caption_cues(cues, composition, 1080, 1920)
    second = place_caption_cues(cues, composition, 1080, 1920)
    assert first == second
    for item in first:
        assert set(item) == {
            "start", "end", "text", "style", "highlight", "anchor", "x", "y",
        }
        assert item["style"] == "editorialPhrase"
        assert item["end"] > item["start"]


def test_big_text_captions_stay_above_the_full_hero_title_band() -> None:
    composition = EditorialComposition(
        id="closing-card", start=0.0, duration=7.0,
        template=EditorialTemplate.BIG_TEXT_REVEAL,
        elements=[EditorialElement(
            id="headline", type=EditorialElementType.TEXT,
            text="THE STORY GOES DEEPER", role="headline",
        )],
    )
    cue = _cue(1.0, 2.0, "The full story gets much deeper.")
    placed = place_caption_cues([cue], composition, 1080, 1920)
    assert placed[0]["anchor"] == "upper-left"
    assert placed[0]["y"] == 140


# --- Reveal suppression -------------------------------------------------------


def test_fullscreen_reveal_hides_overlapping_captions_only() -> None:
    composition = _archive_composition(reveal_at=5.0, duration=10.0)
    cues = [
        _cue(3.0, 4.5, "The chief ruler"),   # ends before the reveal window
        _cue(4.8, 5.8, "Their name was Elon"),  # overlaps the ELON moment
        _cue(5.0, 5.15, "Elon"),             # lingers 150ms into it: stays up
    ]
    surviving = suppress_cues_for_reveals(cues, [composition])
    assert [cue.text for cue in surviving] == ["The chief ruler", "Elon"]


def test_headline_reveal_owns_its_full_screen_moment() -> None:
    """A big-text headline owns the screen: every overlapping caption is hidden
    so the moment gets room (COINCIDENCE?) instead of sharing it with a beat."""
    composition = EditorialComposition(
        id="moment", start=0.0, duration=10.0,
        template=EditorialTemplate.BIG_TEXT_REVEAL,
        elements=[EditorialElement(
            id="head", type=EditorialElementType.TEXT, text="TEN MEN",
            role="headline",
        )],
        events=[EditorialEvent(time=2.0, action=MotionPrimitive.FADE_UP, target="head")],
    )
    cues = [
        _cue(0.5, 1.6, "The year 1949"),       # before the headline window
        _cue(1.5, 2.4, "Mars is ruled by"),    # overlaps: the moment owns the screen
        _cue(2.2, 3.2, "Ten men."),            # duplicates the on-screen text
    ]
    surviving = suppress_cues_for_reveals(cues, [composition])
    assert [cue.text for cue in surviving] == ["The year 1949", "Mars is ruled by"]
    assert surviving[1].end == 1.85


def test_caption_crossing_end_of_fullscreen_reveal_resumes_after_cut() -> None:
    composition = EditorialComposition(
        id="question", start=0.0, duration=4.0,
        template=EditorialTemplate.BIG_TEXT_REVEAL,
        elements=[EditorialElement(
            id="head", type=EditorialElementType.TEXT,
            text="COINCIDENCE?", role="headline",
        )],
        events=[EditorialEvent(
            time=2.0, action=MotionPrimitive.HARD_CUT, target="head",
        )],
    )
    cue = _cue(3.6, 5.4, "And that is not the end.")
    surviving = suppress_cues_for_reveals([cue], [composition])
    assert len(surviving) == 1
    assert surviving[0].start == 4.0


def test_kicker_hides_only_duplicating_captions() -> None:
    """A kicker shares the frame with ordinary beats: only captions that restate
    the kicker's words are hidden (PROJECT MARS vs a 'Project Mars.' cue)."""
    composition = EditorialComposition(
        id="closing", start=0.0, duration=10.0,
        template=EditorialTemplate.BIG_TEXT_REVEAL,
        elements=[
            EditorialElement(
                id="kick", type=EditorialElementType.TEXT,
                text="PROJECT MARS", role="kicker",
            ),
            EditorialElement(
                id="head", type=EditorialElementType.TEXT,
                text="THE FULL STORY GETS STRANGER", role="headline",
            ),
        ],
        events=[
            EditorialEvent(time=0.0, action=MotionPrimitive.FADE_UP, target="kick"),
            EditorialEvent(time=4.0, action=MotionPrimitive.SCALE_IN, target="head"),
        ],
    )
    cues = [
        _cue(0.4, 1.4, "isn't even the strangest"),  # shares frame: stays up
        _cue(3.0, 4.0, "Project Mars."),              # restates the kicker
        _cue(4.4, 5.4, "The full story gets deeper."),  # headline owns the screen
    ]
    surviving = suppress_cues_for_reveals(cues, [composition])
    assert [cue.text for cue in surviving] == ["isn't even the strangest"]


def test_year_numeral_hides_duplicate_captions() -> None:
    """A giant year numeral owns the year fact: beats that still say the year
    out loud (even partly) are dropped, so the opening does not show 1949
    three times."""
    composition = _archive_composition(reveal_at=None, duration=12.0)
    cues = [
        _cue(0.2, 1.0, "What if I told you that in"),
        _cue(1.0, 1.6, "1949,"),
        _cue(1.6, 3.0, "a famous rocket scientist wrote"),
        _cue(3.0, 4.2, "1949, a famous rocket scientist"),
    ]
    surviving = suppress_cues_for_reveals(cues, [composition])
    assert [cue.text for cue in surviving] == [
        "What if I told you that in", "a famous rocket scientist wrote",
    ]


def test_non_year_numeric_archive_title_does_not_hide_ten_men_caption() -> None:
    composition = EditorialComposition(
        id="rulers", start=0.0, duration=8.0,
        template=EditorialTemplate.ARCHIVE_CANVAS,
        elements=[EditorialElement(
            id="title", type=EditorialElementType.TEXT,
            text="10 RULERS", role="year",
        )],
        events=[EditorialEvent(
            time=0.0, action=MotionPrimitive.FADE_UP, target="title",
        )],
    )
    cue = _cue(2.0, 3.0, "10 men.", highlight=True)
    assert suppress_cues_for_reveals([cue], [composition]) == [cue]


def test_no_reveals_keeps_every_cue() -> None:
    cues = [_cue(0.0, 1.0, "Mars"), _cue(1.2, 2.0, "is governed")]
    assert suppress_cues_for_reveals(cues, [_archive_composition(reveal_at=None)]) == cues


# --- Per-clip slicing ---------------------------------------------------------


def test_slice_caption_cues_retimes_and_drops_slivers() -> None:
    cues = [
        _cue(0.0, 1.0, "before"),     # outside the window
        _cue(4.5, 7.5, "inside"),     # spans the window start
        _cue(11.97, 12.05, "sliver"), # visible for under 50ms: dropped
    ]
    sliced = slice_caption_cues(cues, 5.0, 12.0)
    assert [(cue.start, cue.end, cue.text) for cue in sliced] == [
        (0.0, 2.5, "inside"),
    ]


# --- Renderer caption layer ---------------------------------------------------


def _editorial_plan(style: EditorialCaptionStyle = EditorialCaptionStyle.EDITORIAL_PHRASE) -> EditPlan:
    return EditPlan(
        project_id="p", width=1080, height=1920, fps=24,
        captions_enabled=True, caption_style=style,
        compositions=[_archive_composition(reveal_at=None, duration=8.0)],
    )


def test_compile_edit_plan_html_adds_caption_layer_for_documentary_styles() -> None:
    cues = place_caption_cues(
        build_editorial_caption_cues(
            _words("Mars is governed by ten men."),
            EditorialCaptionStyle.EDITORIAL_PHRASE, emphasis_texts=["ten men"],
        ),
        _archive_composition(reveal_at=None, duration=8.0),
        1080, 1920,
    )
    html = compile_edit_plan_html(_editorial_plan(), captions=cues)
    assert "caption-style-editorialPhrase" in html
    assert "const CAPTIONS=" in html
    assert "const CAPTIONS=[]" not in html  # carries real beats
    assert "caption-layer" in html


def _body_tag(html: str) -> str:
    start = html.index("<body")
    return html[start : html.index(">", start)]


def test_compile_edit_plan_html_has_no_caption_layer_for_standard_or_disabled() -> None:
    html = compile_edit_plan_html(_editorial_plan(EditorialCaptionStyle.STANDARD))
    assert "const CAPTIONS=[]" in html  # runtime present but inert
    assert "caption-style" not in _body_tag(html)
    disabled = _editorial_plan()
    disabled.captions_enabled = False
    html_off = compile_edit_plan_html(disabled)
    assert "const CAPTIONS=[]" in html_off
    assert "caption-style" not in _body_tag(html_off)


# --- Pipeline separation: Editorial vs Classic ---------------------------------


class _SyntheticEditorialRenderer:
    def __init__(self, ffmpeg) -> None:
        self.ffmpeg = ffmpeg
        self.calls: list[tuple[EditPlan, Path, Path | None, list]] = []

    def render(
        self, plan: EditPlan, output: Path, *, preview_html=None,
        asset_root=None, captions=(),
    ) -> Path:
        self.calls.append((plan, output, asset_root, list(captions)))
        output.parent.mkdir(parents=True, exist_ok=True)
        run_media_process([
            str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"color=c=#111315:s={plan.width}x{plan.height}:r={plan.fps}:d={plan.duration}",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], timeout=60)
        return output


def _editorial_pipeline(tmp_path: Path) -> PipelineService:
    return PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )


def test_classic_mode_keeps_ass_captions_untouched(tmp_path: Path) -> None:
    pipeline = _editorial_pipeline(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Classic Mode", topic="unchanged captions", target_duration=2,
        resolution=(320, 180), fps=12,
    ))
    pipeline.run_project(project.id)
    root = pipeline.store.project_path(project)
    assert (root / "subtitles" / "captions.ass").is_file()
    timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["subtitles"], "Classic Mode must keep burning ASS subtitles"
    assert not (root / "editorial" / "captions.json").exists()
    assert timeline["metadata"].get("caption_style") is None


def test_editorial_caption_styles_are_separated_from_classic_captions(tmp_path: Path) -> None:
    pipeline = _editorial_pipeline(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Editorial Styles", topic="caption styles", target_duration=1,
        resolution=(320, 568), fps=12, video_mode=VideoMode.EDITORIAL,
    ))
    pipeline.ensure_plan(project.id)
    project = pipeline._project(project.id)
    pipeline._ensure_narration(project, force=False)
    pipeline._ensure_music(project, force=False)
    pipeline._ensure_subtitles(project, force=False)
    root = pipeline.store.project_path(project)
    from backend.tts.audio import wav_duration

    duration = wav_duration(root / "narration" / "master.wav")
    script = pipeline.store.load_plan(project.slug)
    synthetic = _SyntheticEditorialRenderer(require_ffmpeg(pipeline.renderer.binaries))
    pipeline._editorial_renderer = synthetic  # type: ignore[assignment]

    def make_plan(style: EditorialCaptionStyle) -> EditPlan:
        return EditPlan(
            project_id=project.id, width=320, height=568, fps=12,
            captions_enabled=True, caption_style=style,
            compositions=[EditorialComposition(
                id="master", start=0, duration=duration,
                template=EditorialTemplate.ARCHIVE_CANVAS,
                elements=[EditorialElement(
                    id="headline", type=EditorialElementType.TEXT,
                    text="EVIDENCE", role="year",
                )],
                events=[EditorialEvent(
                    time=0, action=MotionPrimitive.FADE_UP, target="headline",
                )],
                narration_refs=[script.scenes[0].id],
            )],
        )

    def render_current(style: EditorialCaptionStyle) -> None:
        pipeline.save_edit_plan(project.id, make_plan(style))
        job = pipeline.queue_render(project.id, force=True)
        pipeline.run_render(project.id, force=True, parent_job_id=job.id)

    # editorialPhrase: beats are baked into the master; no ASS track.
    render_current(EditorialCaptionStyle.EDITORIAL_PHRASE)
    timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["subtitles"] == []
    assert timeline["metadata"]["caption_style"] == "editorialPhrase"
    phrase_file = json.loads((root / "editorial" / "captions.json").read_text(encoding="utf-8"))
    assert phrase_file["style"] == "editorialPhrase"
    assert phrase_file["captions"]
    assert all(item["style"] == "editorialPhrase" for item in phrase_file["captions"])

    # standard: the shared ASS path carries the subtitles; master stays clean.
    pipeline.update_edit_plan_settings(project.id, caption_style="standard")
    render_current(EditorialCaptionStyle.STANDARD)
    timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["subtitles"], "standard style must keep the ASS caption track"
    assert timeline["metadata"]["caption_style"] == "standard"
    standard_file = json.loads((root / "editorial" / "captions.json").read_text(encoding="utf-8"))
    assert standard_file["style"] == "standard"
    assert standard_file["captions"] == []

    # oneWord: per-word beats baked into the master again.
    pipeline.update_edit_plan_settings(project.id, caption_style="oneWord")
    render_current(EditorialCaptionStyle.ONE_WORD)
    timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["subtitles"] == []
    one_word_file = json.loads((root / "editorial" / "captions.json").read_text(encoding="utf-8"))
    assert one_word_file["style"] == "oneWord"
    assert {item["style"] for item in one_word_file["captions"]} == {"oneWord"}

    # The synthetic renderer saw exactly the per-style caption payloads.
    assert [plan.caption_style for plan, *_ in pipeline._editorial_renderer.calls] == [
        EditorialCaptionStyle.EDITORIAL_PHRASE,
        EditorialCaptionStyle.STANDARD,
        EditorialCaptionStyle.ONE_WORD,
    ]
    phrase_calls = [captions for _plan, _clip, _root, captions in pipeline._editorial_renderer.calls]
    assert phrase_calls[0], "phrase render must receive caption cues"
    assert all(item["style"] == "editorialPhrase" for item in phrase_calls[0])
    assert phrase_calls[1] == []
    assert all(item["style"] == "oneWord" for item in phrase_calls[2])
