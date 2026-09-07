"""Loopback-only, lazy-loading TTS worker.

Run this module with the matching isolated Python environment. The dashboard
never imports model packages and therefore cannot disturb ComfyUI's runtime.
"""

from __future__ import annotations

import argparse
import base64
import gc
import hashlib
import http.client
import io
import importlib.metadata
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib import error as _urllib_error
from urllib import request as _urllib_request

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


ProviderName = Literal[
    "qwen_tts", "step_audio_editx", "chatterbox", "omnivoice", "breeze_tts_2",
    "higgs_tts_3",
]


class GeneratePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str
    text: str = Field(min_length=1)
    output_path: Path
    reference_audio: Path | None = None
    reference_text: str = ""
    language: str = "en"
    seed: int = 0
    mode: str = "clone"
    source_audio: Path | None = None
    edit_type: str = "emotion"
    edit_instruction: str = ""
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    temperature: float = 0.8
    max_new_tokens: int | None = None
    speaker: str = "Ryan"
    voice_instruction: str = ""
    num_step: int | None = None
    guidance_scale: float | None = None
    speed: float | None = None
    breeze_mode: str = "fast"


class SpeechProvider(ABC):
    requires_reference = True

    def __init__(self, model_path: Path, tokenizer_path: Path | None = None) -> None:
        self.model_path = model_path
        self.tokenizer_path = tokenizer_path
        self.model: Any = None
        self.load_seconds: float | None = None

    @property
    @abstractmethod
    def name(self) -> ProviderName: ...

    @abstractmethod
    def _load(self) -> Any: ...

    @abstractmethod
    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]: ...

    def load(self) -> None:
        if self.model is not None:
            return
        if not self.model_path.is_dir():
            raise FileNotFoundError(f"model directory not found: {self.model_path}")
        started = time.monotonic()
        self.model = self._load()
        self.load_seconds = time.monotonic() - started

    def unload(self) -> None:
        self.model = None
        self._clear_conditioning()
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except (ImportError, RuntimeError):
            pass

    def _clear_conditioning(self) -> None:
        return None

    def _extra_metrics(self, payload: GeneratePayload) -> dict[str, Any]:
        """Provider-specific provenance recorded on every chunk."""
        return {}

    def generate(self, payload: GeneratePayload) -> dict[str, Any]:
        if self.requires_reference and (
            payload.reference_audio is None or not payload.reference_audio.is_file()
        ):
            raise ValueError("an existing authorized reference WAV is required")
        if payload.reference_audio is not None and not payload.reference_audio.is_file():
            raise ValueError("reference WAV does not exist")
        import numpy as np
        import soundfile as sf

        try:
            import torch
        except ImportError:
            torch = None

        self.load()
        if torch is not None:
            torch.manual_seed(payload.seed)
        if torch is not None and torch.cuda.is_available():
            torch.cuda.manual_seed_all(payload.seed)
            torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        waveform, sample_rate = self._generate(payload)
        elapsed = time.monotonic() - started
        if hasattr(waveform, "detach"):
            waveform = waveform.detach().float().cpu().numpy()
        audio = np.asarray(waveform, dtype=np.float32).squeeze()
        if audio.ndim != 1 or not audio.size:
            raise RuntimeError("model returned empty or unsupported audio")
        payload.output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(payload.output_path, audio, sample_rate, subtype="PCM_16")
        duration = len(audio) / sample_rate
        peak = current = 0.0
        if torch is not None and torch.cuda.is_available():
            peak = torch.cuda.max_memory_allocated() / 1024**3
            current = torch.cuda.memory_allocated() / 1024**3
        return {
            "output_path": str(payload.output_path),
            "metrics": {
                "model_load_seconds": self.load_seconds,
                "generation_seconds": elapsed,
                "audio_duration_seconds": duration,
                "real_time_factor": elapsed / duration if duration else None,
                "peak_vram_gb": peak,
                "current_vram_gb": current,
                "text_characters": len(payload.text),
                **self._extra_metrics(payload),
            },
        }


