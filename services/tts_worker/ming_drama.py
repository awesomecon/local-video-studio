"""Offline adapters for pinned Ming Python models and the drama-rs Q4 server.

Model libraries are imported only inside the isolated worker. No model lookup
or implicit download is permitted; the installer writes a complete-file receipt.
"""
from __future__ import annotations

import hashlib
import io
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from services.tts_worker.app import GeneratePayload, SpeechProvider
from services.tts_worker.speech_releases import DRAMA_REVISION, MING_REVISION, RELEASES


def validate_bundle(path: Path, provider: str) -> dict[str, Any]:
    """Check the installer receipt and all sizes before invoking upstream code."""
    try:
        receipt = json.loads((path / "lvs-release.json").read_text())
    except (OSError, ValueError):
        raise FileNotFoundError("Incomplete speech model: run scripts/setup_drama_ming.py download.") from None
    repo, revision, _ = RELEASES[provider]
    if receipt.get("repo") != repo or receipt.get("revision") != revision or not receipt.get("files"):
        raise ValueError("Speech checkpoint receipt does not match the pinned release.")
    for entry in receipt["files"]:
        file = (path / entry["path"]).resolve()
        if not file.is_relative_to(path.resolve()) or not file.is_file() or file.stat().st_size != entry["size"]:
            raise FileNotFoundError("Speech checkpoint is incomplete; reinstall the pinned release.")
    return receipt


def require_vram(minimum: float) -> None:
    """Inspect the configured single GPU, including allocations by other apps."""
    device = os.environ.get("CUDA_VISIBLE_DEVICES", "0").split(",")[0]
    result = subprocess.run(
        ["nvidia-smi", "--id", device, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if result.returncode:
        raise RuntimeError("Cannot inspect system-wide free VRAM; speech model was not loaded.")
    free = float(result.stdout.strip()) / 1024
    if free < minimum:
        raise RuntimeError(
            f"Needs {minimum:g} GiB free VRAM; {free:.1f} GiB is available. "
            "Unload other GPU models and retry. No external process was stopped."
        )


class PinnedSpeechProvider(SpeechProvider):
    requires_reference = False
    minimum_vram = 6.0

    def __init__(self, model_path: Path, *, source_path: Path | None = None,
                 runtime_path: Path | None = None, runtime_port: int | None = None) -> None:
        super().__init__(model_path)
        self.source_path = source_path
        self.runtime_path = runtime_path
        self.runtime_port = runtime_port
        self._receipt: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._canceled: set[str] = set()
        self._active_job: str | None = None

    def load(self) -> None:
        with self._lock:
            if self.model is None:
                self._receipt = validate_bundle(self.model_path, self.name)
                for target in (self.model_path, Path(os.environ.get("LVS_AI_CACHE_ROOT", self.model_path))):
                    if shutil.disk_usage(target).free < 50 * 1024**3:
                        raise RuntimeError("Speech model/cache target has less than the required 50 GiB free.")
                require_vram(self.minimum_vram)
                os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                   "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"})
                super().load()

    def generate(self, payload: GeneratePayload) -> dict[str, Any]:
        with self._lock:
            if payload.job_id in self._canceled:
                raise ValueError("Speech job was canceled.")
            self._active_job = payload.job_id
            try:
                result = super().generate(payload)
                if payload.job_id in self._canceled:
                    payload.output_path.unlink(missing_ok=True)
                    raise ValueError("Speech job was canceled.")
                return result
            finally:
                self._active_job = None

    def cancel(self, job_id: str) -> None:
        self._canceled.add(job_id)

    def reset_cancel(self, job_id: str) -> None:
        self._canceled.discard(job_id)

    def unload(self) -> None:
        with self._lock:
            super().unload()

    def _extra_metrics(self, payload: GeneratePayload) -> dict[str, Any]:
        reference_hash = None
        if payload.reference_audio:
            reference_hash = hashlib.sha256(payload.reference_audio.read_bytes()).hexdigest()
        return {"provenance": {
            "model_repo": RELEASES[self.name][0], "model_revision": RELEASES[self.name][1],
            "runtime_revision": DRAMA_REVISION if self.name == "fish_drama_3" else MING_REVISION,
            "quantization": "Q4 experts / INT8 dense" if self.name == "fish_drama_3" else "bf16",
            "reference_sha256": reference_hash,
            "bundle_receipt_sha256": hashlib.sha256(
                json.dumps(self._receipt, sort_keys=True).encode()).hexdigest(),
            "workflow_version": "drama-ming-worker-v1",
            "python_packages": {
                name: importlib.metadata.version(name)
                for name in ("torch", "torchaudio", "transformers", "x-transformers")
            } if self.name.startswith("ming_tts_") else {},
        }}


