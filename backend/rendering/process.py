"""Owned media subprocesses with explicit, job-scoped cancellation."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

import psutil

_CancellationLineage = tuple[tuple[str, threading.Event], ...]
_TERMINATE_SECONDS = 0.5
_KILL_SECONDS = 2.0


@dataclass
class _ProcessState:
    proc: subprocess.Popen
    job_id: str | None
    token: threading.Event | None = None
    owns_group: bool = False
    reason: str | None = None
    stop_lock: threading.Lock = field(default_factory=threading.Lock)
    stopped: bool = False
    reaper_started: bool = False
    root: psutil.Process | None = None
    lifecycle_lock: threading.RLock = field(default_factory=threading.RLock)
    lineage: _CancellationLineage = ()


_active_processes: dict[int, subprocess.Popen] = {}
_process_jobs: dict[int, str] = {}
_process_states: dict[int, _ProcessState] = {}
_job_cancellations: dict[str, threading.Event] = {}
_active_lock = threading.RLock()
_current_job = threading.local()


class MediaProcessError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        executable = Path(argv[0]).name if argv else "media process"
        tail = stderr.strip()[-2000:]
        super().__init__(f"{executable} failed with exit code {returncode}: {tail}")


class CanceledError(MediaProcessError):
    """Explicit cancellation, regardless of the operating system's exit code."""


def _job_token(job_id: str | None) -> threading.Event | None:
    if job_id is None:
        return None
    with _active_lock:
        return _job_cancellations.setdefault(job_id, threading.Event())


@contextlib.contextmanager
def media_process_scope(job_id: str | None) -> Iterator[None]:
    """Attribute work to this attempt and all immutable enclosing attempts."""
    previous = getattr(_current_job, "job_id", None)
    previous_token = getattr(_current_job, "token", None)
    previous_lineage = getattr(_current_job, "lineage", ())
    lineage = _cancellation_lineage(job_id)
    _current_job.job_id = job_id
    _current_job.token = next((token for owner, token in reversed(lineage) if owner == job_id), None)
    _current_job.lineage = lineage
    try:
        yield
    finally:
        _current_job.job_id = previous
        _current_job.token = previous_token
        _current_job.lineage = previous_lineage


def reset_media_process_cancellation(job_id: str) -> None:
    """Begin an explicitly retried attempt. Existing runners retain their token.

    Call only when the job queue accepts an explicit retry, never on entry to
    an ordinary runner: cancellation before the first subprocess must survive.
    The caller must serialize its queue retry/reset and queue cancel/signal
    operations with the same control lock, so a fresh cancel cannot be cleared.
    """
    with _active_lock:
        _job_cancellations.pop(job_id, None)


def current_media_job_id() -> str | None:
    """Return the enclosing pipeline attribution, if this thread has one."""
    return getattr(_current_job, "job_id", None)


def _cancellation_lineage(job_id: str | None = None) -> _CancellationLineage:
    lineage = getattr(_current_job, "lineage", ())
    attribution = job_id if job_id is not None else current_media_job_id()
    if attribution is not None and not any(owner == attribution for owner, _ in lineage):
        token = _job_token(attribution)
        assert token is not None
        lineage = (*lineage, (attribution, token))
    return lineage


def _attribution(job_id: str | None) -> tuple[str | None, threading.Event | None]:
    attribution = job_id if job_id is not None else current_media_job_id()
    lineage = _cancellation_lineage(job_id)
    token = next((token for owner, token in reversed(lineage) if owner == attribution), None)
    return attribution, token


def raise_if_media_job_canceled(job_id: str | None = None) -> None:
    """Check cancellation between subprocesses or before publishing an artifact."""
    if any(token.is_set() for _, token in _cancellation_lineage(job_id)):
        raise CanceledError([], -1, "explicitly canceled")


@contextlib.contextmanager
def media_output_publication(job_id: str | None = None) -> Iterator[None]:
    """Serialize the final local rename with media cancellation intent.

    Keep this boundary short: stage and validate the artifact beforehand, then
    perform only the atomic publication inside the context.
    """
    with _active_lock:
        raise_if_media_job_canceled(job_id)
        yield


