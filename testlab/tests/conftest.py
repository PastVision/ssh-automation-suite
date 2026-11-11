"""Conftest for the SSH Integration Test Lab tests.

Keeps lab tests independent of the backend suite: pytest is pointed at the
testlab directory explicitly, and PyYAML is treated as optional so the
Docker-free config tests run even without it.
"""

from __future__ import annotations

import sys
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = LAB_DIR / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
