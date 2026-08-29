/**
 * Typed API client for the Local Video Studio FastAPI backend.
 *
 * All response shapes below are documented from the real backend contract
 * (backend/api/main.py + backend/schemas + backend/core/environment.py +
 * backend/workers/gpu.py). No response is fabricated: the UI renders only
 * what these endpoints return. Asset media uses project- and asset-scoped
 * local URLs returned by the backend.
 *
 * Error handling is centralized in `normalizeError` and `ApiError`. Backend
 * structured error codes are preserved verbatim (`code`), so screens can
 * distinguish offline, timeout, authentication, incompatibility,
 * insufficient-VRAM, conflict, and not-yet-implemented (pending-phase)
 * states.
 *
 * Network policy:
 *  - Requests go only to the configured API base (localhost by default).
 *  - AbortController is used for every call so navigation and user actions
 *    can cancel in-flight reads; call-site abort is linked with the
 *    per-request timeout abort.
 *  - Non-idempotent calls (create project, plan, render, generate, approve,
 *    cancel, retry) are never auto-retried.
 *  - Headers are never logged.
 */

import { apiUrl } from "./config.js";

/* ============================================================================
 * Contract types (JSDoc) — mirror the backend JSON exactly.
 * ==========================================================================*/

/**
 * @typedef {"ok" | "mock" | "local"} HealthMode
 */
/**
 * GET /health
 * @typedef {Object} Health
 * @property {"ok"} status
 * @property {HealthMode} mode
 */

/**
 * GPU device snapshot from `MappingSnapshot` (backend/workers/gpu.py).
 * @typedef {Object} GpuDevice
 * @property {number} index
 * @property {string} name
 * @property {number} total_gb
 * @property {number} used_gb
 * @property {number} free_gb
 * @property {number} captured_at
 */

/**
 * `gpu` field of system status: either a successful snapshot or an error.
 * @typedef {Object} GpuStatus
 * @property {GpuDevice[]} [devices]
 * @property {string | null} [active_backend]
 * @property {number} [minimum_free_vram_gb]
 * @property {ApiErrorDetail} [error] — present when nvidia-smi inspection failed
 */

/**
 * Torch/pytorch info inside `environment` (backend/core/environment.py).
 * @typedef {Object} TorchInfo
 * @property {boolean} installed
 * @property {string | null} [version]
 * @property {string | null} [cuda_runtime]
 * @property {boolean} [cuda_available]
 * @property {string | null} [cuda_device_name]
 * @property {number | null} [total_vram_gb]
 * @property {string | null} [import_error]
 */

/**
 * @typedef {Object} BinaryTool
 * @property {boolean} available
 * @property {string | null} [path]
 * @property {string | null} [version]
 * @property {string | null} [source]
 * @property {string | null} [error]
 */

/**
 * @typedef {Object} DiskInfo
 * @property {string} target
 * @property {string} inspected_path
 * @property {number} total_gb
 * @property {number} free_gb
 * @property {boolean} meets_free_space_policy
 */

/**
 * `environment` field: `EnvironmentReport` model dump.
 * @typedef {Object} EnvironmentReport
 * @property {"compatible_existing_environment"|"compatible_with_warnings"|"incompatible_environment_requiring_isolation"} classification
 * @property {string} python_version
 * @property {string} python_executable
 * @property {string} operating_system
 * @property {number} system_ram_gb
 * @property {TorchInfo} torch
 * @property {Array<{name:string, driver_version?:string|null, total_vram_gb:number, free_vram_gb?:number|null}>} nvidia_gpus
 * @property {BinaryTool} ffmpeg
 * @property {BinaryTool} ffprobe
 * @property {BinaryTool} git
 * @property {DiskInfo[]} disks
 * @property {Record<string, string | null>} optional_packages
 * @property {Record<string, {disposition?:string, available?:boolean, detail?:string}>} backend_compatibility
 * @property {string[]} warnings
 * @property {string[]} version_conflicts
 * @property {string[]} recommendations
 */

/**
 * GET /api/system/status
 * @typedef {Object} SystemStatus
 * @property {EnvironmentReport} environment
 * @property {GpuStatus} gpu
 * @property {number} queued_jobs
 * @property {string | null} [active_model]
 * @property {string | null} [comfyui_resident_backend]
 * @property {{free_gib:number|null,total_gib:number|null,threshold_gib:number,resident_comfy_family:string|null,cold_load_required:boolean,must_free_vram:boolean,error:string|null}} [h3_readiness]
 * @property {{llm_external:number, backend_configured:number, backend_effective?:number|null, frontend_configured:number, comfyui_external:number}} [ports]
 * @property {boolean} [mock_mode]
 */

/**
 * `GET /api/projects`
 * @typedef {Object} ProjectList
 * @property {Project[]} projects
 * @property {Array<{type: string, slug?: string, project_id?: string, detail: string}>} [recovery]
 *   — present when the backend reconciled on-disk directories with its database
 *     (recovered / orphaned / conflict / unreadable entries).
 */

/**
 * A project (schemas/models.py Project).
 * @typedef {Object} Project
 * @property {string} id
 * @property {string} slug
 * @property {string} title
 * @property {string} topic
 * @property {number} target_duration
 * @property {"fixed"|"llm"} [duration_mode] — fixed scales scenes to the target; llm ignores the target and the director sizes the runtime to its script
 * @property {"classic"|"editorial"} [video_mode] — "classic" = the existing scene-based generator; "editorial" = motion-graphics compositions; omitted means classic
 * @property {string} aspect_ratio  — "16:9" | "9:16" | "1:1"
 * @property {number} fps
 * @property {[number, number]} resolution
 * @property {string} style
 * @property {string} audience
 * @property {string | null} narrator_preference
 * @property {string} visual_quality
 * @property {string} instructions
 * @property {string} created_at
 * @property {string} [url] — same-origin API URL when the backend can serve the asset
 * @property {string} updated_at
 * @property {"draft"|"planning"|"generating"|"rendering"|"completed"|"failed"|"canceled"} status
 * @property {string} selected_llm_model
 * @property {Record<string, any>} settings
 */