def _register_process(
    proc: subprocess.Popen, job_id: str | None, *, owns_group: bool = False,
    token: threading.Event | None = None,
    lineage: _CancellationLineage | None = None,
) -> int:
    with _active_lock:
        # Capture identity while the newly spawned child has not been reaped.
        # Never reconstruct root ownership from a bare PID during cancellation.
        try:
            root = psutil.Process(proc.pid)
        except psutil.Error:
            root = None
        _active_processes[proc.pid] = proc
        if job_id is not None:
            _process_jobs[proc.pid] = job_id
        state = _ProcessState(
            proc, job_id, token if token is not None else _job_token(job_id), owns_group,
        )
        state.root = root
        state.lineage = _cancellation_lineage(job_id) if lineage is None else lineage
        _process_states[proc.pid] = state
    return proc.pid


def _unregister_process(pid: int) -> None:
    with _active_lock:
        _active_processes.pop(pid, None)
        _process_jobs.pop(pid, None)
        _process_states.pop(pid, None)


def get_active_media_pids() -> list[int]:
    with _active_lock:
        return list(_active_processes)


def _stop_owned_process(state: _ProcessState) -> None:
    """Bounded terminate/kill/reap of this process and observed descendants.

    Reaping and signaling share one lock. POSIX group escalation occurs before
    reaping its leader, including when the leader exits during the grace period.
    This keeps its PID reserved and reaches new group members without risking a
    reused process group. Once a leader was already reaped, only captured process
    identities are safe targets. Detached or already-orphaned descendants on
    Windows require native job containment for a stronger guarantee.
    """
    with state.stop_lock:
        if state.stopped:
            return
        with state.lifecycle_lock:
            proc = state.proc
            descendants: list[psutil.Process] = []
            root_owned = False
            if proc.returncode is None and state.root is not None:
                with contextlib.suppress(psutil.Error):
                    root_owned = state.root.is_running()
                    if root_owned:
                        descendants = state.root.children(recursive=True)
            group_owned = root_owned and state.owns_group and os.name == "posix"
            if group_owned:
                # Do not call Popen.poll/terminate/wait until group escalation:
                # those methods may reap an exited leader and release its PID.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGTERM)
            elif proc.returncode is None and (state.root is None or root_owned):
                with contextlib.suppress(OSError):
                    proc.terminate()
            for child in reversed(descendants):
                with contextlib.suppress(psutil.Error):
                    child.terminate()
            if group_owned:
                # The runner cannot reap while this lock is held. Even a zombie
                # group leader therefore still reserves the group's identity.
                time.sleep(_TERMINATE_SECONDS)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            else:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_TERMINATE_SECONDS)
            for child in reversed(descendants):
                with contextlib.suppress(psutil.Error):
                    if child.is_running():
                        child.kill()
            if proc.returncode is None and (state.root is None or root_owned):
                with contextlib.suppress(OSError):
                    proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_KILL_SECONDS)
            if descendants:
                psutil.wait_procs(descendants, timeout=_KILL_SECONDS)
            state.stopped = True


def _request_stop(state: _ProcessState, reason: str) -> bool:
    with _active_lock:
        if _process_states.get(state.proc.pid) is not state or state.reason is not None:
            return False
        with state.lifecycle_lock:
            state.reason = reason
        return True


def _cancel_states(states: list[_ProcessState]) -> list[int]:
    # Record every intent before waiting for any process to stop.
    with _active_lock:
        targets = [state for state in states if _request_stop(state, "canceled")]
    for state in targets:
        _stop_owned_process(state)
    return [state.proc.pid for state in targets]


def cancel_all_media_processes() -> list[int]:
    """Cancel only registered media processes, recording intent before signaling."""
    with _active_lock:
        states = list(_process_states.values())
        for token in _job_cancellations.values():
            token.set()
        for state in states:
            for _, token in state.lineage:
                token.set()
        return_targets = [state for state in states if _request_stop(state, "canceled")]
    for state in return_targets:
        _stop_owned_process(state)
    return [state.proc.pid for state in return_targets]


def cancel_media_processes_for_job(job_id: str) -> list[int]:
    """Cancel a job, including the gaps before and between its subprocesses."""
    with _active_lock:
        token = _job_token(job_id)
        assert token is not None
        token.set()
        targets = [
            state for state in _process_states.values()
            if state.job_id == job_id or any(owner == job_id for owner, _ in state.lineage)
        ]
        for state in targets:
            for owner, ancestor_token in state.lineage:
                if owner == job_id:
                    ancestor_token.set()
        targets = [state for state in targets if _request_stop(state, "canceled")]
    for state in targets:
        _stop_owned_process(state)
    return [state.proc.pid for state in targets]


