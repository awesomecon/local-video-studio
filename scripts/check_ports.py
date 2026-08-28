#!/usr/bin/env python3
"""Inspect Local Video Studio ports without claiming or terminating them."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urllib.parse import urlparse

from backend.core.config import load_config
from backend.core.ports import probe_port, verify_comfyui_service, verify_openai_service


def _llm_target_port(base_url: str) -> int:
    """Port of the configured LLM endpoint; 1234 remains the documented fallback."""
    port = urlparse(base_url).port
    return port if port is not None else 1234


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--verify-external",
        action="store_true",
        help="Verify expected identities for listening local LLM and ComfyUI services",
    )
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    reserved = frozenset(config.ports.reserved)
    targets = {
        "local_llm_external": _llm_target_port(config.llm.base_url),
        "backend_owned": config.ports.backend,
        "frontend_owned": config.ports.frontend,
        "comfyui_external": config.ports.comfyui,
    }
    # Reserved targets (the external LLM port, the configured service ports)
    # are probed connect-only; probe_port never binds them.
    results = {name: probe_port(config.network.bind_address, port,
                                allow_lan=config.network.allow_lan,
                                reserved=reserved)
               for name, port in targets.items()}
    identities = {}
    if arguments.verify_external:
        identities["local_llm_external"] = verify_openai_service(
            config.llm.base_url, os.environ.get(config.llm.api_key_env)
        )
        comfy_endpoint = config.backends.comfyui.endpoint or (
            f"http://{config.network.bind_address}:{config.ports.comfyui}"
        )
        identities["comfyui_external"] = verify_comfyui_service(comfy_endpoint)
    if arguments.as_json:
        payload = {"ports": {name: result.model_dump() for name, result in results.items()}}
        if identities:
            payload["identities"] = {
                name: identity.__dict__ for name, identity in identities.items()
            }
        print(json.dumps(payload, indent=2))
    else:
        for name, result in results.items():
            if result.error and "reserved" in result.error:
                # Reserved targets are connect-only: report listening honestly
                # instead of the bind-based available/occupied states.
                state = (
                    "listening" if result.listening else "not listening"
                ) + " (reserved; never bound)"
            else:
                state = "listening" if result.listening else (
                    "available" if result.bind_available else "occupied")
            owner = ""
            if result.process:
                owner = f" PID={result.process.pid} name={result.process.name or 'unknown'}"
            print(f"{name}: {result.address}:{result.port} {state}{owner}")
        for name, identity in identities.items():
            print(f"{name} identity: {identity.status} ({identity.detail})")
        print("No ports were claimed and no processes were terminated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