/**
 * @typedef {Object} ThumbnailStudioSnapshot
 * @property {Record<string, any>} plan
 * @property {Array<Record<string, any>>} candidates
 * @property {Record<string, any>|null} selection
 * @property {Asset[]} legacy_frames
 * @property {GenerationJob[]} jobs
 */

/**
 * A scene (schemas/models.py Scene).
 * @typedef {Object} Scene
 * @property {string} id
 * @property {string} project_id
 * @property {number} index
 * @property {string} title
 * @property {number} duration
 * @property {string} narration
 * @property {string} visual_prompt
 * @property {string} negative_prompt
 * @property {string} visual_type
 * @property {string} selected_backend
 * @property {string} camera_instruction
 * @property {string} transition
 * @property {string} music_mood
 * @property {string[]} references
 * @property {number} seed
 * @property {boolean} needs_embedded_text
 * @property {string} text_in_image
 * @property {"automatic"|"krea"|"qwen_image"|"ideogram4_local"} preferred_image_model
 * @property {"draft"|"queued"|"generating"|"generated"|"approved"|"locked"|"failed"} status
 * @property {boolean} locked
 * @property {Record<string, any>} settings
 * @property {string} created_at
 * @property {string} updated_at
 */

/**
 * An asset (schemas/models.py Asset). `filepath` is project-relative.
 * @typedef {Object} Asset
 * @property {string} id
 * @property {string} project_id
 * @property {string | null} scene_id
 * @property {string} type
 * @property {string} filepath
 * @property {string} [url] — project-scoped local media URL when available
 * @property {string} backend
 * @property {string} model
 * @property {string} model_version
 * @property {string | null} quantization
 * @property {string | null} workflow_version
 * @property {number} seed
 * @property {string} prompt
 * @property {string} negative_prompt
 * @property {Record<string, any>} settings
 * @property {string | null} hash
 * @property {string} created_at
 */

/**
 * A generation job (schemas/models.py GenerationJob).
 * @typedef {Object} GenerationJob
 * @property {string} id
 * @property {string} project_id
 * @property {string | null} scene_id
 * @property {string} stage
 * @property {string | null} backend
 * @property {"queued"|"preparing"|"loading_model"|"generating"|"postprocessing"|"completed"|"failed"|"canceled"} status
 * @property {number} progress
 * @property {number} priority
 * @property {Record<string, any>} parameters
 * @property {number} attempt_count
 * @property {number} max_attempts
 * @property {string | null} error
 * @property {string} created_at
 * @property {string} updated_at
 * @property {string | null} started_at
 * @property {string | null} completed_at
 * UI hints added by the API (job_payload in backend/api/main.py):
 * @property {boolean} [executable]
 * @property {boolean} [cancelable]
 */

/**
 * `GET /api/jobs`
 * @typedef {Object} JobList
 * @property {GenerationJob[]} jobs
 */

/**
 * `GET /api/projects/{id}` and `POST /api/projects` response.
 * @typedef {Object} ProjectSnapshot
 * @property {Project} project
 * @property {Scene[]} scenes
 * @property {Asset[]} assets
 * @property {GenerationJob[]} jobs
 * @property {string} directory
 * @property {{version?: number, stages?: Record<string, {status?:string, job_id?:string|null, completed_at?:string, outputs?:string[]}>}} stage_state
 * @property {Array<{type: string, slug?: string, project_id?: string, detail: string}>=} recovery
 * @property {{has_edit_plan?: boolean, plan_status?: "missing"|"current"|"stale"|"untracked", stale?: boolean|null, stale_reasons?: string[], edit_plan_url?: string|null, generate_url?: string|null, preview_url?: string|null}=} [editorial]
 *   — present only on editorial project snapshots. The provenance fields
 *   (plan_status, stale, stale_reasons) are optional and may be missing or
 *   malformed on older backends; treat them defensively and fall back to the
 *   classic plan-available presentation. When present, stale_reasons entries
 *   are "project", "script", or "word_timings".
 */

/**
 * `POST /api/projects/{id}/plan` and `.../script` response (ProjectPlan).
 * @typedef {Object} ProjectPlan
 * @property {string} project_id
 * @property {string} title
 * @property {string[]} outline
 * @property {Scene[]} scenes
 * @property {number} target_duration
 * @property {string[]} strategy_notes
 * @property {string} created_at
 * @property {number} [schema_version]
 */

/**
 * `GET /api/llm/models`
 * @typedef {Object} LlmModels
 * @property {string} endpoint
 * @property {{id:string}[]} models
 * @property {string | null} selected_model
 * @property {string | null} [resolved_model]
 */

/**
 * `GET /api/models`
 * @typedef {Object} ModelList
 * @property {Record<string, BackendDescriptor>} models
 * @property {Record<string, ModelRuntimeStatus>} runtime
 */

/**
 * Studio-known backend lifecycle state. External process memory is deliberately
 * never guessed from total GPU usage.
 * @typedef {Object} ModelRuntimeStatus
 * @property {string} state
 * @property {string} ownership
 * @property {string} detail
 * @property {string[]} actions
 */

/**
 * A backend descriptor (backend/models/base.py BackendDescriptor).
 * @typedef {Object} BackendDescriptor
 * @property {string} backend_name
 * @property {string} model_name
 * @property {string} [model_version]
 * @property {string | null} [quantization]
 * @property {string} [device]
 * @property {number} [vram_required_gb]
 * @property {string[]} [capabilities]
 * @property {string[]} [supported_inputs]
 * @property {string[]} [supported_outputs]
 * @property {boolean} [heavyweight]
 */

/**
 * `GET /api/captions/models` — the local caption-alignment model and its
 * honest readiness (health never loads the model or downloads weights).
 * @typedef {Object} CaptionsModels
 * @property {string} backend
 * @property {BackendDescriptor} descriptor
 * @property {{status: string, install_guidance?: string, model_path?: string, device?: string}} health
 * @property {boolean} enabled
 * @property {string | null} model_path
 * @property {boolean} mock_mode
 */