class MingSmallProvider(PinnedSpeechProvider):
    name = "ming_tts_05b"

    def _extra_metrics(self, payload: GeneratePayload) -> dict[str, Any]:
        return {**super()._extra_metrics(payload), "effective_sampling": {
            "temperature": 0.0, "guidance_scale": payload.guidance_scale if payload.guidance_scale is not None else 2.0,
            "sigma": 0.25, "flow_steps": 10 if self.name == "ming_tts_tiny" else None,
            "task": payload.ming_task,
        }}

    def _source(self) -> None:
        if self.source_path is None or not (self.source_path / "ming_tts.py").is_file():
            raise FileNotFoundError("Configure the pinned Ming source_path.")
        if str(self.source_path) not in sys.path:
            sys.path.insert(0, str(self.source_path))

    def _load(self) -> Any:
        self._source()
        from loguru import logger
        from ming_tts import MingTTS

        # Upstream logs the full text/reference prompt at INFO. Keep media and
        # narration out of worker logs without changing its pinned source.
        logger.disable("modeling_bailingmm")

        # Disable the optional pynini frontend; both models keep their own
        # trained text/task protocol. Disable cached stochastic references so
        # a seed does not depend on whether an earlier request warmed a cache.
        return MingTTS(str(self.model_path), device="cuda:0", text_normalization=False,
                       cache_prompts=False)

    @staticmethod
    def _validate_task(payload: GeneratePayload) -> None:
        if payload.ming_task != "tts" and not payload.voice_instruction.strip():
            raise ValueError("Ming voice design/freeform requires a voice instruction.")
        if payload.ming_task == "voice_design" and payload.reference_audio:
            raise ValueError("Ming voice design creates a new voice; deselect the reference profile.")
        if payload.ming_task == "tts" and payload.reference_audio and not payload.reference_text.strip():
            raise ValueError("Ming voice cloning requires the exact reference transcript.")

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        self._validate_task(payload)
        task = {"tts": "TTS", "voice_design": "VOICE_DESIGN", "freeform": "FREEFORM"}[payload.ming_task]
        kwargs: dict[str, Any] = {}
        if task == "VOICE_DESIGN":
            kwargs["controls"] = {"style": payload.voice_instruction}
        elif payload.voice_instruction.strip():
            kwargs["instruction"] = payload.voice_instruction
        if payload.guidance_scale is not None:
            kwargs["cfg"] = payload.guidance_scale
        waveform = self.model.synthesize(
            task=task, text=payload.text, language_id=payload.language,
            prompt_wav_path=str(payload.reference_audio) if payload.reference_audio else None,
            prompt_text=payload.reference_text or None,
            use_spk_emb=payload.reference_audio is not None, **kwargs,
        )
        return waveform, int(self.model.sample_rate)


class MingTinyProvider(MingSmallProvider):
    name = "ming_tts_tiny"
    minimum_vram = 20.0  # Provisional admission floor, not a measured peak.

    def _load(self) -> Any:
        self._source()
        from ming_tiny.api import MingTinyTTS

        return MingTinyTTS.from_pretrained(str(self.model_path), device="cuda:0")

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        self._validate_task(payload)
        kwargs: dict[str, Any] = {}
        task = payload.ming_task
        if task == "tts":
            if payload.voice_instruction.strip():
                raise ValueError("Select Freeform to direct a Ming Tiny voice.")
            if payload.reference_audio:
                task = "seed" if payload.language in {"en", "zh"} else "multilingual"
            else:
                task = "ip"
                kwargs["ip"] = "US_Emma"
        if payload.ming_task == "voice_design":
            kwargs["style"] = payload.voice_instruction
        elif payload.voice_instruction.strip():
            kwargs["instruction"] = payload.voice_instruction
        if payload.guidance_scale is not None:
            kwargs["cfg_scale"] = payload.guidance_scale
        output = self.model.generate(
            task=task,
            text=payload.text,
            target_language=payload.language if task in {"seed", "multilingual"} else None,
            seed=payload.seed,
            prompt_wav=str(payload.reference_audio) if payload.reference_audio else None,
            prompt_text=(payload.reference_text or None) if task in {"seed", "multilingual"} else None,
            **kwargs,
        )
        return output.waveform, int(self.model.sample_rate)


