"""Project-scoped TTS orchestration with restartable chunk artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

from backend.models import GenerationRequest, GenerationResult
from backend.models.errors import BackendError, BackendErrorCode, redact_secrets
from backend.schemas import Asset, AssetType, GenerationAttempt, GenerationJob, JobStatus, utc_now

from .audio import WavJoinResult, apply_wav_gain, join_wav_files_detailed, wav_duration
from .chunking import chunk_narration, chunk_narration_tagged
from .models import NarrationRequest, VoiceProfile
from .performance import (
    NoNarrationTextError,
    PerformanceScript,
    PerformanceSegment,
    validate_tagged,
)
from .performance_llm import PerformanceTagger


logger = logging.getLogger(__name__)

_PROVIDER_DIR = {
    "qwen_tts": "qwen", "step_audio_editx": "step", "chatterbox": "chatterbox",
    "fish_s2_pro": "fish", "voxcpm2": "voxcpm", "omnivoice": "omnivoice",
    "index_tts_2_5": "index", "breeze_tts_2": "breeze",
}
_DEFAULT_CHUNK = {
    "qwen_tts": 60.0, "step_audio_editx": 20.0, "chatterbox": 45.0,
    "fish_s2_pro": 30.0, "voxcpm2": 30.0, "omnivoice": 30.0, "index_tts_2_5": 30.0,
    # Provisional until the 15/30/45 s chunk benchmark lands (see handoff plan).
    "breeze_tts_2": 30.0,
}


class TTSManager:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline
        self._activation_lock = threading.Lock()

    def create_voice_profile(
        self,
        project_id: str,
        *,
        name: str,
        transcript: str,
        language: str,
        authorized: bool,
        audio: bytes,
        gain_db: float = 0.0,
    ) -> VoiceProfile:
        if not authorized:
            raise ValueError("voice-owner authorization must be confirmed")
        if not audio or len(audio) > 100 * 1024 * 1024:
            raise ValueError("reference WAV must be between 1 byte and 100 MB")
        self._validate_wav(audio)
        boosted_audio = apply_wav_gain(audio, gain_db)
        project = self.pipeline._project(project_id)
        root = self.pipeline.store.project_path(project)
        profile = VoiceProfile(
            project_id=project_id,
            name=name,
            reference_audio=Path("voices") / "pending" / "reference.wav",
            reference_transcript=transcript,
            language=language,
            authorized=True,
            gain_db=gain_db,
            audio_sha256=hashlib.sha256(boosted_audio).hexdigest(),
            source_audio_sha256=hashlib.sha256(audio).hexdigest(),
        )
        relative = Path("voices") / profile.id / "reference.wav"
        profile.reference_audio = relative
        directory = root / relative.parent
        directory.mkdir(parents=True, exist_ok=False)
        self._atomic_bytes(directory / "reference.wav", boosted_audio)
        self._atomic_json(directory / "profile.json", profile.model_dump(mode="json"))
        return profile

    def list_voice_profiles(self, project_id: str) -> list[VoiceProfile]:
        project = self.pipeline._project(project_id)
        directory = self.pipeline.store.project_path(project) / "voices"
        profiles: list[VoiceProfile] = []
        for path in sorted(directory.glob("*/profile.json")):
            try:
                profiles.append(VoiceProfile.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                logger.warning("Skipping unreadable voice profile %s: %s", path, exc)
        return profiles

    def get_voice_profile(self, project_id: str, profile_id: str) -> VoiceProfile:
        if not profile_id or any(char not in "0123456789abcdef-" for char in profile_id.lower()):
            raise KeyError("voice profile not found")
        project = self.pipeline._project(project_id)
        path = self.pipeline.store.project_path(project) / "voices" / profile_id / "profile.json"
        if not path.is_file():
            raise KeyError("voice profile not found")
        profile = VoiceProfile.model_validate_json(path.read_text(encoding="utf-8"))
        if profile.project_id != project_id or not profile.authorized:
            raise ValueError("voice profile is not authorized for this project")
        return profile

    def generate(
        self,
        project_id: str,
        request: NarrationRequest,
        *,
        job_id: str,
        activate: bool = True,
    ) -> Path:
        project = self.pipeline._project(project_id)
        root = self.pipeline.store.project_path(project)
        profile = (
            self.get_voice_profile(project_id, request.voice_profile_id)
            if request.voice_profile_id else None
        )
        if profile is None and request.provider not in {"chatterbox", "qwen_tts"}:
            raise ValueError(f"{request.provider} requires an authorized reference voice")
        if profile is None and request.enhance_with_step:
            raise ValueError("Step enhancement requires an authorized reference voice")
        if request.enhance_with_step and request.provider != "qwen_tts":
            raise ValueError("Step enhancement is supported after Qwen generation")
        text = self.resolve_narration_text(project_id, request.text)
        seconds = request.chunk_seconds or _DEFAULT_CHUNK[request.provider]
        performance, performance_meta = self._resolve_performance(project_id, request)
        chunk_specs = self._narration_chunks(project_id, request.text, seconds, performance)
        chunks = [str(item["text"]) for item in chunk_specs]
        reference = root / profile.reference_audio if profile is not None else None
        profile_id = profile.id if profile is not None else None
        reference_text = profile.reference_transcript if profile is not None else ""
        job_component = self._safe_component(job_id)
        provider_dir = root / "audio" / _PROVIDER_DIR[request.provider] / job_component
        provider_dir.mkdir(parents=True, exist_ok=True)
        backend = self.pipeline.registry.get(request.provider)
        outputs: list[Path] = []
        failures: list[str] = []
        worker_started = self.pipeline.tts_workers.ensure_running_if_managed(request.provider)
        try:
            backend.load()
        except Exception:
            if worker_started:
                self.pipeline.tts_workers.stop(request.provider)
            raise
        try:
            for index, (chunk, chunk_spec) in enumerate(zip(chunks, chunk_specs), start=1):
                filename = f"{index:04d}.wav"
                settings = {
                    "filename": filename,
                    "language": request.language,
                    "reference_text": reference_text,
                    "mode": "clone",
                    "exaggeration": request.exaggeration,
                    "cfg_weight": request.cfg_weight,
                    "temperature": request.temperature,
                    "speaker": request.speaker,
                    "voice_instruction": request.voice_instruction,
                    "guidance_scale": request.guidance_scale,
                    "inference_timesteps": request.inference_timesteps,
                    "num_steps": request.num_steps,
                    "speed": request.speed,
                    "breeze_mode": request.breeze_mode,
                }
                try:
                    result = backend.generate(GenerationRequest(
                        job_id=f"{job_id}:{index}", output_dir=provider_dir, prompt=chunk,
                        seed=request.seed + index - 1,
                        references=(reference,) if reference is not None else (),
                        settings=settings,
                    ))
                    output = result.outputs[0]
                    duration = wav_duration(output)
                    metadata = {
                        "chunk": index, "text": chunk, "provider": request.provider,
                        "voice_profile_id": profile_id, "duration": duration,
                        "seed": request.seed + index - 1, "status": "completed",
                        "scene_id": chunk_spec.get("scene_id"),
                        "scene_index": chunk_spec.get("scene_index"),
                        "scene_title": chunk_spec.get("scene_title"),
                        **dict(result.metadata),
                    }
                    self._atomic_json(output.with_suffix(".json"), metadata)
                    self.pipeline._record_asset(
                        project, None, output, AssetType.NARRATION, result, role="narration_chunk",
                    )
                    outputs.append(output)
                except Exception as exc:
                    error = str(exc)[:1000]
                    failures.append(f"chunk {index}: {error}")
                    self._atomic_json(provider_dir / f"{index:04d}.json", {
                        "chunk": index, "text": chunk, "provider": request.provider,
                        "voice_profile_id": profile_id, "seed": request.seed + index - 1,
                        "scene_id": chunk_spec.get("scene_id"),
                        "scene_index": chunk_spec.get("scene_index"),
                        "scene_title": chunk_spec.get("scene_title"),
                        "status": "failed", "error": error,
                    })
                    descriptor = backend.descriptor()
                    self.pipeline.database.save_attempt(GenerationAttempt(
                        job_id=job_id, backend=request.provider, model=descriptor.model_name,
                        model_version=descriptor.model_version, parameters=settings,
                        seed=request.seed + index - 1, success=False, error=error,
                    ))
                    # A retryable service failure (HTTP 5xx, timeout, lost
                    # worker) is systemic rather than specific to this text
                    # chunk. Do not hammer the same failed worker for every
                    # remaining chunk in a long narration.
                    if isinstance(exc, BackendError) and exc.retryable:
                        break
        finally:
            try:
                if request.unload_after or request.enhance_with_step or worker_started:
                    backend.unload()
            finally:
                if worker_started:
                    self.pipeline.tts_workers.stop(request.provider)
        if failures:
            raise RuntimeError("; ".join(failures))
        if request.enhance_with_step:
            assert profile is not None
            outputs = self._enhance_with_step(
                project, profile, chunks, outputs, request, job_id=job_id,
            )
        chunk_records = self._chunk_records(root, outputs, chunk_specs, request, job_id)
        take = root / "narration" / "takes" / request.provider / f"{job_component}.wav"
        join_result = self._publish_master_join(outputs, take, pause_ms=request.pause_ms)
        duration = join_result.duration_seconds
        script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        master_result = GenerationResult(
            outputs=(take,),
            metadata={
                "backend": request.provider,
                "model": backend.descriptor().model_name,
                "model_version": backend.descriptor().model_version,
                "workflow_version": "tts-narration-v3",
                "seed": request.seed,
                "prompt": text,
                "settings": {
                    "role": "narration_take", "provider": request.provider,
                    "job_id": job_id, "voice_profile_id": profile_id,
                    "built_in_voice": profile is None,
                    "speaker": request.speaker if profile is None else None,
                    "chunk_count": len(outputs), "chunk_seconds": seconds,
                    "pause_ms": request.pause_ms, "duration": duration,
                    "inserted_pause_ms": [
                        round(seconds * 1000) for seconds in join_result.inserted_pause_seconds
                    ],
                    "script_sha256": script_hash,
                    "scene_script_sha256": self._scene_script_hash(project_id),
                    **({"performance_tags": performance_meta}
                       if performance_meta is not None else {}),
                    "timing_mode": "scene_audio_v1" if request.text is None else "override",
                    "chunks": chunk_records,
                    "scene_durations": self._scene_durations(
                        chunk_records, join_result.inserted_pause_seconds,
                    ),
                    "request": request.model_dump(mode="json", exclude={"text"}),
                },
            },
        )
        asset = self.pipeline._record_asset(
            project, None, take, AssetType.NARRATION, master_result,
            role="narration_take",
            job_id=job_id if self.pipeline.jobs.get(job_id) is not None else None,
        )
        self._atomic_json(take.with_suffix(".json"), asset.model_dump(mode="json"))
        if activate:
            return self.activate_take(project_id, asset.id, stage_job_id=job_id)
        return take

    def _narration_scenes(self, project_id: str) -> list[Any]:
        """Scene records, with a portable plan.json fallback for imported projects."""
        project = self.pipeline._project(project_id)
        scenes = self.pipeline.database.list_scenes(project.id)
        if not scenes:
            plan_path = self.pipeline.store.project_path(project) / "plan.json"
            if plan_path.is_file():
                scenes = self.pipeline.store.load_plan(project.slug).scenes
        return scenes

    def _narration_chunks(
        self,
        project_id: str,
        override: str | None,
        target_seconds: float,
        performance: PerformanceScript | None = None,
    ) -> list[dict[str, Any]]:
        """Split at scene boundaries so rendered pictures can follow measured speech.

        When a Fish S2 Pro delivery-tag script is active, each scene segment is
        chunked from its tagged text (cue-aware) so the cues reach the model
        while the chunk-to-scene mapping — and therefore picture sync — is
        unchanged.  Stale segments (source no longer matches the current
        narration) fall back to the clean text.
        """
        if override is not None:
            text = override.strip()
            segment = self._performance_segment(performance, "override")
            if segment is not None and segment.source == text:
                text = segment.tagged
                chunks = chunk_narration_tagged(text, target_seconds)
            else:
                chunks = chunk_narration(text, target_seconds)
            return [{"text": text} for text in chunks]
        scenes = self._narration_scenes(project_id)
        chunks: list[dict[str, Any]] = []
        for scene in scenes:
            source = scene.narration.strip()
            segment = self._performance_segment(performance, f"scene:{scene.id}")
            if segment is not None and segment.source == source:
                chunk_texts = chunk_narration_tagged(segment.tagged, target_seconds)
            else:
                chunk_texts = chunk_narration(source, target_seconds)
            for text in chunk_texts:
                chunks.append({
                    "text": text,
                    "scene_id": scene.id,
                    "scene_index": scene.index,
                    "scene_title": scene.title,
                })
        if not chunks:
            raise ValueError("narration text is empty")
        return chunks

    @staticmethod
    def _performance_segment(
        performance: PerformanceScript | None, key: str,
    ) -> PerformanceSegment | None:
        if performance is None:
            return None
        return next((segment for segment in performance.segments if segment.key == key), None)

    @staticmethod
    def _chunk_records(
        root: Path,
        outputs: list[Path],
        specs: list[dict[str, Any]],
        request: NarrationRequest,
        job_id: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for index, (output, spec) in enumerate(zip(outputs, specs), start=1):
            records.append({
                "index": index,
                "text": spec["text"],
                "scene_id": spec.get("scene_id"),
                "scene_index": spec.get("scene_index"),
                "scene_title": spec.get("scene_title"),
                "duration": wav_duration(output),
                "seed": request.seed + index - 1,
                "provider": request.provider,
                "job_id": job_id,
                "filepath": output.relative_to(root).as_posix(),
            })
        return records

    @staticmethod
    def _scene_durations(
        chunks: list[dict[str, Any]], inserted_pauses: tuple[float, ...],
    ) -> list[dict[str, Any]]:
        if not chunks or any(not item.get("scene_id") for item in chunks):
            return []
        if len(inserted_pauses) != len(chunks) - 1:
            raise ValueError("one inserted-pause value is required for each chunk boundary")
        totals: dict[str, dict[str, Any]] = {}
        for position, chunk in enumerate(chunks):
            scene_id = str(chunk["scene_id"])
            entry = totals.setdefault(scene_id, {
                "scene_id": scene_id,
                "scene_index": chunk.get("scene_index"),
                "duration": 0.0,
            })
            entry["duration"] += float(chunk["duration"])
            if position < len(chunks) - 1:
                entry["duration"] += inserted_pauses[position]
        return list(totals.values())

    def _scene_script_hash(self, project_id: str) -> str:
        scenes = self.pipeline.database.list_scenes(project_id)
        payload = [(scene.id, scene.narration) for scene in scenes]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()

    def list_narration_takes(self, project_id: str) -> tuple[list[Asset], str | None]:
        """Return immutable takes plus one usable legacy master, and the active ID."""
        project = self.pipeline._project(project_id)
        root = self.pipeline.store.project_path(project)
        self._recover_take_assets(project_id, root)
        candidates = [
            asset for asset in self.pipeline.database.list_assets(project_id)
            if asset.type is AssetType.NARRATION
            and asset.settings.get("role") in {"narration_take", "narration"}
            and (root / asset.filepath).is_file()
        ]
        immutable = [
            asset for asset in candidates
            if asset.settings.get("role") == "narration_take"
        ]
        # Historical releases indexed every replacement at master.wav. Those
        # rows no longer identify distinct bytes, so expose only the newest as
        # a legacy take instead of presenting duplicates that play one file.
        legacy = [asset for asset in candidates if asset.settings.get("role") == "narration"]
        takes = [*immutable, *legacy[-1:]]
        manifest = self._read_narration_manifest(root)
        active_id = manifest.get("active_asset_id")
        if not isinstance(active_id, str) or not any(asset.id == active_id for asset in takes):
            active_id = legacy[-1].id if legacy else None
        return takes, active_id

    def list_take_chunks(self, project_id: str, asset_id: str) -> list[dict[str, Any]]:
        """Return playable chunk metadata for a project-owned narration take."""
        project = self.pipeline._project(project_id)
        asset = self.pipeline.database.get_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise KeyError("narration take not found")
        if asset.type is not AssetType.NARRATION or asset.settings.get("role") != "narration_take":
            return []
        root = self.pipeline.store.project_path(project)
        raw = asset.settings.get("chunks")
        chunks = [dict(item) for item in raw] if isinstance(raw, list) else []
        if not chunks:
            provider = str(asset.settings.get("provider") or asset.backend)
            job_id = asset.settings.get("job_id")
            provider_name = _PROVIDER_DIR.get(provider)
            if provider_name and isinstance(job_id, str):
                directory = root / "audio" / provider_name / self._safe_component(job_id)
                for sidecar in sorted(directory.glob("*.json")):
                    try:
                        item = json.loads(sidecar.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    audio = sidecar.with_suffix(".wav")
                    if item.get("status") != "completed" or not audio.is_file():
                        continue
                    chunks.append({
                        "index": int(item.get("chunk", len(chunks) + 1)),
                        "text": str(item.get("text", "")),
                        "duration": float(item.get("duration") or wav_duration(audio)),
                        "seed": int(item.get("seed", asset.seed + len(chunks))),
                        "provider": provider,
                        "job_id": job_id,
                        "filepath": audio.relative_to(root).as_posix(),
                        "scene_id": item.get("scene_id"),
                        "scene_index": item.get("scene_index"),
                        "scene_title": item.get("scene_title"),
                    })
        valid: list[dict[str, Any]] = []
        for item in chunks:
            relative = Path(str(item.get("filepath", "")))
            path = (root / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or root.resolve() not in path.parents:
                continue
            if not path.is_file():
                continue
            item["duration"] = float(item.get("duration") or wav_duration(path))
            valid.append(item)
        return sorted(valid, key=lambda item: int(item.get("index", 0)))

    def take_chunk_path(self, project_id: str, asset_id: str, chunk_index: int) -> Path:
        project = self.pipeline._project(project_id)
        chunk = next(
            (item for item in self.list_take_chunks(project_id, asset_id)
             if int(item.get("index", 0)) == chunk_index),
            None,
        )
        if chunk is None:
            raise KeyError("narration chunk not found")
        return self.pipeline.store.project_path(project) / str(chunk["filepath"])

    def queue_chunk_regeneration(
        self, project_id: str, asset_id: str, chunk_index: int,
    ) -> GenerationJob:
        asset = self.pipeline.database.get_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise KeyError("narration take not found")
        chunks = self.list_take_chunks(project_id, asset_id)
        if not any(int(item.get("index", 0)) == chunk_index for item in chunks):
            raise KeyError("narration chunk not found")
        active = next(
            (job for job in self.pipeline.jobs.list(project_id)
             if job.stage in {"narration", "narration_chunk"}
             and job.status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}),
            None,
        )
        if active is not None:
            raise RuntimeError("Another narration job is already running for this project.")
        return self.pipeline.jobs.enqueue(GenerationJob(
            project_id=project_id,
            stage="narration_chunk",
            backend=str(asset.settings.get("provider") or asset.backend),
            parameters={"source_asset_id": asset_id, "chunk_index": chunk_index},
        ))

    def run_chunk_regeneration_job(self, job_id: str) -> Path:
        job = self.pipeline.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        try:
            self.pipeline.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
            self.pipeline.jobs.transition(job.id, JobStatus.LOADING_MODEL, progress=0.1)
            self.pipeline.jobs.transition(job.id, JobStatus.GENERATING, progress=0.2)
            with self.pipeline._lock, self.pipeline._gpu_lock:
                output = self._regenerate_chunk(
                    job.project_id,
                    str(job.parameters["source_asset_id"]),
                    int(job.parameters["chunk_index"]),
                    job.id,
                )
            current = self.pipeline.jobs.get(job.id)
            if current is not None and current.status is JobStatus.CANCELED:
                return output
            self.pipeline.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
            self.pipeline.jobs.complete(job.id)
            return output
        except Exception as exc:
            current = self.pipeline.jobs.get(job.id)
            if current is not None and current.status not in {
                JobStatus.FAILED, JobStatus.CANCELED, JobStatus.COMPLETED,
            }:
                self.pipeline.jobs.fail(job.id, redact_secrets(exc))
            raise

    def _regenerate_chunk(
        self, project_id: str, asset_id: str, chunk_index: int, job_id: str,
    ) -> Path:
        project = self.pipeline._project(project_id)
        root = self.pipeline.store.project_path(project)
        source_asset = self.pipeline.database.get_asset(asset_id)
        if source_asset is None:
            raise KeyError("narration take not found")
        chunks = self.list_take_chunks(project_id, asset_id)
        selected_position = next(
            position for position, item in enumerate(chunks)
            if int(item["index"]) == chunk_index
        )
        selected = chunks[selected_position]
        request_payload = dict(source_asset.settings.get("request") or {})
        request_payload.update({
            "provider": source_asset.settings.get("provider") or source_asset.backend,
            "seed": int(selected.get("seed", source_asset.seed)),
            "text": str(selected["text"]),
        })
        request = NarrationRequest.model_validate(request_payload)
        profile = (
            self.get_voice_profile(project_id, request.voice_profile_id)
            if request.voice_profile_id else None
        )
        reference = root / profile.reference_audio if profile is not None else None
        reference_text = profile.reference_transcript if profile is not None else ""
        directory = root / "audio" / _PROVIDER_DIR[request.provider] / self._safe_component(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        backend = self.pipeline.registry.get(request.provider)
        worker_started = self.pipeline.tts_workers.ensure_running_if_managed(request.provider)
        try:
            backend.load()
            result = backend.generate(GenerationRequest(
                job_id=f"{job_id}:{chunk_index}", output_dir=directory,
                prompt=str(selected["text"]), seed=request.seed,
                references=(reference,) if reference is not None else (),
                settings={
                    "filename": f"{chunk_index:04d}.wav",
                    "language": request.language,
                    "reference_text": reference_text,
                    "mode": "clone",
                    "exaggeration": request.exaggeration,
                    "cfg_weight": request.cfg_weight,
                    "temperature": request.temperature,
                    "speaker": request.speaker,
                    "voice_instruction": request.voice_instruction,
                    "guidance_scale": request.guidance_scale,
                    "inference_timesteps": request.inference_timesteps,
                    "num_steps": request.num_steps,
                    "speed": request.speed,
                    "breeze_mode": request.breeze_mode,
                },
            ))
        finally:
            try:
                if request.unload_after or request.enhance_with_step or worker_started:
                    backend.unload()
            finally:
                if worker_started:
                    self.pipeline.tts_workers.stop(request.provider)
        replacement = result.outputs[0]
        replacement_metadata = {
            "chunk": chunk_index,
            "text": selected["text"],
            "provider": request.provider,
            "voice_profile_id": request.voice_profile_id,
            "duration": wav_duration(replacement),
            "seed": request.seed,
            "status": "completed",
            "scene_id": selected.get("scene_id"),
            "scene_index": selected.get("scene_index"),
            "scene_title": selected.get("scene_title"),
            **dict(result.metadata),
        }
        self._atomic_json(replacement.with_suffix(".json"), replacement_metadata)
        self.pipeline._record_asset(
            project, None, replacement, AssetType.NARRATION, result,
            role="narration_chunk", job_id=job_id,
            extra_settings={
                "chunk": chunk_index,
                "scene_id": selected.get("scene_id"),
                "scene_index": selected.get("scene_index"),
            },
        )
        if request.enhance_with_step:
            if profile is None:
                raise ValueError("Step enhancement requires an authorized reference voice")
            replacement = self._enhance_with_step(
                project, profile, [str(selected["text"])], [replacement], request, job_id=job_id,
            )[0]
        updated_chunks = [dict(item) for item in chunks]
        replacement_record = dict(selected)
        replacement_record.update({
            "duration": wav_duration(replacement),
            "seed": request.seed,
            "job_id": job_id,
            "filepath": replacement.relative_to(root).as_posix(),
        })
        updated_chunks[selected_position] = replacement_record
        pause_ms = int(source_asset.settings.get("pause_ms", request.pause_ms))
        outputs = [root / str(item["filepath"]) for item in updated_chunks]
        take = root / "narration" / "takes" / request.provider / f"{self._safe_component(job_id)}.wav"
        join_result = self._publish_master_join(outputs, take, pause_ms=pause_ms)
        duration = join_result.duration_seconds
        settings = {
            **source_asset.settings,
            "job_id": job_id,
            "duration": duration,
            "chunks": updated_chunks,
            "inserted_pause_ms": [
                round(seconds * 1000) for seconds in join_result.inserted_pause_seconds
            ],
            "scene_durations": self._scene_durations(
                updated_chunks, join_result.inserted_pause_seconds,
            ),
            "derived_from_asset_id": source_asset.id,
        }
        take_result = GenerationResult(outputs=(take,), metadata={
            "backend": request.provider,
            "model": backend.descriptor().model_name,
            "model_version": backend.descriptor().model_version,
            "workflow_version": "tts-narration-v3",
            "seed": source_asset.seed,
            "prompt": source_asset.prompt,
            "settings": settings,
        })
        asset = self.pipeline._record_asset(
            project, None, take, AssetType.NARRATION, take_result,
            role="narration_take", job_id=job_id,
        )
        self._atomic_json(take.with_suffix(".json"), asset.model_dump(mode="json"))
        return self.activate_take(project_id, asset.id, stage_job_id=job_id)

    def active_scene_durations(self, project_id: str) -> dict[str, float] | None:
        """Measured per-scene narration lengths for the active, current-script take."""
        takes, active_id = self.list_narration_takes(project_id)
        asset = next((item for item in takes if item.id == active_id), None)
        if asset is None or asset.settings.get("timing_mode") != "scene_audio_v1":
            return None
        if asset.settings.get("scene_script_sha256") != self._scene_script_hash(project_id):
            return None
        raw = asset.settings.get("scene_durations")
        if not isinstance(raw, list):
            return None
        result = {
            str(item["scene_id"]): float(item["duration"])
            for item in raw if isinstance(item, dict) and item.get("scene_id")
        }
        return result or None

    def _recover_take_assets(self, project_id: str, root: Path) -> None:
        """Re-index valid portable take sidecars that are absent from SQLite."""
        known = {asset.id for asset in self.pipeline.database.list_assets(project_id)}
        for sidecar in sorted((root / "narration" / "takes").glob("*/*.json")):
            try:
                asset = Asset.model_validate_json(sidecar.read_text(encoding="utf-8"))
                expected = sidecar.with_suffix(".wav").relative_to(root)
                if (
                    asset.id in known
                    or asset.project_id != project_id
                    or asset.type is not AssetType.NARRATION
                    or asset.settings.get("role") != "narration_take"
                    or asset.filepath != expected
                    or not (root / expected).is_file()
                ):
                    continue
                self.pipeline.database.save_asset(asset)
                known.add(asset.id)
            except (OSError, ValueError):
                logger.warning("Skipping unreadable narration take sidecar %s", sidecar)

    def activate_take(
        self,
        project_id: str,
        asset_id: str,
        *,
        stage_job_id: str | None = None,
    ) -> Path:
        """Atomically publish one project-owned narration take as master.wav."""
        project = self.pipeline._project(project_id)
        asset = self.pipeline.database.get_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise KeyError("narration take not found")
        if asset.type is not AssetType.NARRATION or asset.settings.get("role") not in {
            "narration_take", "narration",
        }:
            raise ValueError("asset is not a selectable narration take")
        root = self.pipeline.store.project_path(project)
        source = root / asset.filepath
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError("narration take file is missing")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if asset.hash and digest != asset.hash:
            raise ValueError("narration take file does not match its recorded hash")
        master = root / "narration" / "master.wav"
        with self._activation_lock:
            previous = self._read_narration_manifest(root)
            gains = self._take_gains_from_manifest(previous)
            if source.resolve() != master.resolve():
                self._atomic_copy(source, master)
            self._atomic_json(self._manifest_path(root), {
                "version": 2,
                "active_asset_id": asset.id,
                "active_file": asset.filepath.as_posix(),
                "take_gains_db": gains,
                "updated_at": utc_now().isoformat(),
            })
            self.pipeline._mark_stage(
                project, "narration", [master], stage_job_id or f"activation:{asset.id}",
            )
            self.pipeline._invalidate_stages(project, {
                "subtitles", "timeline", "render_preview", "quality_control",
                "render_final", "thumbnails", "metadata",
            })
        return master

    def narration_take_gains(self, project_id: str) -> dict[str, float]:
        """Return saved, non-destructive playback/render gain for each take."""
        project = self.pipeline._project(project_id)
        root = self.pipeline.store.project_path(project)
        return self._take_gains_from_manifest(self._read_narration_manifest(root))

    def active_narration_gain(self, project_id: str) -> float:
        """Return the gain attached to the take currently published as master."""
        project = self.pipeline._project(project_id)
        root = self.pipeline.store.project_path(project)
        manifest = self._read_narration_manifest(root)
        active_id = manifest.get("active_asset_id")
        if not isinstance(active_id, str):
            return 0.0
        return self._take_gains_from_manifest(manifest).get(active_id, 0.0)

    def set_narration_take_gain(
        self, project_id: str, asset_id: str, gain_db: float,
    ) -> float:
        """Persist take gain without modifying immutable narration WAV bytes."""
        if not math.isfinite(gain_db) or not 0 <= gain_db <= 24:
            raise ValueError("narration gain must be between 0 and 24 dB")
        project = self.pipeline._project(project_id)
        asset = self.pipeline.database.get_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise KeyError("narration take not found")
        if asset.type is not AssetType.NARRATION or asset.settings.get("role") not in {
            "narration_take", "narration",
        }:
            raise ValueError("asset is not a selectable narration take")
        root = self.pipeline.store.project_path(project)
        with self._activation_lock:
            manifest = self._read_narration_manifest(root)
            gains = self._take_gains_from_manifest(manifest)
            if gain_db == 0:
                gains.pop(asset_id, None)
            else:
                gains[asset_id] = float(gain_db)
            manifest.update({
                "version": 2,
                "take_gains_db": gains,
                "updated_at": utc_now().isoformat(),
            })
            self._atomic_json(self._manifest_path(root), manifest)
            if manifest.get("active_asset_id") == asset_id:
                self.pipeline._invalidate_stages(project, {
                    "timeline", "render_preview", "quality_control", "render_final",
                    "thumbnails", "metadata",
                })
        return float(gain_db)

    def activate_take_path(
        self, project_id: str, path: Path, *, stage_job_id: str | None = None,
    ) -> Path:
        project = self.pipeline._project(project_id)
        relative = path.relative_to(self.pipeline.store.project_path(project))
        asset = next(
            (
                item for item in reversed(self.pipeline.database.list_assets(project_id))
                if item.filepath == relative
                and item.settings.get("role") == "narration_take"
            ),
            None,
        )
        if asset is None:
            raise KeyError("narration take not found")
        return self.activate_take(project_id, asset.id, stage_job_id=stage_job_id)

    @staticmethod
    def _manifest_path(root: Path) -> Path:
        return root / "narration" / "takes.json"

    @classmethod
    def _read_narration_manifest(cls, root: Path) -> dict[str, Any]:
        path = cls._manifest_path(root)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _take_gains_from_manifest(manifest: dict[str, Any]) -> dict[str, float]:
        raw = manifest.get("take_gains_db")
        if not isinstance(raw, dict):
            return {}
        gains: dict[str, float] = {}
        for asset_id, value in raw.items():
            try:
                gain = float(value)
            except (TypeError, ValueError):
                continue
            if isinstance(asset_id, str) and math.isfinite(gain) and 0 <= gain <= 24:
                gains[asset_id] = gain
        return gains

    @staticmethod
    def _safe_component(value: str) -> str:
        component = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
        if not component:
            raise ValueError("job id cannot be used as a narration take name")
        return component[:160]

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copyfile(source, temporary)
            handle = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def resolve_narration_text(self, project_id: str, override: str | None) -> str:
        """Resolve an override or the project's current persisted scene narration."""
        project = self.pipeline._project(project_id)
        if override is not None:
            text = override.strip()
            if not text:
                raise ValueError("narration text is empty")
            return text

        # Scene records are what the Script and Scene Editor screens display, so
        # prefer them over plan.json. This also makes edited narration take effect.
        scenes = self.pipeline.database.list_scenes(project.id)
        text = "\n\n".join(scene.narration.strip() for scene in scenes if scene.narration.strip())
        if text:
            return text

        # Keep a portable-file fallback for projects imported without their SQLite
        # index. A normally planned project has both representations.
        plan_path = self.pipeline.store.project_path(project) / "plan.json"
        if plan_path.is_file():
            plan = self.pipeline.store.load_plan(project.slug)
            text = "\n\n".join(
                scene.narration.strip() for scene in plan.scenes if scene.narration.strip()
            )
            if text:
                return text

        raise ValueError(
            "No planned narration exists for this project. Run planning from the "
            "Script screen, or enter text in Voice > Script override."
        )

    # ------------------------------------------------------------------
    # Fish S2 Pro delivery tags
    # ------------------------------------------------------------------

    def _performance_path(self, project_id: str) -> Path:
        project = self.pipeline._project(project_id)
        return (
            self.pipeline.store.project_path(project)
            / "narration" / "performance-tags.json"
        )

    def get_performance_script(self, project_id: str) -> PerformanceScript | None:
        """Load the portable delivery-tag script, or ``None`` when absent."""
        self.pipeline._project(project_id)
        path = self._performance_path(project_id)
        if not path.is_file():
            return None
        try:
            return PerformanceScript.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable performance script %s: %s", path, exc)
            return None

    def clear_performance_script(self, project_id: str) -> None:
        self.pipeline._project(project_id)
        self._performance_path(project_id).unlink(missing_ok=True)

    def save_performance_script(
        self,
        project_id: str,
        script: PerformanceScript,
        *,
        accept: bool = False,
    ) -> PerformanceScript:
        """Persist the script; validates every segment against its clean source.

        ``accept=True`` keeps a hand edit the validator dislikes (logged as
        ``manually_edited``) so a creator can always win over the rules.
        """
        self.pipeline._project(project_id)
        problems: list[str] = []
        for segment in script.segments:
            errors = validate_tagged(segment.source, segment.tagged)
            if errors:
                problems.append(f"segment {segment.key}: {'; '.join(errors)}")
        if problems and not accept:
            raise ValueError("; ".join(problems))
        if problems:
            logger.warning(
                "Saving manually_edited performance tags for project %s despite "
                "validation: %s", project_id, "; ".join(problems),
            )
        self._atomic_json(
            self._performance_path(project_id), script.model_dump(mode="json"),
        )
        return script

    def performance_script_is_stale(
        self, project_id: str, script: PerformanceScript,
    ) -> bool:
        """True when any scene segment no longer matches the current narration."""
        scenes = {scene.id: scene for scene in self._narration_scenes(project_id)}
        for segment in script.segments:
            if segment.scene_id is None:
                continue
            scene = scenes.get(segment.scene_id)
            if scene is None or scene.narration.strip() != segment.source:
                return True
        return False

    def _performance_source_segments(
        self, project_id: str, override: str | None,
    ) -> list[PerformanceSegment]:
        """The narration that would actually be generated, as taggable segments."""
        if override is not None:
            text = override.strip()
            if not text:
                raise ValueError("narration text is empty")
            return [PerformanceSegment(key="override", source=text, tagged=text)]
        segments = [
            PerformanceSegment(
                key=f"scene:{scene.id}",
                source=scene.narration.strip(),
                tagged=scene.narration.strip(),
                scene_id=scene.id,
                scene_index=scene.index,
                scene_title=scene.title,
            )
            for scene in self._narration_scenes(project_id)
            if scene.narration.strip()
        ]
        if not segments:
            raise NoNarrationTextError(
                "No planned narration exists for this project. Run planning from "
                "the Script screen, or enter text in Voice > Script override."
            )
        return segments

    def _require_tagging_llm(self, project: Any) -> tuple[Any, str]:
        """Mirror ``PipelineService._require_selected_llm_model`` for tagging."""
        llm = self.pipeline.director.llm
        if self.pipeline.mock_mode or llm is None:
            raise BackendError(
                BackendErrorCode.MODEL_SELECTION_REQUIRED,
                "The local LLM is not available for delivery tagging. Start the "
                "local LLM service or disable mock mode.",
            )
        model = project.selected_llm_model.strip()
        if not model or model == "auto":
            raise BackendError(
                BackendErrorCode.MODEL_SELECTION_REQUIRED,
                "Choose a local LLM model for this project before adding "
                "delivery tags.",
            )
        # selected_model performs a model-list request and validates the exact
        # project selection without asking the external router to load anything.
        llm.selected_model(model=model)
        return llm, model

    def _video_context(self, project: Any) -> str:
        """A short description of the video so the tagger can shape delivery.

        The LLM otherwise only sees each line in isolation; the project's
        subject, style, and the creator's instructions tell it what kind of
        narration it is directing.
        """
        lines: list[str] = []
        if project.title:
            lines.append(f"Title: {project.title}")
        if project.topic:
            lines.append(f"About: {project.topic}")
        if project.style:
            lines.append(f"Style: {project.style}")
        if project.audience:
            lines.append(f"Audience: {project.audience}")
        if project.instructions:
            lines.append(f"Creator instructions: {project.instructions}")
        return "\n".join(lines)

    def _scene_outline(
        self, project_id: str, current_scene_id: str | None,
    ) -> str:
        """A compact map of the narration so one segment is tagged in context.

        For a scene segment this shows the surrounding scene titles (marking
        the current one) plus the immediately adjacent narration, so a
        single-segment regeneration knows where the line sits in the story.
        For an override segment (no scene) it lists the planned scene titles.
        """
        scenes = sorted(self._narration_scenes(project_id), key=lambda s: s.index)
        if not scenes:
            return ""
        lines = [f"Scene outline ({len(scenes)} scenes total):"]
        position = next(
            (i for i, scene in enumerate(scenes) if scene.id == current_scene_id),
            None,
        )
        if position is None:
            for scene in scenes[:12]:
                title = scene.title or f"Scene {scene.index + 1}"
                lines.append(f"Scene {scene.index + 1}: {title}")
            if len(scenes) > 12:
                lines.append(f"... and {len(scenes) - 12} more.")
            return "\n".join(lines)
        low = max(0, position - 3)
        high = min(len(scenes) - 1, position + 3)
        for scene in scenes[low:high + 1]:
            marker = "  <-- current" if scene.id == current_scene_id else ""
            title = scene.title or f"Scene {scene.index + 1}"
            lines.append(f"Scene {scene.index + 1}: {title}{marker}")
        if position > 0:
            previous = scenes[position - 1]
            lines.append(
                f"\nImmediately before (Scene {previous.index + 1}): "
                f"{previous.narration}"
            )
        if position < len(scenes) - 1:
            following = scenes[position + 1]
            lines.append(
                f"\nImmediately after (Scene {following.index + 1}): "
                f"{following.narration}"
            )
        return "\n".join(lines)

    def generate_performance_script(
        self,
        project_id: str,
        *,
        text: str | None = None,
        intensity: str = "balanced",
        notes: str = "",
    ) -> tuple[PerformanceScript, list[str]]:
        """Tag the narration with the local LLM and persist the script."""
        project = self.pipeline._project(project_id)
        segments = self._performance_source_segments(project_id, text)
        llm, model = self._require_tagging_llm(project)
        voice_settings = project.settings.get("voice", {})
        language = str(voice_settings.get("language", "en") or "en")
        tagger = PerformanceTagger(llm)
        script, warnings = tagger.tag(
            segments,
            intensity=intensity,
            notes=notes,
            language=language,
            model=model,
            context=self._video_context(project),
        )
        script.source_sha256 = hashlib.sha256(
            script.source_text.encode("utf-8"),
        ).hexdigest()
        self.save_performance_script(project_id, script)
        return script, warnings

    def regenerate_performance_segment(
        self,
        project_id: str,
        key: str,
        *,
        intensity: str = "balanced",
        notes: str = "",
    ) -> tuple[PerformanceScript, list[str]]:
        """Re-tag one segment with the local LLM and persist the updated script.

        Only the named segment is re-sent to the LLM (with the per-segment
        repair/degrade logic); every other segment keeps its stored tags.  The
        regenerated segment is validated by the tagger, so it is always safe to
        persist.  ``accept=True`` on save preserves any previously-accepted
        hand edits on the other segments.
        """
        project = self.pipeline._project(project_id)
        script = self.get_performance_script(project_id)
        if script is None:
            raise ValueError("no delivery-tag script exists to regenerate")
        segment = next((s for s in script.segments if s.key == key), None)
        if segment is None:
            raise ValueError(f"unknown segment key: {key}")
        llm, model = self._require_tagging_llm(project)
        voice_settings = project.settings.get("voice", {})
        language = str(voice_settings.get("language", "en") or "en")
        context = self._video_context(project)
        outline = self._scene_outline(project_id, segment.scene_id)
        if outline:
            context = f"{context}\n\n{outline}" if context else outline
        tagger = PerformanceTagger(llm)
        result, warnings = tagger.tag(
            [segment], intensity=intensity, notes=notes,
            language=language, model=model, context=context,
        )
        new_tagged = result.segments[0].tagged
        for s in script.segments:
            if s.key == key:
                s.tagged = new_tagged
        self.save_performance_script(project_id, script, accept=True)
        return script, warnings

    def _resolve_performance(
        self, project_id: str, request: NarrationRequest,
    ) -> tuple[PerformanceScript | None, dict[str, Any] | None]:
        """Resolve the delivery-tag script for a narration run.

        Only Fish S2 Pro consumes cues; every other provider gets clean text
        and a ``reason: "provider"`` marker so the take metadata explains why
        the toggle had no effect.
        """
        if not request.use_performance_tags:
            return None, None
        if request.provider != "fish_s2_pro":
            return None, {"enabled": False, "reason": "provider"}
        script = self.get_performance_script(project_id)
        if script is None:
            return None, {"enabled": False, "reason": "no_script"}
        used, skipped = self._performance_usage(project_id, request.text, script)
        return script, {
            "enabled": True,
            "segments_used": used,
            "segments_skipped": skipped,
            "model": script.model,
            "sha256": script.source_sha256,
        }

    def _performance_usage(
        self,
        project_id: str,
        override: str | None,
        script: PerformanceScript,
    ) -> tuple[int, int]:
        """Count segments whose stored source still matches the current text."""
        by_key = {segment.key: segment for segment in script.segments}
        if override is not None:
            segment = by_key.get("override")
            if segment is not None and segment.source == override.strip():
                return 1, 0
            return 0, 1
        used = skipped = 0
        for scene in self._narration_scenes(project_id):
            if not scene.narration.strip():
                continue
            segment = by_key.get(f"scene:{scene.id}")
            if segment is not None and segment.source == scene.narration.strip():
                used += 1
            else:
                skipped += 1
        return used, skipped

    def _enhance_with_step(
        self,
        project: Any,
        profile: VoiceProfile,
        chunks: list[str],
        qwen_outputs: list[Path],
        request: NarrationRequest,
        *,
        job_id: str,
    ) -> list[Path]:
        root = self.pipeline.store.project_path(project)
        step_dir = root / "audio" / "step" / self._safe_component(job_id)
        step_dir.mkdir(parents=True, exist_ok=True)
        backend = self.pipeline.registry.get("step_audio_editx")
        edited: list[Path] = []
        failures: list[str] = []
        worker_started = self.pipeline.tts_workers.ensure_running_if_managed("step_audio_editx")
        try:
            backend.load()
        except Exception:
            if worker_started:
                self.pipeline.tts_workers.stop("step_audio_editx")
            raise
        try:
            for index, (text, source) in enumerate(zip(chunks, qwen_outputs), start=1):
                settings = {
                    "filename": f"{index:04d}.wav", "mode": "edit",
                    "source_audio": str(source.resolve()), "reference_text": text,
                    "edit_type": request.step_edit_type,
                    "edit_instruction": request.step_instruction,
                    "language": request.language,
                }
                try:
                    result = backend.generate(GenerationRequest(
                        job_id=f"{job_id}:step:{index}", output_dir=step_dir, prompt=text,
                        seed=request.seed + index - 1,
                        references=(root / profile.reference_audio,), settings=settings,
                    ))
                    output = result.outputs[0]
                    self._atomic_json(output.with_suffix(".json"), {
                        "chunk": index, "text": text, "provider": "step_audio_editx",
                        "source_audio": str(source.relative_to(root)),
                        "voice_profile_id": profile.id, "edit_type": request.step_edit_type,
                        "edit_instruction": request.step_instruction,
                        "duration": wav_duration(output), "status": "completed",
                        **dict(result.metadata),
                    })
                    self.pipeline._record_asset(
                        project, None, output, AssetType.NARRATION, result,
                        role="narration_step_edit",
                    )
                    edited.append(output)
                except Exception as exc:
                    error = str(exc)[:1000]
                    failures.append(f"Step chunk {index}: {error}")
                    self._atomic_json(step_dir / f"{index:04d}.json", {
                        "chunk": index, "text": text, "provider": "step_audio_editx",
                        "source_audio": str(source.relative_to(root)), "status": "failed",
                        "error": error,
                    })
        finally:
            try:
                if request.unload_after or worker_started:
                    backend.unload()
            finally:
                if worker_started:
                    self.pipeline.tts_workers.stop("step_audio_editx")
        if failures:
            raise RuntimeError("; ".join(failures))
        return edited

    @staticmethod
    def _publish_master_join(
        inputs: list[Path], master: Path, *, pause_ms: int,
    ) -> WavJoinResult:
        """Join chunks and publish the completed WAV with a single rename."""
        master.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{master.name}.", dir=master.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            result = join_wav_files_detailed(inputs, temporary, pause_ms=pause_ms)
            handle = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
            os.replace(temporary, master)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return result

    @staticmethod
    def _validate_wav(audio: bytes) -> None:
        descriptor, name = tempfile.mkstemp(suffix=".wav")
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(audio)
            with wave.open(str(path), "rb") as source:
                if source.getnframes() <= 0 or source.getframerate() <= 0:
                    raise ValueError("reference WAV contains no audio")
        except (wave.Error, EOFError) as exc:
            raise ValueError("reference audio must be a valid PCM WAV") from exc
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes) -> None:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