/**
 * `POST /api/scenes/{id}` body (backend SceneEdit). Only provided fields are
 * sent; the backend applies them and returns the updated scene.
 * @typedef {Object} SceneEdit
 * @property {string} [narration]
 * @property {string} [visual_prompt]
 * @property {string} [negative_prompt]
 * @property {boolean} [needs_embedded_text]
 * @property {string} [text_in_image]
 * @property {"automatic"|"krea"|"qwen_image"|"ideogram4_local"} [preferred_image_model]
 * @property {string} [selected_backend]
 * @property {number} [seed]
 * @property {number} [duration]
 * @property {string[]} [references]
 */

/**
 * Structured backend error detail (docs/api-contract.md + errors.py).
 * @typedef {Object} ApiErrorDetail
 * @property {string} [code]
 * @property {string} [message]
 * @property {string | null} [action]
 * @property {boolean} [retryable]
 * @property {any} [details]
 */

/**
 * Normalized error. `kind` is the UI-facing classification; `code` preserves
 * the backend's own error code verbatim when present. `kind: "pending"` marks
 * structured HTTP 501 `{code:"not_implemented"}` responses — a phase that has
 * not landed yet, never a failure of an existing capability.
 * @typedef {Object} ApiError
 * @property {"offline"|"timeout"|"auth"|"incompatible"|"insufficient_vram"|"conflict"|"validation"|"not_found"|"server"|"pending"|"unknown"} kind
 * @property {string} [code]
 * @property {number | null} [status]
 * @property {string | null} [action]
 * @property {boolean} [retryable]
 * @property {any} [details]
 * @property {string} message
 */

/* ============================================================================
 * Error normalization
 * ==========================================================================*/

/** Backend codes mapped to UI-facing kinds. */
const CODE_KINDS = /** @type {Record<string, string>} */ ({
  server_not_running: "offline",
  request_timeout: "timeout",
  authentication_failed: "auth",
  not_openai_compatible: "incompatible",
  invalid_response: "incompatible",
  model_unavailable: "incompatible",
  insufficient_vram: "insufficient_vram",
  canceled: "conflict",
  backend_unavailable: "server",
  unexpected_service: "incompatible",
  not_implemented: "pending",
});

/**
 * Classify an HTTP status + parsed detail into an error record.
 * @param {number} status
 * @param {any} detail — raw `detail` from the FastAPI body (string, list of
 *   validation issues, or structured object).
 * @returns {ApiError}
 */
export function classifyHttpError(status, detail) {
  let code;
  let message;
  let action;
  let retryable;
  let details;
  if (typeof detail === "string") {
    message = detail;
  } else if (Array.isArray(detail)) {
    details = detail;
    message = detail
      .map((item) => {
        const location = item && Array.isArray(item.loc) ? item.loc.join(".") : "request";
        return `${location}: ${item && item.msg ? item.msg : "invalid"}`;
      })
      .join("; ");
    if (!message) message = "Request validation failed";
  } else if (detail && typeof detail === "object") {
    code = detail.code;
    message = detail.message;
    action = detail.action ?? null;
    retryable = detail.retryable;
    details = detail.details;
  }
  let kind;
  if (code && CODE_KINDS[code]) kind = CODE_KINDS[code];
  else if (status === 401 || status === 403) kind = "auth";
  else if (status === 404) kind = "not_found";
  else if (status === 409) kind = "conflict";
  else if (status === 422) kind = "validation";
  else if (status === 408 || status === 504) kind = "timeout";
  else if (status >= 500) kind = "server";
  else kind = "unknown";
  return {
    kind,
    code: code || undefined,
    status,
    action: action || null,
    retryable: typeof retryable === "boolean" ? retryable : undefined,
    details: details !== undefined ? details : undefined,
    message: message || `Request failed with status ${status}`,
  };
}

/**
 * Normalize any thrown value (fetch TypeError, AbortError, ApiError, or
 * unknown) into an ApiError-shaped record.
 * @param {any} err
 * @param {{timeoutMs?: number, url?: string}} [ctx]
 * @returns {ApiError}
 */
export function normalizeError(err, ctx = {}) {
  if (err && err.kind && err.message) return err; // already normalized
  if (err instanceof DOMException && err.name === "AbortError") {
    const timedOut = !!(ctx.timeoutMs) && !ctx.userAborted;
    return {
      kind: timedOut ? "timeout" : "offline",
      message: timedOut
        ? `Request timed out after ${Math.round(ctx.timeoutMs / 1000)}s`
        : "Request canceled",
      status: null,
      action: null,
    };
  }
  if (err instanceof TypeError) {
    // fetch() throws TypeError on network failure.
    return {
      kind: "offline",
      message: `Cannot reach the Local Video Studio backend${ctx.url ? ` at ${ctx.url}` : ""}.`,
      status: null,
      action: null,
      retryable: true,
    };
  }
  if (err instanceof ApiErrorInstance) {
    return {
      kind: err.kind, code: err.code, status: err.status, action: err.action,
      retryable: err.retryable, details: err.details, message: err.message,
    };
  }
  return {
    kind: "unknown",
    message: err instanceof Error ? err.message : String(err),
    status: null,
    action: null,
  };
}

/** Error class carrying the normalized shape. */
class ApiErrorInstance extends Error {
  /** @param {ApiError} init */
  constructor(init) {
    super(init.message);
    this.name = "ApiError";
    /** @type {ApiError["kind"]} */ this.kind = init.kind;
    this.code = init.code;
    this.status = init.status;
    this.action = init.action;
    this.retryable = init.retryable;
    this.details = init.details;
  }
}

/* ============================================================================
 * Request core
 * ==========================================================================*/

/**
 * Core fetch with timeout + caller-signal linking and JSON (de)serialization.
 * Throws a normalized ApiError on any failure.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} path — backend path, e.g. "/api/jobs"
 * @param {{method?: string, body?: any, signal?: AbortSignal, timeoutMs?: number, userAborted?: boolean}} [opts]
 * @returns {Promise<any>}
 */
