#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    project = Path(__file__).resolve().parent
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=project, check=True)
    subprocess.run([sys.executable, "tests/container_smoke_test.py"], cwd=project, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
