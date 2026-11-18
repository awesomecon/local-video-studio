/**
 * Shared video-mode compatibility helper.
 *
 * Lives in its own dependency-neutral module so both the app shell
 * (app.js) and the page modules can import it: the shell already imports
 * pages/project.js, and the Project page depends on the shell's live-update
 * hook, so the shell cannot take the helper from there without a module
 * cycle. pages/project.js re-exports it for existing importers.
 */

/**
 * "editorial" only for an explicit editorial value. Legacy projects with a
 * missing, omitted, or unknown `video_mode` read as classic; only a
 * deliberate "editorial" value switches the mode-aware screens over.
 * @param {{video_mode?: any} | null | undefined} project
 * @returns {"classic" | "editorial"}
 */
export function effectiveVideoMode(project) {
  return project?.video_mode === "editorial" ? "editorial" : "classic";
}

export const VIDEO_MODE_OPTIONS = [
  { value: "classic", label: "Classic — Build shots in a storyboard" },
  { value: "editorial", label: "Editorial — Build graphics around narration" },
];

export function editorialMediaIssues(editorial) {
  return Object.entries(editorial?.media_issues || {}).flatMap(([compositionId, issues]) =>
    Array.isArray(issues) ? issues.map((issue) => ({ compositionId, issue })) : []);
}

export function missingEditorialMediaLabel(count) {
  return `Replace ${count} missing ${count === 1 ? "visual" : "visuals"}`;
}