class DramaProvider(PinnedSpeechProvider):
    name = "fish_drama_3"
    minimum_vram = 22.0

    def _extra_metrics(self, payload: GeneratePayload) -> dict[str, Any]:
        # CUDA allocations belong to the Rust child, not this Python worker.
        # Never present this process's zero Torch allocation as Rust's peak.
        return {**super()._extra_metrics(payload), "peak_vram_gb": None,
                "current_vram_gb": None, "vram_accounting": "external-process-unmeasured"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._child: subprocess.Popen | None = None
        self._registered_voices: set[str] = set()
        import atexit

        atexit.register(self._stop_child)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.runtime_port or 8202}"

    def _load(self) -> Any:
        if self.runtime_path is None or not os.access(self.runtime_path, os.X_OK):
            raise FileNotFoundError("Configure the drama-rs executable runtime_path.")
        required = ["drama.json", "codec.safetensors", "globals.safetensors", "tower.safetensors",
                    "tokenizer.json", *(f"layer{i:02d}.safetensors" for i in range(40))]
        if not all((self.model_path / file).is_file() for file in required):
            raise FileNotFoundError("Incomplete Drama Q4 bundle; implicit downloads are disabled.")
        port = self.runtime_port or 8202
        if not 1024 <= port <= 65535 or port == 1234:
            raise ValueError("Drama engine requires a non-reserved local port.")
        with socket.socket() as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("Drama engine port is occupied; no external process was stopped.")
        self._child = subprocess.Popen(
            [str(self.runtime_path), "serve", "--model", str(self.model_path), "--experts", "q4",
             "--addr", f"127.0.0.1:{port}", "--max-len", "2048"],
            stdin=subprocess.DEVNULL, env=os.environ.copy(),
        )
        try:
            deadline = time.monotonic() + 110
            with httpx.Client(base_url=self.endpoint, timeout=1, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if self._child.poll() is not None:
                        raise RuntimeError("drama-rs exited during loading; inspect the local worker log.")
                    try:
                        if client.get("/health").is_success:
                            return self._child
                    except httpx.TransportError:
                        pass
                    time.sleep(0.25)
            raise RuntimeError("Timed out loading Drama Q4.")
        except BaseException:
            self._stop_child()
            raise

    def _stop_child(self) -> None:
        child, self._child = self._child, None
        self._registered_voices.clear()
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def unload(self) -> None:
        with self._lock:
            self._stop_child()
            super().unload()

    def cancel(self, job_id: str) -> None:
        super().cancel(job_id)
        if self._active_job == job_id:
            self._stop_child()
            self.model = None

    def _generate(self, payload: GeneratePayload) -> tuple[Any, int]:
        import soundfile as sf

        body: dict[str, Any] = {"text": payload.text, "seed": payload.seed,
                                "temperature": payload.temperature}
        with httpx.Client(base_url=self.endpoint, timeout=900, trust_env=False) as client:
            if payload.reference_audio:
                if not payload.reference_text.strip():
                    raise ValueError("Drama cloning requires the exact reference transcript.")
                # Upstream requires query metadata. Use a private, non-logging
                # HTTP transport so transcripts cannot enter HTTP client logs.
                voice = hashlib.sha256(payload.reference_audio.read_bytes()
                                       + payload.reference_text.encode()).hexdigest()
                if voice not in self._registered_voices:
                    from urllib.parse import urlencode
                    import http.client

                    connection = http.client.HTTPConnection("127.0.0.1", self.runtime_port or 8202, timeout=120)
                    try:
                        connection.request("POST", "/v1/voices?" + urlencode({"name": voice, "text": payload.reference_text}),
                                           body=payload.reference_audio.read_bytes(),
                                           headers={"Content-Type": "audio/wav"})
                        registered = connection.getresponse()
                        registered.read()
                        if registered.status >= 400:
                            raise RuntimeError("Drama could not register the reference voice.")
                    finally:
                        connection.close()
                    self._registered_voices.add(voice)
                body["voice"] = voice
            response = client.post("/v1/speak", json=body)
            response.raise_for_status()
        waveform, sample_rate = sf.read(io.BytesIO(response.content), dtype="float32")
        return waveform, int(sample_rate)