class QwenProvider(SpeechProvider):
    name: ProviderName = "qwen_tts"
    requires_reference = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prompt_key: str | None = None
        self._voice_prompt: Any = None
        self._requested_model_path: Path | None = None
        self._loaded_model_path: Path | None = None
        default_custom = self.model_path.with_name(
            self.model_path.name.replace("-Base", "-CustomVoice")
        )
        self.custom_voice_model_path = Path(
            os.environ.get("LVS_QWEN_CUSTOM_VOICE_MODEL", str(default_custom))
        ).expanduser()

    def load(self) -> None:
        # The worker supports two Qwen checkpoints. /load remains a lightweight
        # readiness check; the request determines which checkpoint is loaded.
        if self._requested_model_path is None:
            if not self.model_path.is_dir():
                raise FileNotFoundError(f"model directory not found: {self.model_path}")
            return
        if self.model is not None and self._loaded_model_path == self._requested_model_path:
            return
        if self.model is not None:
            self.unload()
        if not self._requested_model_path.is_dir():
            raise FileNotFoundError(
                "Qwen CustomVoice model directory is required for reference-free speech: "
                f"{self._requested_model_path}"
            )
        super().load()
        self._loaded_model_path = self._requested_model_path

    def generate(self, payload: GeneratePayload) -> dict[str, Any]:
        self._requested_model_path = (
            self.model_path if payload.reference_audio is not None else self.custom_voice_model_path
        )
        try:
            return super().generate(payload)
        finally:
            self._requested_model_path = None

    def _load(self) -> Any:
        import torch
        from qwen_tts import Qwen3TTSModel

        return Qwen3TTSModel.from_pretrained(
            str(self._requested_model_path or self.model_path),
            device_map="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa", local_files_only=True,
        )

    def _clear_conditioning(self) -> None:
        self._prompt_key = None
        self._voice_prompt = None
        self._loaded_model_path = None

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        language = {
            "en": "English", "de": "German", "fr": "French", "es": "Spanish",
            "it": "Italian", "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
            "zh": "Chinese", "ru": "Russian",
        }.get(payload.language.lower(), payload.language)
        kwargs: dict[str, Any] = {"temperature": payload.temperature}
        if payload.max_new_tokens is not None:
            kwargs["max_new_tokens"] = payload.max_new_tokens
        if payload.reference_audio is None:
            wavs, sample_rate = self.model.generate_custom_voice(
                text=payload.text, language=language, speaker=payload.speaker,
                instruct=payload.voice_instruction or None, **kwargs,
            )
            return wavs[0], sample_rate
        key = _reference_key(payload.reference_audio, payload.reference_text)
        if key != self._prompt_key:
            self._voice_prompt = self.model.create_voice_clone_prompt(
                ref_audio=str(payload.reference_audio),
                ref_text=payload.reference_text or None,
                x_vector_only_mode=not bool(payload.reference_text),
            )
            self._prompt_key = key
        wavs, sample_rate = self.model.generate_voice_clone(
            text=payload.text, language=language, voice_clone_prompt=self._voice_prompt, **kwargs,
        )
        return wavs[0], sample_rate


class ChatterboxProvider(SpeechProvider):
    name: ProviderName = "chatterbox"
    requires_reference = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prompt_key: str | None = None
        self._builtin_conds: Any = None

    def _load(self) -> Any:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        model = ChatterboxMultilingualTTS.from_local(
            self.model_path, "cuda", t3_model="t3_mtl23ls_v3.safetensors",
        )
        if model.conds is None:
            raise RuntimeError("Chatterbox built-in conditioning file conds.pt is missing")
        self._builtin_conds = model.conds
        return model

    def _clear_conditioning(self) -> None:
        self._prompt_key = None
        self._builtin_conds = None

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        if payload.reference_audio is not None:
            key = _reference_key(payload.reference_audio, str(payload.exaggeration))
            if key != self._prompt_key:
                self.model.prepare_conditionals(
                    str(payload.reference_audio), exaggeration=payload.exaggeration,
                )
                self._prompt_key = key
        else:
            self.model.conds = self._builtin_conds
            self._prompt_key = None
        waveform = self.model.generate(
            payload.text, language_id=payload.language, audio_prompt_path=None,
            exaggeration=payload.exaggeration, cfg_weight=payload.cfg_weight,
            temperature=payload.temperature,
        )
        return waveform, int(self.model.sr)


