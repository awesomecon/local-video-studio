/**
 * Runtime API configuration. Zero-build, local-first.
 *
 * Resolution order (first source that exists wins):
 *   1. `window.LVS_CONFIG` — an object the host page/server may inject, e.g.
 *      `{ "api_base": "http://127.0.0.1:8009" }`.
 *   2. `config.json` served next to index.html (a plain static file; a 404 is
 *      ignored). Shape: `{ "api_base": "", "media_base": null }`.
 *   3. Defaults: same-origin API (`api_base: ""`), no media base.
 *
 * Security: this module never reads, stores, or logs credentials. API keys
 * live in the backend process environment and must not appear anywhere in the
 * frontend.
 */

/** @typedef {{ apiBase: string, mediaBase: string | null }} LvsConfig */

const DEFAULTS = /** @type {LvsConfig} */ ({ apiBase: "", mediaBase: null });

/**
 * @param {any} raw
 * @param {string} field
 * @returns {string}
 */
function readBase(raw, field) {
  const value = raw && typeof raw === "object" ? raw[field] : null;
  if (typeof value !== "string") return "";
  let base = value.trim();
  if (!base || base === "auto") return "";
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(base)) base = `http://${base}`;
  return base.replace(/\/+$/, "");
}

/**
 * Load configuration (resolves immediately for the injected case).
 * @returns {Promise<LvsConfig>}
 */
export async function loadConfig() {
  const injected = /** @type {any} */ (globalThis).LVS_CONFIG;
  if (injected && typeof injected === "object") {
    return {
      apiBase: readBase(injected, "api_base"),
      mediaBase: readBase(injected, "media_base") || null,
    };
  }
  try {
    const res = await fetch(
      new URL("config.json", document.baseURI).toString(),
      { cache: "no-store" },
    );
    if (res.ok) {
      const raw = await res.json();
      return {
        apiBase: readBase(raw, "api_base"),
        mediaBase: readBase(raw, "media_base") || null,
      };
    }
  } catch {
    /* fall through to defaults */
  }
  return { ...DEFAULTS };
}

/**
 * Build an absolute API URL for a backend path (e.g. "/api/jobs").
 * @param {LvsConfig} config
 * @param {string} path
 * @returns {string}
 */
export function apiUrl(config, path) {
  return `${config.apiBase}${path.startsWith("/") ? path : `/${path}`}`;
}
