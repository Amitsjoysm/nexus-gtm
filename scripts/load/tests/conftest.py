"""Offline tests for the load-test runner: no network, no Docker, no staging.

    python -m pytest scripts/load/tests -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
