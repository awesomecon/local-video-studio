# Gemini TTS (remote provider)

Local Video Studio is local-first. Gemini TTS is its one exception: a cloud
text-to-speech provider that runs on Google's Gemini API. It exists for
narration quality and multilingual preset voices on machines that cannot
(should not) run the heavier local TTS stacks.

## What is sent to Google

* The narration text of each chunk and any optional Voice style delivery direction,
  in a `generateContent` request.
* Your Google AI Studio API key, as the `x-goog-api-key` request header.

Nothing else leaves the machine: no reference audio, voice samples, media, or
project files. Generation happens only when *you* select
the Gemini provider in the Voice page (or pass `provider: "gemini_tts"` to
the API) and start a job. No telemetry, no background checks: the dashboard
never polls Google, and provider "health" is computed locally from key
availability only.

## Requirements

* A [Google AI Studio API key](https://aistudio.google.com) (free tier and
  paid tiers both work; usage is billed by Google under your own account).
* Outbound HTTPS to `generativelanguage.googleapis.com` (or your own loopback
  HTTPS relay configured via `base_url`).
* No GPU, no model downloads, no extra Python dependencies — the adapter
  speaks the public REST API through the application's existing `httpx`
  client.

## Configuring a key

Two ways, and the environment variable always wins:

1. **Environment variable.** Export `GEMINI_API_KEY` before starting the
   dashboard (see `.env.example` for file-based loading). This is the
   recommended path for anything automated.
2. **Voice page.** Select "Gemini TTS (Google, cloud)" in the TTS model
   list. A Gemini panel appears with a key field: paste the key and press
   **Save key**. The key is validated, then written to a private file
   (`~/.local/share/local-video-studio/secrets/gemini_tts_api_key.key`, mode
   `0600` with a `0700` directory on POSIX, or a user-only ACL on Windows).
   No endpoint ever returns the key's value — status checks report only
   whether a key is set and where it came
   from (`environment`, `file`, or `none`). **Remove saved key** deletes the
   file; it never touches environment variables.

If the Voice page shows `needs API key` on the Gemini option, the dashboard
cannot generate with it: the Generate button stays disabled and the API
answers narration requests with a `409` that tells you exactly what to do.

## Using it

* Gemini speaks **preset voices only** (its built-in voice gallery, e.g.
  `Kore`, `Puck`, `Charon` — the dropdown on the Voice page lists the
  current gallery, and well-formed names outside it are accepted because
  Google evolves the gallery). It **cannot clone a voice profile**; selecting
  a profile with Gemini is rejected with a clear error.
* **Models.** The Voice page offers a model picker per take (blank = backend
  default from `backends.gemini_tts.model`):
  * `gemini-3.1-flash-tts-preview` (default) — latest, expressive, low-latency.
  * `gemini-2.5-flash-preview-tts` — previous Flash, low-latency fallback.
  * `gemini-2.5-pro-preview-tts` — higher quality, slower; suited to audiobooks.
  Well-formed identifiers outside this gallery are passed through, so future
  Google models keep working; an unknown model fails the take with a clear
  `model_unavailable` error naming the identifier. The effective model is
  recorded in each chunk's provenance sidecar.
* An optional **Voice style** field is included in the text prompt as a delivery
  direction ("warm documentary narrator, measured pace").
* Chunking works like every other provider (default 30 s; keep it at or
  below that — see Limitations). Scene grouping, pauses, takes, chunk
  re-generation, and the take library all behave normally.
* Gemini delivery tags (`[bracket]` / `<|inline|>`) are **not** supported;
  the performance-tag panel stays hidden for this provider.

## Limitations and honest provenance

* **Not bit-for-bit reproducible.** The API ignores seeds. Studio still
  records the requested seed in take and chunk metadata, marks the take
  `deterministic: false`, and stores the exact text sent per chunk, so a
  retry reproduces the *intent* even when samples differ.
* **Per-request output cap.** Very long chunks can exhaust the model's output
  capacity and come back as `MAX_TOKENS`. The error suggests a shorter chunk
  limit; 15–30 s chunks are the safe range.
* **Safety filtering.** Lines the model blocks (safety, prohibited content)
  fail that chunk with the block reason; rephrase the line.
* **Intermittent text responses.** Occasionally the API answers a chunk with
  text instead of audio; that failure is marked retryable so a retry (or a
  shorter chunk) usually recovers it.
* **Single speaker per request.** Multi-speaker scenes need one take per
  voice (pick a different preset voice and regenerate).

## Configuration reference (`backends.gemini_tts`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Register the provider. `false` hides generation behind a config error. |
| `model` | `gemini-3.1-flash-tts-preview` | Backend default; the Voice page can override it per take (see Models above). Any Gemini TTS model identifier you have access to (e.g. `gemini-2.5-flash-preview-tts`, `gemini-2.5-pro-preview-tts`). |
| `voice` | `Kore` | Default preset voice when the Voice page leaves the voice unset. |
| `api_key_env` | `GEMINI_API_KEY` | Environment variable checked first for the key. |
| `base_url` | `https://generativelanguage.googleapis.com/v1beta` | Must be HTTPS (a loopback HTTP proxy is the only exception). |
| `timeout_seconds` | `180` | Per-request timeout; long takes are split into chunks, so keep this near the default. |

Machine-local overrides go in `config/local.yaml` (never the key itself).

## Troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| `needs API key` / `409 ... needs a Google AI Studio API key` | Set the key (Voice page or `GEMINI_API_KEY`) and restart the dashboard if you used the environment variable. |
| `Google rejected the Gemini API key` (auth) | The key is invalid, revoked, or has no Gemini access. Regenerate it in AI Studio. |
| `Gemini TTS quota or rate limit was hit` | You are on the free tier or exceeded it; wait, or check your AI Studio usage. The job marks this retryable. |
| `model ... was not found` | The `model` identifier in config does not exist (or your account lacks access). |
| `ran out of output capacity` (`MAX_TOKENS`) | Lower the chunk limit (e.g. 15 s) and retry. |
| `answered with text instead of audio` | Transient; retry the chunk or lower the chunk limit. |
| `blocked this narration line` | Safety filter; rephrase the offending line. |
| `Could not reach the Gemini TTS API` | Network/DNS problem, or your `base_url` relay is down. |

Error messages never contain the key value; the adapter redacts
authorization material before any error or log line is written.
