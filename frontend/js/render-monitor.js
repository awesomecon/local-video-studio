import { el } from "./dom.js";
import { projectAssetUrl } from "./api.js";
import { latestAssetForScene } from "./state.js";
import { icon } from "./ui.js";

export function currentRenderAsset(snapshot, stage) {
  const record = snapshot.stage_state?.stages?.[stage];
  if (record?.status !== "completed") return null;
  const role = stage === "render_final" ? "final_render" : "preview_render";
  const normalizePath = (path) => String(path).replaceAll("\\", "/");
  const outputs = Array.isArray(record.outputs) ? record.outputs.map(normalizePath) : [];
  return (snapshot.assets || []).filter((item) =>
    item.settings?.role === role && item.current !== false && item.available !== false
      && projectAssetUrl(snapshot.project.id, item.url)
      && (!outputs.length || outputs.includes(normalizePath(item.filepath))))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] || null;
}

export function createRenderMonitor() {
  const element = el("figure", { class: "render-monitor", hidden: true });
  let key = null;
  let video = null;
  return {
    element,
    get video() { return video; },
    update(snapshot, { final = false, emptyContent = null } = {}) {
      const { project, assets = [], scenes = [] } = snapshot;
      const stages = snapshot.stage_state?.stages || {};
      const prefix = `/api/projects/${encodeURIComponent(project.id)}/assets/`;
      const choices = final ? ["render_final", "render_preview"] : ["render_preview", "render_final"];
      let asset = null;
      let stage = null;
      for (const candidate of choices) {
        asset = currentRenderAsset(snapshot, candidate);
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
      if (nextKey === key && video) {
        if (video && (still?.url || null) !== video.getAttribute("poster")) {
          if (still) video.setAttribute("poster", still.url);
          else video.removeAttribute("poster");
        }
        return;
      }
      key = nextKey;
      element.hidden = false;
      element.classList.toggle("is-portrait", Boolean(asset) && width < height);
      video = asset ? el("video", {
        controls: true, playsinline: "", preload: "metadata", src: asset.url, poster: still?.url,
        style: { aspectRatio: `${width} / ${height}` },
        "aria-label": stage === "render_final" ? "Final video" : "Project preview",
      }) : null;
      const label = stage === "render_final" ? "Final video" : "Project preview";
      element.replaceChildren(
        video || el("div", { class: "empty-state render-monitor-empty" },
          icon("film", 24),
          el("strong", {}, "No current preview"),
          emptyContent || el("a", { class: "btn btn-sm", href: "#/export" }, "Open Export to render a preview"),
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
