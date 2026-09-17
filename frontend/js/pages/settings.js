/**
 * Settings screen: things the user configures.
 *
 *  - Notification sound (per-browser preference, WebAudio synthesis).
 *  - Remote services — Gemini TTS API key (the same shared panel the
 *    Voice screen uses; see frontend/js/gemini-key.js).
 *  - API connection and configuration provenance.
 *
 * Model selection and Studio-owned model memory live on the Models screen;
 * machine and environment diagnostics live on the System status screen.
 */

import { el } from "../dom.js";
import { state } from "../state.js";
import { health, ttsModels } from "../api.js";
import { loadingState, badge, errorPanel } from "../ui.js";
import {
  getAlertPrefs, setAlertPrefs, SOUND_PRESETS, unlockAndPlayNotification,
} from "../alert-sound.js";
import { geminiKeyPanel } from "../gemini-key.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderSettings(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Settings")),
  );
  screen.append(
    el("div", { class: "stack" },
      notificationSoundPanel(),
      geminiTtsPanel(),
      connectionPanel(),
      provenancePanel(),
    ),
  );
  return screen;
}

/* --- notification sound ----------------------------------------------------- */

/**
 * Per-browser notification-sound preference: enable toggle, preset picker,
 * volume, and a Test sound button that plays the selected preset at its
 * alert volume. Updates apply immediately (persisted under lvs-alert-sound).
 * @returns {HTMLElement}
 */
function notificationSoundPanel() {
  const prefs = getAlertPrefs();

  const enable = el("input", { type: "checkbox", checked: prefs.enabled });
  const sound = el("select", { id: "lvs-sound-preset", class: "input" },
    ...Object.entries(SOUND_PRESETS).map(([id, preset]) =>
      el("option", { value: id, selected: id === prefs.sound }, preset.label)));
  const volumeValue = el("output", { class: "mono small" }, `${prefs.volume}`);
  const volume = el("input", {
    id: "lvs-alert-volume",
    type: "range", class: "input", min: "0", max: "100", step: "5",
    value: String(prefs.volume),
  });
  const syncVolume = () => {
    const v = Number(volume.value);
    volumeValue.textContent = String(v);
    setAlertPrefs({ volume: v });
  };
  volume.oninput = syncVolume;
  const test = el("button", { class: "btn btn-sm", type: "button" }, "Test sound");
  // The click is the user gesture: resume the shared context first, then
  // preview the selected preset at its alert (full) volume.
  test.onclick = () => { unlockAndPlayNotification("modal"); };

  const syncEnabled = () => {
    setAlertPrefs({ enabled: enable.checked });
    sound.disabled = !enable.checked;
    volume.disabled = !enable.checked;
  };
  enable.onchange = syncEnabled;
  sound.onchange = () => setAlertPrefs({ sound: sound.value });
  syncEnabled();

  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "Notification sound"),
    el("div", { class: "panel-body stack" },
      el("label", { class: "check-row" }, enable, "Play a sound for notifications"),
      el("div", { class: "field" },
        el("label", { for: "lvs-sound-preset" }, "Sound"),
        sound,
      ),
      el("div", { class: "field" },
        el("div", { class: "row" },
          el("label", { for: "lvs-alert-volume" }, "Volume"),
          el("span", { class: "spacer" }), volumeValue),
        volume,
        el("div", { class: "hint" },
          "Warnings and failures play at full volume; confirmations and status updates play a quiet variant of the same sound."),
      ),
      el("div", { class: "row" },
        test,
        el("span", { class: "small muted" },
          "Browsers keep a page muted until you first click or press a key — alerts before that first interaction are silent and are not replayed. "
          + "Test sound previews the selected preset right away.")),
    ),
  );
}

/* --- remote services: Gemini TTS -------------------------------------------- */

/**
 * The shared Gemini TTS API-key panel in its own region: a failed
 * tts-models request replaces only this panel, never the sound,
 * connection, or provenance panels.
 * @returns {HTMLElement}
 */
function geminiTtsPanel() {
  const region = el("div", { class: "panel-body" });

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Remote services — Gemini TTS"),
      el("span", { class: "spacer" }),
    ),
    region,
  );

  /** @param {any} health */
  function renderHealth(health) {
    const keyPanel = geminiKeyPanel({ health, onSaved: load });
    region.replaceChildren(
      el("div", { class: "stack" },
        keyPanel.status,
        keyPanel.control,
        el("p", { class: "small muted" },
          "The key is used by the Gemini TTS provider on the Voice screen. "
          + "It is stored in a user-private file on this machine and no endpoint ever returns it."),
      ),
    );
  }

  async function load() {
    region.replaceChildren(loadingState(2));
    try {
      const payload = await ttsModels(state.config);
      renderHealth(payload?.models?.gemini_tts?.health || null);
    } catch (err) {
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: load }, "Retry"),
      ));
    }
  }

  load();
  return panel;
}

/* --- API connection ---------------------------------------------------------- */

/** @returns {HTMLElement} */
function connectionPanel() {
  const region = el("div", { class: "panel-body" });
  const retryBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
  }, "Re-check");
  retryBtn.onclick = () => load();

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "API connection"),
      el("span", { class: "spacer" }), retryBtn,
    ),
    region,
  );

  async function load() {
    region.replaceChildren(loadingState(2));
    let connection = state.connection;
    let mode = state.healthMode;
    try {
      const response = await health(state.config);
      connection = "online";
      mode = response.mode;
      state.connection = connection;
      state.healthMode = mode;
    } catch {
      connection = "offline";
    }
    region.replaceChildren(
      el("dl", { class: "kv" },
        el("dt", {}, "API base"),
        el("dd", { class: "mono" }, state.config.apiBase || "(same origin)"),
        el("dt", {}, "Connection"),
        el("dd", {}, connection === "online" ? badge("good", "Connected") : badge("offline", "Backend offline")),
        el("dt", {}, "Pipeline mode"),
        el("dd", {},
          connection !== "online" ? el("span", { class: "muted" }, "—")
            : mode === "mock" ? badge("warning", "Mock pipeline") : badge("neutral", "Local pipeline")),
      ),
    );
  }

  load();
  return panel;
}

/* --- configuration provenance ------------------------------------------------ */

/** @returns {HTMLElement} */
function provenancePanel() {
  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "Where configuration comes from"),
    el("div", { class: "panel-body stack" },
      el("p", { class: "small" },
        "This page talks to the backend at ",
        el("code", { class: "mono" }, state.config.apiBase ? state.config.apiBase : "the same origin as this page"),
        ". The base is resolved at boot from (in order): an injected ",
        el("code", { class: "mono" }, "window.LVS_CONFIG"),
        " object, a static ",
        el("code", { class: "mono" }, "config.json"),
        " served next to index.html, or the same-origin default. No build step is involved."),
      el("p", { class: "small muted" },
        "Backend behavior — LLM endpoint, ports, and GPU thresholds — is set in the backend process. The LLM API key, when required, lives only in that environment: this UI never reads, displays, or transmits it."),
      el("p", { class: "small muted" },
        "Choose script models and manage Studio-owned model memory on the Models screen. Machine and environment diagnostics — core capabilities, compatibility, project recovery, runtime, and ports — live on the System status screen."),
    ),
  );
}
