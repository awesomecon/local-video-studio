"""Typed application configuration with safe environment overrides."""

from __future__ import annotations

import copy
import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
LOCAL_CONFIG_PATH = DEFAULT_CONFIG_PATH.with_name("local.yaml")
ENV_PREFIX = "LOCAL_VIDEO_STUDIO__"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigurationError(RuntimeError):
    """Raised when configuration cannot be safely loaded."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PathsConfig(StrictModel):
    model_root: Path = Path("~/ai/models")
    project_root: Path = Path("~/ai/projects")
    cache_root: Path = Path("~/ai/cache")
    torch_cache: Path = Path("~/ai/cache/torch")
    huggingface_cache: Path = Path("~/ai/cache/huggingface")
    huggingface_hub_cache: Path = Path("~/ai/cache/huggingface/hub")
    generation_cache_root: Path = Path("~/ai/cache/generation")
    temp_root: Path = Path("~/.local/share/local-video-studio/tmp")
    app_data: Path = Path("~/.local/share/local-video-studio")
    minimum_free_disk_gb: float = Field(default=50, ge=0)

    @field_validator(
        "model_root", "project_root", "cache_root", "torch_cache",
        "huggingface_cache", "huggingface_hub_cache", "generation_cache_root",
        "temp_root", "app_data", mode="before",
    )
    @classmethod
    def expand_path(cls, value: object) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("path must be a string or path-like value")
        return Path(value).expanduser()


class HardwareConfig(StrictModel):
    preferred_device: str = "cuda"
    max_vram_gb: float = Field(default=23, gt=0)
    allow_cpu_offload: bool = True
    system_ram_budget_gb: float = Field(default=56, gt=0)


class GPUConfig(StrictModel):
    minimum_free_vram_gb_for_heavy_job: float = Field(default=20, ge=0)
    wait_for_vram: bool = False
    serialize_heavy_jobs: bool = True
    generation_cache_max_gb: float = Field(default=20, ge=0)


class NetworkConfig(StrictModel):
    bind_address: str = "127.0.0.1"
    allow_lan: bool = False
    allow_remote_backends: bool = False

    @model_validator(mode="after")
    def validate_binding(self) -> NetworkConfig:
        try:
            address = ipaddress.ip_address(self.bind_address)
        except ValueError as exc:
            raise ValueError("bind_address must be a literal IP address") from exc
        if not self.allow_lan and not address.is_loopback:
            raise ValueError("allow_lan=false requires a loopback bind_address")
        return self


class PortsConfig(StrictModel):
    backend: int = Field(default=8009, ge=1, le=65535)
    frontend: int = Field(default=3000, ge=1, le=65535)
    comfyui: int = Field(default=8188, ge=1, le=65535)
    reserved: list[int] = Field(default_factory=lambda: [1234])
    auto_select_free_port: bool = True
    allowed_range: tuple[int, int] = (8000, 8999)

    @field_validator("reserved")
    @classmethod
    def validate_reserved(cls, value: list[int]) -> list[int]:
        if 1234 not in value:
            raise ValueError("port 1234 must remain reserved for the external local LLM")
        if any(port < 1 or port > 65535 for port in value):
            raise ValueError("reserved ports must be between 1 and 65535")
        return sorted(set(value))

    @field_validator("allowed_range")
    @classmethod
    def validate_range(cls, value: tuple[int, int]) -> tuple[int, int]:
        start, end = value
        if not (1 <= start <= end <= 65535):
            raise ValueError("allowed_range must be an ordered valid TCP port range")
        return value

    @model_validator(mode="after")
    def protect_reserved(self) -> PortsConfig:
        owned = {"backend": self.backend, "frontend": self.frontend}
        conflicts = [name for name, port in owned.items() if port in self.reserved]
        if conflicts:
            raise ValueError(f"application-owned ports cannot be reserved: {', '.join(conflicts)}")
        return self


class LLMConfig(StrictModel):
    provider: str = "openai_compatible"
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key_env: str = "LOCAL_LLM_API_KEY"
    model: str = "auto"
    timeout_seconds: float = Field(default=600, gt=0)
    enable_completion_health_check: bool = False

    @field_validator("api_key_env")
    @classmethod
    def validate_secret_reference(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("api_key_env must be an environment-variable name")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an HTTP(S) URL")
        return value.rstrip("/")


class BackendServiceConfig(StrictModel):
    enabled: bool = False
    endpoint: str | None = None
    managed: bool = False
    python_path: Path | None = None
    model_path: Path | None = None
    tokenizer_path: Path | None = None
    startup_timeout_seconds: float = Field(default=15, gt=0, le=120)
    provider: str = "comfyui"
    model: str = "xl_turbo"
    workflow_path: str | None = None
    thinking: bool = True
    poll_interval_seconds: float = Field(default=0.5, gt=0)
    generation_timeout_seconds: float = Field(default=1800, gt=0)

    @field_validator("python_path", "model_path", "tokenizer_path", mode="before")
    @classmethod
    def expand_optional_path(cls, value: object) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("path must be a string or path-like value")
        return Path(value).expanduser()


class BackendsConfig(StrictModel):
    comfyui: BackendServiceConfig = Field(default_factory=lambda: BackendServiceConfig(
        enabled=True, endpoint="http://127.0.0.1:8188"))
    ideogram4_local: BackendServiceConfig = Field(default_factory=lambda: BackendServiceConfig(
        enabled=True,
        endpoint="http://127.0.0.1:8190",
        provider="comfyui",
        model="nf4",
        generation_timeout_seconds=3600,
    ))
    h3: BackendServiceConfig = Field(default_factory=BackendServiceConfig)
    chatterbox: BackendServiceConfig = Field(default_factory=BackendServiceConfig)
    qwen_tts: BackendServiceConfig = Field(default_factory=BackendServiceConfig)
    step_audio_editx: BackendServiceConfig = Field(default_factory=BackendServiceConfig)
    ace_step: BackendServiceConfig = Field(default_factory=BackendServiceConfig)
    whisper: BackendServiceConfig = Field(default_factory=BackendServiceConfig)
    # Four-model voice-cloning comparison providers. fish_s2_pro, voxcpm2, and
    # index_tts_2_5 run through ComfyUI workflow templates; omnivoice runs as a
    # managed isolated worker.
    fish_s2_pro: BackendServiceConfig = Field(default_factory=lambda: BackendServiceConfig(
        enabled=True, provider="comfyui", model="s2-pro"))
    voxcpm2: BackendServiceConfig = Field(default_factory=lambda: BackendServiceConfig(
        enabled=True, provider="comfyui"))
    index_tts_2_5: BackendServiceConfig = Field(default_factory=lambda: BackendServiceConfig(
        enabled=True, provider="comfyui"))
    omnivoice: BackendServiceConfig = Field(default_factory=BackendServiceConfig)
    # Breeze TTS 2 runs as a managed isolated worker that wraps the official
    # breeze_infer.api (one child process per engine mode; see
    # services/tts_worker/app.py BreezeProvider).
    breeze_tts_2: BackendServiceConfig = Field(default_factory=BackendServiceConfig)


class RenderConfig(StrictModel):
    fps: int = Field(default=24, ge=1, le=240)
    resolution: tuple[int, int] = (1920, 1080)
    preview_resolution: tuple[int, int] = (640, 360)
    video_codec: str = "libx264"
    audio_codec: str = "aac"

    @field_validator("resolution", "preview_resolution")
    @classmethod
    def validate_resolution(cls, value: tuple[int, int]) -> tuple[int, int]:
        if any(component <= 0 for component in value):
            raise ValueError("resolution components must be positive")
        return value


_ALLOWED_IMAGE_MODELS = {"krea", "qwen_image", "ideogram4_local"}


class ImageGenerationConfig(StrictModel):
    """Image-generator routing policy.

    Ideogram 4 is being tested against Qwen Image for scenes with embedded
    text because Qwen's lettering was not strong enough; Krea stays the
    default for ordinary cinematic imagery. ``comparison_mode`` renders both
    Qwen and Ideogram variants for every text scene so they can be reviewed
    side-by-side before any replacement decision.
    """

    comparison_mode: bool = False
    # Preferred generator for scenes without embedded text (cinematic b-roll).
    general_model: str = "krea"
    # Preferred generator when a scene needs readable words inside the image.
    text_model: str = "ideogram4_local"
    # Kept temporarily as fallback/A-B candidate for text scenes.
    fallback_text_model: str = "qwen_image"

    @field_validator("general_model", "text_model", "fallback_text_model")
    @classmethod
    def validate_models(cls, value: str) -> str:
        if value not in _ALLOWED_IMAGE_MODELS:
            raise ValueError(
                f"image model must be one of {sorted(_ALLOWED_IMAGE_MODELS)}"
            )
        return value


class AppConfig(StrictModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    gpu: GPUConfig = Field(default_factory=GPUConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    ports: PortsConfig = Field(default_factory=PortsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    llm_roles: dict[str, str] = Field(default_factory=lambda: {
        "writer": "auto", "director": "auto", "prompt_engineer": "auto", "qc": "auto"})
    backends: BackendsConfig = Field(default_factory=BackendsConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)

    @model_validator(mode="after")
    def validate_local_first(self) -> AppConfig:
        if self.network.allow_remote_backends:
            return self
        endpoints = {"llm": self.llm.base_url}
        for name in (
            "comfyui", "h3", "qwen_tts", "step_audio_editx", "chatterbox",
            "ace_step", "whisper", "ideogram4_local", "fish_s2_pro", "voxcpm2", "index_tts_2_5",
            "omnivoice", "breeze_tts_2",
        ):
            backend = getattr(self.backends, name)
            if backend.enabled and backend.endpoint:
                endpoints[name] = backend.endpoint
        for name, endpoint in endpoints.items():
            host = urlparse(endpoint).hostname
            if host is None or not _is_loopback_host(host):
                raise ValueError(
                    f"{name} endpoint must be localhost when allow_remote_backends=false"
                )
        for name in ("qwen_tts", "step_audio_editx", "chatterbox", "omnivoice", "breeze_tts_2"):
            backend = getattr(self.backends, name)
            if backend.managed and not backend.enabled:
                raise ValueError(f"{name} managed worker requires enabled=true")
            if backend.managed and not all((backend.endpoint, backend.python_path, backend.model_path)):
                raise ValueError(
                    f"{name} managed worker requires endpoint, python_path, and model_path"
                )
        return self

    def cache_environment(self) -> dict[str, str]:
        """Return non-secret cache variables suitable for child model services."""
        return {
            "TORCH_HOME": str(self.paths.torch_cache),
            "HF_HOME": str(self.paths.huggingface_cache),
            "HUGGINGFACE_HUB_CACHE": str(self.paths.huggingface_hub_cache),
        }


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_environment_value(value: str) -> Any:
    parsed = yaml.safe_load(value)
    return value if parsed is None and value.strip().lower() != "null" else parsed


def _environment_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    aliases = {
        "LOCAL_LLM_BASE_URL": ("llm", "base_url"),
        "LOCAL_VIDEO_STUDIO_MODEL_ROOT": ("paths", "model_root"),
        "LOCAL_VIDEO_STUDIO_PROJECT_ROOT": ("paths", "project_root"),
        "LOCAL_VIDEO_STUDIO_CACHE_ROOT": ("paths", "cache_root"),
        "TORCH_HOME": ("paths", "torch_cache"),
        "HF_HOME": ("paths", "huggingface_cache"),
        "HUGGINGFACE_HUB_CACHE": ("paths", "huggingface_hub_cache"),
    }
    entries: list[tuple[tuple[str, ...], str]] = []
    for name, keys in aliases.items():
        if name in environ:
            entries.append((keys, environ[name]))
    for name, value in environ.items():
        if name.startswith(ENV_PREFIX):
            keys = tuple(part.lower() for part in name[len(ENV_PREFIX):].split("__") if part)
            if keys:
                secret_override = any(
                    marker in keys[-1] for marker in ("secret", "password", "token", "api_key")
                )
                if secret_override and keys[-1] != "api_key_env":
                    raise ConfigurationError(
                        "secret values cannot be provided through structured "
                        "configuration overrides"
                    )
                entries.append((keys, value))
    for keys, value in entries:
        cursor = overrides
        for key in keys[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[keys[-1]] = _parse_environment_value(value)
    return overrides


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_config_mapping(config_path: Path) -> dict[str, Any]:
    """Read one non-secret YAML configuration mapping."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"unable to read configuration: {config_path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be a mapping")
    return raw


def load_config(path: str | os.PathLike[str] | None = None,
                environ: Mapping[str, str] | None = None, *,
                local_path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load portable defaults, optional machine overrides, then environment overrides.

    Normal application startup automatically reads ignored ``config/local.yaml`` when it exists.
    Passing an explicit config path or environment mapping keeps tests and tools deterministic;
    callers can still opt into an overlay with ``local_path``. Configuration files must contain
    paths and non-secret settings only—the configured API-key variable is never resolved here.
    """
    config_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    raw = _read_config_mapping(config_path)
    overlay_path: Path | None = None
    if local_path is not None:
        overlay_path = Path(local_path).expanduser()
    elif path is None and environ is None and LOCAL_CONFIG_PATH.is_file():
        overlay_path = LOCAL_CONFIG_PATH
    if overlay_path is not None:
        raw = _deep_merge(raw, _read_config_mapping(overlay_path))
    environment = os.environ if environ is None else environ
    merged = _deep_merge(raw, _environment_overrides(environment))
    try:
        return AppConfig.model_validate(merged)
    except ValidationError as exc:
        locations = [".".join(str(part) for part in item["loc"])
                     for item in exc.errors(include_input=False)]
        raise ConfigurationError(
            f"invalid configuration ({exc.error_count()} error(s)) at: {', '.join(locations)}"
        ) from exc
