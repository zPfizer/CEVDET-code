"""vault_retrieval — dilim 2: anlık görüntü, önbellek ve sorgu sınıflama kolları."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import stat as stat_module
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import vault_retrieval as vr
from memory_ledger import MemoryRead


def _seed_vault(temporary: Path) -> Path:
    vault = temporary / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "🧠 500-Knowledge").mkdir()
    (vault / "🧠 500-Knowledge" / "Atlas Notu.md").write_text(
        "---\ntitle: Atlas Notu\ntags:\n  - proje\n---\n"
        "# Atlas Notu\nAtlas kararları burada anlatılır.\n",
        encoding="utf-8",
    )
    return vault


def _lstat_script(target: Path, results):
    original = Path.lstat
    queue = list(results)

    def fake(self, *args, **kwargs):
        if self == target:
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, type) and issubclass(item, Exception):
                raise item()
            return item
        return original(self, *args, **kwargs)

    return mock.patch.object(Path, "lstat", fake)


def _payload_template() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        vault = _seed_vault(Path(temporary))
        vr.build_vault_map(vault, write_cache=True)
        cache = json.loads(
            (vault / ".codex/scripts/.state/vault-retrieval-cache.json")
            .read_text(encoding="utf-8")
        )
    return dict(next(iter(cache["files"].values()))["entry"])


class SnapshotArms(unittest.TestCase):
    def test_entry_text_heading_exclusion(self) -> None:
        memory = types.SimpleNamespace(
            excludes=lambda value: value == "Gizli",
            filter=lambda text: text,
        )
        self.assertIsNone(
            vr._entry_from_text(Path("not.md"), "🧠 500-Knowledge/not.md",
                                "# Gizli\ngövde\n", memory=memory)
        )

    def test_companion_sources_are_invisible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / "🔮 850-Companion" / "Sources" / "x.md"
            source.parent.mkdir(parents=True)
            source.write_text("gizli", encoding="utf-8")
            self.assertEqual(
                vr._source_snapshot(vault, source), (None, None, None)
            )

    def test_virtual_view_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(
                vr._is_virtual_companion_view(
                    Path(temporary) / "vault", Path(temporary) / "dis.md"
                )
            )

    def test_stable_entry_snapshot_rejects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            klasor = Path(temporary) / "klasor.md"
            klasor.mkdir()
            self.assertEqual(
                vr._stable_entry_snapshot(
                    Path(temporary), klasor, lambda _r, _p: None
                ),
                (None, None, True),
            )

    def test_stable_source_snapshot_race_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            note = vault / "🧠 500-Knowledge" / "Atlas Notu.md"
            other = vault / "🧠 500-Knowledge" / "farkli.md"
            other.write_text("bambaşka içerik " * 20, encoding="utf-8")
            klasor = vault / "🧠 500-Knowledge" / "klasor.md"
            klasor.mkdir()

            result = vr._stable_source_snapshot(vault, klasor)
            self.assertTrue(result[-1])

            reg = os.lstat(note)
            reg_b = os.lstat(other)
            dir_stat = os.lstat(klasor)
            sabit = (PurePosixPath("x"), "metin", "a" * 64)
            with mock.patch.object(vr, "_source_snapshot",
                                   return_value=sabit):
                cases = [
                    [reg, FileNotFoundError],
                    [FileNotFoundError, reg],
                    [reg, dir_stat],
                    [reg, reg_b],
                ]
                for script in cases:
                    with self.subTest(script=str(script)[:40]):
                        with _lstat_script(note, script * 3):
                            result = vr._stable_source_snapshot(vault, note)
                        self.assertTrue(result[-1])

    def test_stable_note_snapshot_missing_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            klasor = vault / "klasor.md"
            klasor.mkdir()
            result = vr._stable_note_snapshot(vault, klasor,
                                              lambda _r, _p: None)
            self.assertTrue(result[-1])
            result = vr._stable_note_snapshot(vault, vault / "yok.md",
                                              lambda _r, _p: None)
            self.assertTrue(result[-1])

    def test_read_entry_snapshot_stable_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            note = Path(temporary) / "not.md"
            note.write_text("sabit", encoding="utf-8")
            entry, digest = vr._read_entry_snapshot(
                Path(temporary), note, lambda _r, _p: None
            )
        self.assertIsNone(entry)
        self.assertEqual(len(digest), 64)


class CachePayloadArms(unittest.TestCase):
    def test_payload_line_and_term_shape_guards(self) -> None:
        base = _payload_template()
        for payload in (
            {**base, "historical_lines": [1]},
            {**base, "title_terms": [1]},
        ):
            with self.subTest(payload=str(payload)[:40]):
                with self.assertRaises(ValueError):
                    vr._entry_from_payload(payload)


class BuildMapArms(unittest.TestCase):
    def test_paths_outside_vault_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            dis = Path(temporary) / "dis.md"
            dis.write_text("dışarıda", encoding="utf-8")
            with mock.patch.object(
                vr, "markdown_paths", return_value=[dis]
            ):
                vault_map = vr.build_vault_map(vault, write_cache=False)
        self.assertEqual(list(vault_map), [])

    def test_symlink_note_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            hedef = Path(temporary) / "hedef.md"
            hedef.write_text("dış", encoding="utf-8")
            link = vault / "🧠 500-Knowledge" / "link.md"
            try:
                os.symlink(hedef, link)
            except OSError:
                self.skipTest("symlink izni yok")
            with mock.patch.object(
                vr, "markdown_paths", return_value=[link]
            ):
                vault_map = vr.build_vault_map(vault, write_cache=False)
        self.assertEqual(list(vault_map), [])

    def test_junction_parent_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            gercek = Path(temporary) / "gercek"
            gercek.mkdir()
            (gercek / "kacak.md").write_text("kaçak", encoding="utf-8")
            junction = vault / "🧠 500-Knowledge" / "alt"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(gercek)],
                capture_output=True,
            )
            if result.returncode != 0:
                self.skipTest("junction oluşturulamadı")
            with mock.patch.object(
                vr, "markdown_paths",
                return_value=[junction / "kacak.md"],
            ):
                vault_map = vr.build_vault_map(vault, write_cache=False)
        self.assertEqual(list(vault_map), [])

    def test_sourceless_snapshot_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            with mock.patch.object(
                vr, "_stable_source_snapshot",
                return_value=(None, None, None, None, False),
            ):
                vault_map = vr.build_vault_map(vault, write_cache=False)
        self.assertEqual(list(vault_map), [])

    def test_cached_entry_raw_hash_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            vr.build_vault_map(vault, write_cache=True)

            with mock.patch.object(
                vr, "_stable_raw_hash", return_value=(None, None, True)
            ):
                vault_map = vr.build_vault_map(vault, write_cache=False)
            self.assertEqual(list(vault_map), [])

            note = vault / "🧠 500-Knowledge" / "Atlas Notu.md"
            diger = vault / "🧠 500-Knowledge" / "baska.md"
            diger.write_text("içerik " * 30, encoding="utf-8")
            with mock.patch.object(
                vr, "_stable_raw_hash",
                return_value=("f" * 64, os.lstat(diger), False),
            ):
                vault_map = vr.build_vault_map(vault, write_cache=False)
            self.assertTrue(list(vault_map))

    def test_cached_entry_path_mismatch_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            vr.build_vault_map(vault, write_cache=True)
            cache_path = (
                vault / ".codex/scripts/.state/vault-retrieval-cache.json"
            )
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            for value in cache["files"].values():
                value["entry"]["path"] = "🧠 500-Knowledge/baska.md"
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            vault_map = vr.build_vault_map(vault, write_cache=False)
        self.assertTrue(list(vault_map))

    def test_cache_write_failure_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            with mock.patch.object(vr, "_save_cache", return_value=False):
                vault_map = vr.build_vault_map(vault, write_cache=True)
            self.assertTrue(list(vault_map))

            original_stat = Path.stat

            def fake_stat(self, *args, **kwargs):
                if self.name == "vault-retrieval-cache.json":
                    raise OSError("erişilemedi")
                return original_stat(self, *args, **kwargs)

            with mock.patch.object(Path, "stat", fake_stat):
                vault_map = vr.build_vault_map(vault, write_cache=True)
            self.assertTrue(list(vault_map))


class QueryClassifierArms(unittest.TestCase):
    def test_personal_and_history_edges(self) -> None:
        self.assertTrue(vr._is_personal_query(frozenset({"levent"})))
        self.assertFalse(vr._has_history_context(""))

    def test_date_bounds_calendar_failure(self) -> None:
        with mock.patch.object(
            vr.calendar, "monthrange", side_effect=ValueError
        ):
            self.assertIsNone(vr._date_bounds(2026, 5))

    def test_date_reference_shapes(self) -> None:
        swapped = vr._date_references("13.02.2026 ne oldu")
        self.assertTrue(swapped and swapped[0].start_date is not None)
        gecersiz = vr._date_references("0.0.2026 ne oldu")
        self.assertTrue(gecersiz and gecersiz[0].start_date is None)
        cift = vr._date_references("13.13.2026 ne oldu")
        self.assertTrue(cift and cift[0].start_date is None)

    def test_past_date_status_edges(self) -> None:
        self.assertFalse(
            vr._past_date_status(
                "2026-05-05 toplantı before",
                frozenset({"before", "toplanti"}),
            )
        )
        self.assertFalse(
            vr._past_date_status("", frozenset({"2026", "5"}))
        )
        self.assertFalse(
            vr._past_date_status("", frozenset({"0000"}))
        )

    def test_split_current_history_matrix(self) -> None:
        cases = [
            "aktiF ve dünkü karar",
            "güncel geçmiş plan ve elma",
            "güncel plan ile tarih",
            "current retention and history",
            "son plan değişikliği ile güncel",
        ]
        for query in cases:
            with self.subTest(query=query):
                self.assertIsNone(vr._split_current_history_query(query))

    def test_selection_exclusion_empty_sides(self) -> None:
        self.assertIsNone(vr._split_selection_exclusion(", excluding history"))
        self.assertIsNone(vr._split_selection_exclusion("plan and excluding"))

    def test_independent_current_cue_without_cue(self) -> None:
        self.assertFalse(vr._has_independent_current_cue("elma armut"))


class RankAndExcerptArms(unittest.TestCase):
    def test_entry_slices_with_both_exclusions(self) -> None:
        base = _payload_template()
        entry = vr._entry_from_payload(base)
        self.assertEqual(
            vr._entry_lines(entry, include_history=False,
                            exclude_historical_material=True,
                            exclude_current_material=True),
            (),
        )
        self.assertEqual(
            vr._entry_body_terms(entry, include_history=False,
                                 exclude_historical_material=True,
                                 exclude_current_material=True),
            {},
        )
        self.assertTrue(vr._route_allows(entry, "CEVDET"))

    def test_excerpt_metadata_overflow_message(self) -> None:
        base = _payload_template()
        uzun = ("- `gecerli` `" + "t" * 600 + "` `k` 2026-01-01 "
                + "gövde " * 40)
        entry = vr._entry_from_payload({**base, "safe_lines": [uzun]})
        excerpt = vr._excerpt(entry, frozenset())
        self.assertEqual(excerpt, "Metin bütçeye sığmıyor; tam kaynağı oku.")

    def test_merge_scoped_hits_deduplicates(self) -> None:
        hit = types.SimpleNamespace(entry=types.SimpleNamespace(path="x"))
        merged = vr._merge_scoped_hits([hit], [hit], top_k=5)
        self.assertEqual(len(merged), 1)

    def test_rank_prefers_active_duplicate_on_current_query(self) -> None:
        base = _payload_template()
        base["body_terms"] = {"elma": 2}
        base["safe_lines"] = ["elma hakkında karar"]
        base["title"] = "Elma"
        base["title_terms"] = ["elma"]
        base["content_key"] = "ortak-icerik"
        eski = vr._entry_from_payload(
            {**base, "path": "🧠 500-Knowledge/a.md", "status": "stale"}
        )
        yeni = vr._entry_from_payload(
            {**base, "path": "🧠 500-Knowledge/b.md", "status": "active"}
        )
        from collections import Counter

        vault_map = vr.VaultMap([eski, yeni], Counter({"elma": 2}), 2)
        hits = vr._rank_query(vault_map, "güncel elma", top_k=3)
        self.assertEqual(
            [hit.entry.status for hit in hits], ["active"]
        )


class FreshHitsAndViewArms(unittest.TestCase):
    def test_replacement_updates_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = _seed_vault(Path(temporary))
            vault_map = vr.build_vault_map(vault, write_cache=False)
            note = vault / "🧠 500-Knowledge" / "Atlas Notu.md"
            note.write_text(
                note.read_text(encoding="utf-8") + "\nYeni satır eklendi.\n",
                encoding="utf-8",
            )
            memory = MemoryRead(vault, frozenset())
            hits = vr._fresh_hits(vault, vault_map, "atlas", 5, memory)
            self.assertTrue(hits)

    def test_linked_view_entry_resolution(self) -> None:
        entry = types.SimpleNamespace(
            path="knowledge/Atlas Notu.md", title="Atlas Notu"
        )
        aliases = vr._view_aliases([entry])
        text = (
            "[dış](https://ornek.com/sayfa) "
            "[not](alt/Atlas Notu.md) [[Atlas Notu]]"
        )
        linked = vr._linked_view_entries(text, aliases)
        self.assertEqual(len(linked), 1)

    def test_required_view_sources_skips_forgotten(self) -> None:
        entry = types.SimpleNamespace(
            path="knowledge/Atlas Notu.md", title="Atlas Notu"
        )
        hit = types.SimpleNamespace(entry=entry)
        memory = types.SimpleNamespace(
            read_source=lambda _path: ("rel", None)
        )
        with tempfile.TemporaryDirectory() as temporary:
            selected = vr._required_view_sources(
                Path(temporary), [entry], [hit], memory
            )
        self.assertEqual(len(selected), 1)


if __name__ == "__main__":
    unittest.main()
