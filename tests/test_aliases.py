from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from llvm_mgr.aliases import ensure_versioned_binaries, ensure_windows_mt_alias


class AliasTests(unittest.TestCase):
    def test_creates_versioned_executable_aliases_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            bin_dir.mkdir()
            clang = bin_dir / "clang"
            clang.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            clang.chmod(0o755)
            clangxx = bin_dir / "clang++"
            clangxx.symlink_to("clang")

            first = ensure_versioned_binaries(prefix, 22)
            second = ensure_versioned_binaries(prefix, 22)

            self.assertIn(bin_dir / "clang-22", first)
            self.assertTrue((bin_dir / "clang-22").exists())
            self.assertTrue((bin_dir / "clang++-22").exists())
            self.assertEqual(second, [])

    def test_creates_windows_mt_compatibility_alias_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            bin_dir.mkdir()
            llvm_mt = bin_dir / "llvm-mt.exe"
            llvm_mt.write_bytes(b"llvm-mt")

            first = ensure_windows_mt_alias(prefix, windows=True)
            second = ensure_windows_mt_alias(prefix, windows=True)

            self.assertEqual(first, [bin_dir / "mt.exe"])
            self.assertTrue((bin_dir / "mt.exe").is_file())
            self.assertEqual((bin_dir / "mt.exe").read_bytes(), llvm_mt.read_bytes())
            self.assertEqual(second, [])
