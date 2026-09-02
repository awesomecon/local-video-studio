/**
 * In-browser reference-voice recording screen.
 *
 * Opens a guided modal that tells the speaker exactly what to read, captures
 * the microphone with MediaRecorder, previews the take, and re-encodes it as
 * mono 16-bit PCM WAV — the format the backend voice-profile endpoint
 * validates with Python's `wave` module.
 */

import { el, fmtDuration } from "./dom.js";
import { icon, openModal } from "./ui.js";

export const LANGUAGE_PAIRS = [
  ["en", "English"], ["de", "German"], ["fr", "French"], ["es", "Spanish"],
  ["it", "Italian"], ["pt", "Portuguese"], ["ja", "Japanese"], ["ko", "Korean"],
  ["zh", "Chinese"], ["ru", "Russian"],
];

const MAX_SECONDS = 60;
const REFERENCE_SAMPLE_RATE = 48000;

/* Roughly 20 seconds each of phonetically varied, natural prose. */
const PASSAGES = {
  en: "The morning sun spilled across the quiet harbor as fishermen prepared their boats. A gentle breeze carried the smell of salt and pine through the narrow streets. Somewhere nearby, a church bell rang three times, and the whole square seemed to pause and listen. Then, just as quickly, daily life resumed — engines hummed, gulls called, and friendly voices filled the air.",
  de: "Die Morgensonne fiel über den stillen Hafen, während die Fischer ihre Boote vorbereiteten. Ein leichter Wind trug den Duft von Salz und Kiefern durch die engen Gassen. Irgendwo in der Nähe läutete eine Kirchenglocke dreimal, und der ganze Platz schien kurz innezuhalten. Dann kehrte das tägliche Leben zurück — Motoren summten, Möwen riefen, und freundliche Stimmen erfüllten die Luft.",
  fr: "Le soleil du matin baignait le port tranquille tandis que les pêcheurs préparaient leurs bateaux. Une brise légère apportait le parfum du sel et du pin dans les ruelles étroites. Quelque part tout près, une cloche d'église sonna trois fois, et la place entière sembla s'arrêter pour écouter. Puis, aussitôt, la vie quotidienne reprit — moteurs ronronnants, cris de mouettes, voix chaleureuses.",
  es: "El sol de la mañana bañaba el puerto tranquilo mientras los pescadores preparaban sus barcas. Una brisa suave llevaba el aroma a sal y pino por las calles estrechas. En algún lugar cercano, una campana de iglesia sonó tres veces, y toda la plaza pareció detenerse a escuchar. Luego, en un instante, la vida volvió a su ritmo — motores, gaviotas y voces alegres llenaron el aire.",
  it: "Il sole del mattino illuminava il porto silenzioso mentre i pescatori preparavano le barche. Una brezza leggera portava il profumo di sale e di pino per le stradine. Da qualche parte vicino, una campana suonò tre volte, e l'intera piazza sembrò fermarsi ad ascoltare. Poi, subito dopo, la vita quotidiana riprese — motori, gabbiani e voci amichevoli riempirono l'aria.",
  pt: "O sol da manhã banhava o porto tranquilo enquanto os pescadores preparavam os barcos. Uma brisa suave trazia o cheiro de sal e pinho pelas ruas estreitas. Em algum lugar perto, um sino de igreja tocou três vezes, e a praça inteira pareceu parar para ouvir. Depois, num instante, a vida voltou ao normal — motores, gaivotas e vozes amigáveis encheram o ar.",
  ja: "朝日が静かな港に差し込み、漁師たちは船の支度を始めました。そよ風が潮と松の香りを狭い通りへ運んできます。近くの教会の鐘が三度鳴ると、広場全体が耳を澄ますように静まりました。そしてすぐに日常が戻り、エンジンの音、カモメの声、人々のにぎやかな話し声が空気を満たしました。",
  ko: "아침 해가 고요한 항구 위로 부드럽게 퍼지는 동안 어부들은 배를 준비했습니다. 가벼운 바람이 소금과 소나무 향기를 좁은 골목으로 실어 왔습니다. 가까운 곳에서 교회 종이 세 번 울리자 광장 전체가 잠시 멈춰 듣는 듯했습니다. 그러고는 금세 일상이 돌아와 엔진 소리, 갈매기 울음, 다정한 목소리가 공기를 채웠습니다.",
  zh: "清晨的阳光洒满安静的港口，渔民们开始整理渔船。微风把海盐与松树的清香带进狭窄的街巷。不远处教堂的钟敲了三下，整个广场仿佛都停下来聆听。转眼间日常生活恢复了——引擎轻轻作响，海鸥鸣叫，亲切的谈话声充满了空气。",
  ru: "Утреннее солнце заливало тихую гавань, пока рыбаки готовили свои лодки. Лёгкий ветерок нёс запах соли и сосны по узким улочкам. Где-то рядом трижды прозвонил церковный колокол, и вся площадь словно замерла, прислушиваясь. И тут же обычная жизнь вернулась — зажужжали моторы, закричали чайки, воздух наполнился дружелюбными голосами.",
};

