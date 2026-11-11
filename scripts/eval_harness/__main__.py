"""Allows ``python -m scripts.eval_harness``."""

from __future__ import annotations

import sys

from scripts.eval_harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
