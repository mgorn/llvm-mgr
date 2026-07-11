#!/usr/bin/env python3
from pathlib import Path

from llvm_mgr.cli import main


if __name__ == "__main__":
    raise SystemExit(main(default_root=Path(__file__).resolve().parent))
