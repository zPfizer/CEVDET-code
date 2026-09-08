from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


VAULT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VAULT / ".codex" / "scripts"))

import tag_taxonomy  # noqa: E402
import tansu_semantic_metadata  # noqa: E402


TANSU = VAULT / "🏰 300-Projects" / "Tansu X Veri Havuzu"
SEMANTIC_MAP = TANSU / "tansu-semantik-kullanim-haritasi.md"
TAXONOMY = VAULT / ".codex" / "tag-taxonomy.json"
SEMANTIC_SCHEMA = VAULT / ".codex" / "tansu-semantic-schema.json"

FOCUS_TAG_BY_HEADING = {
    "Sinyal ve Strateji": "sinyal-stratejisi",
    "Backtest ve Doğrulama": "backtest-doğrulama",
    "Veri ve Kanıt": "veri-kanıtı",
    "Piyasa ve Rejim": "piyasa-rejimi",
    "Temel Analiz ve Değerleme": "temel-analiz-değerleme",
    "Risk ve Pozisyon": "risk-pozisyon",
    "Portföy ve Tahsis": "portföy-tahsisi",
    "İcra ve Likidite": "icra-likidite",
    "Outcome ve Öğrenme": "sonuç-öğrenme",
    "Raporlama ve Karar Yüzeyi": "raporlama-karar-yüzeyi",
}

MAP_ENTRY = re.compile(
    r"^- `\d+` · \[\[Tansu X Veri Havuzu/([^|\]]+)\|"
)


def expected_focus_tags() -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {}
    current_tag: str | None = None
    for line in SEMANTIC_MAP.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            current_tag = FOCUS_TAG_BY_HEADING.get(line.removeprefix("### ").strip())
            continue
        match = MAP_ENTRY.match(line)
        if current_tag is not None and match is not None:
            expected.setdefault(match.group(1), set()).add(current_tag)
    return expected


def inline_tags(path: Path) -> set[str]:
    for line in path.read_text(encoding="utf-8").splitlines()[:80]:
        if line.startswith("tags:"):
            raw = line.split(":", 1)[1].strip().strip("[]")
            return {tag.strip() for tag in raw.split(",") if tag.strip()}
    return set()


class TansuSemanticTagTests(unittest.TestCase):
    def test_semantic_tags_are_project_scoped(self) -> None:
        taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
        schema = json.loads(SEMANTIC_SCHEMA.read_text(encoding="utf-8"))
        canonical = set(taxonomy["canonical"])
        scoped = set(
            taxonomy["scoped"]["🏰 300-Projects/Tansu X Veri Havuzu"]
        )
        expected = (
            set(schema["navigation_tags"])
            | set(schema["module_tags"].values())
            | {rule["tag"] for rule in schema["topic_rules"]}
        )

        self.assertEqual(scoped, expected)
        self.assertEqual(scoped & canonical, set())
        self.assertNotIn("ajanlar", canonical)
        self.assertIn("kod-inceleme", canonical)
        self.assertNotEqual(taxonomy["aliases"].get("code-review"), "sürüm-kontrolü")

    def test_scoped_tags_are_allowed_only_below_their_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(
                json.dumps(
                    {
                        "canonical": ["finans"],
                        "scoped": {"Tansu": ["sinyal-stratejisi"]},
                        "aliases": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tansu = root / "Tansu"
            other = root / "Other"
            tansu.mkdir()
            other.mkdir()
            note = "---\ntags: [finans, sinyal-stratejisi]\n---\n# Note\n"
            (tansu / "allowed.md").write_text(note, encoding="utf-8")
            (other / "blocked.md").write_text(note, encoding="utf-8")

            taxonomy = tag_taxonomy.load_taxonomy(taxonomy_path)
            violations = tag_taxonomy.audit_vault(root, taxonomy)

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].path.name, "blocked.md")
        self.assertEqual(violations[0].tag, "sinyal-stratejisi")

    def test_project_scoped_tags_can_use_obsidian_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(
                json.dumps(
                    {
                        "canonical": ["finans"],
                        "scoped": {"Tansu": ["tansu/teknik/formasyon"]},
                        "aliases": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tansu = root / "Tansu"
            tansu.mkdir()
            (tansu / "note.md").write_text(
                "---\ntags: [tansu/teknik/formasyon]\n---\n# Note\n",
                encoding="utf-8",
            )

            taxonomy = tag_taxonomy.load_taxonomy(taxonomy_path)
            violations = tag_taxonomy.audit_vault(root, taxonomy)

        self.assertEqual(violations, [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