class StepProvider(SpeechProvider):
    name: ProviderName = "step_audio_editx"

    def unload(self) -> None:
        if self.model is not None:
            engine = getattr(getattr(self.model, "llm", None), "llm_engine", None)
            shutdown = getattr(engine, "shutdown", None)
            if callable(shutdown):
                shutdown()
        super().unload()

    def _load(self) -> Any:
        if self.tokenizer_path is None or not self.tokenizer_path.is_dir():
            raise FileNotFoundError("Step-Audio-Tokenizer directory is required")
        source = Path(os.environ.get(
            "LVS_STEP_AUDIO_SOURCE", str(Path.home() / "ai/services/Step-Audio-EditX"),
        ))
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        cache_root = Path(os.environ.get("LVS_AI_CACHE_ROOT", "~/ai/cache")).expanduser()
        hf_root = cache_root / "huggingface"
        os.environ.setdefault("TORCH_HOME", str(cache_root / "torch"))
        os.environ.setdefault("HF_HOME", str(hf_root))
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(hf_root / "hub"))
        os.environ.setdefault("HF_MODULES_CACHE", str(hf_root / "modules"))
        # Step1 uses alibi_sqrt, which the official project requires TRITON_ATTN for.
        os.environ["VLLM_ATTENTION_BACKEND"] = "TRITON_ATTN"
        from model_loader import ModelSource
        from tokenizer import StepAudioTokenizer
        from tts import StepAudioTTS

        # TorchAudio 2.9 delegates file I/O to TorchCodec, which needs system
        # FFmpeg shared libraries. Our boundary accepts PCM WAV only, so use
        # the already-installed libsndfile binding without changing TorchAudio.
        import numpy as np
        import soundfile as sf
        import torch
        import torchaudio

        def load_pcm_wav(path: str, *args: Any, **kwargs: Any) -> tuple[Any, int]:
            del args, kwargs
            audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
            return torch.from_numpy(np.asarray(audio).T.copy()), int(sample_rate)

        def save_pcm_wav(path: str, audio: Any, sample_rate: int, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            data = audio.detach().float().cpu().numpy()
            sf.write(path, data.T, sample_rate, subtype="PCM_16")

        torchaudio.load = load_pcm_wav
        torchaudio.save = save_pcm_wav

        tokenizer = StepAudioTokenizer(str(self.tokenizer_path), model_source=ModelSource.LOCAL)
        return StepAudioTTS(
            str(self.model_path), tokenizer, model_source=ModelSource.LOCAL,
            gpu_memory_utilization=0.5, max_model_len=8192, enforce_eager=True,
            dtype="bfloat16", max_num_seqs=1, cosyvoice_dtype="bfloat16",
            cosyvoice_cuda_graph=False,
        )

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        if payload.mode == "edit":
            source = payload.source_audio or payload.reference_audio
            return self.model.edit(
                str(source), payload.reference_text, payload.edit_type,
                payload.edit_instruction or None, payload.text or None,
            )
        if not payload.reference_text:
            raise ValueError("Step voice cloning requires the reference transcript")
        return self.model.clone(
            str(payload.reference_audio), payload.reference_text, payload.text,
        )


class BreezeProvider(SpeechProvider):
    """Thin adapter over the official ``breeze_infer.api`` server.

    One official API process is one engine mode (the fast flags are startup
    arguments and the model loads once in its lifespan), so this provider
    owns exactly one child process at a time on a dedicated loopback port —
    8196 for ``eager`` (≈7.7 GiB) or 8197 for ``fast`` (≈14.4 GiB, includes
    the official warmup) — and swaps it when the requested engine changes.
    The LVS worker protocol on the supervisor's port stays unchanged, and a
    later move to direct in-process integration only has to replace the
    innards of this class.

    Pinned checkout ``ca632ce6c4d05f7985da4eab29b1a5d445b43f7b`` of
    ``breezeblue-ai/breeze-tts``; weights ``BreezeBlue/Breeze-TTS-2`` at
    revision ``c1c8ca18b70b30822735633991d9ebf4898e47d4`` (non-commercial
    license). The contract used here (``GET /health`` → status ok/loading;
    ``POST /v1/audio/speech`` multipart form → PCM s16le stream with an
    ``X-Sample-Rate`` header; HTTP 409 while a request is in flight) was
    verified against that exact SHA.
    """

    name: ProviderName = "breeze_tts_2"
    requires_reference = True
    ENGINES = ("eager", "fast")
    _INSTRUCTION_FALLBACK = "Speak clearly and naturally."

    def __init__(self, model_path: Path, tokenizer_path: Path | None = None) -> None:
        super().__init__(model_path, tokenizer_path)
        self._child: subprocess.Popen[bytes] | None = None
        self._active_mode: str | None = None
        self._code_revision: str = "unrecorded"
        import atexit

        atexit.register(self._stop_child)

    # --- configuration (env-overridable, documented in docs/local-tts.md) ---

    @staticmethod
    def _source_root() -> Path:
        """Resolve the pinned checkout root.

        Order: explicit ``LVS_BREEZE_TTS_SOURCE``; the checkout that owns the
        venv this process runs in (``<checkout>/.venv/bin/python`` — how the
        supervisor launches the worker, so relocated checkouts work without
        any extra environment); finally the ``~/ai/services/breeze-tts``
        default.
        """
        env_value = os.environ.get("LVS_BREEZE_TTS_SOURCE")
        if env_value:
            return Path(env_value).expanduser()
        venv_root = BreezeProvider._venv_checkout_root()
        if venv_root is not None:
            return venv_root
        return Path.home() / "ai/services/breeze-tts"

    @staticmethod
    def _venv_checkout_root() -> Path | None:
        """Infer the checkout root when running under the checkout's own venv."""
        exe = Path(sys.executable)
        if exe.parent.name in ("bin", "Scripts") and exe.parent.parent.name == ".venv":
            candidate = exe.parent.parent.parent
            if (candidate / "breeze_infer" / "api.py").is_file():
                return candidate
        return None

    @staticmethod
    def _port_for(mode: str) -> int:
        env = "LVS_BREEZE_EAGER_PORT" if mode == "eager" else "LVS_BREEZE_FAST_PORT"
        return int(os.environ.get(env, 8196 if mode == "eager" else 8197))

    @staticmethod
    def _min_free_gb(mode: str) -> float:
        env = "LVS_BREEZE_EAGER_MIN_FREE_GB" if mode == "eager" else "LVS_BREEZE_FAST_MIN_FREE_GB"
        return float(os.environ.get(env, 10.0 if mode == "eager" else 20.0))

    @staticmethod
    def _free_vram_gb() -> float | None:
        try:
            import torch

            if torch.cuda.is_available():
                free, _total = torch.cuda.mem_get_info()
                return free / 1024**3
        except (ImportError, RuntimeError, OSError):
            return None
        return None

    # --- lifecycle -----------------------------------------------------------

    def _load(self) -> Any:
        source = self._source_root()
        if not (source / "breeze_infer" / "api.py").is_file():
            raise FileNotFoundError(
                f"breeze-tts checkout not found at {source} "
                "(set LVS_BREEZE_TTS_SOURCE to the checkout root)"
            )
        self._code_revision = self._git_revision(source)
        return True

    @staticmethod
    def _git_revision(source: Path) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
            return out[:12] if out else "unrecorded"
        except (OSError, subprocess.SubprocessError):
            return os.environ.get("LVS_BREEZE_TTS_SHA", "unrecorded")[:12]

    def _hf_revision(self) -> str:
        pin = self.model_path / "lvs-pinned-revision.json"
        try:
            data = json.loads(pin.read_text(encoding="utf-8"))
            return str(data.get("revision", "unrecorded"))
        except (OSError, ValueError):
            return "unrecorded"

    def unload(self) -> None:
        self._stop_child()
        super().unload()

    def _stop_child(self) -> None:
        child = self._child
        self._child = None
        self._active_mode = None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # --- engine management ---------------------------------------------------

    def _spawn(
        self, command: list[str], environment: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        # The single injection seam for the official-API child process; tests
        # replace this to avoid launching a real model server.
        return subprocess.Popen(
            command, cwd=str(self._source_root()),
            env=environment if environment is not None else os.environ.copy(),
            stdin=subprocess.DEVNULL,
        )

    def _ensure_mode(self, mode: str) -> str:
        if mode not in self.ENGINES:
            raise ValueError(f"breeze_mode must be one of {list(self.ENGINES)}")
        if (
            self._active_mode == mode
            and self._child is not None
            and self._child.poll() is None
        ):
            return mode
        self._stop_child()
        free = self._free_vram_gb()
        minimum = self._min_free_gb(mode)
        if free is not None and free < minimum:
            other = "fast" if mode == "eager" else "eager"
            raise RuntimeError(
                f"Breeze {mode} engine needs ~{minimum:g} GiB free VRAM "
                f"({free:.1f} GiB free); try the {other} engine or free VRAM."
            )
        port = self._port_for(mode)
        command = [
            sys.executable, "-m", "breeze_infer.api", str(self.model_path),
            "--host", "127.0.0.1", "--port", str(port),
        ]
        if mode == "fast":
            # The pinned upstream warmup profile freezes backbone-prefill
            # graphs but omits valid no-CFG sequence buckets used by ordinary
            # narration (including 480+ tokens). Enable the other optimized
            # stages individually so fast mode cannot abort a response on an
            # undeclared prefill graph.
            command.extend((
                "--fast-text-encoder",
                "--fast-backbone-decode",
                "--fast-depth-decoder",
                "--fast-codec",
            ))
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            self._child = self._spawn(command, environment)
        except Exception:
            self._child = None
            raise
        self._active_mode = mode
        try:
            self._wait_healthy(port)
        except Exception:
            self._stop_child()
            raise
        return mode

    def _wait_healthy(self, port: int) -> None:
        timeout = float(os.environ.get("LVS_BREEZE_STARTUP_TIMEOUT_SECONDS", "240"))
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{port}/health"
        last_error = "no response yet"
        while time.monotonic() < deadline:
            if self._child is not None and self._child.poll() is not None:
                raise RuntimeError(
                    f"Breeze {self._active_mode} API exited during startup "
                    f"with code {self._child.returncode}; see the worker log."
                )
            try:
                with _urllib_request.urlopen(url, timeout=2) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    if isinstance(body, dict) and body.get("status") == "ok":
                        return
            except Exception as exc:  # 503 while loading, refused, ... retry.
                last_error = str(exc)
            time.sleep(0.5)
        raise RuntimeError(f"Timed out starting the Breeze API ({last_error}).")

    # --- inference -----------------------------------------------------------

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        import numpy as np

        mode = self._ensure_mode(str(payload.breeze_mode or "fast"))
        instruction = (payload.voice_instruction or "").strip() or self._INSTRUCTION_FALLBACK
        cfg_scale = (
            float(payload.guidance_scale)
            if payload.guidance_scale is not None
            else (4.0 if (payload.voice_instruction or "").strip() else 1.0)
        )
        assert payload.reference_audio is not None  # enforced by the base class
        reference_bytes = payload.reference_audio.read_bytes()
        fields = {
            "text": payload.text,
            "instruction": instruction,
            "cfg_scale": str(cfg_scale),
            "ref_text": payload.reference_text or "",
            "seed": str(payload.seed),
        }
        body, content_type = self._multipart(fields, reference_bytes,
                                             payload.reference_audio.name)
        url = f"http://127.0.0.1:{self._port_for(mode)}/v1/audio/speech"
        deadline = time.monotonic() + float(
            os.environ.get("LVS_BREEZE_GENERATION_TIMEOUT_SECONDS", "1800")
        )
        audio_bytes: bytes | None = None
        sample_rate = 24000
        while audio_bytes is None:
            request = _urllib_request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": content_type},
            )
            try:
                with _urllib_request.urlopen(request, timeout=3600) as response:
                    audio_bytes = response.read()
                    sample_rate = int(response.headers.get("X-Sample-Rate", "24000"))
            except _urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code == 409 and time.monotonic() < deadline:
                    # The official API is single-flight; wait for the slot.
                    time.sleep(0.5)
                    continue
                raise RuntimeError(f"Breeze API returned HTTP {exc.code}: {detail}") from None
            except http.client.IncompleteRead as exc:
                raise RuntimeError(
                    "Breeze API ended its audio stream before completion; "
                    "see the Breeze worker log for the inference error."
                ) from exc
            except (OSError, _urllib_error.URLError) as exc:
                if time.monotonic() < deadline:
                    time.sleep(1.0)
                    continue
                raise RuntimeError(f"Breeze API unreachable at {url}: {exc}") from None
        if not audio_bytes:
            raise RuntimeError("Breeze API returned an empty audio payload")
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32767.0
        if not audio.size:
            raise RuntimeError("Breeze API returned an empty audio payload")
        return audio, int(sample_rate)

    @staticmethod
    def _multipart(
        fields: dict[str, str], file_bytes: bytes, filename: str,
    ) -> tuple[bytes, str]:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for key, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                f"{value}\r\n".encode("utf-8")
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"ref_audio\"; "
            f"filename=\"{filename}\"\r\nContent-Type: audio/wav\r\n\r\n"
            .encode("utf-8")
        )
        parts.append(file_bytes + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def _extra_metrics(self, payload: GeneratePayload) -> dict[str, Any]:
        instruction = (payload.voice_instruction or "").strip()
        cfg_scale = (
            float(payload.guidance_scale)
            if payload.guidance_scale is not None
            else (4.0 if instruction else 1.0)
        )
        return {
            "breeze_mode": str(payload.breeze_mode or "fast"),
            "breeze_cfg_scale_used": cfg_scale,
            "breeze_code_revision": self._code_revision,
            "breeze_hf_revision": self._hf_revision(),
        }


class HiggsTTS3Provider(SpeechProvider):
    """Local adapter for Boson AI Higgs TTS 3 through SGLang-Omni.

    SGLang-Omni has a separate, tightly pinned Torch stack, so the LVS worker
    starts it as a child of the isolated provider process. Reference clips are
    passed as data URLs: the child stays loopback-only and receives no blanket
    permission to read project files.
    """

    name: ProviderName = "higgs_tts_3"
    requires_reference = False
    # Higgs emits 25 codec frames/s plus seven codebook delay steps.
    # Allow the UI's 180-second chunks with room for slower delivery.
    MAX_AUDIO_TOKENS = 8192
    AUDIO_FRAMES_PER_SECOND = 25

    @classmethod
    def _token_budget(cls, payload: GeneratePayload) -> int:
        if payload.max_new_tokens is not None:
            budget = payload.max_new_tokens
        else:
            spoken = re.sub(r"<\|[^|\n]*\|>", " ", payload.text)
            # Match the chunker's 2.5 words/s with 50% headroom for slow
            # delivery and pauses, plus ten seconds of fixed allowance.
            seconds = len(spoken.split()) / 2.5 * 1.5 + 10
            budget = max(1024, math.ceil(seconds * cls.AUDIO_FRAMES_PER_SECOND) + 7)
        if not 1 <= budget <= cls.MAX_AUDIO_TOKENS:
            raise ValueError(
                "Higgs audio token budget is outside 1–8192; use shorter narration chunks."
            )
        return budget

    def __init__(self, model_path: Path, tokenizer_path: Path | None = None) -> None:
        super().__init__(model_path, tokenizer_path)
        self._child: subprocess.Popen[bytes] | None = None
        self._runtime_version = "unrecorded"
        import atexit

        atexit.register(self._stop_child)

    @staticmethod
    def _port() -> int:
        return int(os.environ.get("LVS_HIGGS_TTS_ENGINE_PORT", "8199"))

    @staticmethod
    def _min_free_gb() -> float:
        return float(os.environ.get("LVS_HIGGS_TTS_MIN_FREE_GB", "18"))

    @staticmethod
    def _free_vram_gb() -> float | None:
        try:
            import torch

            if torch.cuda.is_available():
                free, _total = torch.cuda.mem_get_info()
                return free / 1024**3
        except (ImportError, RuntimeError, OSError):
            return None
        return None

    @staticmethod
    def _server_executable() -> Path:
        return Path(sys.executable).with_name("sgl-omni")

    def _spawn(self, command: list[str], environment: dict[str, str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            command, env=environment, stdin=subprocess.DEVNULL, start_new_session=True,
        )

    def _load(self) -> Any:
        executable = self._server_executable()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FileNotFoundError(
                f"SGLang-Omni launcher is unavailable: {executable}"
            )
        free = self._free_vram_gb()
        minimum = self._min_free_gb()
        if free is not None and free < minimum:
            raise RuntimeError(
                f"Higgs TTS 3 needs about {minimum:g} GiB free VRAM to start safely "
                f"({free:.1f} GiB free)."
            )
        port = self._port()
        with socket.socket() as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(
                    f"Higgs engine port {port} is already occupied; the worker will not "
                    "reuse or stop an externally owned process."
                )
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        venv_root = Path(sys.executable).parent.parent
        bundled_cuda_roots = sorted(
            (venv_root / "lib").glob("python*/site-packages/nvidia/cu13")
        )
        cuda_root_value = os.environ.get("LVS_HIGGS_TTS_CUDA_HOME")
        if not cuda_root_value and bundled_cuda_roots:
            cuda_root_value = str(bundled_cuda_roots[0])
        if cuda_root_value:
            cuda_root = Path(cuda_root_value).expanduser()
            bundled_nvcc = cuda_root / "bin" / "nvcc"
            if bundled_nvcc.is_file():
                environment["CUDA_HOME"] = str(cuda_root)
                environment["CUDACXX"] = str(bundled_nvcc)
                environment["PATH"] = (
                    f"{cuda_root / 'bin'}{os.pathsep}{environment.get('PATH', '')}"
                )
        # FlashInfer compiles CUDA kernels on first load. Prefer a compatible
        # host compiler when it is installed, while respecting explicit
        # operator choices on machines with a different CUDA toolchain.
        compatible_cc = os.environ.get("LVS_HIGGS_TTS_CC") or shutil.which("gcc-13")
        compatible_cxx = os.environ.get("LVS_HIGGS_TTS_CXX") or shutil.which("g++-13")
        if compatible_cc and compatible_cxx:
            environment.setdefault("CC", compatible_cc)
            environment.setdefault("CXX", compatible_cxx)
            environment.setdefault("NVCC_CCBIN", compatible_cxx)
            environment.setdefault("NVCC_PREPEND_FLAGS", f"-ccbin={compatible_cxx}")
        system_libstdcpp = os.environ.get("LVS_HIGGS_TTS_LIBSTDCXX")
        if not system_libstdcpp:
            candidate = Path("/usr/lib/x86_64-linux-gnu/libstdc++.so.6")
            if candidate.is_file():
                system_libstdcpp = str(candidate)
        if system_libstdcpp:
            environment.setdefault("LD_PRELOAD", system_libstdcpp)
        command = [
            str(executable), "serve", "--model-path", str(self.model_path),
            "--model-name", "higgs_tts_3",
            "--host", "127.0.0.1", "--port", str(port),
            "--mem-fraction-static",
            os.environ.get("LVS_HIGGS_TTS_MEM_FRACTION_STATIC", "0.65"),
            "--tts_engine.factory.max_new_tokens", str(self.MAX_AUDIO_TOKENS),
        ]
        try:
            self._child = self._spawn(command, environment)
            self._wait_healthy(port)
            try:
                self._runtime_version = importlib.metadata.version("sglang-omni")
            except importlib.metadata.PackageNotFoundError:
                self._runtime_version = "unrecorded"
        except Exception:
            self._stop_child()
            raise
        return True

    def unload(self) -> None:
        self._stop_child()
        super().unload()

    def _stop_child(self) -> None:
        child = self._child
        self._child = None
        if child is None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            if child.poll() is None:
                child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        # The SGLang launcher can exit before its multiprocessing stage
        # workers. Kill any remainder in the private child process group.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        if child.poll() is None:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _wait_healthy(self, port: int) -> None:
        timeout = float(os.environ.get("LVS_HIGGS_TTS_STARTUP_TIMEOUT_SECONDS", "600"))
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{port}/health"
        last_error = "no response yet"
        while time.monotonic() < deadline:
            if self._child is not None and self._child.poll() is not None:
                raise RuntimeError(
                    f"Higgs SGLang-Omni engine exited during startup with code "
                    f"{self._child.returncode}; see the worker log."
                )
            try:
                with _urllib_request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise RuntimeError(f"Timed out starting the Higgs engine ({last_error}).")

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        import numpy as np
        import soundfile as sf

        body: dict[str, Any] = {
            "model": "higgs_tts_3",
            "voice": "default",
            "input": payload.text,
            "response_format": "wav",
            "temperature": payload.temperature,
            "top_k": 50,
            "max_new_tokens": self._token_budget(payload),
            "seed": payload.seed,
        }
        if payload.reference_audio is not None:
            encoded = base64.b64encode(payload.reference_audio.read_bytes()).decode("ascii")
            body["references"] = [{
                "audio_path": f"data:audio/wav;base64,{encoded}",
                "text": payload.reference_text or "",
            }]
        request = _urllib_request.Request(
            f"http://127.0.0.1:{self._port()}/v1/audio/speech",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with _urllib_request.urlopen(request, timeout=1800) as response:
                audio_bytes = response.read()
        except _urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise RuntimeError(
                f"Higgs SGLang-Omni API returned HTTP {exc.code}: {detail}"
            ) from None
        except (OSError, _urllib_error.URLError) as exc:
            raise RuntimeError(f"Higgs SGLang-Omni API is unreachable: {exc}") from None
        if not audio_bytes:
            raise RuntimeError("Higgs SGLang-Omni API returned empty audio")
        try:
            audio, sample_rate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        except Exception as exc:
            raise RuntimeError(f"Higgs API returned invalid WAV audio: {exc}") from None
        # The WAV endpoint does not expose a finish reason. A waveform at the
        # token ceiling is likely incomplete; do not silently activate it.
        ceiling_seconds = (body["max_new_tokens"] - 7) / self.AUDIO_FRAMES_PER_SECOND
        if len(audio) / sample_rate >= ceiling_seconds - 0.08:
            raise RuntimeError(
                "Higgs reached its audio token limit; narration may be cut off. "
                "Use shorter chunks and regenerate."
            )
        return np.asarray(audio, dtype=np.float32), int(sample_rate)

    def _extra_metrics(self, payload: GeneratePayload) -> dict[str, Any]:
        return {
            "higgs_runtime": "sglang-omni",
            "higgs_runtime_version": self._runtime_version,
            "higgs_hf_revision": self._hf_revision(),
            "higgs_reference_mode": "clone" if payload.reference_audio else "default",
            "higgs_max_new_tokens": self._token_budget(payload),
            "higgs_engine_max_new_tokens": self.MAX_AUDIO_TOKENS,
            "license": "Boson Higgs TTS 3 Research and Non-Commercial License",
            "creator_attribution_required": True,
        }

    def _hf_revision(self) -> str:
        pin = self.model_path / "lvs-pinned-revision.json"
        try:
            data = json.loads(pin.read_text(encoding="utf-8"))
            return str(data.get("revision", "unrecorded"))
        except (OSError, ValueError):
            return "unrecorded"


def _reference_key(path: Path, extra: str) -> str:
    stat = path.stat()
    return hashlib.sha256(
        f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{extra}".encode()
    ).hexdigest()


class OmniVoiceProvider(SpeechProvider):
    """Official k2-fsa/OmniVoice Python API in this isolated worker process."""

    name: ProviderName = "omnivoice"
    requires_reference = True

    def _load(self) -> Any:
        import torch

        source = Path(
            os.environ.get(
                "LVS_OMNIVOICE_SOURCE", str(Path.home() / "ai/services/OmniVoice"),
            )
        ).expanduser()
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        cache_root = Path(os.environ.get("LVS_AI_CACHE_ROOT", "~/ai/cache")).expanduser()
        hf_root = cache_root / "huggingface"
        os.environ.setdefault("TORCH_HOME", str(cache_root / "torch"))
        os.environ.setdefault("HF_HOME", str(hf_root))
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(hf_root / "hub"))
        from omnivoice.models.omnivoice import OmniVoice

        device = "cuda" if torch.cuda.is_available() else "cpu"
        return OmniVoice.from_pretrained(
            str(self.model_path), device_map=device, dtype=torch.float16
        )

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        kwargs: dict[str, Any] = {}
        if payload.num_step is not None:
            kwargs["num_step"] = payload.num_step
        if payload.guidance_scale is not None:
            kwargs["guidance_scale"] = payload.guidance_scale
        if payload.speed is not None:
            kwargs["speed"] = payload.speed
        audios = self.model.generate(
            text=payload.text,
            language=payload.language or None,
            ref_audio=str(payload.reference_audio),
            ref_text=payload.reference_text or None,
            instruct=payload.voice_instruction or None,
            **kwargs,
        )
        return audios[0], int(self.model.sampling_rate)


def create_app(provider: SpeechProvider, *, output_root: Path) -> FastAPI:
    root = output_root.expanduser().resolve()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        provider.unload()

    app = FastAPI(
        title=f"Local Video Studio {provider.name} worker", version="1", lifespan=lifespan,
    )
    canceled: set[str] = set()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "healthy", "provider": provider.name,
            "loaded": provider.model is not None, "model_path": str(provider.model_path),
        }

    @app.post("/load")
    def load() -> dict[str, Any]:
        try:
            provider.load()
            return {"provider": provider.name, "loaded": True, "load_seconds": provider.load_seconds}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)[:1000]) from None

    @app.post("/unload")
    def unload() -> dict[str, Any]:
        provider.unload()
        return {"provider": provider.name, "loaded": False}

    @app.post("/generate")
    def generate(payload: GeneratePayload) -> dict[str, Any]:
        output = payload.output_path.expanduser().resolve()
        if output.suffix.lower() != ".wav" or (output != root and root not in output.parents):
            raise HTTPException(status_code=422, detail="output_path must be a WAV inside output root")
        for source in (payload.reference_audio, payload.source_audio):
            if source is not None:
                resolved = source.expanduser().resolve()
                if resolved != root and root not in resolved.parents:
                    raise HTTPException(status_code=422, detail="audio inputs must be inside output root")
        if payload.job_id in canceled:
            raise HTTPException(status_code=409, detail="job was canceled")
        try:
            return provider.generate(payload.model_copy(update={"output_path": output}))
        except RuntimeError as exc:
            message = str(exc)
            code = 507 if "out of memory" in message.lower() else 500
            raise HTTPException(status_code=code, detail=message[:1000]) from None
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:1000]) from None

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, bool]:
        canceled.add(job_id)
        return {"canceled": True}

    @app.post("/jobs/{job_id}/reset_cancel")
    def reset_cancel(job_id: str) -> dict[str, bool]:
        canceled.discard(job_id)
        return {"canceled": False}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one isolated local TTS provider")
    parser.add_argument(
        "--provider",
        choices=(
            "qwen_tts", "step_audio_editx", "chatterbox", "omnivoice",
            "breeze_tts_2", "higgs_tts_3",
        ),
        required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("~/ai/projects"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("TTS workers must bind to loopback")
    classes = {
        "qwen_tts": QwenProvider,
        "step_audio_editx": StepProvider,
        "chatterbox": ChatterboxProvider,
        "omnivoice": OmniVoiceProvider,
        "breeze_tts_2": BreezeProvider,
        "higgs_tts_3": HiggsTTS3Provider,
    }
    provider = classes[args.provider](args.model_path.expanduser(), args.tokenizer_path)
    uvicorn.run(create_app(provider, output_root=args.output_root), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
