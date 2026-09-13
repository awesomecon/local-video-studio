/**
 * Score Studio timeline: five lanes (scene boundaries, narration waveform,
 * music waveform, SFX clips, music automation) under one ruler, with a
 * shared playhead and a single transport (play/pause, stop, seek, spacebar).
 *
 * Cues are draggable in the automation lane; dragging updates the cue time
 * live (snapping to scene/caption points when enabled) and commits through
 * `onCueCommit` on pointer-up only — the page then marks the plan dirty and
 * persists it on Save, never per drag frame. Numeric editing of the same
 * times lives in the cue inspector (the accessible alternative).
 *
 * Waveform lanes decode the served audio in the browser (waveform.js); when
 * no soundtrack exists yet the lanes stay empty and the transport runs a
 * silent clock so cues can still be placed.
 */

import { el, fmtDuration, clamp } from "../dom.js";
import { icon } from "../ui.js";
import { drawWaveform, computeWaveformPeaks } from "./waveform.js";

/**
 * @typedef {object} ScoreTimelineOptions
 * @property {(cueId: string, timeSeconds: number) => void} onCueCommit
 * @property {(cueId: string) => void} onSelect
 * @property {(timeSeconds: number) => void} [onAddAtTime]
 * @property {() => number} [getDuration] - live duration (narration-driven)
 * @property {() => ({
 *   scenes: {start_seconds: number, end_seconds: number, title: string}[],
 *   cues: {id: string, time_seconds: number, action: string, label?: string, locked?: boolean, duration_seconds?: number | null}[],
 *   snapPoints: number[],
 *   narration_url: string | null,
 *   music_url: string | null,
 * })} getData
 * @property {(url: string | null) => void} [setSnappingUi]
 */

/**
 * @param {HTMLElement} container
 * @param {ScoreTimelineOptions} options
 */
