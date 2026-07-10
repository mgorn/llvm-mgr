#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=root, check=True)
subprocess.run([sys.executable, "tests/container_smoke_test.py"], cwd=root, check=True)
