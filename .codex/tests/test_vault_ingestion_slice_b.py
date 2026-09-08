from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import sys


CODEX_DIR = Path(__file__).resolve().parents[1]
HOOKS_DIR = CODEX_DIR / "hooks"
SCRIPTS_DIR = CODEX_DIR / "scripts"
VAULT_ROOT = CODEX_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import hook  # noqa: E402
import intake_contract  # noqa: E402
from knowledge_schema import parse_frontmatter  # noqa: E402


class VaultIngestionSliceBTests(unittest.TestCase):
    def test_context_reader_omits_companion_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Companion.md"
            path.write_text(
                "---\ntype: companion-state\nstatus: active\n---\n# Body\nLine\n",
                encoding="utf-8",
            )

            emitted = hook._read_limited(path, 1)

        self.assertEqual(emitted, "# Body")

if __name__ == "__main__":
    unittest.main(verbosity=2)
