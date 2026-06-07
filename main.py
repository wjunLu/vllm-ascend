#!/usr/bin/env python
"""Thin wrapper — run ``python main.py`` from repo root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from cli import kickoff  # noqa: E402

if __name__ == "__main__":
    kickoff()