export async function request(config, path, opts = {}) {
  const method = opts.method || "GET";
  const timeoutMs = opts.timeoutMs != null ? opts.timeoutMs : 10000;
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) onAbort();
    else opts.signal.addEventListener("abort", onAbort, { once: true });
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const url = apiUrl(config, path);
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: opts.body != null ? { "Content-Type": "application/json" } : undefined,
      body: opts.body != null ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
      credentials: "same-origin",
    });
  } catch (err) {
    const norm = normalizeError(err, {
      timeoutMs,
      url,
      userAborted: !!(opts.signal && opts.signal.aborted),
    });
    throw new ApiErrorInstance(norm);
  } finally {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
  }
  let detail;
  const text = await res.text();
  if (text) {
    try { detail = JSON.parse(text); } catch { detail = text; }
  }
  if (!res.ok) {
    const d = detail && typeof detail === "object" && !Array.isArray(detail) && "detail" in detail
      ? detail.detail
      : detail;
    throw new ApiErrorInstance(classifyHttpError(res.status, d));
  }
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new ApiErrorInstance({
      kind: "incompatible",
      message: "Backend returned a non-JSON response",
      status: res.status,
      action: null,
      retryable: false,
    });
  }
}

/* ============================================================================
 * Endpoint methods (each documents its exact contract)
 * ==========================================================================*/

/**
 * GET /health
 * @param {import("./config.js").LvsConfig} config
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Health>}
 */
export function health(config, opts = {}) {
  return request(config, "/health", { timeoutMs: 3000, ...opts });
}

/**
 * GET /api/system/status
 * @param {import("./config.js").LvsConfig} config
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<SystemStatus>}
 */
export function systemStatus(config, opts = {}) {
  return request(config, "/api/system/status", { timeoutMs: 15000, ...opts });
}

/**
 * GET /api/models
 * @param {import("./config.js").LvsConfig} config
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<ModelList>}
 */
export function models(config, opts = {}) {
  return request(config, "/api/models", { timeoutMs: 10000, ...opts });
}

/** GET /api/models for a project's scoped model selection and runtime state. */
export function projectModels(config, projectId, opts = {}) {
  return request(config, `/api/models?project_id=${encodeURIComponent(projectId)}`, {
    timeoutMs: 10000, ...opts,
  });
}

/** POST /api/comfyui/free — release models cached by the local ComfyUI service. */
export function freeComfyMemory(config, opts = {}) {
  return request(config, "/api/comfyui/free", {
    method: "POST", body: {}, timeoutMs: 30000, ...opts,
  });
}

/** POST /api/ideogram4/unload — release Ideogram and stop an owned worker. */
export function unloadIdeogram4(config, opts = {}) {
  return request(config, "/api/ideogram4/unload", {
    method: "POST", body: {}, timeoutMs: 30000, ...opts,
  });
}

/** GET /api/tts/models */
export function ttsModels(config, opts = {}) {
  return request(config, "/api/tts/models", { timeoutMs: 15000, ...opts });
}

/** POST /api/tts/{provider}/unload — release a TTS provider's loaded weights. */
export function unloadTtsProvider(config, provider, opts = {}) {
  return request(config, `/api/tts/${encodeURIComponent(provider)}/unload`, {
    method: "POST", body: {}, timeoutMs: 30000, ...opts,
  });
}

/** GET /api/captions/models */
export function captionsModels(config, opts = {}) {
  return request(config, "/api/captions/models", { timeoutMs: 10000, ...opts });
}

/** Align captions from the project's active narration with local Whisper. */
export function generateCaptions(config, projectId, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(projectId)}/captions/generate`, {
    method: "POST", body: {}, timeoutMs: 30000, ...opts,
  });
}

/** GET project voice profiles. */
export function listVoiceProfiles(config, projectId, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(projectId)}/tts/voices`, {
    timeoutMs: 15000, ...opts,
  });
}

/** Upload an authorized PCM WAV without multipart dependencies. */
export async function uploadVoiceProfile(config, projectId, file, metadata) {
  const params = new URLSearchParams({
    name: metadata.name,
    transcript: metadata.transcript || "",
    language: metadata.language || "en",
    authorized: String(!!metadata.authorized),
    gain_db: String(metadata.gain_db || 0),
  });
  const url = apiUrl(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/voices?${params.toString()}`);
  let response;
  try {
    response = await fetch(url, {
      method: "POST", headers: { "Content-Type": "audio/wav" }, body: file,
      credentials: "same-origin",
    });
  } catch (err) {
    throw new ApiErrorInstance(normalizeError(err, { url }));
  }
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) {
    const detail = body && typeof body === "object" && "detail" in body ? body.detail : body;
    throw new ApiErrorInstance(classifyHttpError(response.status, detail));
  }
  return body;
}

/** Queue local narration generation. */
export function generateNarration(config, projectId, body, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(projectId)}/tts/generate`, {
    method: "POST", body, timeoutMs: 30000, ...opts,
  });
}

/**
 * GET /api/projects/{id}/tts/performance-tags
 * Fish S2 Pro delivery-tag script (square-bracket delivery cues).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{script: {version: number, provider: string, model: string, generated_at: string, source_sha256: string, intensity: string, segments: {key: string, source: string, tagged: string, scene_id: string | null, scene_index: number | null, scene_title: string | null}[]} | null, stale: boolean, tag_count: number, llm: {available: boolean, model: string | null}} | null>}
 */
export function getPerformanceTags(config, projectId, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/performance-tags`, {
      timeoutMs: 10000, ...opts,
    });
}

/**
 * POST /api/projects/{id}/tts/performance-tags
 * Tags the narration that would actually be generated (scene narration, or
 * the script-override text when `text` is set) with the local LLM. Synchronous
 * like plan generation; `force` re-tags over an existing script.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {{text?: string | null, intensity?: "subtle"|"balanced"|"expressive", notes?: string, force?: boolean}} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{script: any, tag_count: number, warnings: string[]}>}
 */
export function generatePerformanceTags(config, projectId, body, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/performance-tags`, {
      method: "POST", body, timeoutMs: 600000, ...opts,
    });
}

