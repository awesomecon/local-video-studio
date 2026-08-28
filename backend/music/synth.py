"""Deterministic procedural music synthesis (standard library only).

The composer renders one musical movement at a time so long videos can be
scored as several related pieces. Each movement is a small arrangement —
sustained chord pad, bass line, melodic random walk, and lightweight
percussion — driven by a tempo/key/time-signature setting, a 0..1 energy
level, and a seed. Output is byte-deterministic for identical inputs, which
keeps the mock pipeline's reproducibility guarantees intact.
"""

from __future__ import annotations

import math
import random
import wave
from array import array
from pathlib import Path

SAMPLE_RATE = 24000

_ROOTS = {
    "c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "f": 5,
    "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10,
    "b": 11,
}
_MODE_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_MODE_MINOR = (0, 2, 3, 5, 7, 8, 10)
_PROGRESSIONS = {
    # Chord roots expressed as scale degrees (triads are degree, +2, +4).
    "major": ((0, 5, 3, 4), (0, 4, 5, 3), (0, 3, 1, 4), (0, 5, 1, 4)),
    "minor": ((0, 5, 2, 6), (0, 3, 5, 4), (0, 6, 5, 4)),
}


def parse_key(key_scale: str) -> tuple[int, tuple[int, ...]]:
    """Parse a human key string like ``"C major"`` or ``"G# minor"``."""
    text = (key_scale or "").lower()
    tokens = text.replace("-", " ").split()
    root = 0
    for token in tokens:
        if token in _ROOTS:
            root = _ROOTS[token]
            break
    is_minor = any(token.startswith("min") for token in tokens)
    return root, (_MODE_MINOR if is_minor else _MODE_MAJOR)


