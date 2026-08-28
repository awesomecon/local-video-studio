"""Authoritative Python TCP port safety and local-service identity helpers."""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

RESERVED_EXTERNAL_PORTS = frozenset({1234})


class PortSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    name: str | None = None


@dataclass(frozen=True)
class PortProbe:
    address: str
    port: int
    listening: bool
    bind_available: bool
    process: ProcessIdentity | None = None
    error: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceIdentity:
    service: str
    compatible: bool
    status: str
    detail: str


def _validate_address(address: str, allow_lan: bool = False) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise PortSafetyError("bind address must be a literal IP address") from exc
    if not allow_lan and not ip.is_loopback:
        raise PortSafetyError("LAN binding is disabled; use a loopback address")


def can_bind(address: str, port: int, *, allow_lan: bool = False) -> tuple[bool, str | None]:
    """Actually bind a TCP socket and close it; this is the authoritative preflight test."""
    _validate_address(address, allow_lan)
    if not 1 <= port <= 65535:
        raise PortSafetyError("port must be between 1 and 65535")
    sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind((address, port))
        return True, None
    except OSError as exc:
        return False, f"{exc.strerror or type(exc).__name__} (errno={exc.errno})"
    finally:
        sock.close()


def is_listening(address: str, port: int, timeout: float = 0.2) -> bool:
    _validate_address(address, allow_lan=True)
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_process(address: str, port: int) -> ProcessIdentity | None:
    """Best-effort PID lookup; unavailable permissions never invalidate socket results."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        for connection in psutil.net_connections(kind="tcp"):
            if not connection.laddr or connection.status != psutil.CONN_LISTEN:
                continue
            if connection.laddr.port != port:
                continue
            listener_host = connection.laddr.ip
            if listener_host not in {address, "0.0.0.0", "::", "::0"}:
                continue
            if connection.pid is None:
                return None
            try:
                return ProcessIdentity(connection.pid, psutil.Process(connection.pid).name())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                return ProcessIdentity(connection.pid)
    except (psutil.AccessDenied, PermissionError):
        return None
    return None


def probe_port(address: str, port: int, *, allow_lan: bool = False,
               reserved: frozenset[int] | set[int] | None = None) -> PortProbe:
    """Inspect a port without claiming it.

    Reserved ports (always including RESERVED_EXTERNAL_PORTS, e.g. the local
    LLM's 1234) are probed connect-only: binding one could win a race against
    its external owner restarting, and reserved ports are never claimed even
    transiently. Other ports keep the authoritative bind probe.
    """
    protected = RESERVED_EXTERNAL_PORTS | frozenset(reserved or frozenset())
    listening = is_listening(address, port)
    if port in protected:
        process = find_process(address, port) if listening else None
        return PortProbe(
            address, port, listening, False, process,
            "reserved external port; connect-only probe (never bound)",
        )
    available, error = can_bind(address, port, allow_lan=allow_lan)
    process = find_process(address, port) if listening or not available else None
    return PortProbe(address, port, listening, available, process, error)


def assert_application_port(address: str, port: int, *, reserved: set[int] | None = None,
                            allow_lan: bool = False) -> None:
    protected = RESERVED_EXTERNAL_PORTS | frozenset(reserved or set())
    if port in protected:
        raise PortSafetyError(f"port {port} is externally owned and reserved")
    result = probe_port(address, port, allow_lan=allow_lan)
    if result.bind_available:
        return
    owner = ""
    if result.process:
        owner = f" by PID {result.process.pid}"
        if result.process.name:
            owner += f" ({result.process.name})"
    raise PortSafetyError(
        f"Port {port} is already in use{owner}. Local Video Studio did not terminate it."
    )


def select_application_port(address: str, preferred: int, *, allowed_range: tuple[int, int],
                            reserved: set[int] | None = None, auto_select: bool = True,
                            allow_lan: bool = False) -> int:
    """Select using real binds; the eventual server bind remains the final authority."""
    protected = RESERVED_EXTERNAL_PORTS | frozenset(reserved or set())
    if preferred not in protected:
        available, _ = can_bind(address, preferred, allow_lan=allow_lan)
        if available:
            return preferred
    if not auto_select:
        assert_application_port(address, preferred, reserved=set(protected), allow_lan=allow_lan)
    start, end = allowed_range
    if not (1 <= start <= end <= 65535):
        raise PortSafetyError("invalid allowed port range")
    candidates = [port for port in range(start, end + 1)
                  if port not in protected and port != preferred]
    secrets.SystemRandom().shuffle(candidates)
    for candidate in candidates:
        available, _ = can_bind(address, candidate, allow_lan=allow_lan)
        if available:
            return candidate
    raise PortSafetyError(f"no available application port in allowed range {start}-{end}")


def verify_openai_service(base_url: str, api_key: str | None = None,
                          timeout: float = 2) -> ServiceIdentity:
    """Verify a structurally OpenAI-compatible models response without exposing credentials."""
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return ServiceIdentity("openai_compatible", False, "unsafe_endpoint",
                               "identity checks are limited to localhost")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(f"{base_url.rstrip('/')}/models", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost validated
            payload = json.load(response)
    except HTTPError as exc:
        status = "authentication_failed" if exc.code in {401, 403} else "unexpected_http_status"
        return ServiceIdentity("openai_compatible", False, status, f"HTTP {exc.code}")
    except (URLError, TimeoutError, OSError):
        return ServiceIdentity("openai_compatible", False, "unreachable",
                               "local service did not respond")
    except (ValueError, UnicodeDecodeError):
        # A 200 response with an HTML (or otherwise non-JSON) body is an
        # incompatible service, not a diagnostic crash.
        return ServiceIdentity("openai_compatible", False, "incompatible",
                               "response is not valid JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return ServiceIdentity("openai_compatible", False, "incompatible",
                               "response does not contain a models data array")
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str)
               for item in payload["data"]):
        return ServiceIdentity("openai_compatible", False, "incompatible",
                               "models response entries are invalid")
    return ServiceIdentity("openai_compatible", True, "connected",
                           f"discovered {len(payload['data'])} model(s)")


def verify_comfyui_service(endpoint: str, timeout: float = 2) -> ServiceIdentity:
    parsed = urlparse(endpoint)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return ServiceIdentity("comfyui", False, "unsafe_endpoint",
                               "identity checks are limited to localhost")
    request = Request(
        f"{endpoint.rstrip('/')}/system_stats", headers={"Accept": "application/json"}
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost validated
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError):
        return ServiceIdentity("comfyui", False, "unreachable",
                               "local service did not provide ComfyUI system stats")
    except (ValueError, UnicodeDecodeError):
        # A 200 with an HTML (or otherwise non-JSON) body is an incompatible
        # service, not a diagnostic crash.
        return ServiceIdentity("comfyui", False, "incompatible",
                               "system stats response is not recognizable as ComfyUI")
    if not isinstance(payload, dict) or not ({"system", "devices"} & payload.keys()):
        return ServiceIdentity("comfyui", False, "incompatible",
                               "system stats response is not recognizable as ComfyUI")
    return ServiceIdentity("comfyui", True, "connected", "ComfyUI identity verified")
