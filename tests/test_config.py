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
    # Breeze TTS 2 worker 8195 + official-API children 8196/8197, and Higgs TTS
    # 3 worker 8198 + SGLang-Omni child 8199) so selection
    # never claims one.
    assert config.ports.reserved == [
        1234, 8188, 8190, 8191, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199,
    ]
    assert config.paths.project_root == Path.home() / "ai/projects"
    assert Path(config.cache_environment()["TORCH_HOME"]) == Path.home() / "ai/cache/torch"
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
    assert config.backends.higgs_tts_3.enabled is True
    assert config.backends.higgs_tts_3.managed is True
    assert config.backends.higgs_tts_3.endpoint == "http://127.0.0.1:8198"
    assert config.backends.higgs_tts_3.python_path == Path.home() / "ai/services/sglang-omni/.venv/bin/python"
    assert config.backends.higgs_tts_3.model_path == Path.home() / "ai/models/tts/higgs/bosonai-higgs-tts-3-4b"


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
    assert Path(config.cache_environment()["HUGGINGFACE_HUB_CACHE"]) == tmp_path / "hf-hub"


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


def test_gemini_tts_defaults_are_on_but_inert_without_a_key() -> None:
    config = load_config(environ={})
    gemini = config.backends.gemini_tts
    assert gemini.enabled is True
    assert gemini.model == "gemini-2.5-flash-preview-tts"
    assert gemini.voice == "Kore"
    assert gemini.api_key_env == "GEMINI_API_KEY"
    assert gemini.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert gemini.timeout_seconds == 180
    # The provider is intentionally remote; it must not trip the loopback
    # policy that guards every local service endpoint.
    assert config.network.bind_address == "127.0.0.1"


def test_gemini_tts_environment_overrides() -> None:
    config = load_config(environ={
        "LOCAL_VIDEO_STUDIO__backends__gemini_tts__model": "gemini-2.5-pro-preview-tts",
        "LOCAL_VIDEO_STUDIO__backends__gemini_tts__voice": "Charon",
        "LOCAL_VIDEO_STUDIO__backends__gemini_tts__enabled": "false",
    })
    assert config.backends.gemini_tts.model == "gemini-2.5-pro-preview-tts"
    assert config.backends.gemini_tts.voice == "Charon"
    assert config.backends.gemini_tts.enabled is False


def test_gemini_tts_rejects_insecure_and_malformed_settings() -> None:
    with pytest.raises(ConfigurationError, match="gemini_tts.base_url"):
        load_config(environ={
            "LOCAL_VIDEO_STUDIO__backends__gemini_tts__base_url":
                "http://gemini-relay.example.com/v1beta",
        })
    # A loopback HTTPS proxy (user-managed) stays allowed.
    load_config(environ={
        "LOCAL_VIDEO_STUDIO__backends__gemini_tts__base_url":
            "https://127.0.0.1:9443/v1beta",
    })
    with pytest.raises(ValueError, match="environment-variable name"):
        AppConfig(backends={"gemini_tts": {"api_key_env": "bad-name!"}})
    with pytest.raises(ValueError, match="model must be a Gemini model identifier"):
        AppConfig(backends={"gemini_tts": {"model": "not/a/model"}})
    with pytest.raises(ValueError, match="preset voice name"):
        AppConfig(backends={"gemini_tts": {"voice": "lowercase"}})