/**
 * POST /api/projects/{id}/tts/performance-tags/regenerate
 * Re-tags a single segment with the local LLM; every other segment keeps its
 * stored tags. Synchronous like plan generation.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {{key: string, intensity?: "subtle"|"balanced"|"expressive", notes?: string}} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{script: any, tag_count: number, warnings: string[]}>}
 */
export function regeneratePerformanceSegment(config, projectId, body, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/performance-tags/regenerate`, {
      method: "POST", body, timeoutMs: 600000, ...opts,
    });
}

/**
 * PUT /api/projects/{id}/tts/performance-tags
 * Saves hand-edited tagged text. The backend validates each segment against
 * the clean source; `accept=true` keeps a hand edit the validator dislikes.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {{segments: {key: string, tagged: string}[]}} body
 * @param {{signal?: AbortSignal, accept?: boolean}} [opts]
 * @returns {Promise<{script: any}>}
 */
export function savePerformanceTags(config, projectId, body, opts = {}) {
  const { accept, ...rest } = opts;
  const suffix = accept ? "?accept=true" : "";
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/performance-tags${suffix}`, {
      method: "PUT", body, timeoutMs: 10000, ...rest,
    });
}

/**
 * DELETE /api/projects/{id}/tts/performance-tags
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{deleted: boolean}>}
 */
export function clearPerformanceTags(config, projectId, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/performance-tags`, {
      method: "DELETE", timeoutMs: 10000, ...opts,
    });
}

/** List immutable narration takes and the take currently used by rendering. */
export function listNarrationTakes(config, projectId, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/narrations`, {
      timeoutMs: 10000, ...opts,
    });
}

/** Select an existing narration take as narration/master.wav. */
export function activateNarrationTake(config, projectId, assetId, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/narrations/${encodeURIComponent(assetId)}/activate`, {
      method: "POST", timeoutMs: 30000, ...opts,
    });
}

/** Save non-destructive gain for one full narration take. */
export function setNarrationTakeGain(config, projectId, assetId, gainDb, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/narrations/${encodeURIComponent(assetId)}/gain`, {
      method: "PUT", body: { gain_db: gainDb }, timeoutMs: 30000, ...opts,
    });
}

/** Queue regeneration of one narration chunk as a new immutable take. */
export function regenerateNarrationChunk(config, projectId, assetId, chunkIndex, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(projectId)}/tts/narrations/${encodeURIComponent(assetId)}`
      + `/chunks/${encodeURIComponent(chunkIndex)}/regenerate`, {
      method: "POST", timeoutMs: 30000, ...opts,
    });
}

/**
 * GET /api/llm/models
 * @param {import("./config.js").LvsConfig} config
 * @param {string | null} [projectId]
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<LlmModels>}
 */
export function llmModels(config, projectId = null, opts = {}) {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return request(config, `/api/llm/models${query}`, { timeoutMs: 10000, ...opts });
}

/**
 * PUT /api/llm/models — select a discovered model for runtime and optional project metadata.
 * @param {import("./config.js").LvsConfig} config
 * @param {{model:string, project_id?:string|null}} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<LlmModels>}
 */
export function selectLlmModel(config, body, opts = {}) {
  return request(config, "/api/llm/models", {
    method: "PUT", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * PATCH /api/projects/{id} — update persisted project-level settings.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {Record<string, any>} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<ProjectSnapshot>}
 */
export function editProject(config, id, body, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}`, {
    method: "PATCH", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * GET /api/projects
 * @param {import("./config.js").LvsConfig} config
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<ProjectList>}
 */
export function listProjects(config, opts = {}) {
  return request(config, "/api/projects", { timeoutMs: 10000, ...opts });
}

/**
 * POST /api/projects — create a project (non-idempotent, never auto-retried).
 * @param {import("./config.js").LvsConfig} config
 * @param {Partial<Project>} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<ProjectSnapshot>}
 */
export function createProject(config, body, opts = {}) {
  return request(config, "/api/projects", {
    method: "POST", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * GET /api/projects/{id}
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<ProjectSnapshot>}
 */
export function getProject(config, id, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}`, { timeoutMs: 15000, ...opts });
}

/**
 * DELETE /api/projects/{id} — permanently removes the portable project
 * directory and all database index rows (non-idempotent). 409 when the
 * project still has queued or running jobs.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{deleted: boolean, project_id: string, slug: string, directory: string}>}
 */
export function deleteProject(config, id, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}`, {
    method: "DELETE", timeoutMs: 30000, ...opts,
  });
}

/** GET the project-scoped Thumbnail Studio record. */
export function getThumbnails(config, id, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}/thumbnails`, {
    timeoutMs: 15000, ...opts,
  });
}

/** Persist the bounded thumbnail plan. */
export function saveThumbnailPlan(config, id, body, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}/thumbnails/plan`, {
    method: "PUT", body, timeoutMs: 15000, ...opts,
  });
}

/** Generate and persist the current Ideogram thumbnail Magic Prompt only. */
export function regenerateThumbnailMagicPrompt(config, id, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(id)}/thumbnails/magic-prompt/regenerate`,
    { method: "POST", body: {}, timeoutMs: 180000, ...opts });
}

/** Queue a candidate generation in one of the three bounded slots. */
export function createThumbnailCandidate(config, id, body = {}, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}/thumbnails/candidates`, {
    method: "POST", body, timeoutMs: 30000, ...opts,
  });
}

/** Regenerate one candidate while retaining the completed previous version on failure. */
export function regenerateThumbnailCandidate(config, id, candidateId, body = {}, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(id)}/thumbnails/candidates/${encodeURIComponent(candidateId)}/regenerate`,
    { method: "POST", body, timeoutMs: 30000, ...opts });
}

/** Select the immutable composite hash used by Export. */
export function selectThumbnailCandidate(config, id, candidateId, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(id)}/thumbnails/candidates/${encodeURIComponent(candidateId)}/select`,
    { method: "POST", body: {}, timeoutMs: 15000, ...opts });
}

