#!/usr/bin/env python3
"""Fail-open Antigravity CLI PreInvocation hook entrypoint for agy-smart-kit."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import pipeline

    pipeline.hook_entry()
except Exception:
    print("{}")
