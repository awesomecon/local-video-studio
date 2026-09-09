/**
 * Not-found screen: destination for hash routes that match no known screen
 * (mistyped URL, stale deep link). Honest fallback with a way back to the
 * Dashboard; no data is fetched.
 */

import { el } from "../dom.js";
import { emptyState } from "../ui.js";
import { navigate } from "../router.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderNotFound(_route) {
  return el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Page not found")),
    emptyState(
      `Nothing lives at ${window.location.hash || "#/"}`,
      "The link may be mistyped or outdated.",
      [el("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("#/") }, "Go to Dashboard")],
    ),
  );
}