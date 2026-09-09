"""Paylaşılan test path seam'i."""

from __future__ import annotations

from pathlib import Path
import sys


CODEX_DIR = Path(__file__).resolve().parents[1]
HOOKS_DIR = CODEX_DIR / "hooks"
SCRIPTS_DIR = CODEX_DIR / "scripts"

for _directory in (SCRIPTS_DIR, HOOKS_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))
