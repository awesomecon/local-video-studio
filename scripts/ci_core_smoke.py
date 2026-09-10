"""Native, model-free acceptance check. Installs nothing; missing tools fail."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
from importlib.metadata import version
import json
import multiprocessing
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
REPORTS = Path("ci-reports")
SERVER_BIND_TIMEOUT_SECONDS = 60.0


def write_report(name: str, payload: dict) -> None:
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def prerequisites() -> dict:
    from backend.rendering.binaries import discover_binaries
    from playwright.sync_api import sync_playwright

    binaries = discover_binaries()
    assert binaries.ffmpeg is not None, "Required FFmpeg unavailable"
    assert binaries.ffprobe is not None, "Required ffprobe unavailable"
    versions = {"playwright": version("playwright")}
    for name, executable in (("ffmpeg", binaries.ffmpeg), ("ffprobe", binaries.ffprobe)):
        result = subprocess.run([str(executable), "-version"], capture_output=True,
                                text=True, check=True, timeout=15)
        # Only the version token, never build flags, paths or environment.
        versions[name] = result.stdout.split()[2]
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
        assert Path(executable).is_file(), "Required Chromium unavailable"
        with playwright.chromium.launch(executable_path=executable) as browser:
            versions["chromium"] = browser.version
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as output:
            output.write(f"LVS_CHROME={executable}\n")
            output.write("LVS_CHROME_VALIDATED=1\n")
            output.write(f"LVS_CHROME_VERSION={versions['chromium']}\n")
    # Set for this process too; the separate smoke command checks again.
    os.environ["LVS_CHROME"] = executable
    os.environ["LVS_CHROME_VALIDATED"] = "1"
    os.environ["LVS_CHROME_VERSION"] = versions["chromium"]
    if os.environ.get("LVS_REQUIRE_CORE_TOOLS") == "1":
        for package in ("torch", "torchvision", "torchaudio", "faster_whisper"):
            assert importlib.util.find_spec(package) is None, f"Unexpected AI dependency: {package}"
    return {
        "runner": os.environ.get("LVS_CI_RUNNER", "local"),
        "os": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(), "versions": versions,
    }


def serve(config_path: str, ready, stop) -> None:
    """Spawn-safe entry point; even the module-level app gets isolated config."""
    try:
        import uvicorn
        from backend.core.config import load_config

        config = load_config(config_path, environ={})
        with patch("backend.core.load_config", return_value=config):
            from backend.api.main import create_app
        app = create_app(config, mock_mode=True)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False,
                                              timeout_graceful_shutdown=10))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            ready.send({"status": "ready", "port": listener.getsockname()[1]})

            def watch_stop() -> None:
                stop.wait()
                server.should_exit = True

            threading.Thread(target=watch_stop, daemon=True).start()
            server.run(sockets=[listener])
    except BaseException as error:
        try:
            ready.send({
                "status": "error",
                "error_type": type(error).__name__,
                "message": str(error)[:500],
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        ready.close()


@contextmanager
def running_app(config_path: Path):
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    stop = context.Event()
    process = context.Process(target=serve, args=(str(config_path), send, stop))
    process.start()
    send.close()
    try:
        deadline = time.monotonic() + SERVER_BIND_TIMEOUT_SECONDS
        while not receive.poll(0.1):
            if not process.is_alive():
                process.join()
                raise AssertionError(
                    f"Owned server exited before binding (exit code {process.exitcode})"
                )
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"Server did not bind within {SERVER_BIND_TIMEOUT_SECONDS:g} seconds"
                )
        try:
            startup = receive.recv()
        except EOFError as error:
            process.join()
            raise AssertionError(
                f"Owned server closed its startup channel (exit code {process.exitcode})"
            ) from error
        if startup.get("status") == "error":
            raise RuntimeError(
                "Owned server startup failed: "
                f"{startup['error_type']}: {startup['message']}"
            )
        port = startup["port"]
        assert port != 1234
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                          timeout=10) as client:
            for _ in range(150):
                assert process.is_alive(), "Owned server exited during startup"
                try:
                    health = client.get("/health")
                    if health.status_code == 200:
                        assert health.json() == {"status": "ok", "mode": "mock"}
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Server did not become ready")
            yield client
    finally:
        active_error = sys.exc_info()[0] is not None
        receive.close()
        stop.set()
        process.join(20)
        forced = process.is_alive()
        if forced:
            process.terminate()
            process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        exitcode = process.exitcode
        process.close()
        if not active_error:
            assert not forced and exitcode == 0, "Owned server failed graceful shutdown"


def smoke(completed: list[str]) -> None:
    from backend.core.config import AppConfig
    from backend.models import GenerationRequest, MockGeneratorBackend
    from backend.rendering.binaries import discover_binaries
    from backend.rendering.probe import probe_media
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="lvs-native-core-") as directory:
        root = Path(directory)
        config = AppConfig()
        for name in type(config.paths).model_fields:
            if name != "minimum_free_disk_gb":
                setattr(config.paths, name, root / name)
        for name in type(config.backends).model_fields:
            backend = getattr(config.backends, name)
            backend.managed = False
            backend.enabled = False
        config_path = root / "config.json"
        config_path.write_text(config.model_dump_json(), encoding="utf-8")
        with running_app(config_path) as client:
            response = client.get("/")
            assert response.status_code == 200
            assert "Local Video Studio" in response.text
            # Request every shipped JS/CSS/icon, including transitive modules.
            frontend = Path(__file__).resolve().parents[1] / "frontend"
            for asset in sorted(frontend.rglob("*")):
                if asset.suffix not in {".js", ".css", ".svg"}:
                    continue
                response = client.get("/" + asset.relative_to(frontend).as_posix())
                assert response.status_code == 200
                assert response.content == asset.read_bytes()
            completed.append("root-and-static-assets")
            with sync_playwright() as playwright:
                with playwright.chromium.launch(executable_path=os.environ["LVS_CHROME"]) as browser:
                    page = browser.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    # Block all non-loopback requests from the test browser.
                    origin = str(client.base_url).rstrip("/")
                    page.route("**/*", lambda route: route.continue_()
                               if route.request.url.startswith(origin + "/") else route.abort())
                    page.goto(origin + "/#/", wait_until="domcontentloaded")
                    page.locator('#app[aria-busy="false"]').wait_for()
                    assert "Failed to load" not in page.locator("#app").inner_text()
                    assert page.get_by_role("button", name="Dashboard", exact=True).count() > 0
                    assert not errors, "Browser raised an uncaught exception"
            completed.append("chromium-ui-boot")
            response = client.post("/api/projects", json={
                "title": "Native core smoke", "topic": "Synthetic test",
                "target_duration": 1, "resolution": [160, 90], "fps": 12,
            })
            assert response.status_code == 201
            project_id = response.json()["project"]["id"]
            completed.append("project-create")
        with running_app(config_path) as client:
            response = client.get(f"/api/projects/{project_id}")
            assert response.status_code == 200
            assert response.json()["project"]["title"] == "Native core smoke"
            completed.append("project-reopen-after-restart")
        result = MockGeneratorBackend().generate(GenerationRequest(
            job_id="native-smoke", output_dir=root / "video", prompt="Synthetic test",
            seed=7, duration_seconds=0.5, width=160, height=90, settings={"kind": "video"},
        ))
        media = probe_media(result.outputs[0], discover_binaries())
        assert result.outputs[0].suffix == ".mp4"
        assert media.has_video and media.has_audio
        assert (media.width, media.height) == (160, 90)
        assert media.duration_seconds is not None and 0.4 <= media.duration_seconds <= 0.7
        completed.append("mock-mp4-generate-and-probe")
    assert not root.exists()
    completed.append("owned-server-shutdown-and-temp-cleanup")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prerequisites", action="store_true")
    args = parser.parse_args()
    completed: list[str] = []
    report = {
        "status": "failed", "completed": completed,
        "runner": os.environ.get("LVS_CI_RUNNER", "local"),
        "os": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(),
    }
    try:
        report.update(prerequisites())
        completed.append("required-prerequisites")
        if not args.prerequisites:
            smoke(completed)
        report["status"] = "passed"
    except Exception as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        write_report("prerequisites.json" if args.prerequisites else "native-smoke.json", report)


if __name__ == "__main__":
    main()
