from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llvm_mgr.config import ManagerPaths
from llvm_mgr.models import InstallInfo
from llvm_mgr.switcher import switch_install
from llvm_mgr.versioning import LLVMVersion


class SwitcherTests(unittest.TestCase):
    def test_switch_updates_current_and_profile_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "manager"
            home = base / "home"
            prefix = root / "install" / "llvmorg-22.1.8"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            clang = bin_dir / "clang"
            clang.write_text("#!/bin/sh\n", encoding="utf-8")
            clang.chmod(0o755)
            install = InstallInfo(prefix, clang, LLVMVersion(22, 1, 8), True, tag="llvmorg-22.1.8")
            paths = ManagerPaths.create(root, home)

            profile = switch_install(paths, install, shell="/bin/bash")
            switch_install(paths, install, shell="/bin/bash")

            self.assertEqual(paths.current.resolve(), prefix.resolve())
            self.assertEqual(profile, home / ".bashrc")
            text = profile.read_text(encoding="utf-8")
            self.assertEqual(text.count("# >>> llvm-manager >>>"), 1)
            self.assertIn(str(paths.activation_sh), text)
            self.assertIn("LLVM_HOME", paths.activation_sh.read_text(encoding="utf-8"))
