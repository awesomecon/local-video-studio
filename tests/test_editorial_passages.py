from __future__ import annotations

import base64
import io
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from backend.editorial.passages import locate_passage, match_words
from backend.editorial import EditPlan, compile_edit_plan_html


def test_matching_requires_complete_unique_confident_words() -> None:
    rows = [{"text": word, "conf": "95"} for word in
            "A horrible intervention of technology in daily life".split()]
    assert len(match_words(rows, '“horrible intervention of technology”')) == 4
    assert not match_words(rows, "wonderful intervention")
    assert not match_words(rows, "tech")
    assert not match_words(rows + rows, "horrible intervention")
    rows[2]["conf"] = "30"
    assert not match_words(rows, "horrible intervention")


def _source(tmp_path: Path) -> Path:
    font = ImageFont.truetype(
        "backend/editorial/fonts/NotoSerif-Regular.ttf", 40,
    )
    image = Image.new("RGB", (1300, 1600), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate([
        "This is the preceding sentence on the page.",
        "A horrible intervention of technology",
        "in the sacred mysteries of the human mind.",
        "This is the next sentence on the page.",
    ]):
        draw.text((100, 600 + index * 65), line, fill="black", font=font)
    path = tmp_path / "source.png"
    image.save(path)
    return path


def test_local_ocr_crops_context_and_marks_only_matched_lines(tmp_path: Path) -> None:
    if not shutil.which("tesseract"):
        pytest.skip("Local OCR is unavailable")
    source = _source(tmp_path)
    data = "data:image/png;base64," + base64.b64encode(source.read_bytes()).decode()
    result = locate_passage(data, "intervention of technology in the sacred mysteries")
    assert result is not None
    url, boxes = result
    assert len(boxes) == 2
    with Image.open(io.BytesIO(base64.b64decode(url.split(",")[1]))) as crop:
        assert crop.height < 400
        assert crop.width < 1300
    assert all(0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1
               for x, y, w, h in boxes)
    assert boxes[0][0] > boxes[1][0]
    assert locate_passage(data, "unrelated words not present") is None


def test_compiler_uses_local_source_and_suppresses_legacy_random_mark(tmp_path: Path) -> None:
    source = _source(tmp_path)
    plan = EditPlan.model_validate({
        "project_id": "p", "compositions": [{
            "id": "c", "start": 0, "duration": 3, "template": "documentReveal",
            "assets": [{"id": "page", "type": "document", "source": source.name,
                        "evidence_class": "evidence", "locked": True}],
            "elements": [
                {"id": "doc", "type": "document", "role": "document", "asset_id": "page"},
                {"id": "mark", "type": "underline", "role": "passage-mark"},
            ],
            "events": [{"time": 0, "action": "fade", "target": "doc"},
                       {"time": 1, "action": "underline", "target": "mark"}],
        }],
    })
    html = compile_edit_plan_html(plan, asset_root=tmp_path)
    assert 'id="mark"' not in html.split("<script>")[0]
    plan.compositions[0].elements[1].text = "horrible intervention of technology"
    if shutil.which("tesseract"):
        html = compile_edit_plan_html(plan, asset_root=tmp_path)
        assert 'id="mark" class="verified-passage' in html
        assert 'data-box="' in html
        assert 'SOURCE EXCERPT' in html
        assert '“horrible intervention of technology”' in html
        assert 'img.offsetTop' in html
    assert source.read_bytes()  # Source remains intact.