def cancel_media_process_by_pid(pid: int) -> bool:
    """Cancel an owned, registered process. Never act on an arbitrary PID."""
    with _active_lock:
        state = _process_states.get(pid)
    return bool(_cancel_states([state])) if state is not None else False


def _spawn(argv: list[str], job_id: str | None, **kwargs: object) -> _ProcessState:
    # Serialize the cancellation check, spawn and registration. A cancel cannot
    # land in a gap that leaves an untracked child running afterward.
    with _active_lock:
        attribution, token = _attribution(job_id)
        lineage = _cancellation_lineage(job_id)
        if any(ancestor_token.is_set() for _, ancestor_token in lineage):
            raise CanceledError(argv, -1, "explicitly canceled before process start")
        options: dict[str, object] = {}
        if os.name == "posix":
            options["start_new_session"] = True
        elif os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(argv, **kwargs, **options)
        _register_process(proc, attribution, owns_group=True, token=token, lineage=lineage)
        return _process_states[proc.pid]


def _finish(state: _ProcessState) -> str | None:
    with _active_lock:
        if state.reason is None:
            with state.lifecycle_lock:
                if state.proc.poll() is not None:
                    _unregister_process(state.proc.pid)
                else:
                    _start_reaper(state)
            return None
    _stop_owned_process(state)
    with _active_lock:
        with state.lifecycle_lock:
            if state.proc.poll() is not None:
                _unregister_process(state.proc.pid)
            else:
                _start_reaper(state)
        return state.reason


def _start_reaper(state: _ProcessState) -> None:
    """Retain ownership of an uninterruptible child until it actually exits."""
    if state.reaper_started:
        return
    state.reaper_started = True

    def reap() -> None:
        with state.lifecycle_lock:
            state.proc.wait()
        with _active_lock:
            if _process_states.get(state.proc.pid) is state:
                _unregister_process(state.proc.pid)

    threading.Thread(target=reap, name="media-reaper", daemon=True).start()


def _check_result(
    argv: Sequence[str], state: _ProcessState, reason: str | None,
    stderr: str, timeout: float | None,
) -> None:
    returncode = state.proc.returncode
    if reason == "canceled":
        raise CanceledError(argv, returncode if returncode is not None else -1, stderr)
    if reason == "timeout":
        raise MediaProcessError(argv, -1, f"timed out after {timeout} seconds")
    if returncode != 0:
        raise MediaProcessError(argv, returncode if returncode is not None else -1, stderr)


def _wait_for_process(state: _ProcessState, timeout: float | None = None) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    while state.reason is None:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            with state.lifecycle_lock:
                if state.reason is not None:
                    return
                state.proc.wait(timeout=0.1 if remaining is None else min(0.1, remaining))
            return
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                _request_stop(state, "timeout")


class OwnedMediaProcess:
    """A tracked process handle whose polling respects cancellation ownership."""

    def __init__(self, state: _ProcessState) -> None:
        self._state = state

    @property
    def pid(self) -> int:
        return self._state.proc.pid

    @property
    def returncode(self) -> int | None:
        return self._state.proc.returncode

    def poll(self) -> int | None:
        with self._state.lifecycle_lock:
            # Once cancellation wins, cleanup must keep the leader unreaped
            # until group signaling ends. Polling must not release that PID.
            if self._state.reason is not None:
                return self._state.proc.returncode
            return self._state.proc.poll()


