"""Frontend source-contract tests for the Editorial-mode Timeline screen.

For Editorial projects (``video_mode === "editorial"``) the Timeline screen
is a read-only composition and motion-event timeline built from the same
effective, narration-retimed plan that preview and export render:

  - the screen branches on the shared ``effectiveVideoMode`` helper;
  - the plan is read only from the snapshot's exact project-local
    ``timeline_plan_url`` (anything else degrades to an honest state and
    never becomes a request);
  - the response envelope (``{plan, timing_basis, narration_synced}``) is
    strictly validated before any pixel is drawn;
  - a timing badge above the viewport says whether the plan is aligned to
    the active narration (word timings or the recorded scene clock) or
    still on the authored plan's clock;
  - without a plan the screen points at the Editorial workspace instead of
    inventing a timeline.

The JS itself is exercised by ``frontend/tests/run_js_tests.py``.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _js(rel: str) -> str:
    return (FRONTEND / "js" / rel).read_text(encoding="utf-8")


TIMELINE = _js("pages/timeline.js")
API = _js("api.js")


def test_editorial_timeline_reads_only_the_snapshot_timeline_plan_url() -> None:
    source = TIMELINE
    api = API
    # The mode branch comes from the shared, dependency-neutral helper.
    assert 'import { effectiveVideoMode } from "../video-mode.js";' in source
    assert 'effectiveVideoMode(snap.project) === "editorial"' in source
    # The API helper exists and is imported by the page.
    assert "export function getEditorialTimelinePlan(config, timelinePlanUrl, opts = {})" in api
    assert 'import { getProject, getEditorialTimelinePlan } from "../api.js";' in source
    # The strict URL validator: only the exact project-local path for the
    # mounted project is ever fetched.
    validator = source.split("export function safeEditorialTimelinePlanUrl(", 1)[1].split(
        "\n}", 1,
    )[0]
    assert (
        "expected = `/api/projects/${encodeURIComponent(projectId)}/editorial/timeline-plan`;"
        in validator
    )
    assert "return value === expected ? value : null;" in validator
    # The snapshot URL is validated before any request, and an untrusted
    # value degrades to an explained state, not a fetch.
    url_check = source.index("safeEditorialTimelinePlanUrl(editorial.timeline_plan_url")
    fetch = source.index("await getEditorialTimelinePlan(state.config, url)")
    assert url_check < fetch
    assert "not a usable project-local path" in source
    # No plan yet: an empty state points at the workspace, no fetch.
    no_plan = source.index("editorial.has_edit_plan !== true")
    assert no_plan < url_check
    assert "No Edit Plan yet" in source


def test_editorial_timeline_validates_the_envelope_before_rendering() -> None:
    source = TIMELINE
    # The pure summarizer is exported for the JS test harness.
    assert "export function summarizeEditorialTimeline(envelope)" in source
    validator = source.split("export function summarizeEditorialTimeline(", 1)[1].split(
        "\n}\n", 1,
    )[0]
    # The envelope contract: plan + known timing basis + strict flag.
    assert "envelope.timing_basis" in validator
    assert "envelope.narration_synced" in validator
    assert "Array.isArray(plan.compositions)" in validator
    # Geometry: contiguous compositions, finite non-negative starts,
    # positive finite durations.
    assert "Math.abs(start - expectedStart) > TL_FRAME_TOL" in validator
    assert "duration <= 0" in validator
    # Events: malformed numeric fields are ignored, and the display width is
    # clamped at the composition end without mutating the source values.
    assert "Math.min(dur, Math.max(0, duration - time))" in validator
    # Rendering happens only after validation passes.
    render_call = source.index("buildEditorialTimeline(summary, snap, zoom)")
    summary_call = source.index("summarizeEditorialTimeline(envelope)")
    assert summary_call < render_call


def test_editorial_timeline_states_the_timing_basis_honestly() -> None:
    source = TIMELINE
    # The three bases, each with its own badge and an explanatory title.
    assert 'badge("good", "Narration aligned")' in source
    assert 'badge("neutral", "Recorded narration clock")' in source
    assert 'badge("warning", "Planned timing")' in source
    assert "function timingBasisBadge(basis)" in source
    # The badge sits above the viewport (rendered in the header row, not in
    # a lane), and the composition clips link into the Editorial workspace
    # instead of offering Classic scene controls.
    header = source.split("function buildEditorialTimeline(", 1)[1]
    assert "timingBasisBadge(summary.timingBasis)" in header
    assert 'navigate("#/editorial")' in header
    # Motion events render as escaped labels; unknown actions never become
    # class names (only the five renderer-owned template tints do).
    assert "function motionActionLabel(action)" in source
    for template in (
        "archiveCanvas",
        "documentReveal",
        "comparisonCanvas",
        "illustrationCanvas",
        "bigTextReveal",
    ):
        assert f"tpl-{template}" in source


def test_editorial_timeline_css_uses_the_design_system() -> None:
    css = (FRONTEND / "css" / "components.css").read_text(encoding="utf-8")
    for selector in (
        ".tl-comp",
        ".tl-comp.tpl-bigTextReveal",
        ".tl-clip-info",
        ".tl-event",
        ".tl-event-label",
    ):
        assert selector in css, f"{selector} missing from components.css"
    # Template tints reuse the shared --tpl-* tokens, not new color values.
    for token in (
        "var(--tpl-archive-canvas)",
        "var(--tpl-document-reveal)",
        "var(--tpl-comparison-canvas)",
        "var(--tpl-illustration-canvas)",
        "var(--tpl-big-text-reveal)",
    ):
        assert token in css


def test_app_shell_hides_storyboard_for_editorial_projects() -> None:
    app = _js("app.js")
    # The mode-aware sync helper is exported and driven by the shared mode.
    assert "export function syncModeAwareNavigation()" in app
    assert 'import { effectiveVideoMode } from "./video-mode.js";' in app
    sync = app.split("export function syncModeAwareNavigation()", 1)[1].split(
        "\n}", 1,
    )[0]
    assert "effectiveVideoMode(project) === \"editorial\"" in sync
    assert "storyboardNavEl.hidden" in sync
    # It runs on every route render (so the sidebar never shows the previous
    # project's mode) and after the project list reconciles.
    route = app.split("function renderRoute()", 1)[1].split("\n}", 1)[0]
    assert "syncModeAwareNavigation();" in route
    boot = app.split("async function bootProjects()", 1)[1].split("\n}", 1)[0]
    assert "syncModeAwareNavigation();" in boot
    # The storyboard button is kept by reference for the sync to toggle.
    assert "storyboardNavEl = primary.find(" in app