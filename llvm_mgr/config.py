from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_manager_root(home: Path | None = None) -> Path:
    override = os.environ.get("LLVM_MANAGER_ROOT")
    if override:
        return Path(override).expanduser()

    user_home = (home or Path.home()).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "llvm-manager" if base else user_home / "AppData" / "Local" / "llvm-manager"
    if sys.platform == "darwin":
        return user_home / "Library" / "Application Support" / "llvm-manager"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    return Path(xdg_data_home).expanduser() / "llvm-manager" if xdg_data_home else user_home / ".local" / "share" / "llvm-manager"


@dataclass(frozen=True)
class ManagerPaths:
    root: Path
    home: Path

    @classmethod
    def create(cls, root: Path | None = None, home: Path | None = None) -> "ManagerPaths":
        resolved_home = (home or Path.home()).expanduser().resolve()
        resolved_root = (root or default_manager_root(resolved_home)).expanduser().resolve()
        return cls(root=resolved_root, home=resolved_home)

    @property
    def install_root(self) -> Path:
        return self.root / "install"

    @property
    def source_root(self) -> Path:
        return self.root / "source"

    @property
    def build_root(self) -> Path:
        return self.root / "build"

    @property
    def repository(self) -> Path:
        return self.source_root / "llvm-project"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def state_file(self) -> Path:
        return self.root / ".llvm-manager-state.json"

    @property
    def lock_file(self) -> Path:
        return self.root / ".llvm-manager.lock"

    @property
    def activation_sh(self) -> Path:
        return self.root / "activate.sh"

    @property
    def activation_fish(self) -> Path:
        return self.root / "activate.fish"

    @property
    def activation_ps1(self) -> Path:
        return self.root / "activate.ps1"

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def ensure_build_layout(self) -> None:
        self.ensure_root()
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.source_root.mkdir(parents=True, exist_ok=True)
        self.build_root.mkdir(parents=True, exist_ok=True)

    # Compatibility for callers that used the original broad initializer.
    def ensure(self) -> None:
        self.ensure_build_layout()
