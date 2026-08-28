"""Deterministic, lightweight generator used for development and integration tests."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw

from .base import (
    BackendDescriptor,
    Capability,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from .errors import BackendError, BackendErrorCode


_CAPABILITIES = frozenset(Capability)


def find_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


class MockGeneratorBackend(GeneratorBackend):
    """Produces real tiny files without downloading or loading model weights."""

    def __init__(self, *, ffmpeg_path: str | None = None) -> None:
        self.ffmpeg_path = ffmpeg_path or find_ffmpeg()
        self._active: set[str] = set()
        self._canceled: set[str] = set()
        self._lock = threading.Lock()
        self._loaded = False

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name="mock",
            model_name="deterministic-placeholder-v1",
            model_version="1",
            device="cpu",
            capabilities=_CAPABILITIES,
            supported_inputs=("text", "image", "audio", "reference"),
            supported_outputs=("json", "text", "png", "mp4", "wav", "srt", "ass"),
        )

    def health(self) -> Mapping[str, Any]:
        return {
            "status": "healthy",
            "loaded": self._loaded,
            "ffmpeg": self.ffmpeg_path,
            "video_available": bool(self.ffmpeg_path),
        }

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._active:
                self._canceled.add(job_id)
        return True

    def reset_cancel(self, job_id: str) -> None:
        with self._lock:
            self._canceled.discard(job_id)

    def _check_canceled(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._canceled:
                raise BackendError(BackendErrorCode.CANCELED, "The mock generation was canceled.")

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        return {
            "device": "cpu",
            "vram_gb": 0.0,
            "disk_mb": max(1, int(request.duration_seconds or 1)),
        }

    def generate(self, request: GenerationRequest) -> GenerationResult:
        with self._lock:
            self._active.add(request.job_id)
            self._canceled.discard(request.job_id)
        try:
            return self._generate(request)
        finally:
            with self._lock:
                self._active.discard(request.job_id)
                self._canceled.discard(request.job_id)

    def _generate(self, request: GenerationRequest) -> GenerationResult:
        self._check_canceled(request.job_id)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        kind = str(request.settings.get("kind", request.settings.get("output_type", "image")))
        producers = {
            "llm": self._generate_llm,
            "text": self._generate_llm,
            "image": self._generate_image,
            "video": self._generate_video,
            "tts": self._generate_tts,
            "narration": self._generate_tts,
            "music": self._generate_music,
            "transcription": self._generate_transcription,
            "subtitles": self._generate_transcription,
        }
        producer = producers.get(kind)
        if producer is None:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"Unknown mock output kind {kind!r}.",
            )
        outputs = producer(request)
        self._check_canceled(request.job_id)
        digest = hashlib.sha256()
        for output in outputs:
            digest.update(output.read_bytes())
        return GenerationResult(
            outputs=tuple(outputs),
            metadata={
                "backend": "mock",
                "model": "deterministic-placeholder-v1",
                "model_version": "1",
                "seed": request.seed,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "settings": dict(request.settings),
                "content_hash": digest.hexdigest(),
            },
            peak_vram_gb=0.0,
        )

    def _generate_llm(self, request: GenerationRequest) -> list[Path]:
        title = request.prompt.strip().rstrip(".") or "Untitled local video"
        plan = {
            "title": title[:100],
            "outline": ["Opening", "Explanation", "Conclusion"],
            "script": f"A deterministic mock narration about {title}.",
            "scenes": [
                {
                    "index": index,
                    "duration": 2.0,
                    "narration": narration,
                    "visual_prompt": f"Mock scene {index}: {title}",
                    "visual_type": "flux_still_motion",
                    "selected_backend": "mock",
                    "seed": request.seed + index,
                }
                for index, narration in enumerate(
                    ("An opening scene.", "A clear explanation.", "A concise conclusion."), start=1
                )
            ],
            "youtube": {
                "titles": [title[:90], f"Understanding {title}"[:90]],
                "description": f"A locally generated mock video about {title}.",
            },
        }
        output = request.output_dir / "response.json"
        output.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        return [output]

    def _generate_image(self, request: GenerationRequest) -> list[Path]:
        width = request.width or 640
        height = request.height or 360
        digest = hashlib.sha256(f"{request.seed}:{request.prompt}".encode()).digest()
        color = tuple(40 + byte % 170 for byte in digest[:3])
        image = Image.new("RGB", (width, height), color)
        draw = ImageDraw.Draw(image)
        label = f"LOCAL VIDEO STUDIO — MOCK\nseed {request.seed}\n{request.prompt[:120]}"
        draw.multiline_text((24, 24), label, fill="white", spacing=8)
        output = request.output_dir / "image.png"
        image.save(output, format="PNG")
        return [output]

    def _generate_video(self, request: GenerationRequest) -> list[Path]:
        if not self.ffmpeg_path:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "Mock video generation requires FFmpeg or imageio-ffmpeg.",
            )
        width = request.width or 320
        height = request.height or 180
        duration = max(0.1, float(request.duration_seconds or 1.0))
        digest = hashlib.sha256(f"{request.seed}:{request.prompt}".encode()).hexdigest()
        color = digest[:6]
        output = request.output_dir / "video.mp4"
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=#{color}:s={width}x{height}:r=24:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={220 + request.seed % 220}:sample_rate=48000:duration={duration}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(output),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "FFmpeg could not create the mock video.",
                details=completed.stderr[-1000:],
            )
        return [output]

    def _write_wave(self, path: Path, duration: float, frequency: float, volume: float) -> None:
        sample_rate = 24000
        frame_count = round(max(0.1, duration) * sample_rate)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            frames = bytearray()
            for index in range(frame_count):
                envelope = min(1.0, index / 400, (frame_count - index) / 400)
                phase = 2 * math.pi * frequency * index / sample_rate
                sample = int(32767 * volume * envelope * math.sin(phase))
                frames.extend(sample.to_bytes(2, byteorder="little", signed=True))
            output.writeframes(bytes(frames))

    def _generate_tts(self, request: GenerationRequest) -> list[Path]:
        output = request.output_dir / "narration.wav"
        duration = float(request.duration_seconds or max(1.0, len(request.prompt.split()) / 2.5))
        self._write_wave(output, duration, 180 + request.seed % 80, 0.16)
        return [output]

    def _generate_music(self, request: GenerationRequest) -> list[Path]:
        from backend.music import synth as music_synth

        output = request.output_dir / "music.wav"
        total_duration = max(0.1, float(request.duration_seconds or 2.0))
        settings = request.settings
        seed_base = int(request.seed)
        bpm = int(settings.get("bpm", 90) or 90)
        key_scale = str(settings.get("key_scale", "C major") or "C major")
        time_signature_beats = int(str(settings.get("time_signature", "4") or "4").split("/")[0])

        raw_movements = settings.get("movements")
        if not isinstance(raw_movements, list) or not raw_movements:
            raw_movements = [{"duration": total_duration, "energy": 0.5, "mood": ""}]

        movements_dir = request.output_dir / "movements"
        movements_dir.mkdir(parents=True, exist_ok=True)
        for stale in movements_dir.glob("movement-*.wav"):
            stale.unlink()

        clips: list[Path] = []
        rendered_total = 0.0
        for index, movement in enumerate(raw_movements):
            movement_duration = max(0.1, float(movement.get("duration", total_duration)))
            if index == len(raw_movements) - 1:
                # Keep the stitched total exact against the requested duration.
                movement_duration = max(0.1, total_duration - rendered_total)
            clip = movements_dir / f"movement-{index + 1:02d}.wav"
            music_synth.compose_movement(
                clip,
                duration_seconds=movement_duration,
                seed=seed_base + 7919 * index,
                bpm=bpm,
                key_scale=key_scale,
                time_signature_beats=time_signature_beats,
                energy=float(movement.get("energy", 0.5)),
            )
            clips.append(clip)
            rendered_total += movement_duration

        chunks = [music_synth.read_wav_frames(clip) for clip in clips]
        dip_samples = int(1.5 * music_synth.SAMPLE_RATE)
        stitched = music_synth.stitch_dips(chunks, dip_samples)
        music_synth.write_wav_frames(output, stitched)
        return [output]

    def _generate_transcription(self, request: GenerationRequest) -> list[Path]:
        text = str(request.settings.get("transcript", request.prompt)).strip() or "Mock caption."
        duration = float(request.duration_seconds or 2.0)
        srt = request.output_dir / "captions.srt"
        ass = request.output_dir / "captions.ass"
        segments = request.output_dir / "segments.json"
        total_ms = round(duration * 1000)
        hours, remainder = divmod(total_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        srt_end = f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"
        ass_end = f"{hours}:{minutes:02}:{seconds:02}.{milliseconds // 10:02}"
        srt.write_text(
            f"1\n00:00:00,000 --> {srt_end}\n{text}\n",
            encoding="utf-8",
        )
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, Alignment\n"
            "Style: Default,Arial,42,&H00FFFFFF,2\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n"
            f"Dialogue: 0,0:00:00.00,{ass_end},Default,{text}\n",
            encoding="utf-8",
        )
        segments.write_text(
            json.dumps({"segments": [{"start": 0.0, "end": duration, "text": text}]}, indent=2),
            encoding="utf-8",
        )
        return [srt, ass, segments]


class MockLLMBackend(MockGeneratorBackend):
    """Convenience adapter whose requests default to deterministic structured planning."""

    def generate(self, request: GenerationRequest) -> GenerationResult:
        settings = dict(request.settings)
        settings.setdefault("kind", "llm")
        return super().generate(
            GenerationRequest(
                job_id=request.job_id,
                output_dir=request.output_dir,
                prompt=request.prompt,
                negative_prompt=request.negative_prompt,
                seed=request.seed,
                duration_seconds=request.duration_seconds,
                width=request.width,
                height=request.height,
                references=request.references,
                settings=settings,
            )
        )
