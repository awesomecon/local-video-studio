"""Frontend source-contract tests for the dedicated Editorial workspace.

The Editorial screen (``frontend/js/pages/editorial.js``) promotes the
Editorial Mode workflow from a preview-only panel on Project Details to a
first-class screen. These assertions pin the behavioral contracts the UI
relies on (the JS itself is exercised by ``frontend/tests/run_js_tests.py``):

  - route/nav wiring for ``#/editorial`` in router.js and app.js;
  - the screen reuses the strict, shared validation and mutation builders
    from pages/project.js (no copied, drift-prone validators);
  - untrusted plan/preview content is rendered through text nodes only;
  - the Edit Plan is read only after an explicit load, and only for the
    mounted project's exact project-local URL, never for classic projects
    and never on live ticks;
  - the embedded preview frame is pointed only at the mounted project's
    exact ``/api/projects/{id}/editorial/preview`` path;
  - at most one Editorial mutation runs at a time per screen;
  - the Project Details panel keeps linking into the workspace.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _js(rel: str) -> str:
    return (FRONTEND / "js" / rel).read_text(encoding="utf-8")


def test_editorial_route_is_wired_end_to_end() -> None:
    router = _js("router.js")
    app = _js("app.js")
    # The route exists between captions and timeline (the Editorial canvas
    # renders before the timeline in the export workflow).
    assert r'{ re: /^#\/editorial$/, name: "editorial", param: null }' in router
    assert router.index('"captions"') < router.index('"editorial"') < router.index('"timeline"')
    # The screen is registered and reachable from the sidebar.
    assert "editorial: renderEditorial," in app
    assert 'import { renderEditorial } from "./pages/editorial.js";' in app
    assert '{ name: "editorial", hash: "#/editorial", label: "Editorial", icon: "canvas" }' in app
    nav = app.split("const NAV_PRIMARY = ", 1)[1].split("];", 1)[0]
    assert '{ name: "editorial", hash: "#/editorial"' in nav
    # The headless UI capture covers the new screen too.
    shots = (ROOT / "scripts" / "ui_shots.py").read_text(encoding="utf-8")
    assert '("editorial", "#/editorial")' in shots


def test_editorial_workspace_reuses_the_strict_shared_builders() -> None:
    source = _js("pages/editorial.js")
    imports = source.split('from "./project.js";', 1)[0]
    imported = imports[imports.rindex("import {"):]
    for helper in (
        "effectiveVideoMode",
        "editorialPlanState",
        "projectEditorialApiPath",
        "safeEditPlanDownloadUrl",
        "summarizeEditPlanCompositions",
        "parseCompositionEditor",
        "buildEditorialDisplayControls",
        "buildGeneratePlanButton",
        "buildCompositionControls",
        "buildRevisionControl",
        "usableUrl",
        "createEditorialController",
    ):
        assert helper in imported, f"{helper} must come from the shared builders"
    # No second, drift-prone copy of the motion/template allowlists.
    assert "MOTION_PRIMITIVES" not in source
    assert "EDITORIAL_TEMPLATES" not in source


def test_editorial_workspace_never_uses_raw_html_sinks() -> None:
    source = _js("pages/editorial.js")
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source


def test_editorial_workspace_reads_the_plan_only_when_it_can_be_trusted() -> None:
    source = _js("pages/editorial.js")
    # The plan read sits strictly behind the video-mode and plan-availability
    # guards and the exact project-local URL check.
    classic_guard = source.index('if (mode !== "editorial") {')
    plan_fetch = source.index("planPayload = await getEditPlan(state.config, planUrl);")
    url_check = source.index(
        'projectEditorialApiPath(editorial.edit_plan_url, projectId, "edit-plan")',
    )
    assert classic_guard < url_check < plan_fetch
    # A snapshot that reports a plan but not with the exact URL degrades to
    # an explained error state instead of guessing a URL.
    assert "is not a usable project-local path" in source
    # Live ticks re-read the snapshot only; the plan is never re-fetched.
    tick = source.split("async function tick()", 1)[1].split("\n  }", 1)[0]
    assert "getProject(state.config, state.currentProjectId)" in tick
    assert "getEditPlan(" not in tick


def test_editorial_workspace_preview_is_project_scoped_and_toggle_gated() -> None:
    source = _js("pages/editorial.js")
    validator = source.split(
        "export function safeEditorialPreviewUrl(", 1,
    )[1].split("\n}", 1)[0]
    assert (
        'expected = `/api/projects/${encodeURIComponent(projectId)}/editorial/preview`;'
        in validator
    )
    assert "return value === expected ? value : null;" in validator
    # The frame is created only behind the explicit toggle, never at render
    # time, and only for the validated URL.
    assert "if (ctrl.busy !== \"\" || !previewUrl) return;" in source
    assert "if (!previewUrl || !previewOn) return;" in source
    assert 'previewFrame.src = `${previewUrl}?ts=${Date.now()}`;' in source
    # A cross-project / malformed preview_url degrades to the unavailable
    # note rather than navigating the frame somewhere unknown.
    assert "Preview is unavailable for this project" in source


def test_editorial_workspace_serializes_mutations() -> None:
    source = _js("pages/editorial.js")
    run = source.split("runMutation: async (call, context) => {", 1)[1].split("\n      },", 1)[0]
    assert "if (ctrl.busy !== \"\") return null;" in run, "one mutation in flight at a time"
    assert "ctrl.busy = \"composition\";" in run
    # Success re-renders from the returned plan (no follow-up Edit Plan GET);
    # failure restores the last good plan with the standard surfaces.
    assert "paintWorkspace(updated);" in run
    assert "if (prior) paintWorkspace(prior);" in run
    assert "toastError(err, context);" in run
    # The in-flight guard covers the strip selection and the preview toggle
    # as well, so no second interaction can start while a mutation runs.
    assert 'if (ctrl.busy !== "") return; // one mutation in flight at a time' in source
    # Live ticks skip entirely while a mutation is in flight.
    assert "if (ctrl.busy !== \"\") return;" in source.split("async function tick()", 1)[1]


def test_editorial_workspace_uses_the_snapshot_plan_metadata() -> None:
    source = _js("pages/editorial.js")
    # Plan status (current / stale / untracked) is the shared presentation:
    # stale warns with the readable reasons, untracked stays neutral and
    # never reads as broken.
    assert "editorialPlanState(editorial)" in source
    assert 'badge("warning", "Edit Plan is stale")' in source
    assert 'badge("neutral", "Edit Plan available")' in source
    assert "Stale plans are preserved on purpose" in source
    assert "may predate provenance tracking" in source
    # The no-plan state offers the guarded generator (bodyless POST via the
    # shared button) only for a usable snapshot URL.
    assert "const generateUrl = usableUrl(editorial.generate_url);" in source
    assert "buildGeneratePlanButton(generateUrl, errors" in source
    assert "No Edit Plan yet" in source


def test_project_details_links_into_the_workspace() -> None:
    source = _js("pages/project.js")
    # No-plan state: a secondary action opens the workspace screen.
    assert 'onclick: () => navigate("#/editorial")' in source
    assert '"Open Editorial workspace"' in source
    # Has-plan state: a compact anchor after the primary actions, so
    # Open Preview remains the first (primary) link and the download link
    # stays ahead of the secondary workspace link.
    section = source.split("export function editorialPreviewSection", 1)[1].split(
        "\n}\n", 1,
    )[0]
    assert (
        0
        <= section.index('"Open Preview"')
        < section.index('"Download Edit Plan JSON"')
        < section.index('"Open workspace"')
    )
    assert 'href: "#/editorial"' in section


def test_motion_primitive_allowlist_matches_the_backend_contract() -> None:
    source = _js("pages/project.js")
    block = source.split("export const MOTION_PRIMITIVES = [", 1)[1].split("];", 1)[0]
    for primitive in (
        "fade", "fadeUp", "slideInLeft", "slideInRight", "scaleIn", "slowPush",
        "paperSlide", "underline", "highlight", "drawLine", "staggerIn",
        "dimOthers", "focusOne", "promoteNode", "collapseToBlack", "hardCut",
    ):
        assert f'"{primitive}"' in block
    # The doc comment no longer claims a shorter list.
    assert "The sixteen motion primitives" in source


def test_editorial_workspace_css_is_part_of_the_design_system() -> None:
    css = (FRONTEND / "css" / "components.css").read_text(encoding="utf-8")
    for selector in (
        ".ed-layout",
        ".ed-col",
        ".ed-strip",
        ".ed-card",
        ".ed-card.selected",
        ".tpl-bigTextReveal",
        ".ed-preview-well",
        ".ed-preview-frame",
        ".ed-detail-head",
        ".ed-workflow",
    ):
        assert selector in css, f"{selector} missing from components.css"
    # The workspace reflows to one column on narrow viewports.
    workspace_css = css.split("/* --- Editorial workspace", 1)[1]
    assert "@media (max-width: 1180px) {\n  .ed-layout { grid-template-columns: 1fr; }\n}" in workspace_css
