"""Local-only secret storage for optional providers that are not local services.

Environment variables remain the documented, preferred way to supply an API
key, and an environment variable always wins over anything stored here.  A
loopback dashboard still needs a way to configure a key without opening a
shell, so this module provides one deliberately narrow fallback:

* one git-ignored file per secret under the application data directory,
* created with `0600`/`0700` on POSIX or a user-only ACL on Windows,
* never returned, echoed, or summarized by any read endpoint,
* never written into a project folder, a log, or a diagnostic report.

Values are only ever read at request time by the backend that owns them.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SECRET_NAME = re.compile(r"^[a-z0-9_]{1,64}$")
# Reject anything that could smuggle a header, a path, or a log line into a key.
_FORBIDDEN_CHARACTERS = re.compile(r"[\s\x00-\x1f\x7f]")


class SecretValidationError(ValueError):
    """The supplied value is unusable as a secret; nothing was written."""


def validate_api_key(value: object, *, max_length: int = 512) -> str:
    """Return a clean API key or raise without echoing the offending value."""

    if not isinstance(value, str):
        raise SecretValidationError("an API key must be a string")
    candidate = value.strip()
    if not candidate:
        raise SecretValidationError("an API key must not be empty")
    if len(candidate) > max_length:
        raise SecretValidationError(f"an API key must be at most {max_length} characters")
    if _FORBIDDEN_CHARACTERS.search(candidate):
        raise SecretValidationError(
            "an API key must not contain spaces, tabs, or newlines"
        )
    if len(candidate) < 8:
        raise SecretValidationError("an API key looks too short to be valid")
    return candidate


def _secure_path(path: Path, *, directory: bool) -> None:
    """Restrict a secret path to the current user on POSIX and Windows."""

    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    permission = "(OI)(CI)F" if directory else "F"
    try:
        completed = subprocess.run(
            [
                "icacls", str(path), "/inheritance:r", "/grant:r",
                f"{getpass.getuser()}:{permission}",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise OSError("could not apply a private Windows ACL to the secret store") from exc
    if completed.returncode != 0:
        raise OSError("could not apply a private Windows ACL to the secret store")


@dataclass(frozen=True, slots=True)
class LocalSecretStore:
    """Reads and writes user-private secret files inside one application-owned root."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser())

    def path_for(self, name: str) -> Path:
        if not _SECRET_NAME.fullmatch(name):
            raise SecretValidationError(
                "secret names may only contain lowercase letters, digits, and underscores"
            )
        return self.root / f"{name}.key"

    def read(self, name: str) -> str | None:
        """Return the stored value, or None when absent or unreadable.

        A permission problem is treated as "not configured" rather than raising:
        a broken secret file must never take the dashboard down or leak its
        contents through an error message.
        """

        path = self.path_for(name)
        try:
            if not path.is_file():
                return None
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        return value or None

    def write(self, name: str, value: str) -> None:
        clean = validate_api_key(value)
        path = self.path_for(name)
        self.root.mkdir(parents=True, exist_ok=True)
        _secure_path(self.root, directory=True)
        handle, temporary = tempfile.mkstemp(dir=str(self.root), prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                output.write(clean)
            _secure_path(Path(temporary), directory=False)
            os.replace(temporary, path)
        except Exception:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
            raise

    def delete(self, name: str) -> bool:
        path = self.path_for(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    def exists(self, name: str) -> bool:
        return self.read(name) is not None
