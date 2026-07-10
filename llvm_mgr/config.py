from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManagerPaths:
    root: Path
    home: Path

    @classmethod
    def create(cls, root: Path, home: Path | None = None) -> "ManagerPaths":
        return cls(root=root.expanduser().resolve(), home=(home or Path.home()).expanduser().resolve())

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
    def scan_cache(self) -> Path:
        return self.root / ".llvm-manager-installs.json"

    @property
    def activation_sh(self) -> Path:
        return self.root / "activate.sh"

    @property
    def activation_fish(self) -> Path:
        return self.root / "activate.fish"

    @property
    def activation_ps1(self) -> Path:
        return self.root / "activate.ps1"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.source_root.mkdir(parents=True, exist_ok=True)
        self.build_root.mkdir(parents=True, exist_ok=True)
