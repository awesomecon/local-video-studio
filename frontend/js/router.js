/**
 * Minimal hash-based client router.
 *
 * No routing dependency is needed: the app has a fixed set of top-level
 * screens plus parameterized routes. The scene editor takes an optional
 * second segment selecting one shot (`#/scene/{sceneId}/shot/{shotId}`) so
 * timeline shot clips and other shortcuts can land on the exact shot in the
 * editor's strip. Hash routing keeps deep links working under the FastAPI
 * static mount without a server-side route table.
 */

/**
 * @typedef {Object} Route
 * @property {string} name
 * @property {string | null} param
 * @property {string | null} [param2] — second capture (shot id) when present
 */

const ROUTES = [
  { re: /^#\/?$/, name: "dashboard", param: null },
  { re: /^#\/new$/, name: "new-project", param: null },
  { re: /^#\/project$/, name: "project", param: null },
  { re: /^#\/script$/, name: "script", param: null },
  { re: /^#\/storyboard$/, name: "storyboard", param: null },
  { re: /^#\/thumbnails$/, name: "thumbnails", param: null },
  { re: /^#\/scene\/([\w-]+)(?:\/shot\/([\w-]+))?$/, name: "scene-editor", param: "sceneId", param2: "shotId" },
  { re: /^#\/voice$/, name: "voice", param: null },
  { re: /^#\/music$/, name: "music", param: null },
  { re: /^#\/captions$/, name: "captions", param: null },
  { re: /^#\/timeline$/, name: "timeline", param: null },
  { re: /^#\/export$/, name: "export", param: null },
  { re: /^#\/jobs$/, name: "jobs", param: null },
  { re: /^#\/settings$/, name: "settings", param: null },
  { re: /^#\/models$/, name: "models", param: null },
];

/**
 * Build the Scene Editor hash for a scene, optionally deep-linking one shot.
 * @param {string} sceneId
 * @param {string|null} [shotId]
 * @returns {string}
 */
export function sceneEditorHash(sceneId, shotId = null) {
  const base = `#/scene/${encodeURIComponent(sceneId)}`;
  return shotId ? `${base}/shot/${encodeURIComponent(shotId)}` : base;
}

/**
 * Parse the current hash into a route.
 * @returns {Route}
 */
export function parseRoute() {
  const hash = window.location.hash || "#/";
  for (const route of ROUTES) {
    const m = hash.match(route.re);
    if (m) {
      return {
        name: route.name,
        param: route.param ? m[1] : null,
        ...(route.param2 ? { param2: m[2] || null } : {}),
      };
    }
  }
  return { name: "dashboard", param: null };
}

/**
 * Navigate.
 * @param {string} hash
 */
export function navigate(hash) {
  if (window.location.hash === hash) {
    onRoute();
    return;
  }
  window.location.hash = hash;
}

let currentListener = null;

/**
 * @param {() => void} fn
 */
export function onHashChange(fn) {
  currentListener = fn;
  window.addEventListener("hashchange", fn);
}

/**
 * Manually trigger the route handler (used when hash is already correct).
 */
export function onRoute() {
  if (currentListener) currentListener();
}