/**
 * DELETE one candidate slot — files are archived, the export selection clears
 * if it pointed here, and the slot becomes free (non-idempotent).
 */
export function deleteThumbnailCandidate(config, id, candidateId, opts = {}) {
  return request(config,
    `/api/projects/${encodeURIComponent(id)}/thumbnails/candidates/${encodeURIComponent(candidateId)}`,
    { method: "DELETE", timeoutMs: 15000, ...opts });
}

/**
 * POST /api/projects/{id}/plan — trigger/refresh planning (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{force?: boolean}} [body]
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<ProjectPlan>}
 */
export function planProject(config, id, body = {}, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}/plan`, {
    method: "POST", body, timeoutMs: 600000, ...opts,
  });
}

/**
 * POST /api/projects/{id}/script — alias for plan (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{force?: boolean}} [body]
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<ProjectPlan>}
 */
export function scriptProject(config, id, body = {}, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}/script`, {
    method: "POST", body, timeoutMs: 600000, ...opts,
  });
}

/**
 * POST the snapshot-provided editorial generate URL
 * (`snap.editorial.generate_url`, e.g. "/api/projects/{id}/editorial/plan") —
 * generate and validate the project's Edit Plan. The endpoint takes no request
 * body, so none is sent; the synchronous planner can run for a long time, so
 * the timeout mirrors plan generation. Non-idempotent: never auto-retried, and
 * only issued on an explicit user action.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} generateUrl — backend-provided path from the snapshot
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Record<string, any>>} the generated validated Edit Plan JSON
 */
export function generateEditPlan(config, generateUrl, opts = {}) {
  return request(config, generateUrl, { method: "POST", timeoutMs: 600000, ...opts });
}

/** GET /api/music/models */
export function musicModels(config, opts = {}) {
  return request(config, "/api/music/models", { timeoutMs: 15000, ...opts });
}

/** POST /api/projects/{id}/music/generate */
export function generateMusic(config, projectId, body = {}, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(projectId)}/music/generate`, {
    method: "POST", body, timeoutMs: 30000, ...opts,
  });
}

/**
 * POST /api/projects/{id}/render — assemble existing media into a final video.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{force?: boolean}} [body]
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<GenerationJob>}
 */
export function renderProject(config, id, body = {}, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(id)}/render`, {
    method: "POST", body, timeoutMs: 30000, ...opts,
  });
}

/**
 * POST /api/projects/{id}/visuals/batch — queue sequential generation for
 * every unlocked scene that is still missing a visual, optionally restricted
 * to one visual type or one effective image model. Existing visuals are never
 * archived.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {{visual_type?: string|null, image_model?: "krea"|"qwen_image"|"ideogram4_local"}} [body]
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<GenerationJob>} the queued parent batch job
 */
export function queueVisualBatch(config, projectId, body = {}, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(projectId)}/visuals/batch`, {
    method: "POST", body, timeoutMs: 30000, ...opts,
  });
}

/**
 * POST /api/projects/{id}/jobs/cancel-all — cancel every active job for the
 * project (non-idempotent). Visual batch children are canceled with their
 * parent and come back in `canceled` tagged as such.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{canceled: GenerationJob[], count: number}>}
 */
export function cancelAllProjectJobs(config, projectId, opts = {}) {
  return request(config, `/api/projects/${encodeURIComponent(projectId)}/jobs/cancel-all`, {
    method: "POST", body: {}, timeoutMs: 30000, ...opts,
  });
}

/**
 * PATCH /api/scenes/{id} — edit scene metadata (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {SceneEdit} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Scene>}
 */
export function editScene(config, id, body, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(id)}`, {
    method: "PATCH", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * POST a user-selected local image/video to a reused-media scene. The source
 * title is required; rights/license metadata is optional.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} sceneId
 * @param {File} file
 * @param {Record<string, any>} source
 * @returns {Promise<Asset>}
 */
export async function importReusedMedia(config, sceneId, file, source) {
  const form = new FormData();
  form.append("file", file);
  form.append("source", JSON.stringify(source));
  const res = await fetch(apiUrl(config, `/api/scenes/${encodeURIComponent(sceneId)}/reused-media`), {
    method: "POST", body: form, credentials: "same-origin",
  });
  const body = await res.text();
  let detail = null;
  try { detail = body ? JSON.parse(body) : null; } catch { detail = body; }
  if (!res.ok) {
    const message = detail && typeof detail === "object" && "detail" in detail
      ? String(detail.detail) : (typeof detail === "string" ? detail : "Local media import failed");
    throw new Error(message);
  }
  return detail;
}

/** POST a user-selected local image/video to one explicit reused-media shot. */
export async function importShotReusedMedia(config, shotId, file, source) {
  const form = new FormData();
  form.append("file", file);
  form.append("source", JSON.stringify(source));
  const res = await fetch(apiUrl(config, `/api/shots/${encodeURIComponent(shotId)}/reused-media`), {
    method: "POST", body: form, credentials: "same-origin",
  });
  const body = await res.text();
  let detail = null;
  try { detail = body ? JSON.parse(body) : null; } catch { detail = body; }
  if (!res.ok) {
    const message = detail && typeof detail === "object" && "detail" in detail
      ? String(detail.detail) : (typeof detail === "string" ? detail : "Local shot media import failed");
    throw new Error(message);
  }
  return detail;
}

async function importGeneratedImageAt(config, path, file) {
  const form = new FormData();
  form.append("file", file);
  form.append("source", JSON.stringify({
    title: file.name || "Imported AI image",
    classification: "illustration",
    notes: "AI-generated image imported manually by the producer.",
  }));
  const res = await fetch(apiUrl(config, path), {
    method: "POST", body: form, credentials: "same-origin",
  });
  const body = await res.text();
  let detail = null;
  try { detail = body ? JSON.parse(body) : null; } catch { detail = body; }
  if (!res.ok) {
    const message = detail && typeof detail === "object" && "detail" in detail
      ? String(detail.detail) : (typeof detail === "string" ? detail : "AI image import failed");
    throw new Error(message);
  }
  return detail;
}

