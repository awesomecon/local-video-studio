"""Exercise CI report privacy and native smoke lifecycle without browser installs."""
from pathlib import Path
import xml.etree.ElementTree as ET

from backend.core.config import AppConfig
from scripts.ci_core_smoke import running_app
from scripts.ci_test_report import sanitize


def test_report_retains_outcomes_without_captured_private_data(tmp_path: Path) -> None:
    source = tmp_path / "raw.xml"
    target = tmp_path / "safe.xml"
    source.write_text('''<testsuites><testsuite name="pytest" tests="1" failures="1"
        hostname="private-host"><properties><property name="token" value="secret"/></properties>
        <testcase classname="tests.test_example" name="test_example" time="0.1">
        <failure message="private-prompt">private traceback</failure>
        <system-out>private media path</system-out><system-err>private stderr</system-err>
        </testcase></testsuite></testsuites>''', encoding="utf-8")
    sanitize(source, target)
    output = target.read_text(encoding="utf-8")
    assert "private" not in output and "secret" not in output and "token" not in output
    case = ET.fromstring(output).find("testsuite/testcase")
    assert case is not None and case.attrib["name"] == "test_example"
    assert case.find("failure") is not None


def test_native_smoke_server_reopens_isolated_project(tmp_path: Path) -> None:
    config = AppConfig()
    for name in type(config.paths).model_fields:
        if name != "minimum_free_disk_gb":
            setattr(config.paths, name, tmp_path / name)
    for name in type(config.backends).model_fields:
        getattr(config.backends, name).enabled = False
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    with running_app(path) as client:
        assert client.get("/").status_code == 200
        response = client.post("/api/projects", json={
            "title": "CI fixture", "topic": "Synthetic", "target_duration": 1,
            "resolution": [160, 90], "fps": 12,
        })
        assert response.status_code == 201
        project_id = response.json()["project"]["id"]
    with running_app(path) as client:
        response = client.get(f"/api/projects/{project_id}")
        assert response.status_code == 200
        assert response.json()["project"]["title"] == "CI fixture"
