"""Deterministic local Chromium rasterizer for approved Graphic Screen documents."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from backend.schemas import ThumbnailTextLayout

from .browser import chromium_argv, discover_chromium

_THUMBNAIL_PALETTES = {
    "sunset": {"text": (255, 208, 64, 255), "accent": (255, 92, 46, 255)},
    "electric": {"text": (112, 232, 255, 255), "accent": (150, 100, 252, 255)},
    "midnight": {"text": (246, 248, 255, 255), "accent": (98, 128, 255, 255)},
    "paper": {"text": (255, 246, 222, 255), "accent": (190, 44, 54, 255)},
}
_THUMBNAIL_STROKE = (14, 14, 18, 255)


def _smooth(portion: float) -> float:
    """Clamped smoothstep easing for deterministic gradient scrims."""
    portion = max(0.0, min(1.0, portion))
    return portion * portion * (3.0 - 2.0 * portion)


def _kicker_label(layout: ThumbnailTextLayout) -> str:
    """The hook becomes a small kicker line; skipped when it just repeats the title."""
    def normalized(value: str) -> str:
        return value.strip().rstrip(".,!?:;").casefold()
    if not layout.hook.strip() or normalized(layout.hook) == normalized(layout.title):
        return ""
    return layout.hook.strip()



def _chromium_env(profile: Path) -> dict[str, str]:
    """Run Chromium with a throw-away HOME so snap profiles never leak in."""
    return dict(os.environ, HOME=str(profile))

class GraphicScreenRenderer:
    def __init__(self, executable: Path | None = None, *, timeout_seconds: float = 30.0) -> None:
        self.executable = executable or discover_chromium()
        self.timeout_seconds = timeout_seconds
        self._version: str | None = None

    @property
    def available(self) -> bool:
        return self.executable is not None

    @property
    def version(self) -> str:
        # Cached per instance: cache manifests and overlay sidecars embed this
        # string, so it must not drift between identical subprocess calls.
        if self._version is None:
            self._version = self._query_version()
        return self._version

    def _query_version(self) -> str:
        if not self.executable:
            return "unavailable"
        try:
            completed = subprocess.run(
                [str(self.executable), "--version"], shell=False, capture_output=True,
                text=True, timeout=5, check=False,
            )
            return (completed.stdout or completed.stderr).strip()[:200] or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def font_metadata(self) -> tuple[str, str | None]:
        """Record the actual local Noto font when fontconfig can resolve it."""
        try:
            completed = subprocess.run(
                ["fc-match", "-f", "%{file}", "Noto Sans"], shell=False, capture_output=True,
                text=True, timeout=5, check=False,
            )
            font = Path(completed.stdout.strip())
            if completed.returncode == 0 and font.is_file():
                return str(font), hashlib.sha256(font.read_bytes()).hexdigest()
        except (OSError, subprocess.SubprocessError):
            pass
        return "Noto Sans (unresolved)", None

    def render(self, document: str, output: Path, *, width: int, height: int) -> str:
        if not self.executable:
            raise RuntimeError("Chromium is not available for Graphic Screen rendering")
        output.parent.mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix="graphic-screen-", dir=output.parent))
        source = job_dir / "screen.html"
        temporary_png = job_dir / "screen.png"
        profile = job_dir / "profile"
        try:
            source.write_text(document, encoding="utf-8")
            command = chromium_argv(
                self.executable, document=source, output=temporary_png, profile=profile,
                width=width, height=height,
            )
            try:
                completed = subprocess.run(
                    command, shell=False, capture_output=True, text=True,
                    timeout=self.timeout_seconds, check=False,
                    env=_chromium_env(profile),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Chromium timed out while rendering Graphic Screen") from exc
            if completed.returncode:
                raise RuntimeError("Chromium could not render the approved Graphic Screen")
            if not temporary_png.is_file() or temporary_png.stat().st_size == 0:
                raise RuntimeError("Chromium did not produce a PNG")
            with Image.open(temporary_png) as image:
                if image.size != (width, height):
                    raise RuntimeError("Chromium PNG does not match the project resolution")
            publish = output.with_name(f".{output.name}.graphic-screen.tmp")
            shutil.copyfile(temporary_png, publish)
            with publish.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(publish, output)
            return hashlib.sha256(output.read_bytes()).hexdigest()
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def render_transparent(self, document: str, output: Path, *, width: int, height: int) -> str:
        """Render a trusted, sanitized overlay document to an RGBA PNG.

        Same local-Chromium pipeline as :meth:`render`, launched with a
        transparent default background so only the document's own paint is
        opaque; the result is re-encoded as RGBA deterministically.
        """
        if not self.executable:
            raise RuntimeError("Chromium is not available for Graphic Screen rendering")
        output.parent.mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix="graphic-overlay-", dir=output.parent))
        source = job_dir / "overlay.html"
        temporary_png = job_dir / "overlay.png"
        profile = job_dir / "profile"
        try:
            source.write_text(document, encoding="utf-8")
            command = chromium_argv(
                self.executable, document=source, output=temporary_png, profile=profile,
                width=width, height=height, transparent=True,
            )
            try:
                completed = subprocess.run(
                    command, shell=False, capture_output=True, text=True,
                    timeout=self.timeout_seconds, check=False,
                    env=_chromium_env(profile),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Chromium timed out while rendering the overlay") from exc
            if completed.returncode:
                raise RuntimeError("Chromium could not render the overlay")
            if not temporary_png.is_file() or temporary_png.stat().st_size == 0:
                raise RuntimeError("Chromium did not produce an overlay PNG")
            with Image.open(temporary_png) as image:
                rgba = image.convert("RGBA")
                publish = output.with_name(f".{output.name}.overlay.tmp")
                rgba.save(publish, format="PNG")
            with publish.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(publish, output)
            return hashlib.sha256(output.read_bytes()).hexdigest()
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def render_thumbnail(
        self,
        artwork: Path,
        output: Path,
        layout: ThumbnailTextLayout,
        *,
        text_side: str,
        width: int = 1280,
        height: int = 720,
    ) -> tuple[str, str, str | None]:
        """Composite exact text over local artwork with bounded deterministic styling."""
        if (width, height) != (1280, 720):
            raise ValueError("Thumbnail Studio v1 output must be 1280x720")
        with Image.open(artwork) as source:
            image = source.convert("RGB")
        if image.size != (width, height):
            image = image.resize((width, height), Image.Resampling.LANCZOS)

        font_path, font_hash = self.thumbnail_font_metadata(layout.font_preset)
        if not Path(font_path).is_file():
            raise RuntimeError(f"The local {layout.font_preset} thumbnail font is unavailable")
        colors = _THUMBNAIL_PALETTES[layout.palette]
        banner = layout.layout_preset == "banner"
        left, right = (64, 1104) if banner else (
            (64, 680) if text_side == "left" else (600, 1216)
        )

        # Uppercase transforms happen before fitting so wrap widths match the drawn glyphs.
        title_text = layout.title.upper() if layout.font_preset == "impact" else layout.title
        kicker_text = _kicker_label(layout)
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        title_font, title_lines = self._fit_thumbnail_text(
            probe, title_text, font_path, right - left,
            max_lines=2 if banner else 3,
            initial_size=92 if banner else 118, minimum_size=44, preferred_lines=2,
        )
        stroke_width = 3 if layout.outline else 0
        ascent, descent = title_font.getmetrics()
        title_advance = int((ascent + descent) * 0.9)
        kicker_font = None
        kicker_tracking = 0
        if kicker_text:
            kicker_font = self._fit_single_line(
                probe, kicker_text.upper(), font_path, right - left, initial_size=30, minimum_size=18,
            )
            kicker_tracking = max(2, kicker_font.size // 9)

        image = image.convert("RGBA")
        image = Image.alpha_composite(image, self._scrim(
            layout.layout_preset, text_side, width, height,
        ))

        block_height = title_advance * (len(title_lines) - 1) + ascent + descent
        if kicker_font:
            block_height += 26 + kicker_font.size
        else:
            block_height += 28  # accent bar row hugging the title's cap line
        if banner:
            top = min(648 - block_height, 400)
        else:
            top = max(72, min((height - block_height) // 2, 648 - block_height))

        # Collect positioned runs first so the soft shadow pass can mirror them exactly.
        rows: list[tuple[int, int, str, ImageFont.FreeTypeFont, int]] = []
        bars: list[tuple[int, int, int]] = []
        y = top
        if kicker_font:
            bars.append((left, y + kicker_font.size // 2 - 4, 46))
            rows.append((left + 62, y, kicker_text.upper(), kicker_font, kicker_tracking))
            y += kicker_font.size + 26
        else:
            bars.append((left, y + 2, 58))
            y += 28
        for line in title_lines:
            rows.append((left, y, line, title_font, 0))
            y += title_advance

        if layout.shadow:
            shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow)
            for x, yy, text, font, tracking in rows:
                self._draw_tracked(shadow_draw, (x, yy + 7), text, font, (0, 0, 0, 255),
                                   tracking, stroke_width)
            shadow = shadow.filter(ImageFilter.GaussianBlur(6))
            shadow.putalpha(shadow.getchannel("A").point(lambda value: value * 64 // 100))
            image = Image.alpha_composite(image, shadow)

        canvas = ImageDraw.Draw(image, "RGBA")
        for x, mid_y, width_px in bars:
            canvas.rounded_rectangle((x, mid_y, x + width_px, mid_y + 8), radius=4, fill=colors["accent"])
        for index, (x, yy, text, font, tracking) in enumerate(rows):
            is_title = index >= len(rows) - len(title_lines)
            fill = colors["text"] if is_title else (255, 255, 255, 236)
            self._draw_tracked(canvas, (x, yy), text, font, fill, tracking, stroke_width)

        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".png", dir=output.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            image.save(temporary, format="PNG", optimize=True)
            with Image.open(temporary) as rendered:
                if rendered.size != (width, height):
                    raise RuntimeError("Thumbnail composite has unexpected dimensions")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(output.read_bytes()).hexdigest(), font_path, font_hash

    @staticmethod
    def _scrim(preset: str, text_side: str, width: int, height: int) -> Image.Image:
        """Directional darkening so copy stays legible without a flat panel."""
        alpha = Image.new("L", (width, height), 26)
        if preset == "banner":
            ramp = Image.new("L", (1, height))
            ramp.putdata([
                int(238 * _smooth((y - 300) / (height - 300))) for y in range(height)
            ])
        elif preset == "split":
            panel = Image.new("L", (width, height), 0)
            edge_top, edge_bottom = (500, 380) if text_side == "right" else (
                width - 500, width - 380,
            )
            ImageDraw.Draw(panel).polygon(
                [(edge_top, 0), (width, 0), (width, height), (edge_bottom, height)], fill=198,
            )
            ramp = panel.filter(ImageFilter.GaussianBlur(24))
        else:
            ramp = Image.new("L", (1, width))
            ramp.putdata([
                int(232 * _smooth(1.0 - ((width - x) if text_side == "right" else x) / 850.0))
                for x in range(width)
            ])
        if ramp.size != (width, height):
            ramp = ramp.resize((width, height))
        alpha = ImageChops.lighter(alpha, ramp)
        scrim = Image.new("RGBA", (width, height), (6, 8, 12, 255))
        scrim.putalpha(alpha)
        return scrim

    @staticmethod
    def _draw_tracked(
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple[int, ...],
        tracking: int,
        stroke_width: int = 0,
    ) -> None:
        """Draw text with fixed letter spacing; mirrors the width used while fitting."""
        x, y = xy
        for character in text:
            draw.text(
                (x, y), character, font=font, fill=fill,
                stroke_width=stroke_width, stroke_fill=_THUMBNAIL_STROKE,
            )
            x += draw.textlength(character, font=font) + tracking

    @classmethod
    def _fit_single_line(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        font_path: str,
        max_width: float,
        *,
        initial_size: int,
        minimum_size: int,
    ) -> ImageFont.FreeTypeFont:
        for size in range(initial_size, minimum_size - 1, -2):
            font = ImageFont.truetype(font_path, size)
            tracking = max(2, size // 9)
            if cls._tracked_width(draw, text, font, tracking) <= max_width:
                return font
        raise ValueError("thumbnail hook is too long for the selected layout")

    @staticmethod
    def _tracked_width(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
        tracking: int,
    ) -> float:
        return sum(draw.textlength(character, font=font) for character in text) \
            + tracking * max(0, len(text) - 1)

    @staticmethod
    def _wrap_thumbnail_text(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
        max_lines: int | None = None,
    ) -> list[str]:
        """Greedy-feasible word wrap, then balanced breaks that minimize ragged slack."""
        words = text.strip().split()
        if not words:
            return []
        expanded: list[str] = []
        for word in words:
            if draw.textlength(word, font=font) <= max_width:
                expanded.append(word)
                continue
            chunk = ""
            for character in word:
                if chunk and draw.textlength(chunk + character, font=font) > max_width:
                    expanded.append(chunk)
                    chunk = character
                else:
                    chunk += character
            if chunk:
                expanded.append(chunk)
        count = len(expanded)
        space = draw.textlength(" ", font=font)
        widths = [draw.textlength(word, font=font) for word in expanded]
        starts = [0]
        for index in range(count):
            starts.append(starts[index] + widths[index] + (space if index else 0))

        def line_width(begin: int, end: int) -> float:  # words[begin:end]
            return starts[end] - starts[begin] - (space if begin else 0)

        infinity = float("inf")
        limit = min(max_lines, count) if max_lines is not None else count
        best = [[infinity] * (count + 1) for _ in range(limit + 1)]
        cuts = [[count] * (count + 1) for _ in range(limit + 1)]
        best[0][count] = 0.0
        for lines_used in range(1, limit + 1):
            for begin in range(count - 1, -1, -1):
                for end in range(begin + 1, count + 1):
                    width = line_width(begin, end)
                    if width > max_width or best[lines_used - 1][end] == infinity:
                        continue
                    cost = (max_width - width) ** 2 + best[lines_used - 1][end]
                    if cost < best[lines_used][begin]:
                        best[lines_used][begin] = cost
                        cuts[lines_used][begin] = end
        used = next((k for k in range(limit + 1) if best[k][0] < infinity), None)
        if used is None:
            # Not balanced-packable within the limit; return the minimal greedy packing.
            lines = []
            begin = 0
            while begin < count:
                end = begin + 1
                while end < count and line_width(begin, end + 1) <= max_width:
                    end += 1
                lines.append(" ".join(expanded[begin:end]))
                begin = end
            return lines
        lines: list[str] = []
        begin = 0
        for remaining in range(used, 0, -1):
            end = cuts[remaining][begin]
            lines.append(" ".join(expanded[begin:end]))
            begin = end
        return lines

    @classmethod
    def _fit_thumbnail_text(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        font_path: str,
        max_width: int,
        *,
        max_lines: int,
        initial_size: int,
        minimum_size: int,
        preferred_lines: int | None = None,
    ) -> tuple[ImageFont.FreeTypeFont, list[str]]:
        """Largest fitting size, preferring tight balanced blocks over raw maximum size."""
        for limit in dict.fromkeys(filter(None, [preferred_lines, max_lines])):
            for size in range(initial_size, minimum_size - 1, -2):
                font = ImageFont.truetype(font_path, size)
                lines = cls._wrap_thumbnail_text(draw, text, font, max_width, limit)
                if len(lines) <= limit:
                    return font, lines
        font = ImageFont.truetype(font_path, minimum_size)
        lines = cls._wrap_thumbnail_text(draw, text, font, max_width, max_lines)
        if len(lines) > max_lines:
            raise ValueError("thumbnail text is too long for the selected layout")
        return font, lines

    @staticmethod
    def thumbnail_font_metadata(preset: str) -> tuple[str, str | None]:
        family = {
            "impact": "DejaVu Sans Condensed Bold",
            "clean": "Noto Sans Bold",
            "editorial": "Liberation Serif Bold",
        }.get(preset)
        if family is None:
            raise ValueError("unknown thumbnail font preset")
        try:
            completed = subprocess.run(
                ["fc-match", "-f", "%{file}", family], shell=False,
                capture_output=True, text=True, timeout=5, check=False,
            )
            font = Path(completed.stdout.strip())
            if completed.returncode == 0 and font.is_file():
                return str(font), hashlib.sha256(font.read_bytes()).hexdigest()
        except (OSError, subprocess.SubprocessError):
            pass
        return family, None
