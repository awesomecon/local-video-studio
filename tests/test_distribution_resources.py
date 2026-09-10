"""Exercise a built wheel outside the checkout, without installing packages."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


def test_wheel_contains_core_resources_and_runs_outside_checkout(tmp_path: Path) -> None:
    if importlib.util.find_spec("wheel") is None:
        pytest.fail("Wheel validation needs the existing wheel build tool; install it explicitly")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "setup.py", "build", "--build-base", str(tmp_path / "build"),
         "bdist_wheel", "--bdist-dir", str(tmp_path / "bdist"),
         "--dist-dir", str(tmp_path / "dist")],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    wheel = next((tmp_path / "dist").glob("*.whl"))
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "backend/_resources/config/default.yaml" in names
        assert "backend/_resources/frontend/index.html" in names
        assert "backend/editorial/fonts/NotoSans-Regular.ttf" in names
        assert not any("local.yaml" in name or ".env" in name or "AGENTS.local" in name
                       for name in names)
        archive.extractall(unpacked)
    # -I ignores PYTHONPATH and the current directory. Insert only the unpacked
    # distribution; third-party dependencies come from the existing interpreter.
    code = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import backend
assert Path(backend.__file__).is_relative_to(Path(sys.argv[1]))
from backend.core.config import load_config
from backend.core.resources import resource_path
config = load_config(environ={})
data = Path(sys.argv[2])
for field in type(config.paths).model_fields:
    if field != "minimum_free_disk_gb":
        setattr(config.paths, field, data / field)
from unittest.mock import patch
with patch("backend.core.load_config", return_value=config):
    from backend.api.main import create_app
from fastapi.testclient import TestClient
with TestClient(create_app(config, mock_mode=True)) as client:
    assert client.get("/health").json()["mode"] == "mock"
    assert client.get("/").status_code == 200
    assert client.get("/js/app.js").status_code == 200
assert resource_path("workflows", "comfyui", "minimax-h3-av.workflow.json").is_file()
from backend.pipeline.service import PipelineService
from backend.schemas import ProjectCreate
service = PipelineService(config, mock_mode=True)
project = service.create_project(ProjectCreate(title="Wheel smoke", topic="A test", target_duration=1, resolution=(160, 90)))
output = service.run_project(project.id)
assert output.is_file() and output.stat().st_size > 0
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(unpacked), str(tmp_path / "data")],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
