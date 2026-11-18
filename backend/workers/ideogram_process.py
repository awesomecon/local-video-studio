"""Managed startup for the isolated Ideogram 4 ComfyUI service."""

from __future__ import annotations

from pathlib import Path

from backend.core import AppConfig

from .comfyui_process import ComfyUIWorkerSupervisor


class IdeogramWorkerSupervisor(ComfyUIWorkerSupervisor):
    service_name = "Ideogram 4"
    required_node = "Ideogram4PipelineLoader"
    port_variable = "LVS_IDEOGRAM_PORT"

    @classmethod
    def from_config(cls, config: AppConfig) -> IdeogramWorkerSupervisor:
        item = config.backends.ideogram4_local
        repo_root = Path(__file__).resolve().parents[2]
        return cls(
            endpoint=item.endpoint or "http://127.0.0.1:8190",
            start_script=repo_root / "scripts" / "start_ideogram4.sh",
            startup_timeout_seconds=item.startup_timeout_seconds,
            log_path=config.paths.app_data / "logs" / "ideogram4.log",
        )
