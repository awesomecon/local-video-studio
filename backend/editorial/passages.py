"""Local, conservative OCR matching for source-document excerpts."""

from __future__ import annotations

import base64
import csv
import io
import re
import shutil
import subprocess
import tempfile
import unicodedata
from functools import lru_cache
from pathlib import Path

from PIL import Image


def tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


def match_words(rows: list[dict[str, str]], quote: str) -> list[dict[str, str]]:
    """Accept one complete, confident match; never choose a nearby/random line."""
    wanted = tokens(quote)
    if not wanted:
        return []
    words: list[str] = []
    owners: list[int] = []
    for index, row in enumerate(rows):
        pieces = tokens(row.get("text", ""))
        words.extend(pieces)
        owners.extend([index] * len(pieces))
    hits = [i for i in range(len(words) - len(wanted) + 1)
            if words[i:i + len(wanted)] == wanted]
    if len(hits) != 1:
        return []
    start = hits[0]
    selected = rows[owners[start]:owners[start + len(wanted) - 1] + 1]
    if any(float(row.get("conf", "-1")) < 60 for row in selected):
        return []
    return selected


@lru_cache(maxsize=24)
def locate_passage(source: str, quote: str) -> tuple[str, tuple[tuple[float, ...], ...]] | None:
    """Return a lossless context crop and normalized per-line highlight boxes.

    Only embedded local images are accepted. Missing OCR or uncertain text yields
    no mark. Original media is never overwritten, and OCR never uses a network.
    """
    executable = shutil.which("tesseract")
    if not executable or not source.startswith("data:image/") or not tokens(quote):
        return None
    try:
        payload = base64.b64decode(source.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(payload)) as original:
            original.load()
            with tempfile.TemporaryDirectory(prefix="lvs-passage-") as directory:
                path = Path(directory) / "source.png"
                original.convert("RGB").save(path)
                result = subprocess.run(
                    [executable, str(path), "stdout", "--psm", "3", "tsv"],
                    capture_output=True, text=True, timeout=30, check=True,
                )
            rows = [row for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
                    if row.get("level") == "5" and row.get("text", "").strip()]
            selected = match_words(rows, quote)
            if not selected:
                return None
            def box(row: dict[str, str]) -> tuple[int, int, int, int]:
                x, y, w, h = (int(row[key]) for key in ("left", "top", "width", "height"))
                return x, y, x + w, y + h
            lines: dict[tuple[str, ...], list[tuple[int, int, int, int]]] = {}
            for row in selected:
                key = tuple(row[name] for name in ("page_num", "block_num", "par_num", "line_num"))
                lines.setdefault(key, []).append(box(row))
            boxes = [(min(b[0] for b in line), min(b[1] for b in line),
                      max(b[2] for b in line), max(b[3] for b in line))
                     for line in lines.values()]
            # Keep neighboring text for context, trim the page's blank margins.
            height = max(b[3] - b[1] for b in boxes)
            top = max(0, min(b[1] for b in boxes) - height * 2)
            bottom = min(original.height, max(b[3] for b in boxes) + height * 2)
            context = [box(row) for row in rows if top <= int(row["top"]) < bottom]
            left = max(0, min(b[0] for b in context) - height)
            right = min(original.width, max(b[2] for b in context) + height)
            cropped = original.crop((left, top, right, bottom))
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            normalized = tuple(((x-left)/cropped.width, (y-top)/cropped.height,
                                (r-x)/cropped.width, (b-y)/cropped.height)
                               for x, y, r, b in boxes)
            return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"), normalized
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None