/** Attach an existing AI-generated image to a scene-level image recipe. */
export function importSceneGeneratedImage(config, sceneId, file) {
  return importGeneratedImageAt(
    config, `/api/scenes/${encodeURIComponent(sceneId)}/imported-image`, file,
  );
}

/** Attach an existing AI-generated image to one explicit image shot. */
export function importShotGeneratedImage(config, shotId, file) {
  return importGeneratedImageAt(
    config, `/api/shots/${encodeURIComponent(shotId)}/imported-image`, file,
  );
}

/**
 * POST /api/scenes/{id}/generate — generate/refresh visual (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Asset>}
 */
export function generateScene(config, id, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(id)}/generate`, {
    method: "POST", body: {}, timeoutMs: 600000, ...opts,
  });
}

/**
 * POST /api/scenes/{id}/regenerate — force regenerate, archiving prior output
 * (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Asset>}
 */
export function regenerateScene(config, id, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(id)}/regenerate`, {
    method: "POST", body: {}, timeoutMs: 600000, ...opts,
  });
}

/**
 * GET /api/scenes/{id}/graphic-screen — approved source for read-only inspection.
 * Callers must render `source` as text, never as HTML.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{manifest: Record<string, any>, source: string}>}
 */
export function getGraphicScreen(config, id, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(id)}/graphic-screen`, {
    method: "GET", timeoutMs: 15000, ...opts,
  });
}

/**
 * POST /api/scenes/{id}/approve — approve (and optionally lock) a scene
 * (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{lock?: boolean}} [body]
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Scene>}
 */
export function approveScene(config, id, body = {}, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(id)}/approve`, {
    method: "POST", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * GET /api/jobs
 * @param {import("./config.js").LvsConfig} config
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<JobList>}
 */
export function listJobs(config, opts = {}) {
  return request(config, "/api/jobs", { timeoutMs: 10000, ...opts });
}

/**
 * POST /api/jobs/{id}/cancel (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<GenerationJob>}
 */
export function cancelJob(config, id, opts = {}) {
  return request(config, `/api/jobs/${encodeURIComponent(id)}/cancel`, {
    method: "POST", body: {}, timeoutMs: 15000, ...opts,
  });
}

/**
 * POST /api/jobs/{id}/retry (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} id
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<GenerationJob>}
 */
export function retryJob(config, id, opts = {}) {
  return request(config, `/api/jobs/${encodeURIComponent(id)}/retry`, {
    method: "POST", body: {}, timeoutMs: 15000, ...opts,
  });
}

/* ============================================================================
 * Shots and overlay cues (multi-shot contracts, backend f3490ff)
 * ==========================================================================*/

/**
 * Incoming shot transition (`ShotTransition`). `cut` always carries
 * duration_seconds 0; every other kind overlaps the previous shot.
 * @typedef {Object} ShotTransition
 * @property {"cut"|"crossfade"|"dissolve"|"fade_through_black"|"dip_to_white"} kind
 * @property {number} duration_seconds
 */

/**
 * An overlay cue embedded in a shot (`OverlayCue`). Timing is shot-local:
 * `start_seconds >= 0` and `start_seconds + duration_seconds <= shot.duration`.
 * @typedef {Object} ShotOverlayCue
 * @property {string} id
 * @property {"exact_text"|"graphic"|"image"} kind
 * @property {string | null} [asset_id] — required for graphic/image kinds
 * @property {string | null} [exact_text] — required (non-empty) for exact_text
 * @property {string} [template]
 * @property {number} start_seconds
 * @property {number} duration_seconds
 * @property {number} [z_index]
 * @property {string} [anchor] — nine-point anchor name ("top_left".."bottom_right")
 * @property {number | null} [x]
 * @property {number | null} [y]
 * @property {number | null} [width] — set together with height
 * @property {number | null} [height]
 * @property {number} [safe_area] — fractional inset 0..0.25
 * @property {"contain"|"cover"|"stretch"} [fit]
 * @property {number} opacity — (0, 1]
 * @property {number} fade_in_seconds
 * @property {number} fade_out_seconds
 */

/**
 * One ordered visual beat inside a scene (`Shot`, backend schemas/shots.py).
 * Snapshot/list payloads additionally carry `implicit: true` when projected
 * from a legacy single-visual scene.
 * @typedef {Object} Shot
 * @property {string} id
 * @property {string} project_id
 * @property {string} scene_id
 * @property {number} index
 * @property {string} title
 * @property {number} duration_seconds
 * @property {"fixed"|"weighted"} start_mode
 * @property {"real"|"image"|"h3"|"html"} lane
 * @property {string} visual_type
 * @property {string} selected_backend
 * @property {string} visual_prompt
 * @property {string} negative_prompt
 * @property {string} camera_instruction
 * @property {string | null} source_asset_id
 * @property {number | null} source_in_seconds
 * @property {number | null} source_out_seconds
 * @property {ShotTransition} transition_in
 * @property {string[]} references
 * @property {number|string} seed
 * @property {"draft"|"queued"|"generating"|"ready"|"approved"|"failed"} status
 * @property {boolean} locked
 * @property {ShotOverlayCue[]} overlays
 * @property {Array<Record<string, any>>} audio_cues
 * @property {Record<string, any>|null} [source]
 * @property {Record<string, any>} settings
 * @property {string} created_at
 * @property {string} updated_at
 * @property {boolean} [implicit]
 */

/**
 * Per-scene shot block embedded in project snapshots (`shot_summary`).
 * There is no explicit stale count yet; `count - ready - failed` is the
 * honest pending remainder (see frontend/API_GAPS.md).
 * @typedef {Object} ShotSummary
 * @property {number} count
 * @property {boolean} materialized
 * @property {number} ready
 * @property {number} approved
 * @property {number} failed
 * @property {number} rendered_duration_seconds — Σduration − Σincoming overlap
 */

