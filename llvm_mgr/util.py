from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence


class LLVMManagerError(RuntimeError):
    pass


def command_text(command: Sequence[str | os.PathLike[str]]) -> str:
    return shlex.join(str(part) for part in command)


def run(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {command_text(command)}", file=sys.stderr)
    try:
        return subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=capture,
            check=check,
            env=env,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if error.stderr else ""
        suffix = f": {detail}" if detail else ""
        raise LLVMManagerError(
            f"Command failed with exit code {error.returncode}: {command_text(command)}{suffix}"
        ) from error


def require_tools(names: Iterable[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        path = shutil.which(name)
        if path:
            resolved[name] = path
        else:
            missing.append(name)
    if missing:
        raise LLVMManagerError(f"Required tools not found on PATH: {', '.join(missing)}")
    return resolved


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def replace_managed_block(path: Path, block: str) -> None:
    begin = "# >>> llvm-manager >>>"
    end = "# <<< llvm-manager <<<"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    start = existing.find(begin)
    finish = existing.find(end, start + len(begin)) if start >= 0 else -1
    if start >= 0 and finish >= 0:
        finish += len(end)
        before = existing[:start].rstrip()
        after = existing[finish:].lstrip("\r\n")
        pieces = [piece for piece in (before, block.rstrip(), after.rstrip()) if piece]
        updated = "\n\n".join(pieces) + "\n"
    else:
        separator = "\n" if existing and not existing.endswith("\n") else ""
        updated = existing + separator
        if updated and not updated.endswith("\n\n"):
            updated += "\n"
        updated += block.rstrip() + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")


def quote_posix(path: Path) -> str:
    return shlex.quote(str(path))
