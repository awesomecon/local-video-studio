/**
 * Video style (`video_mode`) contract shared by the project forms.
 *
 * The backend accepts `"classic"` and `"editorial"` on
 * `POST /api/projects` and `PATCH /api/projects/{id}` (a change is only sent
 * when it differs from the saved value). Existing project payloads may omit
 * the field entirely; the UI must interpret omission as `"classic"`.
 *
 * Keeping these constants in one pure module (no DOM, no app boot) means the
 * New Project form, the Project Details form, and the headless logic tests all
 * agree on the same contract.
 */

/**
 * @typedef {"classic" | "editorial"} VideoMode
 */

/** Allowed video modes, in UI display order. */
export const VIDEO_MODES = /** @type {VideoMode[]} */ (["classic", "editorial"]);

/** New-project default, and the meaning of an omitted field on old projects. */
export const DEFAULT_VIDEO_MODE = /** @type {VideoMode} */ ("classic");

/**
 * Option labels for the "Video Style" select on the New Project and
 * Project Details screens.
 * @type {{value: VideoMode, label: string}[]}
 */
export const VIDEO_MODE_OPTIONS = [
  { value: "classic", label: "Classic — Existing scene-based generator" },
  { value: "editorial", label: "Editorial — Motion-graphics compositions" },
];

/**
 * Resolve the effective mode of an existing project: an omitted or
 * unrecognized `video_mode` means classic; only an explicit `"editorial"`
 * switches it.
 * @param {{video_mode?: any} | null | undefined} project
 * @returns {VideoMode}
 */
export function effectiveVideoMode(project) {
  const mode = project && project.video_mode;
  return mode === "editorial" ? "editorial" : DEFAULT_VIDEO_MODE;
}
