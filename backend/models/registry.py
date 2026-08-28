"""Backend registration, discovery, and health aggregation."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .ace_step_comfyui import ACEStepComfyUIBackend
from .adapters import (
    ChatterboxBackend,
    FluxBackend,
    H3Backend,
    Ideogram4LocalBackend,
    Krea2Backend,
    QwenImage2512Backend,
    WanBackend,
    WhisperBackend,
)
from .base import BackendDescriptor, Capability, GeneratorBackend
from .comfyui import ComfyUIBackend
from .faster_whisper import FasterWhisperBackend
from .local_llm import LocalLLMBackend
from .mock import MockGeneratorBackend, MockLLMBackend
from .tts_comfyui import FishS2ProBackend, IndexTTS25Backend, VoxCPM2Backend
from .tts_service import TTSServiceBackend


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, GeneratorBackend] = {}
        self._lock = threading.RLock()

    def register(
        self,
        backend: GeneratorBackend,
        *,
        name: str | None = None,
        replace: bool = False,
    ) -> str:
        key = name or backend.descriptor().backend_name
        with self._lock:
            if key in self._backends and not replace:
                raise ValueError(f"Backend {key!r} is already registered")
            self._backends[key] = backend
        return key

    def unregister(self, name: str) -> GeneratorBackend:
        with self._lock:
            return self._backends.pop(name)

    def get(self, name: str) -> GeneratorBackend:
        with self._lock:
            try:
                return self._backends[name]
            except KeyError:
                raise KeyError(f"Unknown generator backend {name!r}") from None

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._backends))

    def descriptors(self) -> dict[str, BackendDescriptor]:
        with self._lock:
            return {name: backend.descriptor() for name, backend in self._backends.items()}

    def health(self) -> dict[str, Mapping[str, Any]]:
        with self._lock:
            snapshot = tuple(self._backends.items())
        return {name: backend.health() for name, backend in snapshot}

    def supporting(self, capability: Capability) -> tuple[GeneratorBackend, ...]:
        with self._lock:
            return tuple(
                backend
                for backend in self._backends.values()
                if capability in backend.capabilities()
            )

    def __iter__(self) -> Iterator[GeneratorBackend]:
        with self._lock:
            return iter(tuple(self._backends.values()))

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        mock_mode: bool = False,
    ) -> BackendRegistry:
        registry = cls()
        llm = config.get("llm", {})
        if mock_mode:
            registry.register(MockGeneratorBackend())
            registry.register(MockLLMBackend(), name="mock_llm")
        else:
            registry.register(
                LocalLLMBackend(
                    base_url=str(llm.get("base_url", "http://127.0.0.1:1234/v1")),
                    api_key_env=str(llm.get("api_key_env", "LOCAL_LLM_API_KEY")),
                    model=str(llm.get("model", "auto")),
                    timeout_seconds=float(llm.get("timeout_seconds", 600)),
                )
            )
        backends = config.get("backends", {})
        comfy = backends.get("comfyui", {})
        comfy_endpoint = str(comfy.get("endpoint", "http://127.0.0.1:8188"))
        registry.register(ComfyUIBackend(endpoint=comfy_endpoint))
        registry.register(FluxBackend(endpoint=comfy_endpoint))
        registry.register(Krea2Backend(endpoint=comfy_endpoint))
        registry.register(QwenImage2512Backend(endpoint=comfy_endpoint))
        # Ideogram 4 runs locally through its own workflow; registered alongside
        # Qwen Image (kept for fallback/A-B testing) — neither replaces the other.
        ideogram = backends.get("ideogram4_local", {})
        ideogram_endpoint = str(ideogram.get("endpoint", "http://127.0.0.1:8190"))
        registry.register(Ideogram4LocalBackend(
            endpoint=ideogram_endpoint,
            poll_interval=float(ideogram.get("poll_interval_seconds", 0.5)),
            generation_timeout=float(
                ideogram.get("generation_timeout_seconds", 3600)),
        ))
        registry.register(WanBackend(endpoint=comfy_endpoint))
        h3 = backends.get("h3", {})
        registry.register(H3Backend(endpoint=h3.get("endpoint")))
        for name in ("qwen_tts", "step_audio_editx", "chatterbox", "omnivoice", "breeze_tts_2"):
            tts = backends.get(name, {})
            registry.register(TTSServiceBackend(name, tts.get("endpoint")))
        tts_workflows_dir = Path(__file__).resolve().parents[2] / "workflows" / "comfyui" / "tts"
        registry.register(FishS2ProBackend(
            endpoint=comfy_endpoint,
            workflows_dir=tts_workflows_dir,
            model_preset=backends.get("fish_s2_pro", {}).get("model", "s2-pro"),
            poll_interval=float(backends.get("fish_s2_pro", {}).get("poll_interval_seconds", 0.5)),
            generation_timeout=float(
                backends.get("fish_s2_pro", {}).get("generation_timeout_seconds", 1800)),
        ), name="fish_s2_pro")
        registry.register(VoxCPM2Backend(
            endpoint=comfy_endpoint,
            workflows_dir=tts_workflows_dir,
            poll_interval=float(backends.get("voxcpm2", {}).get("poll_interval_seconds", 0.5)),
            generation_timeout=float(
                backends.get("voxcpm2", {}).get("generation_timeout_seconds", 1800)),
        ), name="voxcpm2")
        registry.register(IndexTTS25Backend(
            endpoint=comfy_endpoint,
            workflows_dir=tts_workflows_dir,
            model_path=backends.get("index_tts_2_5", {}).get("model_path"),
            poll_interval=float(backends.get("index_tts_2_5", {}).get("poll_interval_seconds", 0.5)),
            generation_timeout=float(
                backends.get("index_tts_2_5", {}).get("generation_timeout_seconds", 1800)),
        ), name="index_tts_2_5")
        ace = backends.get("ace_step", {})
        comfy_endpoint = str(comfy.get("endpoint", "http://127.0.0.1:8188"))
        workflows_dir = Path(__file__).resolve().parents[2] / "workflows" / "comfyui"
        registry.register(ACEStepComfyUIBackend(
            endpoint=comfy_endpoint,
            model_name=ace.get("model", "xl_turbo"),
            workflow_path=ace.get("workflow_path"),
            workflows_dir=workflows_dir,
            poll_interval=float(ace.get("poll_interval_seconds", 0.5)),
            generation_timeout=float(ace.get("generation_timeout_seconds", 1800)),
        ))
        whisper = backends.get("whisper", {})
        registry.register(
            FasterWhisperBackend(
                whisper.get("model_path"),
                device=str(config.get("hardware", {}).get("preferred_device", "cuda")),
            )
        )
        return registry