@contextlib.contextmanager
def owned_media_process(
    argv: Sequence[str], *, job_id: str | None = None, **popen_options: object,
) -> Iterator[OwnedMediaProcess]:
    """Track a long-lived local child, terminating its owned tree on scope exit.

    Useful for a browser controlled through a separate protocol. Use the yielded
    handle's poll() instead of raw Popen operations. Normal scope cleanup is not
    cancellation; an explicit cancel raises CanceledError. Process creation is
    shell-free, and the caller cannot override ownership/session isolation.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    prohibited = {"shell", "start_new_session", "creationflags", "preexec_fn", "process_group"}
    if prohibited.intersection(popen_options):
        raise ValueError("owned media process options cannot override shell or process ownership")
    command = [str(part) for part in argv]
    try:
        state = _spawn(command, job_id, **popen_options)
    except OSError as exc:
        raise MediaProcessError(argv, -1, str(exc)) from exc
    failure: BaseException | None = None
    try:
        yield OwnedMediaProcess(state)
    except BaseException as exc:
        failure = exc
    finally:
        _request_stop(state, "closed" if failure is None else "failed")
        _stop_owned_process(state)
        reason = _finish(state)
    if reason == "canceled":
        raise CanceledError(argv, state.proc.returncode if state.proc.returncode is not None else -1,
                            "explicitly canceled")
    if failure is not None:
        raise failure


def run_media_process(
    argv: Sequence[str], *, timeout: float | None = None,
    capture_stdout: bool = False, job_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run trusted argv without a shell. Only explicit intent means canceled."""
    if not argv:
        raise ValueError("argv must not be empty")
    command = [str(part) for part in argv]
    try:
        # Files avoid deadlock and inherited-pipe hangs after the child exits.
        with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as output:
            state = _spawn(
                command, job_id, stdin=subprocess.DEVNULL, stderr=errors,
                stdout=output if capture_stdout else subprocess.DEVNULL,
            )
            try:
                try:
                    _wait_for_process(state, timeout)
                except BaseException:
                    _request_stop(state, "failed")
                    _stop_owned_process(state)
                    raise
            finally:
                reason = _finish(state)
            errors.seek(0)
            stderr = errors.read().decode("utf-8", errors="replace")
            _check_result(argv, state, reason, stderr, timeout)
            output.seek(0)
            stdout = output.read().decode("utf-8", errors="replace") if capture_stdout else ""
            return subprocess.CompletedProcess(command, 0, stdout, stderr)
    except OSError as exc:
        raise MediaProcessError(argv, -1, str(exc)) from exc


def run_media_process_stream(
    argv: Sequence[str], chunks: Iterable[bytes], *, timeout: float | None = None,
    job_id: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Feed lazy binary chunks, bounding blocked pipe writes with a watchdog.

    A producer's own Python computation cannot be forcibly interrupted; producers
    must yield or check cancellation cooperatively. The child still stops on time.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    command = [str(part) for part in argv]
    iterator = iter(chunks)
    done = threading.Event()
    watcher: threading.Thread | None = None
    try:
        with tempfile.TemporaryFile() as errors:
            state = _spawn(
                command, job_id, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=errors, bufsize=0,
            )

            def expire() -> None:
                if not done.wait(timeout) and _request_stop(state, "timeout"):
                    _stop_owned_process(state)

            if timeout is not None:
                watcher = threading.Thread(target=expire, name="media-timeout", daemon=True)
                watcher.start()
            failure: BaseException | None = None
            try:
                assert state.proc.stdin is not None
                for chunk in iterator:
                    if state.reason is not None:
                        break
                    view = memoryview(chunk)
                    while view and state.reason is None:
                        try:
                            written = state.proc.stdin.write(view)
                        except BrokenPipeError:
                            break
                        if not written:
                            raise OSError("media process stdin write made no progress")
                        view = view[written:]
                    if view or OwnedMediaProcess(state).poll() is not None:
                        break
                with contextlib.suppress(BrokenPipeError):
                    state.proc.stdin.close()
                _wait_for_process(state)
            except BaseException as exc:
                failure = exc
                _request_stop(state, "failed")
                _stop_owned_process(state)
            finally:
                done.set()
                reason = _finish(state)
                if watcher is not None:
                    watcher.join(timeout=_TERMINATE_SECONDS + 2 * _KILL_SECONDS + 0.5)
                if state.proc.stdin is not None:
                    with contextlib.suppress(OSError):
                        state.proc.stdin.close()
            errors.seek(0)
            stderr = errors.read()
            if failure is not None and reason not in {"canceled", "timeout"}:
                raise failure
            _check_result(argv, state, reason, stderr.decode("utf-8", errors="replace"), timeout)
            return subprocess.CompletedProcess(command, 0, b"", stderr)
    except OSError as exc:
        raise MediaProcessError(argv, -1, str(exc)) from exc
    finally:
        close_iterator = getattr(iterator, "close", None)
        if callable(close_iterator):
            close_iterator()