/**
 * @param {string} code
 * @returns {string}
 */
export function passageForLanguage(code) {
  return PASSAGES[code] || PASSAGES.en;
}

/**
 * Encode an AudioBuffer as a mono 16-bit PCM WAV Blob.
 * @param {AudioBuffer} buffer
 * @returns {Blob}
 */
export function encodeWavPcm16(buffer) {
  const channels = Math.max(1, buffer.numberOfChannels);
  const frames = buffer.length;
  const mixed = new Float32Array(frames);
  for (let channel = 0; channel < channels; channel += 1) {
    const data = buffer.getChannelData(channel);
    for (let i = 0; i < frames; i += 1) mixed[i] += data[i];
  }
  const scale = 1 / channels;
  const bytes = new ArrayBuffer(44 + frames * 2);
  const view = new DataView(bytes);
  const writeTag = (offset, tag) => {
    for (let i = 0; i < tag.length; i += 1) view.setUint8(offset + i, tag.charCodeAt(i));
  };
  writeTag(0, "RIFF");
  view.setUint32(4, 36 + frames * 2, true);
  writeTag(8, "WAVE");
  writeTag(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, buffer.sampleRate, true);
  view.setUint32(28, buffer.sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeTag(36, "data");
  view.setUint32(40, frames * 2, true);
  let offset = 44;
  for (let i = 0; i < frames; i += 1) {
    const sample = Math.max(-1, Math.min(1, mixed[i] * scale));
    view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    offset += 2;
  }
  return new Blob([bytes], { type: "audio/wav" });
}

/**
 * Open the guided recording screen. Voiceover mode displays the project
 * script and permits a longer complete narration take.
 * @param {object} opts
 * @param {string} [opts.language]
 * @param {"reference"|"voiceover"} [opts.purpose]
 * @param {string} [opts.promptText]
 * @param {number} [opts.maxSeconds]
 * @param {(take: {blob: Blob, url: string, seconds: number, promptText: string,
 *   language: string}) => void} opts.onUse
 * @returns {{close: () => void}}
 */
export function openVoiceRecorder({
  language = "en", purpose = "reference", promptText = "", maxSeconds = MAX_SECONDS, onUse,
}) {
  const voiceover = purpose === "voiceover";
  const recordingLimit = Math.max(1, Math.min(3600, Number(maxSeconds) || MAX_SECONDS));
  let stream = null;
  let recorder = null;
  let chunks = [];
  let audioCtx = null;
  let analyser = null;
  let meterFrame = 0;
  let tickTimer = 0;
  let capTimer = 0;
  let startedAt = 0;
  let aborted = false;
  let capped = false;
  let take = null;
  let useBtn = null;

  const langSelect = el("select", { class: "input", "aria-label": "Reading language" },
    ...LANGUAGE_PAIRS.map(([code, label]) => el("option", { value: code }, label)));
  langSelect.value = LANGUAGE_PAIRS.some(([code]) => code === language) ? language : "en";

  const promptNode = el("p", { class: "rec-prompt" });
  const syncPrompt = () => {
    promptNode.textContent = voiceover
      ? (promptText.trim() || "Record your complete voiceover, then stop when you are finished.")
      : passageForLanguage(langSelect.value);
  };
  langSelect.onchange = syncPrompt;
  syncPrompt();

  const dot = el("span", { class: "rec-dot" });
  const timer = el("span", { class: "rec-timer" }, "0:00");
  const levelBar = el("div", { class: "lvl" });
  const statusText = el("span", { class: "muted small" }, "Ready when you are.");
  const errorBox = el("div", { class: "rec-error", hidden: true });
  const preview = el("audio", { controls: true, preload: "metadata" });

  const recordBtn = el("button", { class: "btn btn-primary", type: "button" },
    icon("mic", 15), "Start recording");
  const stopBtn = el("button", { class: "btn btn-danger", type: "button", hidden: true },
    icon("stop", 14), "Stop");
  const againBtn = el("button", { class: "btn", type: "button", hidden: true },
    icon("refresh", 14), "Record again");

  const showError = (message) => {
    errorBox.textContent = message;
    errorBox.hidden = false;
  };

  const setPhase = (phase) => {
    recordBtn.hidden = phase !== "idle";
    stopBtn.hidden = phase !== "recording";
    againBtn.hidden = phase !== "ready";
    langSelect.disabled = phase === "recording" || phase === "processing";
    dot.classList.toggle("live", phase === "recording");
    if (phase === "recording") {
      statusText.textContent = voiceover
        ? "Recording — read the full voiceover at a natural pace."
        : "Recording — read the passage aloud at a natural pace.";
    } else if (phase === "processing") {
      levelBar.style.width = "0%";
      statusText.textContent = "Processing take…";
    } else if (phase === "ready") {
      levelBar.style.width = "0%";
      timer.textContent = take ? fmtDuration(take.seconds) : "0:00";
      statusText.textContent = take
        ? `Take ready (${Math.round(take.seconds)} s${capped ? `, ${recordingLimit} s limit` : ""}). Listen below, or record again.`
        : "Ready.";
    } else {
      statusText.textContent = "Ready when you are.";
    }
  };

  const releaseTake = () => {
    if (take) URL.revokeObjectURL(take.url);
    take = null;
    preview.pause();
    preview.removeAttribute("src");
    preview.load();
    previewBlock.hidden = true;
  };

  const teardownCapture = () => {
    window.clearInterval(tickTimer);
    window.clearTimeout(capTimer);
    window.cancelAnimationFrame(meterFrame);
    tickTimer = 0;
    capTimer = 0;
    meterFrame = 0;
    analyser = null;
    if (stream) {
      for (const track of stream.getTracks()) track.stop();
      stream = null;
    }
  };

  const meterLoop = () => {
    const data = new Uint8Array(analyser ? analyser.fftSize : 2048);
    const step = () => {
      if (!analyser) return;
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i += 1) {
        const centered = data[i] - 128;
        sum += centered * centered;
      }
      const rms = Math.sqrt(sum / data.length) / 128;
      levelBar.style.width = `${Math.min(100, Math.round(rms * 240))}%`;
      levelBar.classList.toggle("hot", rms > 0.42);
      meterFrame = requestAnimationFrame(step);
    };
    meterFrame = requestAnimationFrame(step);
  };

  const updateTimer = () => {
    timer.textContent = fmtDuration((Date.now() - startedAt) / 1000);
  };

  const prepareAudioContext = () => {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    // Decoding through a 48 kHz context gives the exported PCM WAV a stable
    // speech-friendly sample rate even when MediaRecorder uses another codec.
    if (!audioCtx) audioCtx = new Ctx({ sampleRate: REFERENCE_SAMPLE_RATE });
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
    return audioCtx;
  };

  const finalize = async () => {
    teardownCapture();
    if (aborted || !recorder) return;
    setPhase("processing");
    const raw = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
    chunks = [];
    try {
      const ctx = prepareAudioContext();
      if (!ctx) throw new Error("WebAudio unavailable");
      const bytes = await raw.arrayBuffer();
      const buffer = await new Promise((resolve, reject) => {
        ctx.decodeAudioData(bytes, resolve, reject);
      });
      if (!buffer.length) throw new Error("empty take");
      const wav = encodeWavPcm16(buffer);
      releaseTake();
      take = {
        blob: wav,
        url: URL.createObjectURL(wav),
        seconds: buffer.duration,
        promptText: voiceover ? promptText.trim() : passageForLanguage(langSelect.value),
        language: langSelect.value,
      };
      preview.src = take.url;
      previewBlock.hidden = false;
      setPhase("ready");
      if (useBtn) useBtn.disabled = false;
    } catch {
      setPhase("idle");
      timer.textContent = "0:00";
      showError("That take could not be decoded. Please try recording again.");
    }
  };

  const beginRecording = async () => {
    errorBox.hidden = true;
    releaseTake();
    capped = false;
    if (useBtn) useBtn.disabled = true;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      showError("Recording is unavailable in this browser. Serve over localhost or HTTPS and allow microphone access.");
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: { ideal: 1 },
          sampleRate: { ideal: REFERENCE_SAMPLE_RATE },
          sampleSize: { ideal: 16 },
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
    } catch {
      showError("Microphone unavailable. Allow microphone access for this tab, then try again.");
      return;
    }
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    });
    recorder.addEventListener("stop", finalize);
    const ctx = prepareAudioContext();
    if (ctx) {
      try {
        const source = ctx.createMediaStreamSource(stream);
        analyser = ctx.createAnalyser();
        analyser.fftSize = 2048;
        source.connect(analyser);
        meterLoop();
      } catch { /* metering is optional */ }
    }
    startedAt = Date.now();
    updateTimer();
    tickTimer = window.setInterval(updateTimer, 250);
    capTimer = window.setTimeout(() => {
      capped = true;
      stopRecording();
    }, recordingLimit * 1000);
    recorder.start(250);
    setPhase("recording");
  };

  const stopRecording = () => {
    if (recorder && recorder.state !== "inactive") recorder.stop();
    else finalize();
  };

  recordBtn.onclick = beginRecording;
  stopBtn.onclick = stopRecording;
  againBtn.onclick = beginRecording;

  const cleanup = () => {
    aborted = true;
    if (recorder && recorder.state !== "inactive") {
      try { recorder.stop(); } catch { /* already stopping */ }
    }
    teardownCapture();
    releaseTake();
    if (audioCtx) {
      audioCtx.close().catch(() => {});
      audioCtx = null;
    }
  };

  const meter = el("div", { class: "rec-meter" }, levelBar);
  const statusRow = el("div", { class: "rec-status", role: "status" },
    dot, timer, meter, statusText);
  const previewBlock = el("div", { class: "vp-preview", hidden: true },
    el("strong", { class: "small" }, "Your take"),
    preview);
  const content = el("div", { class: "stack" },
    el("div", { class: "field" },
      el("label", {}, "Reading language"),
      langSelect,
      el("div", { class: "hint" }, voiceover
        ? "Language is saved with this recording."
        : "The passage follows the language you pick.")),
    promptNode,
    statusRow,
    errorBox,
    el("div", { class: "row" }, recordBtn, stopBtn, againBtn),
    previewBlock,
    el("p", { class: "muted small" },
      `Records mono 48 kHz PCM without browser echo cancellation, noise suppression, or automatic gain. ` +
      `Set input level with the microphone and Ubuntu; a quiet room matters more than cleanup.`));

  const modal = openModal({
    title: voiceover ? "Record your complete voiceover" : "Record a reference voice",
    body: content,
    actions: [
      { label: "Cancel", onClick: (done) => done() },
      {
        label: voiceover ? "Use as narration" : "Use this take", kind: "primary",
        onClick: (done) => {
          if (!take) return;
          onUse({
            blob: take.blob, url: take.url, seconds: take.seconds,
            promptText: take.promptText, language: take.language,
          });
          done();
        },
      },
    ],
  });
  useBtn = modal.dialog.querySelector(".modal-foot .btn-primary");
  if (useBtn) useBtn.disabled = true;
  modal.dialog.addEventListener("close", cleanup);
  return { close: modal.close };
}
