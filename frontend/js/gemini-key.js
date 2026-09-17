/**
 * Shared Gemini TTS API-key panel.
 *
 * The key controls (readiness state machine, password input, Save/Replace
 * key, Remove saved key) appear on two screens — the Gemini section of the
 * Voice screen and the Remote services panel on Settings. This module is
 * the single implementation of that state machine so the two hosts cannot
 * drift apart; it is dependency-neutral (dom + state + api + ui) like
 * video-mode.js.
 *
 * Security model (unchanged from the original Voice panel):
 *  - PUT /api/tts/gemini/key stores the key in a user-private local secret
 *    file; DELETE /api/tts/gemini/key removes it; no endpoint ever returns
 *    the key value.
 *  - Status comes from GET /api/tts/models -> models.gemini_tts.health
 *    (status, configured, source: environment|file, api_key_env).
 */

import { el } from "./dom.js";
import { state } from "./state.js";
import { saveGeminiKey, clearGeminiKey } from "./api.js";
import { badge, field as sharedField, toast, toastError, confirm } from "./ui.js";

/**
 * Build the Gemini TTS key panel for one health snapshot.
 *
 * @param {object} opts
 * @param {{status?: string, configured?: boolean, invalid?: boolean, source?: string, api_key_env?: string} | null} opts.health
 *   `models.gemini_tts.health` from GET /api/tts/models (null when the
 *   backend did not register the provider).
 * @param {() => void | Promise<void>} opts.onSaved - host hook run after a
 *   successful save or remove so the host re-fetches ttsModels() and
 *   re-renders the state.
 * @returns {{status: HTMLElement, control: HTMLElement, update: () => void}}
 *   status: live readiness badge + remediation text cell;
 *   control: the API-key field (password input + Save/Replace + Remove);
 *   update: re-apply the state machine (e.g. when the section is shown).
 */
export function geminiKeyPanel({ health, onSaved }) {
  const status = el("div", {
    class: "gemini-key-status", role: "status", "aria-live": "polite",
  }, badge("neutral", "Checking Gemini readiness"));
  const keyInput = el("input", { type: "password", class: "input",
    autocomplete: "off", spellcheck: "false",
    placeholder: "Paste a Google AI Studio API key — stored only on this machine" });
  const saveKey = el("button", { class: "btn", type: "button" }, "Save key");
  const clearKey = el("button", { class: "btn btn-ghost", type: "button" }, "Remove saved key");

  const update = () => {
    if (!health) {
      status.replaceChildren(
        badge("critical", "Gemini unavailable"),
        el("span", {}, "This backend did not register the Gemini TTS provider."));
      saveKey.disabled = true;
      clearKey.disabled = true;
      return;
    }
    if (health.status === "not_configured") {
      status.replaceChildren(
        badge("offline", "Gemini disabled"),
        el("span", {}, "Enable backends.gemini_tts.enabled and restart the dashboard."));
      saveKey.disabled = true;
      clearKey.disabled = true;
      return;
    }
    if (health.status === "key_invalid") {
      const where = health.source === "environment"
        ? `Fix ${health.api_key_env} and restart the dashboard.`
        : "Remove or replace the saved key below.";
      status.replaceChildren(
        badge("critical", "API key is malformed"), el("span", {}, where));
      saveKey.textContent = health.source === "file" ? "Replace saved key" : "Save key";
      saveKey.disabled = health.source === "environment";
      clearKey.disabled = health.source !== "file";
      return;
    }
    if (health.status === "healthy" && health.configured === true) {
      const via = health.source === "environment"
        ? `the ${health.api_key_env} environment variable`
        : "a key saved on this machine";
      status.replaceChildren(
        badge("good", "Ready to generate"),
        el("span", {}, `Gemini TTS will use ${via}. Narration text is sent only when you generate.`));
      saveKey.textContent = health.source === "file" ? "Replace saved key" : "Save key";
      saveKey.disabled = health.source === "environment";
      clearKey.disabled = health.source !== "file";
      return;
    }
    status.replaceChildren(
      badge("warning", "API key needed"),
      el("span", {}, `Paste a key below, or export ${health.api_key_env || "GEMINI_API_KEY"} and restart the dashboard.`));
    saveKey.textContent = "Save key";
    saveKey.disabled = false;
    clearKey.disabled = true;
  };

  saveKey.onclick = async () => {
    const key = keyInput.value.trim();
    if (!key) {
      toast("critical", "API key required",
        "Paste your Google AI Studio key first, then save it.");
      return;
    }
    saveKey.disabled = true;
    try {
      await saveGeminiKey(state.config, key);
      keyInput.value = "";
      toast("good", "Gemini API key saved",
        "It is stored in a private file on this machine; the environment variable still wins.");
      await onSaved();
    } catch (err) { toastError(err, "save Gemini API key"); }
    finally { saveKey.disabled = false; }
  };

  clearKey.onclick = async () => {
    const ok = await confirm({
      title: "Remove saved Gemini key",
      message: "Delete the Google AI Studio key saved on this machine? An environment variable, if set, is not affected.",
      confirmLabel: "Remove",
    });
    if (!ok) return;
    clearKey.disabled = true;
    try {
      await clearGeminiKey(state.config);
      toast("good", "Saved Gemini key removed");
      await onSaved();
    } catch (err) { toastError(err, "remove Gemini API key"); }
    finally { clearKey.disabled = false; }
  };

  update();
  const control = sharedField({
    label: "API key",
    input: el("div", { class: "row" }, keyInput, saveKey, clearKey),
    hint: "Keys are stored only on this machine. Saving refreshes the readiness status above.",
  });
  return { status, control, update };
}