def _degree_semitone(root: int, intervals: tuple[int, ...], degree: int) -> int:
    return root + intervals[degree % 7] + 12 * (degree // 7)


def midi_hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def _tone(
    buffer: list[float],
    start: int,
    length: int,
    frequency: float,
    amplitude: float,
    *,
    attack: float = 0.01,
    release: float = 0.05,
    sustain: float = 1.0,
    decay: float | None = None,
    h2: float = 0.0,
    h3: float = 0.0,
) -> None:
    """Additive tone with attack/release and an optional exponential decay.

    ``decay`` (seconds) pulls the envelope from full level toward ``sustain``;
    leaving it ``None`` keeps a steady sustained note (pad style).
    """
    if length <= 0 or start < 0 or amplitude == 0.0:
        return
    length = min(length, len(buffer) - start)
    if length <= 0:
        return
    step = 2.0 * math.pi * frequency / SAMPLE_RATE
    attack_n = max(1, int(attack * SAMPLE_RATE))
    release_n = max(1, int(release * SAMPLE_RATE))
    decay_factor = math.exp(-1.0 / (decay * SAMPLE_RATE)) if decay else 1.0
    level = 1.0
    phase = 0.0
    for index in range(length):
        if index < attack_n:
            envelope = index / attack_n
        else:
            level *= decay_factor
            envelope = sustain + (1.0 - sustain) * level
        remaining = length - index
        if remaining < release_n:
            envelope *= remaining / release_n
        value = math.sin(phase) + h2 * math.sin(2.0 * phase) + h3 * math.sin(3.0 * phase)
        buffer[start + index] += amplitude * envelope * value
        phase += step


def _kick(buffer: list[float], start: int, length: int, amplitude: float) -> None:
    if length <= 0 or start < 0:
        return
    length = min(length, len(buffer) - start)
    if length <= 0:
        return
    phase = 0.0
    for index in range(length):
        position = index / length
        frequency = 110.0 * (45.0 / 110.0) ** position
        envelope = math.exp(-9.0 * position)
        buffer[start + index] += amplitude * envelope * math.sin(phase)
        phase += 2.0 * math.pi * frequency / SAMPLE_RATE


def _noise_hit(
    buffer: list[float],
    start: int,
    length: int,
    amplitude: float,
    decay: float,
    rng: random.Random,
    *,
    body: float = 0.0,
    body_hz: float = 185.0,
) -> None:
    if length <= 0 or start < 0:
        return
    length = min(length, len(buffer) - start)
    if length <= 0:
        return
    factor = math.exp(-1.0 / (decay * SAMPLE_RATE))
    level = 1.0
    phase = 0.0
    step = 2.0 * math.pi * body_hz / SAMPLE_RATE
    for index in range(length):
        level *= factor
        value = rng.uniform(-1.0, 1.0) * (1.0 - body) + body * math.sin(phase)
        phase += step
        buffer[start + index] += amplitude * level * value


def compose_movement_frames(
    *,
    duration_seconds: float,
    seed: int,
    bpm: int = 90,
    key_scale: str = "C major",
    time_signature_beats: int = 4,
    energy: float = 0.5,
) -> list[float]:
    """Render one movement into a mono float buffer in [-1, 1]."""
    total = max(0.1, float(duration_seconds))
    frame_count = int(round(total * SAMPLE_RATE))
    rng = random.Random((int(seed) * 2654435761) & 0xFFFFFFFF)
    energy = min(max(float(energy), 0.0), 1.0)
    root, intervals = parse_key(key_scale)
    is_minor = intervals == _MODE_MINOR
    beats = max(2, min(7, int(time_signature_beats)))
    tempo = max(40.0, min(240.0, float(int(bpm) if bpm else 90)))
    beat = 60.0 / tempo
    bar_length = beat * beats
    bars = max(1, int(round(total / bar_length)))

    progressions = _PROGRESSIONS["minor" if is_minor else "major"]
    progression = list(rng.choice(progressions))

    buffer: list[float] = [0.0] * frame_count

    def sample_index(time_seconds: float) -> int:
        return max(0, min(frame_count, int(round(time_seconds * SAMPLE_RATE))))

    def arc(time_seconds: float) -> float:
        """Intensity curve: soft intro lift and a gentle outro settle."""
        intro = min(bar_length, total * 0.15)
        outro = min(bar_length * 1.5, total * 0.2)
        if time_seconds < intro and intro > 0:
            return 0.72 + 0.28 * (time_seconds / intro)
        outro_start = total - outro
        if time_seconds > outro_start and outro > 0:
            return 1.0 - 0.25 * ((time_seconds - outro_start) / outro)
        return 1.0

    def degree_midi(degree: int, base_offset: int) -> float:
        return base_offset + _degree_semitone(root, intervals, degree)

    b_section = bars // 2 if bars >= 6 else None
    melody_degree = 2
    eighth = beat / 2.0
    intro_bars = 1 if bars >= 4 else 0

    for bar in range(bars):
        bar_start = bar * bar_length
        if bar_start >= total:
            break
        bar_end = min(total, bar_start + bar_length)
        chord_degree = progression[bar % len(progression)]
        if b_section is not None and bar >= b_section:
            chord_degree = (chord_degree + 1) % 7

        pad_amplitude = 0.085 + 0.035 * energy
        pad_length = sample_index(bar_end) - sample_index(bar_start)
        pad_attack = min(0.9, (bar_end - bar_start) * 0.3)
        pad_release = min(0.9, (bar_end - bar_start) * 0.35)
        for interval in (0, 2, 4):
            _tone(
                buffer,
                sample_index(bar_start),
                pad_length,
                midi_hz(degree_midi(chord_degree + interval, 36)),
                pad_amplitude,
                attack=pad_attack,
                release=pad_release,
                sustain=1.0,
                h2=0.30,
                h3=0.12,
            )
        _tone(
            buffer,
            sample_index(bar_start),
            pad_length,
            midi_hz(degree_midi(chord_degree + 2, 48)),
            pad_amplitude * 0.3,
            attack=pad_attack,
            release=pad_release,
            sustain=1.0,
            h2=0.2,
            h3=0.0,
        )

        if energy < 0.45:
            bass_positions = ((0.0, 1.0),)
        elif energy < 0.7:
            bass_positions = ((0.0, 1.0), (max(1, beats // 2), 0.85))
        else:
            bass_positions = tuple(
                (float(pos), 1.0 if pos % 2 == 0 else 0.8) for pos in range(beats)
            )
        for position, velocity in bass_positions:
            note_start = bar_start + position * beat
            if note_start >= total:
                continue
            note_length = min(beat * 0.92, bar_end - note_start)
            _tone(
                buffer,
                sample_index(note_start),
                sample_index(note_start + note_length) - sample_index(note_start),
                midi_hz(degree_midi(chord_degree, 24)),
                0.17 * velocity * (0.7 + 0.3 * arc(note_start)),
                attack=0.006,
                release=0.05,
                sustain=0.22,
                decay=max(0.05, note_length * 0.85),
                h2=0.5,
                h3=0.25,
            )

        if bar >= intro_bars:
            _kick(
                buffer, sample_index(bar_start), int(0.14 * SAMPLE_RATE),
                0.22 * (0.6 + 0.4 * energy),
            )
            mid_beat = beats / 2.0
            if energy > 0.55 and beats >= 4:
                snare_start = bar_start + mid_beat * beat
                if snare_start < total:
                    _noise_hit(
                        buffer,
                        sample_index(snare_start),
                        int(0.09 * SAMPLE_RATE),
                        0.07 + 0.06 * energy,
                        0.05,
                        rng,
                        body=0.35,
                    )
            hat_probability = 0.35 + 0.45 * energy
            for half in range(beats * 2):
                if half % 2 == 0:
                    continue
                if rng.random() > hat_probability:
                    continue
                hat_start = bar_start + half * eighth
                if hat_start >= total:
                    break
                _noise_hit(
                    buffer,
                    sample_index(hat_start),
                    int(0.04 * SAMPLE_RATE),
                    0.035 * (0.6 + 0.4 * arc(hat_start)),
                    0.02,
                    rng,
                )

        play_probability = 0.28 + 0.5 * energy
        if b_section is not None and bar >= b_section:
            play_probability = min(0.9, play_probability + 0.12)
        step_time = bar_start
        while step_time < bar_end - 1e-9:
            step_length = min(eighth, bar_end - step_time)
            if rng.random() < play_probability:
                melody_degree += rng.choice((-2, -1, -1, 0, 1, 1, 2))
                melody_degree = min(13, max(0, melody_degree))
                _tone(
                    buffer,
                    sample_index(step_time),
                    sample_index(step_time + step_length * 0.88) - sample_index(step_time),
                    midi_hz(degree_midi(melody_degree, 60)),
                    (0.085 + 0.055 * energy) * arc(step_time) * rng.uniform(0.82, 1.12),
                    attack=0.004,
                    release=0.04,
                    sustain=0.35,
                    decay=max(0.08, step_length * 1.4),
                    h2=0.4,
                    h3=0.18,
                )
            step_time += eighth

    _finalize(buffer)
    return buffer


def _finalize(buffer: list[float]) -> None:
    peak = max((abs(value) for value in buffer), default=0.0)
    if peak <= 0.0:
        return
    gain = 0.74 / peak
    drive = 1.15
    normalization = math.tanh(drive)
    fade_n = max(1, int(0.012 * SAMPLE_RATE))
    length = len(buffer)
    for index, value in enumerate(buffer):
        shaped = math.tanh(value * gain * drive) / normalization
        fade = 1.0
        if index < fade_n:
            fade = index / fade_n
        elif index > length - fade_n:
            fade = (length - index) / fade_n
        buffer[index] = shaped * fade


def write_wav(path: Path, buffer: list[float]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for value in buffer:
            clamped = max(-1.0, min(1.0, value))
            frames.extend(int(clamped * 32767).to_bytes(2, byteorder="little", signed=True))
        output.writeframes(bytes(frames))
    return path


def compose_movement(path: Path, **kwargs: object) -> Path:
    """Render one movement WAV (see :func:`compose_movement_frames`)."""
    return write_wav(path, compose_movement_frames(**kwargs))  # type: ignore[arg-type]


def read_wav_frames(path: Path) -> array:
    with wave.open(str(path), "rb") as source:
        raw = source.readframes(source.getnframes())
    return array("h", raw)


def apply_edge_fades(samples: array, fade_in_samples: int, fade_out_samples: int) -> None:
    """Linear fade of raw int16 samples in place."""
    length = len(samples)
    fade_in_samples = max(0, min(fade_in_samples, length))
    fade_out_samples = max(0, min(fade_out_samples, length))
    for index in range(fade_in_samples):
        samples[index] = int(samples[index] * index / fade_in_samples)
    for offset in range(fade_out_samples):
        index = length - 1 - offset
        samples[index] = int(samples[index] * offset / fade_out_samples)


def stitch_dips(chunks: list[array], dip_samples: int) -> array:
    """Concatenate int16 chunks with silence-dipped boundaries.

    Every chunk keeps its full duration (totals stay exact); interior edges
    fade out/in over ``dip_samples`` so consecutive movements breathe apart
    instead of clicking.
    """
    stitched: array = array("h")
    for index, chunk in enumerate(chunks):
        part = array("h", chunk)
        if 0 < index < len(chunks) - 1:
            apply_edge_fades(part, dip_samples, dip_samples)
        elif index == 0 and len(chunks) > 1:
            apply_edge_fades(part, 0, dip_samples)
        elif index == len(chunks) - 1 and len(chunks) > 1:
            apply_edge_fades(part, dip_samples, 0)
        stitched.extend(part)
    return stitched


def write_wav_frames(path: Path, samples: array) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples.tobytes())
    return path
