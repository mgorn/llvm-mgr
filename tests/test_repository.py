from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from llvm_mgr.repository import LLVMRepository, fetch_release_tags


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)


class RepositoryTests(unittest.TestCase):
    def test_fetch_and_checkout_local_release_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            origin = base / "origin"
            origin.mkdir()
            git(origin, "init")
            git(origin, "config", "user.email", "test@example.com")
            git(origin, "config", "user.name", "Test")
            (origin / "README").write_text("test\n", encoding="utf-8")
            git(origin, "add", "README")
            git(origin, "commit", "-m", "initial")
            git(origin, "tag", "llvmorg-21.1.0")
            git(origin, "tag", "llvmorg-22.1.8")

            versions = fetch_release_tags(str(origin))
            self.assertEqual(versions[0].display, "22.1.8")

            checkout = base / "checkout"
            repository = LLVMRepository(checkout, str(origin))
            repository.checkout("llvmorg-21.1.0")
            result = subprocess.run(
                ["git", "describe", "--tags", "--exact-match"],
                cwd=checkout,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.stdout.strip(), "llvmorg-21.1.0")
