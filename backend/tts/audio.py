"""Lossless WAV assembly helpers."""

from __future__ import annotations

import io
import math
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path


_SILENCE_THRESHOLD_DBFS = -45.0


@dataclass(frozen=True)
class WavJoinResult:
    duration_seconds: float
    inserted_pause_seconds: tuple[float, ...]


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / source.getframerate()


def apply_wav_gain(audio: bytes, gain_db: float) -> bytes:
    """Return a PCM WAV amplified by ``gain_db``, clipping at full scale.

    A zero-gain request returns the original bytes exactly. That keeps legacy
    profile uploads byte-for-byte stable while allowing quiet reference takes
    to be boosted without relying on a model-specific preprocessing option.
    """
    if not math.isfinite(gain_db) or not 0 <= gain_db <= 24:
        raise ValueError("reference audio gain must be between 0 and 24 dB")
    if gain_db == 0:
        return audio

    source_buffer = io.BytesIO(audio)
    try:
        with wave.open(source_buffer, "rb") as source:
            params = source.getparams()
            if params.comptype != "NONE" or params.sampwidth not in {1, 2, 3, 4}:
                raise ValueError("reference audio must be an uncompressed PCM WAV")
            frames = source.readframes(params.nframes)
    except (wave.Error, EOFError) as exc:
        raise ValueError("reference audio must be a valid PCM WAV") from exc

    boosted = _scale_pcm(frames, sample_width=params.sampwidth, gain_db=gain_db)
    target_buffer = io.BytesIO()
    with wave.open(target_buffer, "wb") as target:
        target.setparams(params)
        target.writeframes(boosted)
    return target_buffer.getvalue()


def _scale_pcm(audio: bytes, *, sample_width: int, gain_db: float) -> bytes:
    """Scale little-endian integer PCM, saturating instead of wrapping."""
    factor = 10 ** (gain_db / 20)
    if sample_width == 1:
        return bytes(
            max(0, min(255, round((sample - 128) * factor) + 128))
            for sample in audio
        )
    if len(audio) % sample_width:
        raise ValueError("reference WAV contains incomplete PCM samples")

    typecode = {2: "h", 4: "i"}.get(sample_width)
    minimum = -(1 << (sample_width * 8 - 1))
    maximum = (1 << (sample_width * 8 - 1)) - 1
    if typecode is not None:
        samples = array(typecode)
        samples.frombytes(audio)
        if samples.itemsize != sample_width:
            raise ValueError(f"unsupported PCM sample width: {sample_width}")
        for index, sample in enumerate(samples):
            samples[index] = max(minimum, min(maximum, round(sample * factor)))
        return samples.tobytes()

    # Python's array module has no signed 24-bit type.
    output = bytearray(len(audio))
    for offset in range(0, len(audio), 3):
        sample = int.from_bytes(audio[offset:offset + 3], "little", signed=True)
        scaled = max(minimum, min(maximum, round(sample * factor)))
        output[offset:offset + 3] = scaled.to_bytes(3, "little", signed=True)
    return bytes(output)


def join_wav_files(inputs: list[Path], output: Path, *, pause_ms: int = 350) -> float:
    """Join WAV files with at least ``pause_ms`` of silence at each boundary."""
    return join_wav_files_detailed(inputs, output, pause_ms=pause_ms).duration_seconds


def join_wav_files_detailed(
    inputs: list[Path], output: Path, *, pause_ms: int = 350,
) -> WavJoinResult:
    """Join WAVs while counting natural edge silence toward the requested pause."""
    if not inputs:
        raise ValueError("at least one WAV input is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(inputs[0]), "rb") as first:
        params = first.getparams()
    audio_format = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
    # 8-bit WAV samples are unsigned, so digital silence there is 0x80; wider
    # PCM widths are signed and silence as zero bytes.
    silence_unit = b"\x80" if params.sampwidth == 1 else b"\0"
    target_pause_frames = int(params.framerate * pause_ms / 1000)
    frame_counts: list[int] = []
    leading_silence: list[int] = []
    trailing_silence: list[int] = []
    for path in inputs:
        with wave.open(str(path), "rb") as source:
            current = (
                source.getnchannels(), source.getsampwidth(), source.getframerate(),
                source.getcomptype(),
            )
            if current != audio_format:
                raise ValueError("WAV chunks must share channels, sample width, and sample rate")
            frame_count = source.getnframes()
            frames = source.readframes(frame_count)
            frame_counts.append(frame_count)
            leading_silence.append(_edge_silence_frames(
                frames, channels=params.nchannels, sample_width=params.sampwidth, leading=True,
            ))
            trailing_silence.append(_edge_silence_frames(
                frames, channels=params.nchannels, sample_width=params.sampwidth, leading=False,
            ))

    padding_frames = [
        max(0, target_pause_frames - left - right)
        for left, right in zip(trailing_silence, leading_silence[1:])
    ]

    total_frames = 0
    with wave.open(str(output), "wb") as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(params.framerate)
        target.setcomptype(params.comptype, params.compname)
        for index, (path, frame_count) in enumerate(zip(inputs, frame_counts, strict=True)):
            with wave.open(str(path), "rb") as source:
                target.writeframesraw(source.readframes(frame_count))
                total_frames += frame_count
            if index < len(padding_frames) and padding_frames[index]:
                silence = (
                    silence_unit * padding_frames[index] * params.nchannels * params.sampwidth
                )
                target.writeframesraw(silence)
                total_frames += padding_frames[index]
    return WavJoinResult(
        duration_seconds=total_frames / params.framerate,
        inserted_pause_seconds=tuple(frames / params.framerate for frames in padding_frames),
    )


def _edge_silence_frames(
    audio: bytes, *, channels: int, sample_width: int, leading: bool,
) -> int:
    """Count contiguous near-silent PCM frames at one edge of a chunk."""
    frame_width = channels * sample_width
    if frame_width <= 0 or len(audio) % frame_width:
        raise ValueError("WAV chunk contains incomplete PCM frames")
    frame_count = len(audio) // frame_width
    frame_indexes = range(frame_count) if leading else range(frame_count - 1, -1, -1)
    maximum = 127 if sample_width == 1 else (1 << (sample_width * 8 - 1)) - 1
    threshold = maximum * (10 ** (_SILENCE_THRESHOLD_DBFS / 20))
    silent = 0
    for frame_index in frame_indexes:
        offset = frame_index * frame_width
        frame = audio[offset:offset + frame_width]
        if any(
            _pcm_amplitude(frame[index:index + sample_width], sample_width) > threshold
            for index in range(0, frame_width, sample_width)
        ):
            break
        silent += 1
    return silent


def _pcm_amplitude(sample: bytes, sample_width: int) -> int:
    if sample_width == 1:
        return abs(sample[0] - 128)
    if sample_width in {2, 3, 4}:
        return abs(int.from_bytes(sample, byteorder="little", signed=True))
    raise ValueError(f"unsupported PCM sample width: {sample_width}")
