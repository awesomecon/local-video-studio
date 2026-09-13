/**
 * Score Studio waveform: decode a local audio file in the browser and draw
 * its peaks on a canvas lane.
 *
 * Peaks are computed with `AudioContext.decodeAudioData` (no third-party
 * waveform dependency) and cached per URL so the timeline can re-draw on
 * every playhead move without re-decoding. The AudioContext is created
 * lazily and shared; it must be closed on page navigation (see
 * `destroyWaveformContext`) so the Music screen leaves no running audio
 * graph behind.
 */

/** @type {{ctx: AudioContext} | null} */
let contextHolder = null;

/** Lazily shared AudioContext for decoding (playback uses <audio> elements). */
function audioContext() {
  if (contextHolder) return contextHolder.ctx;
  const Ctor = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
  if (!Ctor) throw new Error("AudioContext is not available in this browser");
  const ctx = /** @type {AudioContext} */ new Ctor();
  contextHolder = { ctx };
  return ctx;
}

/** Close the shared decode context (call on navigation away from Music). */
export function destroyWaveformContext() {
  if (contextHolder) {
    contextHolder.ctx.close().catch(() => {});
    contextHolder = null;
  }
}

/** @type {Map<string, {peaks: Float32Array, frames: number}>} */
const peakCache = new Map();

/** Drop decoded peaks for URLs that are no longer served (new asset hashes). */
export function pruneWaveformCache(urls) {
  const keep = new Set(urls);
  for (const key of peakCache.keys()) {
    if (!keep.has(key)) peakCache.delete(key);
  }
}

/**
 * Decode `url` and compute per-bucket peak levels (0..1, max channel).
 * @param {string} url
 * @param {number} buckets
 * @param {AbortSignal} [signal]
 * @returns {Promise<Float32Array>}
 */
export async function computeWaveformPeaks(url, buckets, signal) {
  const cached = peakCache.get(url);
  if (cached && cached.peaks.length === buckets) return cached.peaks;
  if (cached) {
    // Bucket count changed: resample the cached peaks instead of re-decoding.
    const resampled = new Float32Array(buckets);
    for (let i = 0; i < buckets; i += 1) {
      const lo = Math.floor((i / buckets) * cached.peaks.length);
      const hi = Math.max(lo + 1, Math.floor(((i + 1) / buckets) * cached.peaks.length));
      let max = 0;
      for (let j = lo; j < hi; j += 1) max = Math.max(max, cached.peaks[j]);
      resampled[i] = max;
    }
    return resampled;
  }
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  const response = await fetch(url, { credentials: "same-origin", signal });
  if (!response.ok) throw new Error(`audio fetch failed: HTTP ${response.status}`);
  const buffer = await response.arrayBuffer();
  let decoded;
  try {
    decoded = await audioContext().decodeAudioData(buffer);
  } catch (err) {
    throw new Error(`could not decode audio for waveform: ${err.message || err}`);
  }
  const channels = [];
  for (let c = 0; c < decoded.numberOfChannels; c += 1) {
    channels.push(decoded.getChannelData(c));
  }
  const frames = decoded.length;
  const peaks = new Float32Array(buckets);
  const per = Math.max(1, Math.floor(frames / buckets));
  for (let b = 0; b < buckets; b += 1) {
    const lo = b * per;
    const hi = Math.min(frames, lo + per);
    let max = 0;
    const stride = Math.max(1, Math.floor((hi - lo) / 256));
    for (let f = lo; f < hi; f += stride) {
      for (let c = 0; c < channels.length; c += 1) {
        const v = Math.abs(channels[c][f]);
        if (v > max) max = v;
      }
    }
    peaks[b] = Math.min(1, max);
  }
  peakCache.set(url, { peaks, frames });
  return peaks;
}

/** Resolve a design token (e.g. `--accent`) to a concrete color string. */
function tokenColor(name, fallback) {
  try {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  } catch {
    return fallback;
  }
}

/**
 * Draw a peak waveform on a canvas. The portion at or before `progress`
 * (0..1) is drawn at full intensity; the remainder dimmed.
 * @param {HTMLCanvasElement} canvas
 * @param {Float32Array} peaks
 * @param {number} progress
 * @param {object} [style]
 * @param {string} [style.color] - CSS token name (e.g. "--accent") or hex
 * @param {string} [style.dimColor]
 */
export function drawWaveform(canvas, peaks, progress, style = {}) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (width <= 0 || height <= 0) return;
  if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
    canvas.width = width * dpr;
    canvas.height = height * dpr;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const rawColor = style.color || "--text-2";
  const rawDim = style.dimColor || "--text-3";
  const color = rawColor.startsWith("--") ? tokenColor(rawColor, "#c3c2b7") : rawColor;
  const dim = rawDim.startsWith("--") ? tokenColor(rawDim, "#898781") : rawDim;
  const mid = height / 2;
  const barW = Math.max(1, width / peaks.length);
  const playedX = progress * width;
  for (let i = 0; i < peaks.length; i += 1) {
    const x = i * barW;
    const amp = Math.max(1.5, peaks[i] * (height / 2 - 2));
    ctx.fillStyle = x + barW / 2 <= playedX ? color : dim;
    const w = Math.max(1, barW - (barW > 3 ? 1 : 0));
    ctx.fillRect(x, mid - amp, w, amp * 2);
  }
}