export function createScoreTimeline(container, options) {
  let destroyed = false;
  let duration = Math.max(1, options.getDuration ? options.getDuration() : 10);
  let pxPerSecond = 20;
  let playheadTime = 0;
  let playing = false;
  let silentClock = 0; // seconds into the silent (no-audio) clock
  let snapping = true;
  let selectedCueId = null;
  let audioUrl = null;

  // ---------------------------------------------------------------- layout
  const ruler = el("div", { class: "sc-ruler", "aria-hidden": "true" });
  const laneNarration = el("div", { class: "sc-lane sc-lane-wave" });
  const laneMusic = el("div", { class: "sc-lane sc-lane-wave" });
  const laneSfx = el("div", { class: "sc-lane sc-lane-clips" });
  const laneAuto = el("div", { class: "sc-lane sc-lane-auto" });
  const laneScenes = el("div", { class: "sc-lane sc-lane-scenes" });

  // The playhead wrapper is a grid cell spanning the lane column; the line
  // inside it is positioned in lane-local pixels, so ruler, lanes and head
  // can never drift apart. Its row span reserves column 2 for every lane
  // row, so every other cell gets an explicit grid position.
  const playheadWrap = el("div", { class: "sc-playhead-wrap", "aria-hidden": "true" });
  const playhead = el("div", { class: "sc-playhead" });
  playheadWrap.append(playhead);

  const grid = el("div", { class: "sc-grid" },
    el("div", { class: "sc-corner", style: { gridRow: "1", gridColumn: "1" } }, "0:00"),
    el("div", { class: "sc-ruler-cell", style: { gridRow: "1", gridColumn: "2" } }, ruler),
    laneLabel("Scenes", 2), laneScenes,
    laneLabel("Narration", 3), laneNarration,
    laneLabel("Music", 4), laneMusic,
    laneLabel("SFX", 5), laneSfx,
    laneLabel("Automation", 6), laneAuto,
    playheadWrap,
  );
  // Explicit lane positions (column 2 is reserved by the playhead span).
  laneScenes.style.gridRow = "2"; laneScenes.style.gridColumn = "2";
  laneNarration.style.gridRow = "3"; laneNarration.style.gridColumn = "2";
  laneMusic.style.gridRow = "4"; laneMusic.style.gridColumn = "2";
  laneSfx.style.gridRow = "5"; laneSfx.style.gridColumn = "2";
  laneAuto.style.gridRow = "6"; laneAuto.style.gridColumn = "2";

  function laneLabel(text, row) {
    return el("div", { class: "sc-label", style: { gridRow: String(row), gridColumn: "1" } }, text);
  }
  const viewport = el("div", { class: "sc-viewport", tabindex: "-1" }, grid);
  const wrap = el("div", { class: "score-timeline" }, viewport);

  container.append(wrap);

  /** Lane-local x for a client point (accounts for the label column and scroll). */
  function laneOriginX() {
    return laneScenes.getBoundingClientRect().left;
  }

  // ------------------------------------------------------------- transport
  const audio = new Audio();
  audio.preload = "auto";
  const playBtn = el("button", {
    class: "btn btn-sm sc-transport-btn", type: "button",
    "aria-label": "Play", title: "Play / pause (space)",
  }, icon("play", 15));
  const stopBtn = el("button", {
    class: "btn btn-sm", type: "button", "aria-label": "Stop", title: "Stop and return to 0:00",
  }, icon("stop", 15));
  const seek = el("input", {
    type: "range", class: "sc-seek", min: "0", step: "0.01", value: "0",
    "aria-label": "Seek",
  });
  const timeReadout = el("span", { class: "sc-time mono", "aria-live": "off" }, "0:00.0 / 0:00");
  const snapToggle = el("label", { class: "sc-snap" },
    el("input", { type: "checkbox", checked: true, "aria-label": "Snap to scene and caption boundaries" }),
    "Snap",
  );
  const zoom = el("input", { type: "range", min: "5", max: "160", step: "1", value: String(pxPerSecond), "aria-label": "Zoom (pixels per second)" });
  const fit = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Fit");
  const transport = el("div", { class: "sc-transport" },
    playBtn, stopBtn, seek, timeReadout,
    el("span", { class: "spacer" }),
    snapToggle, zoom, fit,
  );
  container.append(transport);

  function timeAt(xPx) {
    return clamp(xPx / pxPerSecond, 0, duration);
  }

  function xAt(t) {
    return t * pxPerSecond;
  }

  // ---------------------------------------------------------------- render
  let waveformTasks = 0;

  async function paintWave(lane, url, colorToken) {
    if (!url) {
      const existing = lane.querySelector("canvas");
      if (existing) existing.remove();
      lane.append(el("div", { class: "sc-empty-note" }, "no audio yet"));
      return;
    }
    let canvas = lane.querySelector("canvas");
    if (!canvas) {
      canvas = el("canvas", { class: "sc-wave" });
      lane.replaceChildren(canvas);
    }
    const buckets = Math.max(40, Math.ceil(lane.clientWidth / 2));
    try {
      const peaks = await computeWaveformPeaks(url, buckets, undefined);
      if (destroyed) return;
      drawWaveform(canvas, peaks, playheadTime / Math.max(duration, 0.001), { color: colorToken });
    } catch {
      if (!destroyed) lane.append(el("div", { class: "sc-empty-note" }, "waveform unavailable"));
    }
  }

  function paintTicks() {
    const width = Math.max(duration * pxPerSecond, 1);
    const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120];
    let step = steps[steps.length - 1];
    for (const candidate of steps) {
      if (width / candidate <= 18) { step = candidate; break; }
      step = candidate;
    }
    ruler.replaceChildren();
    for (let t = 0; t <= duration + 1e-6; t += step) {
      const tick = el("div", {
        class: "sc-tick", style: { left: `${xAt(Math.min(t, duration))}px` },
      }, fmtTick(t, step));
      ruler.append(tick);
    }
  }

  function fmtTick(t, step) {
    if (step < 1) return `${t.toFixed(1)}s`;
    return fmtDuration(t);
  }

  function paintScenes() {
    const data = options.getData();
    laneScenes.replaceChildren();
    for (const scene of data.scenes) {
      const x1 = xAt(scene.start_seconds);
      const x2 = xAt(scene.end_seconds);
      const block = el("div", {
        class: "sc-scene-block",
        style: { left: `${x1}px`, width: `${Math.max(2, x2 - x1)}px` },
        title: scene.title,
      }, el("span", { class: "sc-scene-title" }, scene.title || "scene"));
      laneScenes.append(block);
    }
  }

  function paintSfx() {
    const data = options.getData();
    laneSfx.replaceChildren();
    for (const cue of data.cues) {
      if (!["impact", "riser", "end_sting"].includes(cue.action)) continue;
      const x1 = xAt(cue.time_seconds);
      const width = Math.max(6, (cue.duration_seconds || 0.25) * pxPerSecond);
      laneSfx.append(el("div", {
        class: "sc-sfx-clip",
        style: { left: `${x1}px`, width: `${width}px` },
        title: `${cue.action} — ${cue.label || cue.action}`,
      }, el("span", {}, cue.action)));
    }
  }

  const CUE_INITIALS = { build: "B", pull_back: "P", silence: "S", restore: "R", impact: "I", riser: "R", end_sting: "E" };

  function paintAutomation() {
    const data = options.getData();
    laneAuto.replaceChildren();
    const envelope = el("svg", { class: "sc-envelope", "aria-hidden": "true" });
    laneAuto.append(envelope);
    paintEnvelope(envelope, data.cues);
    // Cue diamonds (all cues) — draggable handles with a vertical line,
    // an action initial, and the cue time.
    for (const cue of data.cues) {
      const handle = el("div", {
        class: [
          "sc-cue",
          `sc-cue-${cue.action.replace(/[^a-z_]/g, "")}`,
          cue.locked ? "locked" : "",
          cue.id === selectedCueId ? "selected" : "",
        ].join(" "),
        style: { left: `${xAt(cue.time_seconds)}px` },
        role: "slider",
        tabindex: "0",
        "aria-label": `Cue ${cue.action}${cue.label ? ` (${cue.label})` : ""} at ${cue.time_seconds.toFixed(3)}s`,
        "aria-valuenow": String(cue.time_seconds),
        "aria-valuemin": "0",
        "aria-valuemax": String(duration),
        "aria-orientation": "horizontal",
      },
        el("div", { class: "sc-cue-line" }),
        el("div", { class: "sc-cue-head" }, CUE_INITIALS[cue.action] || "•"),
        el("div", { class: "sc-cue-time" }, `${cue.time_seconds.toFixed(1)}s`),
      );
      attachCueInteractions(handle, cue);
      laneAuto.append(handle);
    }
  }

  /** dB → lane y (0 dB near the top, −60 dB at the bottom). */
  function envY(db) {
    const clamped = Math.min(12, Math.max(-60, db));
    return Math.min(60, Math.max(0, (1 - (clamped + 60) / 72) * 60));
  }

  /** Piecewise-linear gain envelope through the automation cues. */
  function paintEnvelope(svg, cues) {
    const w = Math.max(1, Math.ceil(duration * pxPerSecond));
    svg.setAttribute("viewBox", `0 0 ${w} 60`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    svg.replaceChildren();
    const GAIN_TARGET = { silence: -60, restore: 0 };
    const automations = cues
      .filter((c) => c.action in GAIN_TARGET || c.action === "build" || c.action === "pull_back")
      .sort((a, b) => a.time_seconds - b.time_seconds);
    const pts = [[0, envY(0)]];
    let db = 0;
    for (const cue of automations) {
      // Backend (compile_music_envelope): build/pull_back are relative moves
      // from the running level; silence/restore are absolute states.
      const target = (cue.action === "build" || cue.action === "pull_back")
        ? Math.min(12, db + (cue.gain_db != null ? cue.gain_db : (cue.action === "build" ? 3 : -6)))
        : (cue.gain_db != null ? cue.gain_db : GAIN_TARGET[cue.action]);
      const t0 = Math.min(cue.time_seconds, duration);
      const tr = Math.min(Math.max(0.05, cue.transition_seconds != null ? cue.transition_seconds : 0.25), duration - t0);
      if (t0 > 0) pts.push([xAt(t0), envY(db)]);
      db = target;
      pts.push([xAt(t0 + Math.max(tr, 0)), envY(db)]);
    }
    pts.push([xAt(duration), envY(db)]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const area = `${line} L${xAt(duration).toFixed(1)},60 L0,60 Z`;
    const fill = document.createElementNS("http://www.w3.org/2000/svg", "path");
    fill.setAttribute("d", area);
    fill.setAttribute("fill", "var(--accent-wash, rgba(57, 135, 229, 0.16))");
    const stroke = document.createElementNS("http://www.w3.org/2000/svg", "path");
    stroke.setAttribute("d", line);
    stroke.setAttribute("fill", "none");
    stroke.setAttribute("stroke", "var(--accent, #3987e5)");
    stroke.setAttribute("stroke-width", "1.5");
    stroke.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(fill, stroke);
  }

  function paint() {
    if (destroyed) return;
    duration = Math.max(0.001, options.getDuration ? options.getDuration() : duration);
    const width = Math.ceil(duration * pxPerSecond);
    grid.style.gridTemplateColumns = `var(--tl-label-w) ${width}px`;
    ruler.style.width = `${width}px`;
    for (const lane of [laneScenes, laneNarration, laneMusic, laneSfx, laneAuto]) {
      lane.style.width = `${width}px`;
    }
    playheadWrap.style.width = `${width}px`;
    playhead.style.left = `${xAt(playheadTime)}px`;
    seek.max = String(duration);
    paintTicks();
    paintScenes();
    paintSfx();
    paintAutomation();
    updateReadout();
  }

  function updateReadout() {
    timeReadout.textContent = `${fmtTime(playheadTime)} / ${fmtTime(duration)}`;
    if (document.activeElement !== seek) seek.value = String(playheadTime);
  }

  function fmtTime(t) {
    const m = Math.floor(t / 60);
    const s = Math.floor(t % 60);
    const tenths = Math.floor((t % 1) * 10);
    return `${m}:${String(s).padStart(2, "0")}.${tenths}`;
  }

  // ---------------------------------------------------------- cue handles
  function attachCueInteractions(handle, cue) {
    let dragOffset = 0;
    let dragging = false;

    handle.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      dragging = true;
      handle.setPointerCapture(ev.pointerId);
      dragOffset = cue.time_seconds - timeAt(ev.clientX - laneOriginX());
      options.onSelect(cue.id);
    });
    handle.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      let t = clamp(timeAt(ev.clientX - laneOriginX()) + dragOffset, 0, duration);
      const data = options.getData();
      if (snapping) {
        const points = data.snapPoints;
        let best = t; let bestDist = 0.35;
        for (const p of points) {
          const d = Math.abs(p - t);
          if (d < bestDist) { bestDist = d; best = p; }
        }
        t = Math.round(best * 1000) / 1000;
      }
      handle.style.left = `${xAt(t)}px`;
      handle.setAttribute("aria-valuenow", String(t));
      handle._liveTime = t;
    });
    handle.addEventListener("pointerup", (ev) => {
      if (!dragging) return;
      dragging = false;
      try { handle.releasePointerCapture(ev.pointerId); } catch { /* no capture */ }
      if (handle._liveTime != null) {
        options.onCueCommit(cue.id, handle._liveTime);
        handle._liveTime = null;
      }
      paint();
    });
    handle.addEventListener("click", (ev) => {
      ev.stopPropagation();
      options.onSelect(cue.id);
    });
    handle.addEventListener("keydown", (ev) => {
      const fine = 0.05;
      const coarse = 0.5;
      let step = null;
      if (ev.key === "ArrowLeft") step = ev.shiftKey ? -coarse : -fine;
      else if (ev.key === "ArrowRight") step = ev.shiftKey ? coarse : fine;
      if (step == null) return;
      ev.preventDefault();
      let t = clamp(cue.time_seconds + step, 0, duration);
      const data = options.getData();
      if (snapping) {
        let best = t; let bestDist = 0.35;
        for (const p of data.snapPoints) {
          const d = Math.abs(p - t);
          if (d < bestDist) { bestDist = d; best = p; }
        }
        t = Math.round(best * 1000) / 1000;
      }
      options.onCueCommit(cue.id, Math.round(t * 1000) / 1000);
      paint();
    });
  }

  // ------------------------------------------------------------- transport
  playBtn.onclick = () => (playing ? pause() : play());
  stopBtn.onclick = () => {
    pause();
    seekTo(0);
  };
  seek.addEventListener("input", () => seekTo(parseFloat(seek.value) || 0));
  snapToggle.querySelector("input").addEventListener("change", (ev) => {
    snapping = /** @type {HTMLInputElement} */ (ev.target).checked;
  });
  zoom.addEventListener("input", () => {
    pxPerSecond = parseInt(zoom.value, 10) || 20;
    paint();
  });
  fit.onclick = () => {
    const width = Math.max(viewport.clientWidth - 100, 100);
    pxPerSecond = clamp(width / duration, 5, 160);
    zoom.value = String(Math.round(pxPerSecond));
    paint();
  };

  ruler.addEventListener("pointerdown", (ev) => {
    seekTo(timeAt(ev.clientX - laneOriginX()));
  });

  let spaceHandler = null;
  function bindSpace() {
    spaceHandler = (ev) => {
      if (ev.key !== " " || ev.repeat) return;
      const target = ev.target;
      if (target instanceof HTMLElement && target.closest("input, select, textarea, [contenteditable]")) return;
      ev.preventDefault();
      playing ? pause() : play();
    };
    window.addEventListener("keydown", spaceHandler);
  }

  function setPlayIcon() {
    playBtn.replaceChildren(playing ? icon("stop", 15) : icon("play", 15));
    playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
  }

  function play() {
    if (audioUrl && !audio.src) {
      audio.src = audioUrl;
    }
    if (audioUrl) {
      audio.play().catch(() => { /* autoplay policy: user gesture usually covers this */ });
    } else {
      silentClock = playheadTime;
      requestAnimationFrame(tickSilent);
    }
    playing = true;
    setPlayIcon();
    if (!spaceHandler) bindSpace();
  }

  function pause() {
    audio.pause();
    playing = false;
    setPlayIcon();
  }

  function tickSilent(ts) {
    if (!playing || destroyed) return;
    if (!tickSilent.last) tickSilent.last = ts;
    const dt = (ts - (tickSilent.last || ts)) / 1000;
    tickSilent.last = ts;
    playheadTime += dt;
    if (playheadTime >= duration) {
      playheadTime = duration;
      pause();
    }
    updatePlayhead();
    if (playing) requestAnimationFrame(tickSilent);
  }
  /** @type {any} */ (tickSilent).last = 0;

  audio.addEventListener("timeupdate", () => {
    if (!playing) return;
    playheadTime = audio.currentTime;
    updatePlayhead();
  });
  audio.addEventListener("ended", () => {
    playing = false;
    setPlayIcon();
  });

  function updatePlayhead() {
    playhead.style.left = `${xAt(playheadTime)}px`;
    updateReadout();
    // Redraw waveforms with the moving played fraction (throttled by rAF).
    if (waveformTasks === 0) {
      waveformTasks = 1;
      requestAnimationFrame(() => {
        waveformTasks = 0;
        const data = options.getData();
        paintWaveCached(laneNarration, data.narration_url, "--accent");
        paintWaveCached(laneMusic, data.music_url, "--text-2");
      });
    }
  }

  /** Waveforms repaint only when their source URL changes; otherwise rAF just re-draws. */
  const waveState = { narration: null, music: null };
  async function paintWaveCached(lane, url, colorToken) {
    if (destroyed) return;
    const key = url || "none";
    if (waveState[lane === laneNarration ? "narration" : "music"] === key) {
      const canvas = lane.querySelector("canvas");
      if (canvas && url) {
        const buckets = Math.max(40, Math.ceil(lane.clientWidth / 2));
        try {
          const peaks = await computeWaveformPeaks(url, buckets);
          if (destroyed) return;
          drawWaveform(canvas, peaks, playheadTime / Math.max(duration, 0.001), { color: colorToken });
        } catch { /* keep the previous paint */ }
      }
      return;
    }
    waveState[lane === laneNarration ? "narration" : "music"] = key;
    await paintWave(lane, url, colorToken);
  }

  function seekTo(t) {
    playheadTime = clamp(t, 0, duration);
    if (audioUrl && audio.src) {
      audio.currentTime = playheadTime;
    } else {
      silentClock = playheadTime;
    }
    updatePlayhead();
    if (destroyed) return;
    const data = options.getData();
    paintWaveCached(laneNarration, data.narration_url, "--accent");
    paintWaveCached(laneMusic, data.music_url, "--text-2");
  }

  // ---------------------------------------------------------------- public
  function destroy() {
    destroyed = true;
    pause();
    audio.src = "";
    if (spaceHandler) window.removeEventListener("keydown", spaceHandler);
    wrap.remove();
    transport.remove();
  }

  // Initial layout
  const rect = container.getBoundingClientRect();
  if (rect.width > 0) {
    pxPerSecond = clamp(rect.width / duration, 5, 160);
    zoom.value = String(Math.round(pxPerSecond));
  }
  paint();
  const data = options.getData();
  paintWave(laneNarration, data.narration_url || null, "--accent");
  paintWave(laneMusic, data.music_url || null, "--text-2");

  return {
    destroy,
    seekTo,
    getTime: () => playheadTime,
    isPlaying: () => playing,
    setAudioSource(url) {
      audioUrl = url;
      audio.src = url || "";
    },
    setDuration(seconds) {
      duration = Math.max(0.001, seconds);
      paint();
    },
    selectCue(id) {
      selectedCueId = id;
      paintAutomation();
    },
    /** Re-paint lanes after plan/snapshot changes. */
    refresh() {
      paint();
      const d = options.getData();
      paintWaveCached(laneNarration, d.narration_url, "--accent");
      paintWaveCached(laneMusic, d.music_url, "--text-2");
    },
  };
}