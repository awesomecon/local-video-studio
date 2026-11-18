import { el } from "./dom.js";
import { latestAssetForScene } from "./state.js";
import { icon } from "./ui.js";

export function createRenderMonitor() {
  const element = el("figure", { class: "render-monitor", hidden: true });
  let key = null;
  let video = null;
  return {
    element,
    get video() { return video; },
    update(snapshot, { final = false } = {}) {
      const { project, assets = [], scenes = [] } = snapshot;
      const stages = snapshot.stage_state?.stages || {};
      const prefix = `/api/projects/${encodeURIComponent(project.id)}/assets/`;
      const choices = final ? ["render_final", "render_preview"] : ["render_preview", "render_final"];
      let asset = null;
      let stage = null;
      for (const candidate of choices) {
        const role = candidate === "render_final" ? "final_render" : "preview_render";
        if (stages[candidate]?.status !== "completed") continue;
        asset = assets.filter((item) => item.settings?.role === role && item.url?.startsWith(prefix))
          .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
        if (asset) {
          stage = candidate;
          break;
        }
      }
      const dimensions = project.resolution?.every((value) => Number.isInteger(value) && value > 0)
        ? project.resolution : [1920, 1080];
      const [width, height] = dimensions;
      const nextKey = [project.id, width, height, asset?.url, stage && stages[stage].completed_at].join("|");
      const still = asset && scenes.map((scene) => latestAssetForScene(assets, scene.id, "visual"))
        .find((item) => item?.type === "image" && item.url?.startsWith(prefix));
      if (nextKey === key) {
        if (video && (still?.url || null) !== video.getAttribute("poster")) {
          if (still) video.setAttribute("poster", still.url);
          else video.removeAttribute("poster");
        }
        return;
      }
      key = nextKey;
      element.hidden = false;
      element.classList.toggle("is-portrait", width < height);
      video = asset ? el("video", {
        controls: true, playsinline: "", preload: "metadata", src: asset.url, poster: still?.url,
        style: { aspectRatio: `${width} / ${height}` },
        "aria-label": stage === "render_final" ? "Final video" : "Project preview",
      }) : null;
      const label = stage === "render_final" ? "Final video" : "Project preview";
      element.replaceChildren(
        video || el("div", { class: "empty-state", style: { aspectRatio: `${width} / ${height}` } },
          icon("film", 32),
          el("span", { class: "empty-title" }, "No current preview"),
          el("span", {}, "Render a preview from Export to review the cut."),
        ),
        el("figcaption", { class: "render-monitor-caption" },
          el("div", { class: "row" },
            el("strong", {}, label),
            el("span", { class: "small muted" }, `${project.aspect_ratio} · ${width} × ${height} delivery canvas`),
          ),
          video ? el("span", { class: "small muted render-monitor-poster-note" }, "Project still shown before playback") : null,
        ),
      );
    },
  };
}
