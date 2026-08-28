# Architecture

Local Video Studio is a local orchestrator around replaceable AI workers and deterministic media
tools. The FastAPI process owns project state, scheduling, and user-facing APIs; it does not absorb
every model's dependency graph.

## Process boundaries

```text
Web UI (plain HTML/CSS/JS, 127.0.0.1, app-owned port)
                    |
FastAPI orchestration API (127.0.0.1, app-owned port)
      |             |                 |
SQLite + JSON    serial job queue   FFmpeg renderer
      |             |
portable project    +-- existing local LLM (127.0.0.1:1234, external)
directories         +-- ComfyUI (127.0.0.1:8188, external)
                    +-- H3 local worker (dynamic localhost port)
                    +-- optional backend-specific local workers
```

The API and UI bind to loopback unless the user deliberately enables LAN access. Port 1234 is
reserved for the user's existing OpenAI-compatible server and is never selected by application-owned
services.

## State model

SQLite indexes projects, scenes, assets, jobs, attempts, prompts, model metadata, and renders. Every
project also carries human-readable JSON and Markdown on disk. The disk representation is the portable
record; SQLite can be rebuilt or reconciled from it.

Each generated asset records its prompt, negative prompt, seed, backend and model identity, model
version, quantization, workflow version, settings, content hash, attempt history, and relevant render
metadata. Rejected variants are archived rather than deleted.

## Restartable pipeline

Stages are small state transitions rather than one monolithic process:

1. create project
2. outline and script
3. direct scenes and choose a cost-appropriate visual method
4. narrate and align timestamps
5. adjust scene durations
6. create references and storyboards
7. generate stills, fallback video, and selected H3 hero shots
8. generate music and subtitles
9. build timeline and preview
10. run media/project QC
11. render final outputs
12. generate thumbnails and publishing metadata

Completed stages and approved/locked scenes are skipped on resume. A scene regeneration creates a new
attempt and variant without invalidating unrelated assets. Downstream stages are invalidated only when
their inputs change.

## Director policy

The director emits a validated `ProjectPlan`. It favors still images with controlled camera movement,
diagrams, title cards, and reusable media for explanatory coverage. Wan provides economical motion;
H3 is reserved for synchronized speech/action, reference consistency, native audiovisual output, and
high-impact hero shots. FFmpeg—not a generative model—assembles the timeline.

## GPU ownership

One heavyweight job runs at a time. Before acquisition the resource manager reads system-wide free
VRAM, because an external LLM may already occupy the GPU. Switching backends calls the active
backend's real `unload()` method, releases references, runs Python garbage collection, and only then
uses `torch.cuda.empty_cache()` when CUDA is available. Emptying the allocator cache is never treated
as model unloading.

The initial policy requires 20 GiB free for a heavy job and reports an actionable wait/fail response.
It never stops the external LLM. Device identifiers and resource leases are structured so a future
multi-GPU scheduler does not change generator requests.

## Backend isolation

The application's main Python/PyTorch environment is preserved. HTTP integrations,
orchestration, mock generation, and compatible libraries share it. ComfyUI and H3 are local services;
backends with incompatible Torch, CUDA, or Python pins use their own environment and cache rather than
changing the application environment.

## Failure and cancellation

Persistent jobs progress through `queued`, `preparing`, `loading_model`, `generating`,
`postprocessing`, and a terminal state (`completed`, `failed`, `canceled`). Execution never survives
a restart: the startup recovery pass fails every non-terminal job with an explanatory error instead
of requeueing it into a queue nobody drains. Retry re-enqueues and re-executes top-level stages
(`pipeline`, `render`, `narration`, thumbnail candidates); child-stage rows are bookkeeping driven by
their parent job and cannot be retried directly. Cancellation is cooperative; a backend is asked to
cancel, then its output is retained as a failed/rejected variant if a partial artifact exists.

## Trust boundaries

- Authorization values are resolved from environment variables at request time and redacted from
  exceptions and logs.
- Model/workflow paths are local and explicit; downloads are never implicit.
- Service identity is verified rather than inferred from an occupied port.
- FFmpeg subprocesses use argument arrays without a shell.
- Reference voice cloning requires an explicitly supplied, authorized file.
