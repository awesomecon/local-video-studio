import io
import socket

import pytest

import backend.core.ports as ports_module
from backend.core.ports import (
    PortSafetyError, assert_application_port, probe_port, select_application_port,
    verify_comfyui_service, verify_openai_service,
)


def bound_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


def test_probe_detects_listener_and_never_terminates_it() -> None:
    listener = bound_listener()
    try:
        port = listener.getsockname()[1]
        result = probe_port("127.0.0.1", port)
        assert result.listening
        assert not result.bind_available
        with pytest.raises(PortSafetyError, match="did not terminate"):
            assert_application_port("127.0.0.1", port)
        assert listener.fileno() >= 0
    finally:
        listener.close()


def test_reserved_external_llm_port_is_never_selected() -> None:
    with pytest.raises(PortSafetyError, match="reserved"):
        assert_application_port("127.0.0.1", 1234)


def test_auto_selection_uses_real_bind_and_skips_occupied_port() -> None:
    listener = bound_listener()
    try:
        occupied = listener.getsockname()[1]
        selected = select_application_port(
            "127.0.0.1", occupied,
            allowed_range=(max(1024, occupied - 20), min(65535, occupied + 20)),
        )
        assert selected != occupied
        assert selected != 1234
        assert probe_port("127.0.0.1", selected).bind_available
    finally:
        listener.close()


def test_lan_bind_is_rejected_by_default() -> None:
    with pytest.raises(PortSafetyError, match="LAN binding is disabled"):
        probe_port("0.0.0.0", 8000)


@pytest.fixture()
def bind_spy(monkeypatch):
    """Record can_bind calls while keeping the real implementation."""
    attempted: list[int] = []
    real_can_bind = ports_module.can_bind

    def spy(address, port, **kwargs):
        attempted.append(port)
        return real_can_bind(address, port, **kwargs)

    monkeypatch.setattr(ports_module, "can_bind", spy)
    return attempted


def test_probe_never_binds_the_reserved_llm_port(bind_spy) -> None:
    result = probe_port("127.0.0.1", 1234)

    # The connect-only probe must never bind 1234 and must report whatever the
    # real LLM state is (it may be running on this machine), so `listening` is
    # intentionally not asserted here; hermetic listening coverage lives in
    # test_reserved_listening_port_reported_without_a_bind.
    assert result.bind_available is False
    assert "reserved" in (result.error or "")
    assert result.error == "reserved external port; connect-only probe (never bound)"
    assert bind_spy == []


def test_probe_connect_only_for_configured_reserved_ports(bind_spy) -> None:
    for port in (8188, 8191, 8192, 8193):
        result = probe_port("127.0.0.1", port, reserved={port})
        assert result.bind_available is False
        assert "reserved" in (result.error or "")
    assert bind_spy == []


def test_reserved_listening_port_reported_without_a_bind(bind_spy) -> None:
    listener = bound_listener()
    try:
        port = listener.getsockname()[1]
        result = probe_port("127.0.0.1", port, reserved={port})
        assert result.listening is True
        assert result.bind_available is False
        assert "reserved" in (result.error or "")
    finally:
        listener.close()
    assert bind_spy == []


def test_non_reserved_ports_keep_the_authoritative_bind_probe(bind_spy) -> None:
    listener = bound_listener()
    try:
        occupied = listener.getsockname()[1]
        blocked = probe_port("127.0.0.1", occupied)
        assert blocked.listening is True
        assert blocked.bind_available is False
    finally:
        listener.close()
    # A closed non-reserved port is still checked with a real bind.
    released = bound_listener()
    freed_port = released.getsockname()[1]
    released.close()
    available = probe_port("127.0.0.1", freed_port)
    assert available.bind_available is True
    assert sorted(bind_spy) == sorted({occupied, freed_port})


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_verify_openai_service_treats_non_json_body_as_incompatible(monkeypatch) -> None:
    monkeypatch.setattr(
        ports_module, "urlopen",
        lambda request, timeout: _FakeResponse(b"<html>502 Bad Gateway</html>"),
    )
    identity = verify_openai_service("http://127.0.0.1:1234/v1")
    assert identity.compatible is False
    assert identity.status == "incompatible"
    assert "JSON" in identity.detail


def test_verify_comfyui_service_treats_html_body_as_incompatible(monkeypatch) -> None:
    monkeypatch.setattr(
        ports_module, "urlopen",
        lambda request, timeout: _FakeResponse(b"<html><body>ComfyUI</body></html>"),
    )
    identity = verify_comfyui_service("http://127.0.0.1:8188")
    assert identity.compatible is False
    assert identity.status == "incompatible"
