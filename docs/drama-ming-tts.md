# Drama 3 Q4 and Ming 3 TTS

Local Video Studio supports three additional local narration providers:

| Provider | Runtime | Checkpoint | Approximate bundle |
| --- | --- | --- | --- |
| `fish_drama_3` | drama-rs v0.3.0 | apiplant/drama-q4 | 21.36 GiB |
| `ming_tts_tiny` | official Ming Python API | inclusionAI/Ming-3.0-tts-tiny | 16.77 GiB |
| `ming_tts_05b` | official Ming Python API | inclusionAI/Ming-3.0-tts-0.5B | 2.80 GiB |

Both Ming workers initially use the same Python environment: Tiny's dependency
pins plus the 0.5B-only packages. Upstream specifies different reference stacks,
so import success alone does not establish generation compatibility. Validate
both models with the smoke check below before relying on the shared stack. If
0.5B proves incompatible, configure its `python_path` independently; never
change the application's PyTorch, existing model services, drivers or CUDA.

## Explicit installation

Use an existing, user-configured writable AI storage root. The commands below
use `/path/to/ai`; replace it with that root. Setup never reads `.env`.

```bash
python scripts/setup_drama_ming.py plan --root /path/to/ai
python scripts/setup_drama_ming.py prepare --root /path/to/ai
python scripts/setup_drama_ming.py download --root /path/to/ai --confirm-download
```

`prepare` installs an isolated Python environment and the checksum-verified
Linux x86-64 drama-rs binary. It downloads no weights. It requires an existing
`uv`; missing development tools are not installed. It uses the official PyPI
and PyTorch package indexes to resolve the upstream CUDA 11.8 dependencies.

`download` resolves exact file sizes from pinned public metadata, checks the
combined allocation on each actual destination filesystem, and preserves
**50 GiB free plus a 1 GiB working margin**. Each file is streamed directly to
its target, with size/hash verification and an atomic completion receipt.
There is no duplicate Hugging Face model cache. `--provider` may be repeated
to install only selected providers. Incomplete downloads are not usable models.

Generation is offline and cannot invoke the installer. A missing receipt,
missing shard, insufficient disk reserve or insufficient free VRAM causes an
actionable error instead of an implicit download or CPU/cloud fallback.

## Configuration

Set these paths in ignored `config/local.yaml`, using your configured storage
root. Keep machine-specific paths out of tracked configuration.

```yaml
backends:
  fish_drama_3:
    python_path: /path/to/ai/services/ming-tts/.venv/bin/python
    model_path: /path/to/ai/models/tts/fish/drama-q4
    runtime_path: /path/to/ai/services/drama-rs/drama
    cache_path: /path/to/ai/cache/drama-ming
  ming_tts_tiny:
    python_path: /path/to/ai/services/ming-tts/.venv/bin/python
    source_path: /path/to/ai/services/ming-tts/source
    model_path: /path/to/ai/models/tts/ming/Ming-3.0-tts-tiny
    cache_path: /path/to/ai/cache/drama-ming
  ming_tts_05b:
    python_path: /path/to/ai/services/ming-tts/.venv/bin/python
    source_path: /path/to/ai/services/ming-tts/source
    model_path: /path/to/ai/models/tts/ming/Ming-3.0-tts-0.5B
    cache_path: /path/to/ai/cache/drama-ming
```

The default wrapper ports are 8201 (Drama), 8203 (Ming Tiny), and 8204 (Ming
0.5B). Drama owns a private child on 8202. Every endpoint binds to loopback;
port conflicts never cause another process to be stopped. Port 1234 remains
externally owned. The normal narration GPU lock serializes generation.

## Voice controls

Select the provider on the Voice page. All three can synthesize without a saved
profile. Ming Tiny uses `US_Emma` for reference-free plain narration. A cloned
voice must have an authorized reference; plain Ming/Drama cloning also requires
its exact transcript.

Ming offers Narration, Voice design and Freeform tasks. Voice design requires a
description and no reference. Freeform requires delivery direction and may use
a reference; Tiny's task protocol does not accept its transcript in that mode.
Select Freeform rather than plain narration to direct a Tiny voice. Optional
`pynini` normalization is disabled for 0.5B; upstream text preparation still
runs. Tiny retains its own canonical task protocol and sampling defaults.

Drama supports Fish-style inline performance cues and the existing performance
script editor. Prompts, seeds, runtime/model revisions, quantization, reference
hashes, worker metrics and output hashes follow the existing restartable
narration artifact pipeline. FFmpeg and measured WAV durations still determine
editing and scene timing.

The initial admission floors are 22 GiB free for Drama, 20 GiB for Tiny, and
6 GiB for 0.5B. These are conservative startup checks, not promised peak
measurements. Context, reference length and other GPU processes affect the
actual requirement. No external LLM is stopped automatically.

## Real-model smoke check

Run CUDA checks outside a sandbox that hides GPU access. Test one model at a
time; these commands release it when finished and never download weights:

```bash
/path/to/ai/services/ming-tts/.venv/bin/python scripts/check_drama_ming.py \
  --root /path/to/ai --provider ming_tts_tiny --output /path/to/ai/checks/tiny.wav
```

Repeat for `ming_tts_05b` and `fish_drama_3`. Use `--reference` and
`--reference-text` to check authorized voice cloning. Runtime package versions
and pinned model identifiers are recorded in the output's JSON sidecar.

Ming is MIT licensed. Drama is a community derivative subject to the model's
Fish Audio license; the Voice page displays “Built with Fish Audio”. The
installer retains the checkpoint's license and attribution files.

Sources: [Ming](https://github.com/inclusionAI/Ming-3.0-tts),
[drama-rs](https://github.com/apiplant/drama-rs),
[Drama Q4](https://huggingface.co/apiplant/drama-q4).
