"""Subprocess execution shared by all rendering components."""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Iterator, Sequence

# Module-level registry of active media subprocesses for cancellation.
# Each tracked PID may be attributed to a pipeline job id (job-level scoping)
# so canceling one job never kills an unrelated job's media processes.
_active_processes: dict[int, subprocess.Popen] = {}
_process_jobs: dict[int, str] = {}
_active_lock = threading.Lock()

# Thread-local job attribution: job runners wrap their work in
# media_process_scope(job_id) so every media subprocess started on that
# thread (directly or deep inside renderer helpers) is tracked under the job.
_current_job = threading.local()


@contextlib.contextmanager
def media_process_scope(job_id: str | None) -> Iterator[None]:
    """Attribute media processes started on this thread to ``job_id``."""
    previous = getattr(_current_job, "job_id", None)
    _current_job.job_id = job_id
    try:
        yield
    finally:
        _current_job.job_id = previous


class MediaProcessError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        executable = Path(argv[0]).name if argv else "media process"
        tail = stderr.strip()[-2000:]
        super().__init__(f"{executable} failed with exit code {returncode}: {tail}")


class CanceledError(MediaProcessError):
    """Raised when a media process was explicitly canceled."""


def cancel_all_media_processes() -> list[int]:
    """Kill every tracked media subprocess and return the PIDs that were killed."""
    killed: list[int] = []
    with _active_lock:
        processes = list(_active_processes.items())
        for pid, proc in processes:
            try:
                proc.kill()
                killed.append(pid)
            except Exception:
                pass
        # Do not remove from registry yet; removal happens on process exit.
    return killed


def cancel_media_processes_for_job(job_id: str) -> list[int]:
    """Kill tracked media subprocesses attributed to one job; return killed PIDs.

    Unlike cancel_all_media_processes(), this never touches processes owned by
    other jobs, so canceling a TTS job cannot kill an in-flight render.
    """
    killed: list[int] = []
    with _active_lock:
        targets = [
            (pid, proc) for pid, proc in _active_processes.items()
            if _process_jobs.get(pid) == job_id
        ]
        for pid, proc in targets:
            try:
                proc.kill()
                killed.append(pid)
            except Exception:
                pass
        # Do not remove from registry yet; removal happens on process exit.
    return killed


def cancel_media_process_by_pid(pid: int) -> bool:
    """Kill a specific tracked process by PID. Returns True if it was found."""
    with _active_lock:
        proc = _active_processes.get(pid)
    if proc is None:
        return False
    try:
        proc.kill()
        return True
    except Exception:
        return False


def _register_process(proc: subprocess.Popen, job_id: str | None) -> int:
    with _active_lock:
        pid = proc.pid
        _active_processes[pid] = proc
        if job_id is not None:
            _process_jobs[pid] = job_id
    return pid


def _unregister_process(pid: int) -> None:
    with _active_lock:
        _active_processes.pop(pid, None)
        _process_jobs.pop(pid, None)


def get_active_media_pids() -> list[int]:
    with _active_lock:
        return list(_active_processes.keys())


def run_media_process(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    capture_stdout: bool = False,
    job_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run trusted argv without a shell and raise a structured error on failure."""

    if not argv:
        raise ValueError("argv must not be empty")
    cmd_strs = [str(part) for part in argv]
    try:
        proc = subprocess.Popen(
            cmd_strs,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Explicit job_id wins; otherwise inherit the thread's media scope.
        attribution = job_id if job_id is not None else getattr(_current_job, "job_id", None)
        pid = _register_process(proc, attribution)
        try:
            try:
                stdout_data, stderr_data = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    # SIGKILL did not reap the child yet (e.g. uninterruptible
                    # I/O). It stays registered so cancellation helpers can
                    # still target the lingering PID; this residual-linger case
                    # is resolved by a later successful reap or process exit.
                    pass
                raise MediaProcessError(argv, -1, f"timed out after {timeout} seconds") from None
        finally:
            # Unregister only once the child's exit status is confirmed reaped,
            # including when communicate() fails unexpectedly, so a stale PID
            # can never be mistaken for a reused one.
            if proc.poll() is not None:
                _unregister_process(pid)

        # If the process was killed (e.g. by cancel), treat it as canceled rather than failed.
        returncode = proc.returncode
        stderr_text = stderr_data or ""
        stdout_text = stdout_data or ""

        if returncode is not None and returncode < 0:
            # Negative returncode indicates signal termination (e.g. SIGKILL from cancel).
            raise CanceledError(argv, returncode, stderr_text)

        # For non-zero positive return codes, it's a real failure.
        if returncode is not None and returncode != 0:
            raise MediaProcessError(argv, returncode, stderr_text)

        # Reconstruct CompletedProcess-like result for compatibility.
        result = subprocess.CompletedProcess(
            args=cmd_strs,
            returncode=returncode if returncode is not None else 0,
            stdout=stdout_text,
            stderr=stderr_text,
        )
        return result
    except (MediaProcessError, CanceledError):
        raise
    except OSError as exc:
        raise MediaProcessError(argv, -1, str(exc)) from exc


def run_media_process_stream(
    argv: Sequence[str],
    chunks: Iterable[bytes],
    *,
    timeout: float | None = None,
    job_id: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a tracked media process while incrementally feeding binary stdin.

    Keeping the producer lazy avoids materializing a complete frame sequence in
    memory or on disk. Stderr goes to a temporary file so FFmpeg can never
    deadlock on a full stderr pipe while the caller is still producing frames.
    """

    if not argv:
        raise ValueError("argv must not be empty")
    cmd_strs = [str(part) for part in argv]
    iterator = iter(chunks)
    started = time.monotonic()
    try:
        with tempfile.TemporaryFile() as stderr_file:
            proc = subprocess.Popen(
                cmd_strs,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )
            attribution = job_id if job_id is not None else getattr(_current_job, "job_id", None)
            pid = _register_process(proc, attribution)
            try:
                try:
                    assert proc.stdin is not None
                    for chunk in iterator:
                        if timeout is not None and time.monotonic() - started >= timeout:
                            raise subprocess.TimeoutExpired(cmd=cmd_strs, timeout=timeout)
                        try:
                            proc.stdin.write(chunk)
                        except BrokenPipeError:
                            break
                    try:
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
                    remaining = (
                        None if timeout is None
                        else max(0.0, timeout - (time.monotonic() - started))
                    )
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise MediaProcessError(argv, -1, f"timed out after {timeout} seconds") from None
                except BaseException:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise
            finally:
                close_iterator = getattr(iterator, "close", None)
                if callable(close_iterator):
                    close_iterator()
                if proc.poll() is not None:
                    _unregister_process(pid)

            stderr_file.seek(0)
            stderr_text = stderr_file.read().decode("utf-8", errors="replace")
            returncode = proc.returncode if proc.returncode is not None else 0
            if returncode < 0:
                raise CanceledError(argv, returncode, stderr_text)
            if returncode != 0:
                raise MediaProcessError(argv, returncode, stderr_text)
            return subprocess.CompletedProcess(
                args=cmd_strs, returncode=returncode, stdout=b"", stderr=stderr_text.encode(),
            )
    except (MediaProcessError, CanceledError):
        raise
    except OSError as exc:
        raise MediaProcessError(argv, -1, str(exc)) from exc
