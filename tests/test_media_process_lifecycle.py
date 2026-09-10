"""Small, model-free subprocess tests for portable cancellation semantics."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from backend.rendering import process


def command(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("subprocess did not reach the expected state")
        time.sleep(0.01)


def job_id() -> str:
    return str(uuid.uuid4())


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_job_cancel_stops_owned_process(stream: bool) -> None:
    job = job_id()

    def run():
        with process.media_process_scope(job):
            if stream:
                return process.run_media_process_stream(
                    command("import time; time.sleep(30)"), [b"x" * (2 * 1024 * 1024)],
                )
            return process.run_media_process(command("import time; time.sleep(30)"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        wait_for(lambda: any(value == job for value in process._process_jobs.values()))
        assert len(process.cancel_media_processes_for_job(job)) == 1
        with pytest.raises(process.CanceledError):
            future.result(timeout=6)
    assert not any(value == job for value in process._process_jobs.values())


def test_cancel_before_spawn_and_retry_keeps_old_scope_canceled(tmp_path: Path) -> None:
    job = job_id()
    marker = tmp_path / "must-not-exist"
    with process.media_process_scope(job):
        assert process.cancel_media_processes_for_job(job) == []
        with pytest.raises(process.CanceledError):
            process.run_media_process(command(f"open({str(marker)!r}, 'w').close()"))
        process.reset_media_process_cancellation(job)
        with pytest.raises(process.CanceledError):
            process.raise_if_media_job_canceled(job)
        with process.media_process_scope(job):
            with pytest.raises(process.CanceledError):
                process.raise_if_media_job_canceled()
    assert not marker.exists()
    with process.media_process_scope(job):
        process.run_media_process(command("pass"))


def test_nested_scope_restores_outer_cancellation() -> None:
    outer, inner = job_id(), job_id()
    with process.media_process_scope(outer):
        with process.media_process_scope(inner):
            process.cancel_media_processes_for_job(inner)
            with pytest.raises(process.CanceledError):
                process.raise_if_media_job_canceled()
        assert process.current_media_job_id() == outer
        process.raise_if_media_job_canceled()
        process.cancel_media_processes_for_job(outer)
        with pytest.raises(process.CanceledError):
            process.raise_if_media_job_canceled()
    process.raise_if_media_job_canceled()


def test_shutdown_cancels_scopes_between_processes() -> None:
    job = job_id()
    with process.media_process_scope(job):
        assert process.current_media_job_id() == job
        process.cancel_all_media_processes()
        with pytest.raises(process.CanceledError):
            process.raise_if_media_job_canceled()
    assert process.current_media_job_id() is None


def test_explicit_job_retains_enclosing_cancellation() -> None:
    enclosing, explicit = job_id(), job_id()
    process.cancel_media_processes_for_job(enclosing)
    with process.media_process_scope(enclosing):
        with pytest.raises(process.CanceledError):
            process.run_media_process(command("pass"), job_id=explicit)
    process.run_media_process(command("pass"), job_id=explicit)


def test_publication_guard_preserves_existing_output_after_cancel(tmp_path: Path) -> None:
    staged, output = tmp_path / "staged", tmp_path / "output"
    staged.write_text("incomplete attempt")
    output.write_text("previous complete artifact")
    job = job_id()
    with process.media_process_scope(job):
        process.cancel_media_processes_for_job(job)
        with pytest.raises(process.CanceledError):
            with process.media_output_publication():
                os.replace(staged, output)
    assert output.read_text() == "previous complete artifact"
    assert staged.read_text() == "incomplete attempt"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.skipif(os.name != "posix", reason="POSIX signal exit semantics")
def test_external_signal_is_failure_not_cancel(stream: bool) -> None:
    argv = command("import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    with pytest.raises(process.MediaProcessError) as raised:
        if stream:
            process.run_media_process_stream(argv, [], timeout=3)
        else:
            process.run_media_process(argv, timeout=3)
    assert type(raised.value) is process.MediaProcessError
    assert raised.value.returncode == -signal.SIGTERM


@pytest.mark.parametrize("exit_code", [0, 7, 1_073_741_510])
def test_cancel_intent_is_independent_of_native_exit_code(exit_code: int) -> None:
    # Includes a Windows-style positive termination status and graceful exit 0.
    class Exited:
        returncode = exit_code

    state = process._ProcessState(Exited(), None, reason="canceled")
    with pytest.raises(process.CanceledError) as raised:
        process._check_result(["child"], state, state.reason, "", None)
    assert raised.value.returncode == exit_code


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.skipif(os.name != "posix", reason="POSIX graceful termination handler")
def test_cancel_prevents_success_when_child_exits_zero(stream: bool, tmp_path: Path) -> None:
    job = job_id()
    ready = tmp_path / "ready"
    script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, lambda *args: exit(0)); "
        f"open({str(ready)!r}, 'w').close(); time.sleep(30)"
    )
    published: list[bool] = []

    def render():
        if stream:
            process.run_media_process_stream(command(script), [], job_id=job)
        else:
            process.run_media_process(command(script), job_id=job)
        published.append(True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(render)
        wait_for(ready.exists)
        process.cancel_media_processes_for_job(job)
        with pytest.raises(process.CanceledError):
            future.result(timeout=6)
    assert published == []


@pytest.mark.parametrize("stream", [False, True])
def test_timeout_is_failure_and_bounds_blocked_writes(stream: bool) -> None:
    started = time.monotonic()
    with pytest.raises(process.MediaProcessError, match="timed out") as raised:
        if stream:
            process.run_media_process_stream(
                command("import time; time.sleep(30)"), [b"x" * (4 * 1024 * 1024)],
                timeout=0.15,
            )
        else:
            process.run_media_process(command("import time; time.sleep(30)"), timeout=0.15)
    assert type(raised.value) is process.MediaProcessError
    assert time.monotonic() - started < 6


def test_first_stop_reason_wins() -> None:
    proc = subprocess.Popen(command("import time; time.sleep(30)"))
    pid = process._register_process(proc, None)
    state = process._process_states[pid]
    try:
        assert process._request_stop(state, "timeout")
        assert not process._request_stop(state, "canceled")
        assert state.reason == "timeout"
    finally:
        process._stop_owned_process(state)
        process._unregister_process(pid)


def test_job_cancel_leaves_other_job_and_unknown_pid_alone() -> None:
    first, second = job_id(), job_id()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(process.run_media_process, command("import time; time.sleep(30)"), job_id=first)
        b = pool.submit(process.run_media_process, command("import time; time.sleep(30)"), job_id=second)
        try:
            wait_for(lambda: {first, second}.issubset(set(process._process_jobs.values())))
            assert not process.cancel_media_process_by_pid(os.getpid())
            process.cancel_media_processes_for_job(first)
            with pytest.raises(process.CanceledError):
                a.result(timeout=6)
            assert not b.done()
        finally:
            process.cancel_media_processes_for_job(second)
        with pytest.raises(process.CanceledError):
            b.result(timeout=6)


def test_owned_descendant_is_killed_and_direct_child_reaped(tmp_path: Path) -> None:
    ready = tmp_path / "child.pid"
    child_script = (
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(ready)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    )
    script = (
        "import subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}]); time.sleep(30)"
    )
    job = job_id()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process.run_media_process, command(script), job_id=job)
        wait_for(lambda: ready.exists() and ready.read_text())
        descendant = psutil.Process(int(ready.read_text()))

        def descendant_exited() -> bool:
            try:
                return not descendant.is_running() or descendant.status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        try:
            process.cancel_media_processes_for_job(job)
            with pytest.raises(process.CanceledError):
                future.result(timeout=6)
            wait_for(descendant_exited)
            assert job not in process._process_jobs.values()
        finally:
            # Cleanup is limited to this test's known child identity if it failed.
            try:
                descendant.kill()
            except psutil.NoSuchProcess:
                pass


def test_stream_producer_error_closes_generator_and_reaps() -> None:
    closed = threading.Event()

    def chunks():
        try:
            yield b"one frame"
            raise ValueError("producer failed")
        finally:
            closed.set()

    before = set(process.get_active_media_pids())
    with pytest.raises(ValueError, match="producer failed"):
        process.run_media_process_stream(command("import time; time.sleep(30)"), chunks())
    assert closed.is_set()
    assert set(process.get_active_media_pids()) == before


def test_regular_stdout_and_nonzero_stderr_are_preserved() -> None:
    result = process.run_media_process(command("print('hello')"), capture_stdout=True)
    assert result.stdout == "hello\n"
    with pytest.raises(process.MediaProcessError) as raised:
        process.run_media_process(command("import sys; sys.stderr.write('bad input'); sys.exit(9)"))
    assert raised.value.returncode == 9
    assert raised.value.stderr == "bad input"


def test_owned_context_cleans_up_without_false_cancellation() -> None:
    with process.owned_media_process(command("import time; time.sleep(30)")) as owned:
        pid = owned.pid
        assert owned.poll() is None
        assert pid in process.get_active_media_pids()
    assert owned.returncode is not None
    assert pid not in process.get_active_media_pids()


def test_owned_context_preserves_body_error_and_explicit_cancel() -> None:
    with pytest.raises(ValueError, match="browser protocol failed"):
        with process.owned_media_process(command("import time; time.sleep(30)")):
            raise ValueError("browser protocol failed")
    job = job_id()
    with pytest.raises(process.CanceledError):
        with process.owned_media_process(command("import time; time.sleep(30)"), job_id=job):
            process.cancel_media_processes_for_job(job)
            raise RuntimeError("browser disappeared during cancellation")


def test_owned_context_refuses_shell_or_group_override() -> None:
    for option in ("shell", "start_new_session", "creationflags", "preexec_fn", "process_group"):
        with pytest.raises(ValueError, match="ownership"):
            with process.owned_media_process(command("pass"), **{option: True}):
                pytest.fail("unsafe process option reached the body")


@pytest.mark.skipif(os.name != "posix", reason="POSIX group lifetime semantics")
def test_already_reaped_owned_context_never_signals_its_old_group(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(process.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    with process.owned_media_process(command("pass")) as owned:
        wait_for(lambda: owned.poll() is not None)
    assert signals == []


def test_poll_does_not_reap_after_cancellation_intent() -> None:
    class Child:
        returncode = None

        def poll(self):
            pytest.fail("poll would release the owned leader before group cleanup")

    state = process._ProcessState(Child(), None, reason="canceled")
    assert process.OwnedMediaProcess(state).poll() is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX group lifetime semantics")
def test_cleanup_uses_captured_identity_and_escalates_before_reaping(monkeypatch) -> None:
    calls: list[str] = []

    class Child:
        pid = 999_910
        returncode = None

        def poll(self):
            pytest.fail("cleanup must not reap before signaling its group")

        def kill(self):
            calls.append("kill-root")

        def wait(self, timeout=None):
            calls.append("reap")
            self.returncode = 0
            return 0

    class Identity:
        def is_running(self):
            return True

        def children(self, recursive=False):
            calls.append("snapshot")
            return []

    monkeypatch.setattr(process.psutil, "Process", lambda pid: Identity())
    child = Child()
    process._register_process(child, None, owns_group=True)
    state = process._process_states[child.pid]

    def reconstructed(pid):
        pytest.fail("cleanup reconstructed ownership from a potentially reused PID")

    monkeypatch.setattr(process.psutil, "Process", reconstructed)
    monkeypatch.setattr(process.os, "killpg", lambda pid, sig: calls.append(f"group-{sig}"))
    monkeypatch.setattr(process.time, "sleep", lambda duration: None)
    try:
        process._stop_owned_process(state)
    finally:
        process._unregister_process(child.pid)
    assert calls[:3] == ["snapshot", f"group-{signal.SIGTERM}", f"group-{signal.SIGKILL}"]
    assert calls.index(f"group-{signal.SIGKILL}") < calls.index("reap")


def test_changed_root_identity_is_never_signaled(monkeypatch) -> None:
    class Child:
        pid = 999_911
        returncode = None

        def terminate(self):
            pytest.fail("the original root identity no longer exists")

        def kill(self):
            pytest.fail("the original root identity no longer exists")

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    class GoneIdentity:
        def is_running(self):
            return False

        def children(self, recursive=False):
            pytest.fail("do not discover children from a reused root PID")

    state = process._ProcessState(Child(), None, owns_group=True, root=GoneIdentity())
    process._stop_owned_process(state)
    assert state.proc.returncode == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX late process-group member")
@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="late-member re-sweep reads group membership from /proc",
)
def test_group_escalation_stops_child_born_during_parent_termination(tmp_path: Path) -> None:
    parent_ready = tmp_path / "parent-ready"
    descendant_ready = tmp_path / "descendant.pid"
    descendant_script = (
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(descendant_ready)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    )
    script = (
        "import os, signal, subprocess, sys, time\n"
        "def stopped(*args):\n"
        f"    subprocess.Popen([sys.executable, '-c', {descendant_script!r}])\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        f"open({str(parent_ready)!r}, 'w').close()\n"
        "time.sleep(30)\n"
    )
    job = job_id()
    descendant = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process.run_media_process, command(script), job_id=job)
        try:
            wait_for(parent_ready.exists)
            process.cancel_media_processes_for_job(job)
            with pytest.raises(process.CanceledError):
                future.result(timeout=6)
            assert descendant_ready.exists(), "the helper never reached the intended race"
            try:
                descendant = psutil.Process(int(descendant_ready.read_text()))
            except psutil.NoSuchProcess:
                return
            assert not descendant.is_running() or descendant.status() == psutil.STATUS_ZOMBIE
        finally:
            process.cancel_media_processes_for_job(job)
            if descendant is not None:
                try:
                    descendant.kill()
                except psutil.NoSuchProcess:
                    pass


@pytest.mark.parametrize("runner", ["regular", "stream", "owned"])
def test_parent_cancel_reaches_nested_media_but_leaves_unrelated_job(runner: str) -> None:
    parent, child, unrelated = job_id(), job_id(), job_id()

    def nested_run():
        with process.media_process_scope(parent):
            with process.media_process_scope(child):
                argv = command("import time; time.sleep(30)")
                if runner == "stream":
                    return process.run_media_process_stream(argv, [], job_id=child)
                if runner == "owned":
                    with process.owned_media_process(argv, job_id=child) as owned:
                        wait_for(lambda: owned.poll() is not None)
                    return None
                return process.run_media_process(argv, job_id=child)

    with ThreadPoolExecutor(max_workers=2) as pool:
        nested = pool.submit(nested_run)
        other = pool.submit(
            process.run_media_process, command("import time; time.sleep(30)"), job_id=unrelated,
        )
        try:
            wait_for(lambda: {child, unrelated}.issubset(set(process._process_jobs.values())))
            assert len(process.cancel_media_processes_for_job(parent)) == 1
            with pytest.raises(process.CanceledError):
                nested.result(timeout=6)
            assert not other.done()
        finally:
            process.cancel_media_processes_for_job(parent)
            process.cancel_media_processes_for_job(unrelated)
        with pytest.raises(process.CanceledError):
            other.result(timeout=6)
    # Canceling the ancestor does not poison an independent use of the leaf id.
    process.raise_if_media_job_canceled(child)


def test_reset_does_not_revive_ancestor_tokens_or_publication(tmp_path: Path) -> None:
    parent, child, explicit = job_id(), job_id(), job_id()
    staged, output = tmp_path / "staged", tmp_path / "output"
    staged.write_text("canceled attempt")
    output.write_text("previous artifact")
    with process.media_process_scope(parent):
        with process.media_process_scope(child):
            process.cancel_media_processes_for_job(parent)
            process.reset_media_process_cancellation(parent)
            with process.media_process_scope(explicit):
                with pytest.raises(process.CanceledError):
                    process.raise_if_media_job_canceled(explicit)
            with pytest.raises(process.CanceledError):
                with process.media_output_publication(child):
                    os.replace(staged, output)
        with pytest.raises(process.CanceledError):
            process.raise_if_media_job_canceled()
    with process.media_process_scope(parent):
        process.raise_if_media_job_canceled()
    assert output.read_text() == "previous artifact"


def test_anonymous_nested_scope_cannot_erase_ancestor_cancellation() -> None:
    parent = job_id()
    with process.media_process_scope(parent):
        process.cancel_media_processes_for_job(parent)
        with process.media_process_scope(None):
            with pytest.raises(process.CanceledError):
                process.raise_if_media_job_canceled()
    process.raise_if_media_job_canceled()
