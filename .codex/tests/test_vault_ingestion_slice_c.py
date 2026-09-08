from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import unittest


CODEX_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = CODEX_DIR / "scripts"
VAULT_ROOT = CODEX_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import tag_taxonomy  # noqa: E402


TANSU_SCOPE = "🏰 300-Projects/Tansu X Veri Havuzu"
OLD_REBALANCING = "tansu/portföy/rebalancing"
REBALANCING = "tansu/portföy/yeniden-dengeleme"
UNUSED = {
    "tansu/istatistik/göreli-değer-portföyü",
    "tansu/istatistik/pair-keşfi",
    "tansu/kanıt/kaynak-çıkarma",
    "tansu/risk/kill-switch",
}


class VaultIngestionSliceCTests(unittest.TestCase):
    def test_scoped_alias_target_is_valid_only_inside_its_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "taxonomy.json"
            path.write_text(
                json.dumps(
                    {
                        "canonical": ["vault"],
                        "scoped": {TANSU_SCOPE: [REBALANCING]},
                        "aliases": {
                            "governance": "vault",
                            OLD_REBALANCING: REBALANCING,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            taxonomy = tag_taxonomy.load_taxonomy(path)
            source = f"---\ntags: [{OLD_REBALANCING}]\n---\n# Note\n"

            inside, inside_unknown = tag_taxonomy.normalize_markdown(
                source,
                taxonomy,
                relative_path=PurePosixPath(TANSU_SCOPE) / "note.md",
            )
            outside, outside_unknown = tag_taxonomy.normalize_markdown(
                source,
                taxonomy,
                relative_path=PurePosixPath("🛠️ 600-Arsenal/note.md"),
            )

        self.assertIn(f"tags: [{REBALANCING}]", inside)
        self.assertEqual(inside_unknown, [])
        self.assertEqual(outside, source)
        self.assertEqual(outside_unknown, [OLD_REBALANCING])

if __name__ == "__main__":
    unittest.main(verbosity=2)
