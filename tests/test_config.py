from pathlib import Path

import pytest

from backend.core.config import AppConfig, ConfigurationError, load_config


def test_default_configuration_is_local_and_expands_paths() -> None:
    config = load_config(environ={})
    assert config.network.bind_address == "127.0.0.1"
    assert config.llm.base_url == "http://127.0.0.1:1234/v1"
    assert config.llm.timeout_seconds == 600
    assert config.ports.backend == 8009
    # The external LLM port plus every configured service endpoint (shared and
    # Ideogram ComfyUI, Qwen3-TTS, Step-Audio-EditX, Chatterbox, OmniVoice,
    # Breeze TTS 2 worker 8195 + official-API children 8196/8197) so selection
    # never claims one.
    assert config.ports.reserved == [
        1234, 8188, 8190, 8191, 8192, 8193, 8194, 8195, 8196, 8197,
    ]
    assert config.paths.project_root == Path.home() / "ai/projects"
    assert config.cache_environment()["TORCH_HOME"].endswith("/ai/cache/torch")
    assert config.backends.qwen_tts.managed is True
    assert config.backends.ideogram4_local.managed is True
    assert config.backends.qwen_tts.python_path == Path.home() / "ai/services/Qwen3-TTS/.venv/bin/python"
    assert config.backends.step_audio_editx.tokenizer_path == Path.home() / "ai/models/tts/step/Step-Audio-Tokenizer"
    assert config.backends.fish_s2_pro.model == "s2-pro"
    assert config.backends.breeze_tts_2.enabled is True
    assert config.backends.breeze_tts_2.managed is True
    assert config.backends.breeze_tts_2.endpoint == "http://127.0.0.1:8195"
    assert config.backends.breeze_tts_2.python_path == Path.home() / "ai/services/breeze-tts/.venv/bin/python"
    assert config.backends.breeze_tts_2.model_path == Path.home() / "ai/models/tts/breeze/Breeze-TTS-2"


def test_local_configuration_overlay_is_optional_and_precedes_environment(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("paths:\n  project_root: /srv/local-projects\n", encoding="utf-8")
    from_local = load_config(environ={}, local_path=local)
    overridden = load_config(
        environ={"LOCAL_VIDEO_STUDIO_PROJECT_ROOT": str(tmp_path / "env-projects")},
        local_path=local,
    )
    assert from_local.paths.project_root == Path("/srv/local-projects")
    assert overridden.paths.project_root == tmp_path / "env-projects"


def test_explicit_empty_environment_does_not_read_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:9999/v1")
    config = load_config(environ={})
    assert config.llm.base_url == "http://127.0.0.1:1234/v1"


def test_nested_and_compatibility_environment_overrides(tmp_path: Path) -> None:
    config = load_config(environ={
        "LOCAL_VIDEO_STUDIO__PORTS__BACKEND": "8765",
            "LOCAL_VIDEO_STUDIO_PROJECT_ROOT": str(tmp_path),
            "LOCAL_LLM_BASE_URL": "http://localhost:1234/v1/",
            "HUGGINGFACE_HUB_CACHE": str(tmp_path / "hf-hub"),
    })
    assert config.ports.backend == 8765
    assert config.paths.project_root == tmp_path
    assert config.llm.base_url == "http://localhost:1234/v1"
    assert config.paths.huggingface_hub_cache == tmp_path / "hf-hub"
    assert config.cache_environment()["HUGGINGFACE_HUB_CACHE"].endswith("/hf-hub")


def test_rejects_non_loopback_binding_without_explicit_lan() -> None:
    with pytest.raises(ValueError, match="allow_lan=false"):
        AppConfig(network={"bind_address": "0.0.0.0", "allow_lan": False})


def test_rejects_remote_backend_by_default() -> None:
    with pytest.raises(ValueError, match="must be localhost"):
        AppConfig(llm={"base_url": "https://example.com/v1"})


def test_port_1234_cannot_be_application_owned() -> None:
    with pytest.raises(ValueError, match="application-owned ports"):
        AppConfig(ports={"backend": 1234})


def test_invalid_yaml_is_wrapped(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text("paths: [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unable to read configuration"):
        load_config(config_path, environ={})


def test_validation_errors_do_not_echo_secret_values(tmp_path: Path) -> None:
    config_path = tmp_path / "unsafe.yaml"
    config_path.write_text("llm:\n  api_key: synthetic-secret-value\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as raised:
        load_config(config_path, environ={})
    assert "synthetic-secret-value" not in str(raised.value)


def test_secret_environment_overrides_are_rejected_without_echo() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_config(environ={"LOCAL_VIDEO_STUDIO__LLM__API_KEY": "synthetic-secret-value"})
    assert "synthetic-secret-value" not in str(raised.value)
