"""Score Studio API (Phase 3): studio snapshot, plan save, score preview,
local effect upload, and deterministic auto-score over a real (mock) app.

These drive the FastAPI endpoints end to end and assert the cross-cutting
Phase 3 rules: optimistic plan concurrency, selective stage invalidation
(cue saves never touch music generation), preview caching, and that effect
uploads stay local, sanitized, and within limits.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core import load_config


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _app(tmp_path: Path):
    config = load_config(environ={})
    # Keep the Studio snapshot local and fast: with ACE-Step disabled the
    # snapshot reports readiness without probing an (absent) ComfyUI service.
    config.backends.ace_step.enabled = False
    app = create_app(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    return app, TestClient(app)


def _service(app):
    return app.state.service


def _project_root(app, project_id: str) -> Path:
    service = _service(app)
    return service.store.project_path(service._project(project_id))


def _create_project(client: TestClient, **overrides) -> str:
    payload = {
        "title": "Score Studio",
        "topic": "scoring a short",
        "target_duration": 4,
        "resolution": [160, 90],
        "fps": 12,
    }
    payload.update(overrides)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _wav_bytes(seconds: float, *, hz: float = 440.0, rate: int = 48000, amp: int = 12000) -> bytes:
    frames = int(seconds * rate)
    mono = [int(amp * math.sin(2 * math.pi * hz * index / rate)) for index in range(frames)]
    interleaved: list[int] = []
    for value in mono:
        interleaved.extend([value, value])
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array("h", interleaved).tobytes())
    return buffer.getvalue()


def _write_stereo_wav(path: Path, seconds: float, **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes(seconds, **kwargs))


def _write_background(app, project_id: str, seconds: float = 4.0, **kwargs) -> None:
    root = _project_root(app, project_id)
    _write_stereo_wav(root / "music" / "background.wav", seconds, **kwargs)


def _write_narration(app, project_id: str, seconds: float = 4.0) -> None:
    root = _project_root(app, project_id)
    _write_stereo_wav(root / "narration" / "master.wav", seconds)


def _plan_payload(duration: float, cues: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "duration_seconds": duration,
        "cues": cues if cues is not None else [],
    }


def _rms(path: Path, start: float, end: float, rate: int = 48000) -> float:
    with wave.open(str(path), "rb") as handle:
        assert handle.getframerate() == rate
        samples = array("h", handle.readframes(handle.getnframes()))
    start_i = int(start * rate) * 2
    end_i = min(int(end * rate) * 2, len(samples))
    segment = samples[start_i:end_i]
    if not segment:
        return 0.0
    return math.sqrt(sum(v * v for v in segment) / len(segment))


# ---------------------------------------------------------------------------
# Studio snapshot
# ---------------------------------------------------------------------------


def test_studio_snapshot_shape(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    _write_narration(app, project_id, 4.0)

    response = client.get(f"/api/projects/{project_id}/music/studio")
    assert response.status_code == 200
    body = response.json()

    assert body["project_id"] == project_id
    # Music generation settings + narration-derived duration + ACE readiness.
    assert body["music"]["duration_seconds"] == pytest.approx(4.0, abs=0.05)
    assert body["music"]["ace"]["enabled"] is False
    # Current soundtrack.
    assert body["soundtrack"] is not None
    assert body["soundtrack"]["hash"]
    assert body["soundtrack"]["url"].endswith("/music/media/background.wav")
    # No score yet: scored/preview absent, plan null, revision 0, no effects.
    assert body["scored"] is None
    assert body["preview"] is None
    assert body["score_plan"] is None
    assert body["score_plan_revision"] == 0
    assert body["effects"] == []
    # Narration asset URL and duration.
    assert body["narration"] is not None
    assert body["narration"]["duration_seconds"] == pytest.approx(4.0, abs=0.05)
    # Scene + caption lanes are present as (possibly empty) lists.
    assert isinstance(body["scenes"], list)
    assert isinstance(body["captions"], list)
    assert isinstance(body["stages"], dict)
    assert isinstance(body["jobs"], list)


def test_studio_snapshot_404_for_unknown_project(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    response = client.get("/api/projects/does-not-exist/music/studio")
    assert response.status_code == 404


def test_studio_snapshot_reflects_saved_plan(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)

    saved = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={
            "plan": _plan_payload(
                4.0,
                [{"time_seconds": 2.0, "action": "silence", "transition_seconds": 0.1}],
            ),
            "expected_revision": 0,
        },
    )
    assert saved.status_code == 200

    body = client.get(f"/api/projects/{project_id}/music/studio").json()
    assert body["score_plan"] is not None
    assert body["score_plan"]["cues"][0]["action"] == "silence"
    assert body["score_plan_revision"] == 1
    assert body["score_plan_hash"]


def test_studio_snapshot_reports_scored_and_preview(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    _write_narration(app, project_id, 4.0)
    client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={"plan": _plan_payload(4.0, [{"time_seconds": 2.0, "action": "pull_back", "gain_db": -8}]), "expected_revision": 0},
    )
    assert client.post(f"/api/projects/{project_id}/music/score-preview").status_code == 200

    body = client.get(f"/api/projects/{project_id}/music/studio").json()
    assert body["scored"] is not None
    assert body["scored"]["url"].endswith("/music/media/scored-background.wav")
    assert body["preview"] is not None
    assert body["preview"]["url"].endswith("/music/media/score-preview.wav")
    assert body["preview"]["has_narration"] is True


# ---------------------------------------------------------------------------
# Save score plan: validation, concurrency, selective invalidation
# ---------------------------------------------------------------------------


def test_save_score_plan_success_and_invalidation(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    service = _service(app)
    project = service._project(project_id)
    # Seed a few completed stage records so there is something to invalidate.
    for stage in ("music", "timeline", "score_mix", "render_preview"):
        service._mark_stage(project, stage, [], f"job-{stage}")

    saved = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={
            "plan": _plan_payload(
                4.0,
                [
                    {"time_seconds": 1.5, "action": "pull_back", "gain_db": -7},
                    {"time_seconds": 3.0, "action": "silence", "transition_seconds": 0.1},
                ],
            ),
            "expected_revision": 0,
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["revision"] == 1
    assert body["plan_hash"]
    # Cue-only change invalidates the mix and its render descendants, never music.
    assert "score_mix" in body["invalidated_stages"]
    assert "timeline" in body["invalidated_stages"]
    assert "music" not in body["invalidated_stages"]

    state = service._read_stage_state(project)["stages"]
    assert "music" in state
    assert "timeline" not in state
    assert "score_mix" not in state
    assert "render_preview" not in state


def test_save_score_plan_conflict_on_stale_revision(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    first = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={"plan": _plan_payload(4.0, [{"time_seconds": 1.0, "action": "build"}]), "expected_revision": 0},
    )
    assert first.status_code == 200
    assert first.json()["revision"] == 1

    # An editor that still believes the revision is 0 must be rejected.
    stale = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={"plan": _plan_payload(4.0, [{"time_seconds": 2.0, "action": "silence"}]), "expected_revision": 0},
    )
    assert stale.status_code == 409

    # The on-disk plan is the first save; the stale one did not land.
    body = client.get(f"/api/projects/{project_id}/music/studio").json()
    assert body["score_plan_revision"] == 1
    assert body["score_plan"]["cues"][0]["action"] == "build"


def test_save_score_plan_requires_expected_revision(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    response = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={"plan": _plan_payload(4.0)},
    )
    assert response.status_code == 422


def test_save_score_plan_rejects_mismatched_duration(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    # Plan claims 60s but the soundtrack is 4s: cannot line up.
    response = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={"plan": _plan_payload(60.0, [{"time_seconds": 30.0, "action": "build"}]), "expected_revision": 0},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("bad_cue", [
    {"time_seconds": 2.0, "action": "repaint"},          # unknown action
    {"time_seconds": 99.0, "action": "silence"},          # past the soundtrack
    {"time_seconds": 2.0, "action": "silence", "gain_db": -999},  # out of range
])
def test_save_score_plan_validation_errors(tmp_path: Path, bad_cue: dict) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    response = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={"plan": _plan_payload(4.0, [bad_cue]), "expected_revision": 0},
    )
    assert response.status_code == 422


def test_save_score_plan_rejects_unknown_plan_field(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    plan = _plan_payload(4.0)
    plan["surprise"] = True
    response = client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={"plan": plan, "expected_revision": 0},
    )
    assert response.status_code == 422


def test_save_score_plan_404_for_unknown_project(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    response = client.put(
        "/api/projects/does-not-exist/music/score-plan",
        json={"plan": _plan_payload(4.0), "expected_revision": 0},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Score preview
# ---------------------------------------------------------------------------


def test_score_preview_creates_and_serves(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    _write_narration(app, project_id, 4.0)

    response = client.post(f"/api/projects/{project_id}/music/score-preview")
    assert response.status_code == 200
    body = response.json()
    assert body["url"].endswith("/music/media/score-preview.wav")
    assert body["hash"]
    assert body["reused"] is False
    assert body["has_narration"] is True

    audio = client.get(f"/api/projects/{project_id}/music/media/score-preview.wav")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")
    assert len(audio.content) > 44


def test_score_preview_reuses_cache(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    _write_narration(app, project_id, 4.0)

    first = client.post(f"/api/projects/{project_id}/music/score-preview").json()
    second = client.post(f"/api/projects/{project_id}/music/score-preview").json()
    assert first["reused"] is False
    assert second["reused"] is True
    assert second["hash"] == first["hash"]


def test_score_preview_requires_soundtrack(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    # No soundtrack generated.
    response = client.post(f"/api/projects/{project_id}/music/score-preview")
    assert response.status_code == 409


def test_score_preview_reflects_silence_cue(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0, hz=440.0, amp=14000)
    client.put(
        f"/api/projects/{project_id}/music/score-plan",
        json={
            "plan": _plan_payload(
                4.0,
                [{"time_seconds": 2.0, "action": "silence", "transition_seconds": 0.0}],
            ),
            "expected_revision": 0,
        },
    )
    assert client.post(f"/api/projects/{project_id}/music/score-preview").status_code == 200

    preview = _project_root(app, project_id) / "music" / "score-preview.wav"
    audible = _rms(preview, 0.6, 1.4)
    silent = _rms(preview, 2.3, 2.9)
    assert audible > 500, "the bed must be audible before the silence cue"
    assert silent == 0, "the preview must be silent after the silence cue"


def test_score_media_file_rejects_unknown_name(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    assert client.get(f"/api/projects/{project_id}/music/media/evil.wav").status_code == 404
    assert client.get(f"/api/projects/{project_id}/music/media/..%2Fproject.json").status_code in {404, 405}


# ---------------------------------------------------------------------------
# Effect upload
# ---------------------------------------------------------------------------


def test_upload_effect_success(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    data = _wav_bytes(0.2, hz=2000.0)

    response = client.post(
        f"/api/projects/{project_id}/music/effects",
        files={"file": ("impact.wav", data, "audio/wav")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["format"] == "wav"
    assert body["sha256"]
    assert body["duplicate"] is False
    assert body["effect_path"].startswith("music/effects/")
    assert body["effect_path"].endswith(".wav")

    # The file is stored inside the project and streamed back through the asset URL.
    stored = _project_root(app, project_id) / body["effect_path"]
    assert stored.is_file()
    assert client.get(body["url"]).status_code == 200

    # The studio snapshot lists the effect.
    effects = client.get(f"/api/projects/{project_id}/music/studio").json()["effects"]
    assert any(item["asset_id"] == body["asset_id"] for item in effects)


def test_upload_effect_dedupes_identical_content(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    data = _wav_bytes(0.2)
    first = client.post(f"/api/projects/{project_id}/music/effects", files={"file": ("hit.wav", data, "audio/wav")}).json()
    second = client.post(f"/api/projects/{project_id}/music/effects", files={"file": ("hit.wav", data, "audio/wav")}).json()
    assert second["duplicate"] is True
    assert second["asset_id"] == first["asset_id"]


def test_upload_effect_rejects_bad_extension(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    response = client.post(
        f"/api/projects/{project_id}/music/effects",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 422


def test_upload_effect_rejects_empty_file(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    response = client.post(
        f"/api/projects/{project_id}/music/effects",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    assert response.status_code == 422


def test_upload_effect_rejects_oversize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.pipeline.service as svc

    monkeypatch.setattr(svc, "MAX_EFFECT_BYTES", 100)
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    response = client.post(
        f"/api/projects/{project_id}/music/effects",
        files={"file": ("big.wav", b"0" * 200, "audio/wav")},
    )
    assert response.status_code == 422


def test_upload_effect_rejects_over_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.pipeline.service as svc

    monkeypatch.setattr(svc, "MAX_EFFECT_SECONDS", 1.0)
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    response = client.post(
        f"/api/projects/{project_id}/music/effects",
        files={"file": ("long.wav", _wav_bytes(3.0), "audio/wav")},
    )
    assert response.status_code == 422


def test_upload_effect_sanitizes_reserved_filename(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    response = client.post(
        f"/api/projects/{project_id}/music/effects",
        files={"file": ("con.wav", _wav_bytes(0.1), "audio/wav")},
    )
    assert response.status_code == 201
    assert response.json()["effect_path"].startswith("music/effects/_con-")


def test_upload_effect_404_for_unknown_project(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    response = client.post(
        "/api/projects/does-not-exist/music/effects",
        files={"file": ("hit.wav", _wav_bytes(0.1), "audio/wav")},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Auto score (deterministic fallback)
# ---------------------------------------------------------------------------


def test_auto_score_returns_suggestions(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)

    response = client.post(f"/api/projects/{project_id}/music/auto-score")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "deterministic"
    assert body["duration_seconds"] == pytest.approx(4.0, abs=0.05)
    assert body["suggestions"], "the fallback recipe always proposes cues"
    for suggestion in body["suggestions"]:
        assert {"time_seconds", "action", "label", "transition_seconds", "reason"} <= set(suggestion)
        assert 0.0 <= suggestion["time_seconds"] <= body["duration_seconds"] + 1e-6
        assert suggestion["action"] in {
            "build", "pull_back", "silence", "restore", "impact", "riser", "end_sting",
        }


def test_auto_score_does_not_modify_saved_plan(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    root = _project_root(app, project_id)
    assert not (root / "music" / "score-plan.json").exists()

    client.post(f"/api/projects/{project_id}/music/auto-score")

    # Suggestions are advisory: nothing is written to the plan.
    assert not (root / "music" / "score-plan.json").exists()
    assert client.get(f"/api/projects/{project_id}/music/studio").json()["score_plan"] is None


def test_auto_score_accepts_optional_steer(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    response = client.post(
        f"/api/projects/{project_id}/music/auto-score",
        json={"music_direction": "tense and urgent", "intensity": "expressive"},
    )
    assert response.status_code == 200
    assert response.json()["intensity"] == "expressive"


def test_auto_score_rejects_unknown_intensity(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    response = client.post(
        f"/api/projects/{project_id}/music/auto-score",
        json={"intensity": "destructive"},
    )
    assert response.status_code == 422


def test_auto_score_404_for_unknown_project(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    assert client.post("/api/projects/does-not-exist/music/auto-score").status_code == 404


# ---------------------------------------------------------------------------
# Auto score: local-LLM preference and deterministic fallback
# ---------------------------------------------------------------------------


class _FakeLlmBackend:
    """Stands in for the configured local LLM in the Studio auto-score path."""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def _patch_llm(app, payload) -> _FakeLlmBackend:
    fake = _FakeLlmBackend(payload)
    app.state.service.director.llm = fake
    return fake


def test_auto_score_prefers_the_local_llm(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    fake = _patch_llm(
        app,
        {"cues": [
            {"time_seconds": 1.0, "action": "build", "label": "LLM lift", "reason": "from the LLM"},
            {"time_seconds": 2.5, "action": "silence", "label": "LLM hold", "reason": "before the reveal"},
        ]},
    )

    response = client.post(
        f"/api/projects/{project_id}/music/auto-score",
        json={"music_direction": "tense", "intensity": "expressive"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "local_llm"
    assert body["suggestions"][0]["label"] == "LLM lift"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["structured"] is True
    assert call["thinking_budget_tokens"] is not None
    prompt = " ".join(m["content"] for m in call["messages"])
    assert "tense" in prompt  # the user's music direction reached the LLM
    assert "http" not in prompt  # and the context stayed project-local

    # The saved plan still never changes from a suggestion pass.
    assert not (_project_root(app, project_id) / "music" / "score-plan.json").exists()


def test_auto_score_falls_back_to_recipe_when_the_llm_fails(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    _patch_llm(app, RuntimeError("local LLM server is down"))

    response = client.post(f"/api/projects/{project_id}/music/auto-score")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "deterministic"
    assert body["model"] is None
    assert body["suggestions"], "the recipe still proposes cues"
    assert "local LLM" in body["note"] or "unavailable" in body["note"]


def test_auto_score_sanitizes_llm_suggestions(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    project_id = _create_project(client)
    _write_background(app, project_id, 4.0)
    _patch_llm(
        app,
        {"cues": [
            {"time_seconds": 99.0, "action": "end_sting", "label": "Late", "reason": "clamped"},
            {"time_seconds": 1.0, "action": "pull_back", "label": "Dip", "reason": "ok", "gain_db": -6},
        ]},
    )
    body = client.post(f"/api/projects/{project_id}/music/auto-score").json()
    times = [cue["time_seconds"] for cue in body["suggestions"]]
    assert times == sorted(times)
    assert max(times) <= body["duration_seconds"] + 1e-6
