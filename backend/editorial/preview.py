"""Player controls for the interactive Editorial preview document."""

from html import escape


def preview_controls(narration_url: str | None) -> str:
    audio = (
        f'<audio id="preview-narration" src="{escape(narration_url, quote=True)}" preload="metadata"></audio>'
        if narration_url else ""
    )
    return f"""
<div id="preview-controls">
  <button id="preview-play" type="button" disabled>Play</button>
  <input id="preview-time" type="range" min="0" max="1" step="any" value="0"
    aria-label="Preview position" disabled>
  <span id="preview-clock"></span>
  <span id="preview-status" role="status">{"" if audio else "Silent preview"}</span>
</div>{audio}"""


PREVIEW_CSS = """
body.editorial-preview{display:grid;grid-template-rows:minmax(0,1fr) auto}
#preview-picture{position:relative;overflow:hidden}
#preview-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px;
  color:#eee;background:#1b1d1f;font:13px/1.4 system-ui,sans-serif;color-scheme:dark}
#preview-controls button{min-width:4.5em;min-height:32px;cursor:pointer}
#preview-time{flex:1;min-width:40px;accent-color:#d6b375}
#preview-clock{font-variant-numeric:tabular-nums;white-space:nowrap}
#preview-status{width:100%;color:#ddd}
#preview-status:empty{display:none}
#preview-narration{display:none}
"""

FIT_PICTURE_SCRIPT = """
function fitEditorialPicture(picture) {
  const stage = document.getElementById("stage");
  const scale = Math.min(
    picture.clientWidth / PLAN.width, picture.clientHeight / PLAN.height,
  ) * Math.min(PLAN.width / stage.offsetWidth, PLAN.height / stage.offsetHeight);
  stage.style.transform = "scale(" + scale + ")";
  stage.style.left = (picture.clientWidth - stage.offsetWidth * scale) / 2 + "px";
  stage.style.top = (picture.clientHeight - stage.offsetHeight * scale) / 2 + "px";
}
"""


PREVIEW_SCRIPT = """
(() => {
  const picture = document.getElementById("preview-picture");
  const button = document.getElementById("preview-play");
  const slider = document.getElementById("preview-time");
  const clock = document.getElementById("preview-clock");
  const status = document.getElementById("preview-status");
  const narration = document.getElementById("preview-narration");
  const last = PLAN.compositions.at(-1);
  const duration = last.start + last.duration;
  let time = 0;
  let playing = false;
  let frame = 0;
  let start = 0;
  let pendingComposition;
  let audioAvailable = Boolean(narration);

  function fitPicture() {
    fitEditorialPicture(picture);
  }

  function formatTime(seconds) {
    const whole = Math.floor(seconds);
    return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
  }

  function draw(seconds) {
    time = Math.max(0, Math.min(duration, seconds));
    window.renderAt(time);
    slider.value = String(time);
    clock.textContent = formatTime(time) + " / " + formatTime(duration);
    slider.setAttribute("aria-valuetext", clock.textContent);
  }

  function stop() {
    playing = false;
    cancelAnimationFrame(frame);
    narration?.pause();
    button.textContent = "Play";
  }

  function seek(seconds) {
    stop();
    draw(seconds);
  }

  function audioFailed() {
    audioAvailable = false;
    start = performance.now() - time * 1000;
    status.textContent = "Narration unavailable. Playing picture only.";
  }

  function tick(now) {
    if (audioAvailable && !narration.ended && time < (narration.duration || Infinity)) {
      draw(narration.currentTime);
      start = now - time * 1000;
    } else {
      draw((now - start) / 1000);
    }
    if (time >= duration) stop();
    if (playing) frame = requestAnimationFrame(tick);
  }

  button.addEventListener("click", () => {
    if (playing) {
      stop();
      return;
    }
    if (time >= duration) draw(0);
    if (audioAvailable && time < (narration.duration || Infinity)) {
      narration.currentTime = time;
      narration.play().catch(error => {
        if (playing && error.name !== "AbortError") audioFailed();
      });
    }
    start = performance.now() - time * 1000;
    button.textContent = "Pause";
    playing = true;
    frame = requestAnimationFrame(tick);
  });
  slider.addEventListener("input", () => seek(Number(slider.value)));
  narration?.addEventListener("error", audioFailed);
  window.addEventListener("message", event => {
    if (window.parent === window || event.source !== window.parent || event.origin !== location.origin ||
        event.data?.type !== "editorial-select-composition" || typeof event.data.id !== "string") return;
    const composition = PLAN.compositions.find(item => item.id === event.data.id);
    if (!composition) return;
    if (window.__editorialReady) seek(composition.start);
    else pendingComposition = composition;
  });
  const resizeObserver = new ResizeObserver(() => {
    fitPicture();
    if (window.__editorialReady) draw(time);
  });
  resizeObserver.observe(picture);
  if (narration?.error) audioFailed();
  window.addEventListener("editorial-ready", () => {
    slider.max = String(duration);
    button.disabled = slider.disabled = false;
    fitPicture();
    draw(pendingComposition?.start ?? 0);
  }, { once: true });
})();
"""
