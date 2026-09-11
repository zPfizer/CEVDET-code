"""vault_retrieval anlık görüntü/önbellek korkuluklarını sürer."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import vault_retrieval as vr


def _seed_vault(temporary: Path) -> Path:
    vault = temporary / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "🧠 500-Knowledge").mkdir()
    (vault / "🧠 500-Knowledge" / "Atlas Notu.md").write_text(
        "---\ntitle: Atlas Notu\ntags:\n  - proje\n---\n"
        "# Atlas Notu\nAtlas kararları burada anlatılır. Karar pazartesi alındı.\n",
        encoding="utf-8",
    )
    return vault


class TokenAndEntryEdges(unittest.TestCase):
    def test_url_terms_skips_unsplittable(self) -> None:
        terms = vr._url_terms("bkz http://[bozuk ve https://ornek.com/yol")
        self.assertTrue(any("ornek" in term for term in terms))

    def test_entry_from_text_memory_exclusions(self) -> None:
        memory = types.SimpleNamespace(
            excludes=lambda value: value == "Gizli Başlık",
            filter=lambda text: text,
        )
        text = "---\ntitle: Gizli Başlık\n---\n# Gizli Başlık\ngövde\n"
        self.assertIsNone(
            vr._entry_from_text(Path("not.md"), "🧠 500-Knowledge/not.md",
                                 text, memory=memory)
        )

    def test_stable_snapshot_race_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            note = vault / "🧠 500-Knowledge" / "Atlas Notu.md"

            klasor = vault / "🧠 500-Knowledge" / "klasor.md"
            klasor.mkdir()
            self.assertEqual(
                vr._stable_raw_hash(klasor), (None, None, True)
            )

            other = vault / "🧠 500-Knowledge" / "farkli-boyut.md"
            other.write_text("çok daha uzun bambaşka içerik " * 4, encoding="utf-8")
            churn = [note.lstat(), other.lstat()] * 4

            with mock.patch.object(Path, "lstat", side_effect=list(churn)):
                value, _stat, unstable = vr._stable_raw_hash(note)
            self.assertTrue(unstable)
            self.assertIsNone(value)

            with mock.patch.object(Path, "lstat", side_effect=FileNotFoundError):
                value, _stat, unstable = vr._stable_raw_hash(note)
            self.assertTrue(unstable)

            with mock.patch.object(Path, "lstat", side_effect=list(churn)):
                entry, _after, unstable = vr._stable_entry_snapshot(
                    vault, note, lambda _root, _path: None
                )
            self.assertTrue(unstable)
            self.assertIsNone(entry)

            missing = vault / "🧠 500-Knowledge" / "yok.md"
            result = vr._stable_source_snapshot(vault, missing, memory=None)
            self.assertTrue(result[-1] or result[0] is None)

            with mock.patch.object(
                vr, "_source_snapshot",
                side_effect=FileNotFoundError,
            ):
                result = vr._stable_source_snapshot(vault, note, memory=None)
            self.assertTrue(result[-1])

    def test_read_entry_snapshot_detects_churn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            note = vault / "🧠 500-Knowledge" / "Atlas Notu.md"
            with mock.patch.object(
                vr, "_source_sha256",
                side_effect=["a", "b"] * 3,
            ):
                with self.assertRaises(OSError):
                    vr._read_entry_snapshot(
                        vault, note, read_entry=lambda _v, _p: None
                    )


class CachePayloadGuards(unittest.TestCase):
    def _payload_template(self) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            vault_map = vr.build_vault_map(vault, write_cache=True)
            cache = json.loads(
                (vault / ".codex/scripts/.state/vault-retrieval-cache.json")
                .read_text(encoding="utf-8")
            )
        self.assertTrue(list(vault_map))
        entry_payload = next(iter(cache["files"].values()))["entry"]
        return dict(entry_payload)

    def test_entry_from_payload_rejects_mutations(self) -> None:
        base = self._payload_template()
        cases = [
            [1],
            {**base, "body_terms": ["liste"]},
            {**base, "historical_body_terms": 5},
            {**base, "safe_lines": [1]},
            {**base, "title": 7},
            {**base, "record_type": 9},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:40]):
                with self.assertRaises((TypeError, ValueError)):
                    vr._entry_from_payload(payload)

    def test_cache_load_and_save_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.json"
            path.write_text(json.dumps({"version": 0}), encoding="utf-8")
            self.assertEqual(vr._load_cache(path), {})
            with mock.patch.object(vr, "MAX_CACHE_BYTES", 10):
                self.assertFalse(
                    vr._save_cache(path, {"version": vr.CACHE_VERSION, "x": "y" * 50})
                )
            from collections import Counter
            with mock.patch.object(vr, "MAX_CACHE_BYTES", 10):
                payload, outcome, _hits, size = vr._bounded_cache_payload(
                    generation=0,
                    files={"a": {"x": "y" * 99}},
                    document_frequency=Counter({"terim": 3}),
                )
        self.assertIsNone(payload)
        self.assertEqual(outcome, "unavailable")
        self.assertGreater(size, 10)


class DateAndQueryEdges(unittest.TestCase):
    def test_date_value_and_bounds_reject_impossible(self) -> None:
        self.assertIsNone(vr._date_value(2026, 13, 1))
        self.assertIsNone(vr._date_bounds(2026, 13))
        bounds = vr._date_bounds(2026, 2)
        self.assertEqual(bounds[1], dt.date(2026, 2, 28))

    def test_query_classification_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            for query in (
                "geçen ay ne konuşmuştuk?",
                "2026-13-99 tarihinde ne oldu?",
                "dünden önce Atlas hakkında ne demiştim?",
                "Atlas kararı değişti mi?",
                "peki ya sonra?",
            ):
                with self.subTest(query=query):
                    result = vr.retrieve_vault_context_detailed(
                        vault, query, max_chars=2000,
                        write_cache=False, write_views=False,
                    )
                    self.assertIn(
                        result.outcome, {"ok", "empty", "skipped", "unavailable"}
                    )


class BuildMapGuards(unittest.TestCase):
    def test_unstable_sources_do_not_enter_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            with mock.patch.object(
                vr, "_stable_source_snapshot",
                return_value=(None, None, None, None, True),
            ):
                vault_map = vr.build_vault_map(vault, write_cache=False)
        self.assertEqual(list(vault_map), [])

    def test_corrupt_cached_entry_falls_back_to_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            vr.build_vault_map(vault, write_cache=True)
            cache_path = vault / ".codex/scripts/.state/vault-retrieval-cache.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            for entry in cache["files"].values():
                entry["entry"] = {"bozuk": True}
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            vault_map = vr.build_vault_map(vault, write_cache=False)
        self.assertTrue(list(vault_map))


if __name__ == "__main__":
    unittest.main()
