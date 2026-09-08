from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR

import hook  # noqa: E402


REMOVED_TERMS = (
    "session access",
    "work packet",
    "write_scope",
    "pretool",
    "posttool",
    "claim permit",
    "mapped packet",
    "session binding",
)


class SessionAccessRemovalTests(unittest.TestCase):
    def test_only_memory_lifecycle_hooks_remain(self) -> None:
        hooks = json.loads((CODEX_DIR / "hooks.json").read_text(encoding="utf-8"))

        self.assertEqual(
            set(hooks["hooks"]),
            {"SessionStart", "UserPromptSubmit", "PreCompact", "SessionEnd", "Stop"},
        )

    def test_startup_and_prompt_keep_memory_without_technical_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex" / "scripts" / ".state"
            companion = vault / "🔮 850-Companion"
            projects = vault / "🏰 300-Projects"
            knowledge = vault / "knowledge"
            state.mkdir(parents=True)
            companion.mkdir()
            projects.mkdir()
            knowledge.mkdir()
            (companion / "Core.md").write_text("# Cevo\nDüşünme ortağı.", encoding="utf-8")
            (companion / "Profile.md").write_text("# Profil\nKısa konuş.", encoding="utf-8")
            (companion / "Last-Session.md").write_text(
                "# Son\n\n## Session: dün\nAtlas kararlı ilerledi.\n",
                encoding="utf-8",
            )
            (knowledge / "index.md").write_text("# Bilgi Tabanı", encoding="utf-8")
            (projects / "Atlas.md").write_text(
                "---\ntitle: Atlas\ntags: [atlas, proje]\n---\n"
                "Karar: yerel önbellek. Gerekçe: çevrimdışı kullanım.\n",
                encoding="utf-8",
            )

            startup = hook.build_session_context(vault, state)
            prompt = hook.handle_user_prompt(
                {
                    "session_id": "plain-memory",
                    "prompt": "Atlas yerel önbellek kararı ve gerekçe",
                },
                state,
                vault_root=vault,
                now=1234,
            )

        combined = f"{startup}\n{prompt}".casefold()
        self.assertIn("düşünme ortağı", combined)
        self.assertIn("atlas", combined)
        self.assertIn("çevrimdışı", combined)
        for term in REMOVED_TERMS:
            self.assertNotIn(term, combined)

    def test_removed_implementation_modules_are_absent(self) -> None:
        removed = (
            *(CODEX_DIR / "hooks").glob("session_access*.py"),
            *(CODEX_DIR / "scripts").glob("session_access*.py"),
        )
        self.assertEqual(removed, ())


if __name__ == "__main__":
    unittest.main()
