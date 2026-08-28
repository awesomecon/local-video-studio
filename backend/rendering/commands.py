"""Pure FFmpeg argv construction for deterministic, reviewable rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from backend.timeline.models import AudioTrack, Timeline

from .binaries import FFmpegBinaries, require_ffmpeg

_CROSSFADE_TRANSITIONS = {"crossfade", "fade", "dissolve", "fade_through_black", "dip_to_white"}


@dataclass(frozen=True, slots=True)
class RenderOptions:
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    video_preset: str = "medium"
    crf: int = 20
    audio_bitrate: str = "192k"
    burn_subtitles: bool = False
    embed_subtitle_track: bool = True
    normalize_narration: bool = True
    clean_narration: bool = True
    duck_music: bool = True
    narration_loudness_lufs: float = -18.0
    narration_true_peak_db: float = -2.0
    music_gain_db: float = -12.0
    limit_audio: bool = True

    def validate(self) -> None:
        if self.width is not None and self.width <= 0:
            raise ValueError("Render width must be positive")
        if self.height is not None and self.height <= 0:
            raise ValueError("Render height must be positive")
        if self.fps is not None and self.fps <= 0:
            raise ValueError("Render fps must be positive")
        if not 0 <= self.crf <= 51:
            raise ValueError("CRF must be between 0 and 51")
        if (
            not math.isfinite(self.narration_loudness_lufs)
            or not -70 <= self.narration_loudness_lufs <= -5
        ):
            raise ValueError("Narration loudness must be between -70 and -5 LUFS")
        if (
            not math.isfinite(self.narration_true_peak_db)
            or not -9 <= self.narration_true_peak_db <= 0
        ):
            raise ValueError("Narration true peak must be between -9 and 0 dBTP")
        if not math.isfinite(self.music_gain_db):
            raise ValueError("Music gain must be finite")


def _dimensions(timeline: Timeline, options: RenderOptions) -> tuple[int, int, int]:
    options.validate()
    return (
        options.width or timeline.width,
        options.height or timeline.height,
        options.fps or timeline.fps,
    )


def _camera_motion_expressions(
    instruction: str, *, frames: int,
) -> tuple[str, str, str]:
    """Eased Ken Burns ``(zoom, x, y)`` expressions covering ``frames`` frames."""

    normalized = instruction.strip().lower().replace("_", " ").replace("-", " ")
    # Ease in and out instead of moving linearly or reaching a zoom cap early.
    progress = f"(0.5-0.5*cos(PI*on/{frames - 1}))"
    zoom = f"1+0.08*{progress}"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"

    if any(phrase in normalized for phrase in ("pull out", "pull back", "zoom out", "dolly out")):
        zoom = f"1.08-0.08*{progress}"
    elif any(phrase in normalized for phrase in ("pan left", "move left", "drift left")):
        zoom = "1.08"
        x = f"(iw-iw/zoom)*(1-{progress})"
    elif any(phrase in normalized for phrase in ("pan right", "move right", "drift right")):
        zoom = "1.08"
        x = f"(iw-iw/zoom)*{progress}"
    elif any(phrase in normalized for phrase in ("pan up", "move up", "drift up", "tilt up")):
        zoom = "1.08"
        y = f"(ih-ih/zoom)*(1-{progress})"
    elif any(phrase in normalized for phrase in ("pan down", "move down", "drift down", "tilt down")):
        zoom = "1.08"
        y = f"(ih-ih/zoom)*{progress}"
    return zoom, x, y


def _camera_motion_filter(
    instruction: str, *, width: int, height: int, fps: int, duration_seconds: float,
) -> str:
    """Build a subtle, eased Ken Burns move that lasts for the whole clip.

    The cover scale can leave a non-canvas-aspect intermediate (e.g. a
    portrait source scaled to 3840x5760 for a 1920x1080 canvas). zoompan
    rescales its *whole* input window to ``s`` when zoom is 1, so without the
    center crop to the exact canvas aspect ratio first, the still would be
    squashed to the canvas and look stretched. With the crop, zoom 1 maps the
    input to the canvas 1:1 and higher zooms are true, undistorted zooms.
    """

    frames = max(2, math.ceil(duration_seconds * fps))
    zoom, x, y = _camera_motion_expressions(instruction, frames=frames)

    # The cover scale can leave a non-canvas-aspect intermediate (e.g. a
    # portrait source scaled to 3840x5760 for a 1920x1080 canvas). zoompan
    # rescales its *whole* input window to ``s`` when zoom is 1, so without the
    # center crop to the exact canvas aspect ratio first, the still would be
    # squashed to the canvas and look stretched. With the crop, zoom 1 maps the
    # input to the canvas 1:1 and higher zooms are true, undistorted zooms.
    #
    # ``setsar=1`` is load-bearing: when the cover-scaled size does not exactly
    # match the source aspect ratio, ``scale`` adjusts the SAR to preserve the
    # display aspect ratio (e.g. 12096:12095), and ``zoompan`` propagates that
    # odd SAR into the output. Downstream ``concat``/``xfade``/``overlay``
    # filters reject inputs whose SARs disagree, so the canvas must end with
    # square pixels.
    return (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={fps},"
        f"setsar=1,fps={fps},trim=duration={duration_seconds:.6f},"
        "settb=AVTB,setpts=PTS-STARTPTS"
    )


def build_video_command(
    timeline: Timeline,
    destination: str | Path,
    options: RenderOptions,
    binaries: FFmpegBinaries,
) -> list[str]:
    """Build a video-only command including still motion and scene transitions."""

    timeline.validate()
    width, height, fps = _dimensions(timeline, options)
    argv = [str(require_ffmpeg(binaries)), "-hide_banner", "-loglevel", "error", "-y"]
    filters: list[str] = []
    for index, clip in enumerate(timeline.clips):
        if not clip.path.is_file():
            raise FileNotFoundError(clip.path)
        if clip.media_kind in {"image", "title", "diagram"}:
            argv.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    str(fps),
                    "-t",
                    f"{clip.duration_seconds:.6f}",
                    "-i",
                    str(clip.path),
                ]
            )
        else:
            argv.extend(["-t", f"{clip.duration_seconds:.6f}", "-i", str(clip.path)])
        padding = (
            f"tpad=stop_mode=clone:stop_duration={clip.duration_seconds:.6f},"
            if clip.media_kind == "video"
            else ""
        )
        common = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
            f"{padding}"
            f"trim=duration={clip.duration_seconds:.6f},settb=AVTB,setpts=PTS-STARTPTS"
        )
        if clip.media_kind in {"image", "title", "diagram"} and clip.camera_motion:
            common = _camera_motion_filter(
                clip.camera_motion,
                width=width,
                height=height,
                fps=fps,
                duration_seconds=clip.duration_seconds,
            )
        filters.append(f"[{index}:v]{common}[clip{index}]")

    current_label = "clip0"
    current_duration = timeline.clips[0].duration_seconds
    for index, clip in enumerate(timeline.clips[1:], start=1):
        output_label = f"joined{index}"
        if clip.transition == "cut":
            filters.append(
                f"[{current_label}][clip{index}]concat=n=2:v=1:a=0[{output_label}]"
            )
            current_duration += clip.duration_seconds
        else:
            if clip.transition not in _CROSSFADE_TRANSITIONS:
                # Fall back: treat unsupported fade variants as crossfade blends.
                if clip.transition in {"fade_through_black", "dip_to_white"}:
                    clip = clip.__class__(
                        scene_id=clip.scene_id,
                        path=clip.path,
                        start_seconds=clip.start_seconds,
                        duration_seconds=clip.duration_seconds,
                        media_kind=clip.media_kind,
                        transition="crossfade",
                        transition_duration_seconds=clip.transition_duration_seconds,
                        camera_motion=clip.camera_motion,
                    )
                else:
                    raise ValueError(f"Unsupported video transition: {clip.transition}")
            overlap = clip.transition_duration_seconds
            offset = current_duration - overlap
            filters.extend(
                [
                    f"[{current_label}]split=2[currentpre{index}src][currenttail{index}src]",
                    f"[currentpre{index}src]trim=start=0:end={offset:.6f},"
                    f"setpts=PTS-STARTPTS[currentpre{index}]",
                    f"[currenttail{index}src]trim=start={offset:.6f}:"
                    f"end={current_duration:.6f},setpts=PTS-STARTPTS[currenttail{index}]",
                    f"[clip{index}]split=2[nexthead{index}src][nextpost{index}src]",
                    f"[nexthead{index}src]trim=start=0:end={overlap:.6f},"
                    f"setpts=PTS-STARTPTS[nexthead{index}]",
                    f"[nextpost{index}src]trim=start={overlap:.6f}:"
                    f"end={clip.duration_seconds:.6f},setpts=PTS-STARTPTS[nextpost{index}]",
                    f"[currenttail{index}][nexthead{index}]blend="
                    f"all_expr='A*(1-T/{overlap:.6f})+B*(T/{overlap:.6f})':"
                    f"shortest=1[blend{index}]",
                    f"[currentpre{index}][blend{index}][nextpost{index}]"
                    f"concat=n=3:v=1:a=0[{output_label}]",
                ]
            )
            current_duration += clip.duration_seconds - overlap
        current_label = output_label
    filters.append(f"[{current_label}]format=yuv420p[vout]")
    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-an",
            "-c:v",
            options.video_codec,
            "-preset",
            options.video_preset,
            "-crf",
            str(options.crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    return argv


def _subtitle_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", r"\\")
    for character in (":", "'", "[", "]", ","):
        value = value.replace(character, f"\\{character}")
    return value


def build_finalize_command(
    video_path: str | Path,
    timeline: Timeline,
    destination: str | Path,
    options: RenderOptions,
    binaries: FFmpegBinaries,
    *,
    subtitle_path: Path | None = None,
) -> list[str]:
    """Build narration/music mixing, subtitle, and final MP4 mux command."""

    duration = timeline.duration_seconds
    argv = [
        str(require_ffmpeg(binaries)),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
    ]
    audio_inputs: list[tuple[int, AudioTrack]] = []
    next_index = 1
    for track in timeline.audio_tracks:
        if not track.path.is_file():
            raise FileNotFoundError(track.path)
        if track.loop:
            argv.extend(["-stream_loop", "-1"])
        argv.extend(["-i", str(track.path)])
        audio_inputs.append((next_index, track))
        next_index += 1
    subtitle_input_index: int | None = None
    if subtitle_path and options.embed_subtitle_track:
        argv.extend(["-i", str(subtitle_path)])
        subtitle_input_index = next_index

    filters: list[str] = []
    video_map = "0:v:0"
    if subtitle_path and options.burn_subtitles:
        escaped = _subtitle_filter_path(subtitle_path)
        filters.append(f"[0:v]subtitles=filename='{escaped}'[subtitled]")
        video_map = "[subtitled]"

    prepared_audio: list[tuple[str, str]] = []
    for input_index, track in audio_inputs:
        label = f"audio{input_index}"
        pieces = ["aresample=48000"]
        if track.start_seconds > 0:
            delay = round(track.start_seconds * 1000)
            # `all=1` delays every channel regardless of count; requires the
            # adelay "all" option (FFmpeg >= 4.3, satisfied by imageio-ffmpeg
            # 7.0.2 and any pinned minimum we support).
            pieces.append(f"adelay=delays={delay}:all=1")
        gain = track.gain_db
        if track.kind == "music":
            gain += options.music_gain_db
        if track.kind == "narration":
            if options.clean_narration:
                # TTS output can have a very low signal level. Remove inaudible
                # rumble and high-frequency fizz, then apply conservative FFT
                # denoising before loudness normalization makes either obvious.
                pieces.extend([
                    "highpass=f=60",
                    "lowpass=f=15000",
                    "afftdn=nr=8:nf=-50:tn=1",
                ])
            if options.normalize_narration:
                pieces.append(
                    f"loudnorm=I={options.narration_loudness_lufs:g}:"
                    f"TP={options.narration_true_peak_db:g}:LRA=7"
                )
        # User-selected narration gain must come after normalization; applying
        # it first would let loudnorm cancel the requested boost.
        pieces.append(f"volume={gain:.2f}dB")
        pieces.extend(["apad", f"atrim=duration={duration:.6f}"])
        filters.append(f"[{input_index}:a]{','.join(pieces)}[{label}]")
        prepared_audio.append((label, track.kind))

    narration = next((label for label, kind in prepared_audio if kind == "narration"), None)
    music = next((label for label, kind in prepared_audio if kind == "music"), None)
    final_audio: str | None = None
    consumed = {label for label in (narration, music) if label}
    if narration and music and options.duck_music:
        filters.append(
            f"[{narration}]asplit=2[{narration}mix][{narration}sidechain]"
        )
        filters.append(
            f"[{music}][{narration}sidechain]sidechaincompress=threshold=0.08:ratio=2.5:"
            "attack=30:release=650:knee=4[duckedmusic]"
        )
        filters.append(
            f"[{narration}mix][duckedmusic]amix=inputs=2:normalize=0[mixedprimary]"
        )
        final_audio = "mixedprimary"
    elif narration and music:
        filters.append(f"[{narration}][{music}]amix=inputs=2:normalize=0[mixedprimary]")
        final_audio = "mixedprimary"
    elif narration or music:
        final_audio = narration or music

    remaining = [label for label, _kind in prepared_audio if label not in consumed]
    if final_audio and remaining:
        labels = "".join(f"[{label}]" for label in [final_audio, *remaining])
        filters.append(f"{labels}amix=inputs={len(remaining) + 1}:normalize=0[mixedall]")
        final_audio = "mixedall"
    elif len(remaining) == 1:
        final_audio = remaining[0]
    elif remaining:
        labels = "".join(f"[{label}]" for label in remaining)
        filters.append(f"{labels}amix=inputs={len(remaining)}:normalize=0[mixedall]")
        final_audio = "mixedall"

    if final_audio and options.limit_audio:
        filters.append(
            f"[{final_audio}]alimiter=limit=0.891:attack=5:release=100:"
            "level=disabled:latency=true[limitedaudio]"
        )
        final_audio = "limitedaudio"

    if filters:
        argv.extend(["-filter_complex", ";".join(filters)])
    argv.extend(["-map", video_map])
    if final_audio:
        argv.extend(
            [
                "-map",
                f"[{final_audio}]",
                "-c:a",
                options.audio_codec,
                "-b:a",
                options.audio_bitrate,
            ]
        )
    else:
        argv.append("-an")
    if subtitle_input_index is not None:
        argv.extend(
            [
                "-map",
                f"{subtitle_input_index}:s:0",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=eng",
            ]
        )
    if options.burn_subtitles:
        argv.extend(
            [
                "-c:v",
                options.video_codec,
                "-preset",
                options.video_preset,
                "-crf",
                str(options.crf),
            ]
        )
    else:
        argv.extend(["-c:v", "copy"])
    argv.extend(
        [
            "-t",
            f"{duration:.6f}",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    return argv
