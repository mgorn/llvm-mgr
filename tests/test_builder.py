from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llvm_mgr.builder import _llvm_major
from llvm_mgr.util import LLVMManagerError


class BuilderVersionTests(unittest.TestCase):
    def test_reads_modern_monorepo_version_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            llvm = root / "llvm"
            llvm.mkdir()
            module = root / "cmake" / "Modules" / "LLVMVersion.cmake"
            module.parent.mkdir(parents=True)
            module.write_text(
                "if(NOT DEFINED LLVM_VERSION_MAJOR)\n"
                "  set(LLVM_VERSION_MAJOR 23)\n"
                "endif()\n",
                encoding="utf-8",
            )
            self.assertEqual(_llvm_major(llvm), 23)

    def test_keeps_cmakelists_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            llvm = Path(temporary) / "llvm"
            llvm.mkdir()
            (llvm / "CMakeLists.txt").write_text(
                "set(LLVM_VERSION_MAJOR 22)\n",
                encoding="utf-8",
            )
            self.assertEqual(_llvm_major(llvm), 22)

    def test_error_lists_searched_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            llvm = Path(temporary) / "llvm"
            llvm.mkdir()
            with self.assertRaisesRegex(LLVMManagerError, "LLVMVersion\\.cmake"):
                _llvm_major(llvm)


if __name__ == "__main__":
    unittest.main()
