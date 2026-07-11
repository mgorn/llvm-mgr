from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


class LLVMManagerError(RuntimeError):
    pass


_COMMAND_ECHO = True
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

_VERSION_NUMBER_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){0,3})(?!\d)")


def set_command_echo(enabled: bool) -> None:
    global _COMMAND_ECHO
    _COMMAND_ECHO = enabled


def _redact_argument(value: str) -> str:
    if not _URL_RE.match(value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.username is None and parsed.password is None:
        return value
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"***@{hostname}", parsed.path, parsed.query, parsed.fragment))


def extract_numeric_version(text: str) -> str | None:
    match = _VERSION_NUMBER_RE.search(text)
    return match.group(1) if match else None


def command_text(command: Sequence[str | os.PathLike[str]]) -> str:
    parts = [_redact_argument(str(part)) for part in command]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def probe_process(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def probe_text(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 10,
    require_success: bool = True,
) -> str:
    completed = probe_process(command, cwd=cwd, env=env, timeout=timeout)
    if completed is None or (require_success and completed.returncode != 0):
        return ""
    return "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()


def run(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    verbose: bool | None = None,
) -> subprocess.CompletedProcess[str]:
    if _COMMAND_ECHO if verbose is None else verbose:
        print(f"+ {command_text(command)}", file=sys.stderr)
    try:
        return subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=capture,
            check=check,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as error:
        raise LLVMManagerError(f"Command was not found: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if error.stderr else ""
        suffix = f": {detail}" if detail else ""
        raise LLVMManagerError(
            f"Command failed with exit code {error.returncode}: {command_text(command)}{suffix}"
        ) from error


def require_tools(names: Iterable[str], *, search_path: str | None = None) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        path = shutil_which(name, search_path)
        if path:
            resolved[name] = path
        else:
            missing.append(name)
    if missing:
        raise LLVMManagerError(f"Required tools not found on PATH: {', '.join(missing)}")
    return resolved


def shutil_which(name: str, search_path: str | None = None) -> str | None:
    import shutil

    return shutil.which(name, path=search_path)


def read_json(
    path: Path,
    default: object,
    *,
    strict: bool = False,
    warn: bool = False,
) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        if strict:
            raise LLVMManagerError(f"Could not read JSON file {path}: {error}") from error
        if warn:
            warnings.warn(f"Ignoring unreadable JSON file {path}: {error}", RuntimeWarning, stacklevel=2)
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def replace_managed_block(path: Path, block: str) -> None:
    begin = "# >>> llvm-manager >>>"
    end = "# <<< llvm-manager <<<"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # Remove every complete managed block. If a prior write was interrupted and
    # left an unmatched begin marker, discard the incomplete tail before adding
    # one canonical block.
    complete = re.compile(
        rf"(?:^|\n)[ \t]*{re.escape(begin)}.*?{re.escape(end)}[ \t]*(?=\n|$)",
        re.DOTALL,
    )
    cleaned = complete.sub("\n", existing)
    unmatched = cleaned.find(begin)
    if unmatched >= 0:
        cleaned = cleaned[:unmatched]
    cleaned = cleaned.strip("\r\n")
    pieces = [piece for piece in (cleaned, block.rstrip()) if piece]
    atomic_write_text(path, "\n\n".join(pieces) + "\n")


def quote_posix(path: Path) -> str:
    return shlex.quote(str(path))


def quote_fish(path: Path) -> str:
    value = str(path).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{value}'"


@contextmanager
def file_lock(path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            while True:
                try:
                    stream.seek(0)
                    if stream.tell() == 0:
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LLVMManagerError(f"Timed out waiting for manager lock: {path}")
                    time.sleep(0.1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LLVMManagerError(f"Timed out waiting for manager lock: {path}")
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()
