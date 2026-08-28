"""Frontend tests for live (job-feed-driven) updates and the [hidden] fix.

These are source-text assertions (the repo is zero-build; the JS itself is
validated by frontend/tests/static_checks.py). They pin the behavioral
contracts that fix the "I always have to refresh" and "never alerts" bugs:

  - the CSS [hidden] rule that makes the attribute actually hide elements;
  - the global terminal-job toast notifier in app.js;
  - per-screen live-update hooks (and the form-state exclusions).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "frontend"


def _js(rel: str) -> str:
    return (ROOT / "js" / rel).read_text(encoding="utf-8")


def test_hidden_attribute_beats_author_display_rules() -> None:
    css = (ROOT / "css" / "components.css").read_text(encoding="utf-8")
    # Author rules like `.field { display: flex }` outrank the user-agent
    # [hidden] rule, so the fix must use !important.
    assert "[hidden] { display: none !important; }" in css


def test_app_toasts_once_per_job_terminal_transition() -> None:
    source = _js("app.js")
    assert 'const TERMINAL_JOB_STATUSES = new Set(["completed", "failed", "canceled"]);' in source
    assert "const lastJobStatus = new Map();" in source
    assert "function announceTerminalJob(job) {" in source
    assert "function notifyJobTransitions(jobs) {" in source
    # The notifier runs on every feed frame, before the top bar count and the
    # screen hook, so toasts fire regardless of which screen is showing.
    assert (
        "onJobs: (jobs) => { state.jobs = jobs; notifyJobTransitions(jobs);"
        in source
    )
    # Jobs already terminal on the first frame must not re-alert: the diff is
    # only taken when a previous status was recorded.
    assert "prev !== undefined && prev !== now" in source
    # Terminal states get the right severity.
    assert 'toast("good", `${label} completed`' in source
    assert 'toast("critical", `${label} failed`' in source
    assert 'toast("warning", `${label} canceled`' in source


def test_scene_editor_updates_live_without_touching_the_form() -> None:
    source = _js("pages/scene-editor.js")
    assert 'import { registerLiveUpdate } from "../app.js";' in source
    assert 'const TERMINAL_JOB_STATUSES = ["completed", "failed", "canceled"];' in source
    assert "function sceneSignature(sceneId, snap) {" in source
    assert "registerLiveUpdate(async () => {" in source
    # Unchanged snapshots are skipped (no churn, no re-fetch of assets).
    assert "if (sig === lastSig) return;" in source
    # A lock change rebuilds the whole form; live refreshes otherwise only
    # patch the status row, meta panel, and button states — never the PATCH
    # form fields (buildForm is not called from inside the hook).
    assert "renderSceneEditorRefresh(sceneId); return;" in source
    hook = source.split("registerLiveUpdate(", 1)[1].split("\n  });", 1)[0]
    assert "buildForm(" not in hook
    assert "buildStatusRow(" in hook
    assert 'region.querySelector("#se-meta")' in hook
    assert 'region.querySelector("#se-regen")' in hook
    assert 'region.querySelector("#se-inspect")' in hook


def test_screens_without_form_state_refresh_live() -> None:
    # These screens hold no long-lived form state, so a full region rebuild
    # per feed frame is safe (and the skeleton is skipped so content does not
    # flash).
    for rel in ("pages/storyboard.js", "pages/timeline.js", "pages/export.js"):
        source = _js(rel)
        assert 'import { registerLiveUpdate } from "../app.js";' in source
        assert "load(body, { skeleton: false })" in source
    # Captions has two regions (alignment model + captions); one shared
    # skeleton-free load refreshes both on every feed frame.
    captions = _js("pages/captions.js")
    assert 'import { registerLiveUpdate } from "../app.js";' in captions
    assert "registerLiveUpdate(() => load({ skeleton: false }));" in captions
    # Script refreshes only the narration region; the plan region is
    # populated exclusively by an explicit planning action.
    script = _js("pages/script.js")
    assert "registerLiveUpdate(() => loadScript(scriptRegion, { skeleton: false }));" in script
    # Dashboard combines both regions (project list + selected overview) into
    # one hook.
    dashboard = _js("pages/dashboard.js")
    assert "registerLiveUpdate(() => {" in dashboard
    assert "list.refresh();" in dashboard
    assert "overview ? overview.panel : null," in dashboard


def test_export_explains_and_tracks_render_only_workflow() -> None:
    source = _js("pages/export.js")
    assert 'j.stage === "render" || j.stage === "pipeline"' in source
    assert '"Render final video"' in source
    assert '"Re-render final video"' in source
    assert "It does not contact the LLM, run TTS, or generate graphics." in source
    assert "renderInputSummary(snap, stages)" in source
    assert "renderStageLabel(job.parameters && job.parameters.current_stage)" in source
    assert "Existing scripts, narration, scene graphics, music, and captions will not be regenerated." in source


def test_form_screens_do_not_full_rerender() -> None:
    # Voice and Music have long-lived editable settings forms; a full live
    # re-render would wipe in-progress edits.
    assert "registerLiveUpdate" not in _js("pages/voice.js")
    # Music registers a hook, but it must only sync the generate/regenerate
    # job region (syncJobState) — never reload the whole screen, which would
    # reset the settings form mid-edit.
    music = _js("pages/music.js")
    assert "registerLiveUpdate" in music
    assert "syncJobState(state.jobs)" in music
    assert "registerLiveUpdate(() => load(" not in music


def test_music_screen_selects_current_uncached_soundtrack_assets() -> None:
    source = _js("pages/music.js")
    assert "const music = soundtracks.length ? soundtracks[soundtracks.length - 1] : null;" in source
    assert "function currentMovements(assets, soundtracks) {" in source
    assert "movement_asset_ids" in source
    assert "current.plan_hash === settings.plan_hash" in source
    assert "function versionedAudioUrl(asset) {" in source
    assert "encodeURIComponent(version)" in source


def test_voice_screen_compares_and_selects_immutable_narration_takes() -> None:
    source = _js("pages/voice.js")
    api = _js("api.js")
    assert "listNarrationTakes" in source
    assert "activateNarrationTake" in source
    assert '"Narration takes"' in source
    assert '"Use this narration"' in source
    assert 'badge("good", "active", false)' in source
    assert "/tts/narrations" in api
    assert "/activate" in api
    assert "regenerateNarrationChunk" in source
    assert '"Regenerate chunk"' in source
    assert '"scene synced"' in source
    assert '"legacy timing"' in source
    assert "/chunks/" in api
    assert "/regenerate" in api


def test_saved_reference_voices_have_local_audio_players() -> None:
    source = _js("pages/voice.js")
    assert 'class: "voice-profile-row saved-voice"' in source
    assert 'controls: true, preload: "metadata", src: item.url' in source
    assert '"aria-label": `Play reference voice ${item.name}`' in source


def test_reference_recorder_requests_clean_mono_48khz_audio() -> None:
    source = _js("voice-recorder.js")
    assert "const REFERENCE_SAMPLE_RATE = 48000;" in source
    assert "channelCount: { ideal: 1 }" in source
    assert "sampleRate: { ideal: REFERENCE_SAMPLE_RATE }" in source
    assert "sampleSize: { ideal: 16 }" in source
    assert "echoCancellation: false" in source
    assert "noiseSuppression: false" in source
    assert "autoGainControl: false" in source
    assert "new Ctx({ sampleRate: REFERENCE_SAMPLE_RATE })" in source


def test_first_load_failure_surfaces_the_error_panel() -> None:
    """Loaders must track rendered content explicitly.

    The skeleton is a child of the region, so a DOM-child check would hide
    the error panel forever after a failed first load. Every loader keeps a
    hasContent flag instead: set on first successful render, and the error
    panel (with Retry) is shown only while !hasContent.
    """
    for rel in (
        "pages/storyboard.js",
        "pages/script.js",
        "pages/captions.js",
        "pages/dashboard.js",
        "pages/export.js",
    ):
        source = _js(rel)
        assert "let hasContent = false;" in source, rel
        assert "hasContent = true;" in source, rel
        assert "token !== inflight || hasContent" in source, rel


def test_polling_tick_rechecks_feed_phase_after_await() -> None:
    source = _js("events.js")
    tick = source.split("const tick = async () => {", 1)[1].split(
        "pollTimer = setTimeout(tick", 1)[0]
    await_index = tick.index("await listJobs(config)")
    recheck = tick.index('if (stopped || phase !== "polling") {', await_index)
    emit_index = tick.index("emitJobs(", recheck)
    assert recheck < emit_index
    assert 'if (stopped || phase !== "polling") {' in tick[:await_index]


def test_scene_editor_applies_conditional_visibility_on_initial_render() -> None:
    source = _js("pages/scene-editor.js")
    init = source.index("populatePredecessorOptions(allScenes);\n  initializeH3Fields();")
    block = source[init:init + 400]
    # The change handlers own conditional visibility; the initial render must
    # apply the same rules once so H3-only/graphic-only controls start hidden.
    assert "updateVisualTypeDetails();" in block
    assert 'if (visualType.value !== "h3_audiovisual") updateH3Details();' in block


def test_scene_editor_blank_seed_and_duration_keep_current_values() -> None:
    source = _js("pages/scene-editor.js")
    # Blank means keep the current value: the key is omitted (undefined is
    # dropped by JSON.stringify) instead of sending an explicit null that the
    # backend's exclude_none would silently ignore.
    assert 'seed: seed.value.trim() === "" ? undefined : Number(seed.value),' in source
    assert 'duration: duration.value.trim() === "" ? undefined : Number(duration.value),' in source
    assert "blank keeps the current seed." in source
    assert "blank keeps the current duration." in source


def test_scene_editor_wires_shot_generation_and_scene_render_actions() -> None:
    source = _js("pages/scene-editor.js")
    assert "generateShot," in source
    assert "regenerateShot," in source
    assert "renderScene," in source
    assert 'generateBtn.addEventListener("click", () => queueGeneration(false));' in source
    assert 'regenerateBtn.addEventListener("click", () => queueGeneration(true));' in source
    assert 'renderSceneBtn.addEventListener("click", async () => {' in source
    assert "Generate (pending)" not in source
    assert "Regenerate (pending)" not in source
    assert "Render scene (pending)" not in source
    assert "Save or revert this shot before generating" in source
    assert "Save or revert this shot before rendering" in source


def test_scene_editor_creates_camera_control_and_surfaces_render_failures() -> None:
    source = _js("pages/scene-editor.js")
    declaration = 'const cameraInstruction = el("select", {'
    assert declaration in source
    assert source.index(declaration) < source.index("cameraInstruction.append(")
    # A synchronous form-building failure must replace the skeleton instead
    # of escaping from the async loader as an unhandled rejection.
    render_guard = source.split("let form;", 1)[1].split(
        "region.replaceChildren(\n    header,", 1,
    )[0]
    assert "try {" in render_guard
    assert "form = buildForm(" in render_guard
    assert "region.replaceChildren(errorPanel(err," in render_guard
    # The controls must be inserted into the returned panel, not merely
    # constructed off-DOM (which produces an apparently empty editor).
    assert "...metaRows," in source
    assert 'el("div", { class: "row mt" }, ...actionButtons)' in source
    assert 'el("div", { class: "grid-2" }, fImageMotionSource)' in source


def test_job_monitor_rows_are_keyed_and_survive_feed_frames() -> None:
    """The ~1s feed frames must not tear down rows that did not change.

    A full-list rebuild replaces every Cancel/Retry button once per second,
    which swallows clicks between mousedown and mouseup and resets hover,
    focus, and text selection. Rows are keyed by job id and rebuilt only when
    their stamp changes; ordering re-appends (moves) the surviving nodes.
    """
    source = _js("pages/jobs.js")
    assert "const rows = new Map();" in source
    assert "function rowStamp(job, ctx) {" in source
    assert "entry.stamp !== stamp" in source
    assert "body.replaceChildren();" in source
    assert "body.append(entry.node);" in source
    # Elapsed time keeps ticking between feed frames without rebuilding rows.
    assert 'setInterval(' in source
    assert 'screen.querySelectorAll("[data-elapsed]")' in source
    assert "clearInterval(ticker);" in source


def test_job_monitor_surfaces_substage_queue_position_and_times() -> None:
    """Fields the job payload already carries must actually be displayed."""
    source = _js("pages/jobs.js")
    shared_ui = _js("ui.js")
    # Parent-job sub-stage (render/pipeline/visual_batch current_stage).
    assert '"Current stage: "' in source
    # Queue order mirrors the backend drain order (priority DESC, oldest first).
    assert "Queue position #" in source
    assert "(b.priority - a.priority)" in source
    # Every row shows timestamps plus elapsed/duration.
    assert "fmtDuration(" in source
    assert "created ${fmtDate(job.created_at)}" in source
    # Human-readable labels for the known stages, including the visual batch
    # and its per-scene children (raw ids were shown before).
    assert "stageLabel," in source
    assert 'scene_visual: "Scene visual",' in shared_ui
    assert 'visual_batch: "Visual batch",' in shared_ui
    assert 'quality_control: "Quality check",' in shared_ui
    # Retry stays hidden for jobs the backend marks non-executable (child
    # stage rows are driven by their parent pipeline).
    assert "job.executable !== false" in source
    # Cancel stays hidden for pipeline bookkeeping rows: canceling them
    # mid-operation is a tolerated no-op the UI must not offer.
    assert "job.cancelable !== false" in source


def test_job_monitor_filters_by_status_group_and_project() -> None:
    source = _js("pages/jobs.js")
    # Status-group chips plus a project filter, session-scoped.
    assert '"active", "Active"' in source
    assert '"queued", "Queued"' in source
    assert '"failed", "Failed"' in source
    assert '"finished", "Finished"' in source
    assert 'let statusFilter = "all";' in source
    assert '"aria-label": "Filter by project"' in source
    assert "state.projects.map" in source
    # The summary counts always describe the full (unfiltered) list.
    assert "function summaryText(all, shown) {" in source
    # Filters never mutate the shared job list (top bar counts stay exact).
    assert "const filtered = all" in source


def test_app_suppresses_toasts_for_parented_child_jobs() -> None:
    source = _js("app.js")
    shared_ui = _js("ui.js")
    # A visual batch produces one terminal toast (its own), not one per scene.
    assert (
        "if (!(job.parameters && job.parameters.parent_job_id)) announceTerminalJob(job);"
        in source
    )
    assert "STAGE_LABELS" in source
    assert 'scene_visual: "Scene visual",' in shared_ui
    assert 'visual_batch: "Visual batch",' in shared_ui