/**
 * GET /api/scenes/{id}/shots response (list_scene_shots).
 * @typedef {Object} SceneShotList
 * @property {Shot[]} shots
 * @property {boolean} materialized
 * @property {number} count
 * @property {number} ready
 * @property {number} approved
 * @property {number} failed
 * @property {number} rendered_duration_seconds
 * @property {string} scene_id
 * @property {number} scene_duration
 */

/**
 * GET /api/scenes/{scene_id}/shots — stored shots plus the implicit legacy
 * projection for scenes without stored shots.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} sceneId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<SceneShotList>}
 */
export function listSceneShots(config, sceneId, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(sceneId)}/shots`, {
    timeoutMs: 10000, ...opts,
  });
}

/**
 * POST /api/scenes/{scene_id}/shots — create a shot; materializes the
 * implicit legacy shot first when the scene predates stored shots.
 * `extra="forbid"` server-side: send only documented fields.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} sceneId
 * @param {Record<string, any>} body — ShotCreate fields (duration_seconds required)
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Shot>}
 */
export function createShot(config, sceneId, body, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(sceneId)}/shots`, {
    method: "POST", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * PATCH /api/shots/{shot_id} — partial edit (extra="forbid"); approval and
 * locking go through approveShot only. Refuses locked shots with 409.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {Record<string, any>} body — ShotEdit fields; omitted keys keep values
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Shot>}
 */
export function editShot(config, shotId, body, opts = {}) {
  return request(config, `/api/shots/${encodeURIComponent(shotId)}`, {
    method: "PATCH", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * DELETE /api/shots/{shot_id} — guarded archive: refuses locked shots with
 * 409, archives shot media, reverts to the implicit projection when the last
 * stored shot goes away (non-idempotent).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {{archiveMedia?: boolean, signal?: AbortSignal}} [opts]
 * @returns {Promise<{deleted_shot_id: string, archived_assets: string[], remaining_shots: number, scene_reverted_to_implicit: boolean}>}
 */
export function deleteShot(config, shotId, opts = {}) {
  const query = opts.archiveMedia === false ? "?archive_media=false" : "";
  return request(config,
    `/api/shots/${encodeURIComponent(shotId)}${query}`,
    { method: "DELETE", timeoutMs: 30000, ...opts });
}

/**
 * POST /api/shots/{shot_id}/approve — approve and optionally lock; also the
 * materialization path for a legacy scene's implicit shot id.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {{lock?: boolean}} [body]
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Shot>}
 */
export function approveShot(config, shotId, body = {}, opts = {}) {
  return request(config, `/api/shots/${encodeURIComponent(shotId)}/approve`, {
    method: "POST", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * POST /api/shots/{shot_id}/generate — validate the lane and queue a
 * shot_generate job. Existing matching media may be reused by the worker.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<GenerationJob>}
 */
export function generateShot(config, shotId, opts = {}) {
  return request(config, `/api/shots/${encodeURIComponent(shotId)}/generate`, {
    method: "POST", body: {}, timeoutMs: 15000, ...opts,
  });
}

/**
 * POST /api/shots/{shot_id}/regenerate — queue a forced shot_generate job;
 * the worker archives current shot media before generating its replacement.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<GenerationJob>}
 */
export function regenerateShot(config, shotId, opts = {}) {
  return request(config, `/api/shots/${encodeURIComponent(shotId)}/regenerate`, {
    method: "POST", body: {}, timeoutMs: 15000, ...opts,
  });
}

/**
 * POST /api/scenes/{scene_id}/render — queue deterministic compilation of
 * the scene's ready shot media, transitions, and overlays through FFmpeg.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} sceneId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<GenerationJob>}
 */
export function renderScene(config, sceneId, opts = {}) {
  return request(config, `/api/scenes/${encodeURIComponent(sceneId)}/render`, {
    method: "POST", body: {}, timeoutMs: 30000, ...opts,
  });
}

/**
 * POST /api/shots/{shot_id}/overlays — attach one overlay cue; returns the
 * updated shot. Refuses locked shots/scenes with 409.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {Record<string, any>} body — OverlayCueRequest (kind required)
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Shot>}
 */
export function addShotOverlay(config, shotId, body, opts = {}) {
  return request(config, `/api/shots/${encodeURIComponent(shotId)}/overlays`, {
    method: "POST", body, timeoutMs: 15000, ...opts,
  });
}

/**
 * PATCH /api/shots/{shot_id}/overlays/{overlay_id} — partial overlay edit;
 * omitted keys keep their values.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {string} overlayId
 * @param {Record<string, any>} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Shot>}
 */
export function patchShotOverlay(config, shotId, overlayId, body, opts = {}) {
  return request(config,
    `/api/shots/${encodeURIComponent(shotId)}/overlays/${encodeURIComponent(overlayId)}`,
    { method: "PATCH", body, timeoutMs: 15000, ...opts });
}

/**
 * DELETE /api/shots/{shot_id}/overlays/{overlay_id} — remove one cue.
 * @param {import("./config.js").LvsConfig} config
 * @param {string} shotId
 * @param {string} overlayId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Shot>}
 */
export function removeShotOverlay(config, shotId, overlayId, opts = {}) {
  return request(config,
    `/api/shots/${encodeURIComponent(shotId)}/overlays/${encodeURIComponent(overlayId)}`,
    { method: "DELETE", timeoutMs: 15000, ...opts });
}

/**
 * PATCH /api/overlays/{overlay_id}?project_id=... — project-scope overlay
 * edit for callers that know only the overlay id (ids resolve within one
 * project because cues are embedded in shots).
 * @param {import("./config.js").LvsConfig} config
 * @param {string} projectId
 * @param {string} overlayId
 * @param {Record<string, any>} body
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<Shot>}
 */
export function patchProjectOverlay(config, projectId, overlayId, body, opts = {}) {
  const query = `?project_id=${encodeURIComponent(projectId)}`;
  return request(config,
    `/api/overlays/${encodeURIComponent(overlayId)}${query}`,
    { method: "PATCH", body, timeoutMs: 15000, ...opts });
}
