"""memory_ledger'ın sanitizasyon, bastırma ve görünüm korkuluklarını sürer."""

from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import memory_ledger


def _junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


class SanitizerEdges(unittest.TestCase):
    def test_reducer_rejects_unknown_role(self) -> None:
        reducer = memory_ledger.PersistentTurnReducer(frozenset())
        with self.assertRaises(ValueError):
            reducer.add("tool", "çıktı")

    def test_truncation_marker_paths(self) -> None:
        short, redactions = memory_ledger.sanitize_text("u" * 500, max_chars=10)
        self.assertLessEqual(len(short), 10)
        self.assertIn("truncated", redactions)

        long_text = "başlangıç " + "u" * 5000 + " son"
        truncated, redactions = memory_ledger.sanitize_text(long_text, max_chars=200)
        self.assertIn("TRUNCATED_SAFE_LEDGER_EVENT", truncated)
        self.assertIn("truncated", redactions)

    def test_quoted_json_value_with_credential_is_redacted(self) -> None:
        inner = json.dumps({"api_key": "cok-gizli-deger-123456"})
        text = json.dumps({"veri": inner})
        cleaned, redactions = memory_ledger.sanitize_text(text)
        self.assertNotIn("cok-gizli-deger-123456", cleaned)
        self.assertIn("credential", redactions)

    def test_credential_shaped_broken_json_is_unverifiable(self) -> None:
        with self.assertRaises(memory_ledger.MemoryPreferenceError):
            memory_ledger.sanitize_text('{"password": }')

    def test_escaped_credential_outside_region_is_unverifiable(self) -> None:
        # Kaçışlı anahtar adı bölgeler ARASINDA kalırsa doğrulanamaz sayılır.
        with self.assertRaises(memory_ledger.MemoryPreferenceError):
            memory_ledger.sanitize_text(
                '{"a": 1} "pass\\u0077ord": gizli {"b": 2}'
            )

    def test_tokenizer_corners_do_not_crash(self) -> None:
        # Kapanmamış tırnak, dengesiz parantez ve bozuk iç içe adaylar
        # sanitizasyonu düşürmemeli; kötümser sonuç kabul, çökme ret.
        for text in (
            '{"a": "\\"',
            "({[)]}",
            '{"b": {bozuk} } sonra {"c": 1}',
            '[1, 2] "yalnız değer" {"d": bozuk2}',
        ):
            with self.subTest(text=text):
                try:
                    memory_ledger.sanitize_text(text)
                except memory_ledger.MemoryPreferenceError:
                    pass

    def test_decoded_key_fragment_and_auth_brace_overlap(self) -> None:
        # Değeri olmayan tırnaklı kimlik anahtarı çözülünce redakte edilir.
        cleaned, redactions = memory_ledger.sanitize_text('{"a": "\\"password\\":"}')
        self.assertIn("credential", redactions)
        self.assertIn("<REDACTED>", cleaned)

        # Süslü değerli Authorization, iç içe ikinci eşleşmeyi yutar.
        overlap = (
            'Authorization: Bearer {"i": "Authorization: Bearer inner"}kuyruk'
        )
        cleaned, redactions = memory_ledger.sanitize_text(overlap)
        self.assertIn("authorization", redactions)
        self.assertNotIn("inner", cleaned)

    def test_deep_nesting_is_unverifiable_not_crash(self) -> None:
        payload = (
            '{"wrapper":'
            + "[" * 200
            + '{"password":"cok-gizli-deger"}'
        )
        limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(120)
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.sanitize_text(payload)
        finally:
            sys.setrecursionlimit(limit)


class SuppressionEdges(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires Windows directory handle guard")
    def test_pinned_controls_directory_blocks_junction_swap_during_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "vault" / ".codex" / "private-memory"
            controls = private / "controls"
            outside = root / "outside-controls"
            controls.mkdir(parents=True)
            outside.mkdir()
            real_write = memory_ledger.atomic_write_text
            rmdir_result = None

            def write(path: Path, text: str, **kwargs: object) -> None:
                nonlocal rmdir_result
                rmdir_result = subprocess.run(
                    ["cmd", "/c", "rmdir", str(controls)],
                    check=False,
                    capture_output=True,
                )
                if rmdir_result.returncode == 0:
                    _junction(controls, outside)
                real_write(path, text, **kwargs)

            with mock.patch.object(memory_ledger, "atomic_write_text", side_effect=write):
                memory_ledger.suppress_derived_memory(private, "hedef", now=1.0)

            self.assertIsNotNone(rmdir_result)
            self.assertNotEqual(rmdir_result.returncode, 0)
            self.assertTrue(controls.is_dir())
            self.assertFalse(controls.is_junction())
            self.assertEqual(list(outside.iterdir()), [])
            self.assertIn(
                memory_ledger.memory_text_hash("hedef"),
                memory_ledger.load_suppressed_hashes(private),
            )

    def test_suppression_controls_junction_is_rejected_before_read_write_or_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            private = vault / ".codex" / "private-memory"
            outside = root / "outside-controls"
            private.mkdir(parents=True)
            outside.mkdir()
            repeated = "unutulacak"
            (outside / "suppressions.jsonl").write_text(
                json.dumps({
                    "schema": 1,
                    "ts": 1,
                    "target_sha256": memory_ledger.memory_text_hash(repeated),
                }) + "\n",
                encoding="utf-8",
            )
            controls = private / "controls"
            try:
                _junction(controls, outside)
            except (OSError, subprocess.CalledProcessError) as exc:
                self.skipTest(f"junction unavailable: {exc}")
            before = {
                path.relative_to(outside): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            try:
                with self.assertRaisesRegex(
                    memory_ledger.MemoryPreferenceError,
                    "memory-suppression-path-invalid",
                ):
                    memory_ledger.load_suppressed_hashes(private)
                with self.assertRaisesRegex(
                    memory_ledger.MemoryPreferenceError,
                    "memory-suppression-path-invalid",
                ):
                    memory_ledger.suppress_derived_memory(private, repeated, now=1.0)
                with self.assertRaisesRegex(
                    memory_ledger.MemoryPreferenceError,
                    "memory-suppression-path-invalid",
                ):
                    memory_ledger.suppress_derived_memory(private, "yeni hedef", now=1.0)
                with self.assertRaisesRegex(
                    memory_ledger.MemoryPreferenceError,
                    "memory-suppression-path-invalid",
                ):
                    with memory_ledger.suppression_guard(private, frozenset()):
                        pass
                after = {
                    path.relative_to(outside): path.read_bytes()
                    for path in outside.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertFalse((outside / "suppressions.lock").exists())
            finally:
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(controls)],
                    check=False,
                    capture_output=True,
                )

    @unittest.skipUnless(os.name == "nt", "requires Windows directory handle guard")
    def test_missing_private_root_is_pinned_before_write_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "vault" / ".codex"
            codex.mkdir(parents=True)
            private = codex / "private-memory"
            outside = root / "outside-private"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_bytes(b"keep")
            before = {
                path.relative_to(outside): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            real_mkdir = Path.mkdir
            injected = False

            def mkdir(path: Path, *args: object, **kwargs: object) -> None:
                nonlocal injected
                if path == private and not injected:
                    injected = True
                    _junction(private, outside)
                real_mkdir(path, *args, **kwargs)

            try:
                with mock.patch.object(Path, "mkdir", new=mkdir):
                    with self.assertRaisesRegex(
                        memory_ledger.MemoryPreferenceError,
                        "memory-suppression-path-invalid",
                    ):
                        memory_ledger.suppress_derived_memory(private, "hedef", now=1.0)
                self.assertTrue(injected)
                after = {
                    path.relative_to(outside): path.read_bytes()
                    for path in outside.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertFalse((outside / "controls").exists())
                self.assertFalse((outside / "suppressions.jsonl").exists())
                self.assertFalse((outside / "suppressions.lock").exists())
            finally:
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(private)],
                    check=False,
                    capture_output=True,
                )

    @unittest.skipUnless(os.name == "nt", "requires Windows directory handle guard")
    def test_missing_private_root_reader_pins_existing_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "vault" / ".codex"
            codex.mkdir(parents=True)
            private = codex / "private-memory"
            outside = root / "outside-private"
            outside_controls = outside / "controls"
            outside_controls.mkdir(parents=True)
            ledger = outside_controls / "suppressions.jsonl"
            ledger.write_text(
                json.dumps({
                    "schema": 1,
                    "ts": 1,
                    "target_sha256": memory_ledger.memory_text_hash("hedef"),
                }) + "\n",
                encoding="utf-8",
            )
            before = {
                path.relative_to(outside): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            real_read = memory_ledger._read_suppression_lines
            rmdir_result = None

            def swap_then_read(private_root: Path, path: Path) -> list[str]:
                nonlocal rmdir_result
                rmdir_result = subprocess.run(
                    ["cmd", "/c", "rmdir", str(codex)],
                    check=False,
                    capture_output=True,
                )
                _junction(private, outside)
                return real_read(private_root, path)

            try:
                with mock.patch.object(
                    memory_ledger,
                    "_read_suppression_lines",
                    side_effect=swap_then_read,
                ):
                    with self.assertRaisesRegex(
                        memory_ledger.MemoryPreferenceError,
                        "memory-suppression-path-invalid",
                    ):
                        memory_ledger.load_suppressed_hashes(private)
                self.assertIsNotNone(rmdir_result)
                self.assertNotEqual(rmdir_result.returncode, 0)
                after = {
                    path.relative_to(outside): path.read_bytes()
                    for path in outside.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
            finally:
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(private)],
                    check=False,
                    capture_output=True,
                )

    @unittest.skipUnless(os.name == "nt", "requires Windows directory handle guard")
    def test_replaced_suppression_directory_fails_before_handle_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private-memory"
            controls = private / "controls"
            controls.mkdir(parents=True)
            (controls / "suppressions.jsonl").write_text("", encoding="utf-8")
            real_pin = memory_ledger._pinned_windows_directory

            for operation in (
                lambda: memory_ledger.load_suppressed_hashes(private),
                lambda: memory_ledger.suppress_derived_memory(
                    private, "hedef", now=1.0
                ),
            ):
                replacement = private / "controls-replacement"
                injected = False

                def replace_before_pin(path: Path):
                    nonlocal injected
                    if path == controls and not injected:
                        injected = True
                        controls.rename(replacement)
                        controls.mkdir()
                    return real_pin(path)

                try:
                    with mock.patch.object(
                        memory_ledger,
                        "_pinned_windows_directory",
                        side_effect=replace_before_pin,
                    ):
                        with self.assertRaisesRegex(
                            memory_ledger.MemoryPreferenceError,
                            "memory-suppression-path-invalid",
                        ):
                            operation()
                    self.assertTrue(injected)
                finally:
                    if controls.exists():
                        controls.rmdir()
                    if replacement.exists():
                        replacement.rename(controls)

    def test_invalid_and_unreadable_suppression_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            controls = private / "controls"
            controls.mkdir()
            (controls / "suppressions.jsonl").write_text(
                json.dumps({"schema": 1, "ts": 1, "target_sha256": "KISA"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.load_suppressed_hashes(private)
            (controls / "suppressions.jsonl").write_bytes(bytes([255, 254, 250]))
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.load_suppressed_hashes(private)

    def test_suppression_ledger_open_errors_do_not_mean_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            controls = private / "controls"
            controls.mkdir()
            ledger = controls / "suppressions.jsonl"
            ledger.write_text("", encoding="utf-8")
            with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(
                    memory_ledger.MemoryPreferenceError,
                    "memory-suppression-unreadable",
                ):
                    memory_ledger.load_suppressed_hashes(private)

    def test_suppression_directory_access_denial_fails_without_retry(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows directory handle contract")
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            (private / "controls").mkdir()
            denied = PermissionError("denied")
            denied.winerror = 5
            with mock.patch.object(
                memory_ledger,
                "_pinned_windows_directory",
                side_effect=denied,
            ) as pin:
                with self.assertRaisesRegex(
                    memory_ledger.MemoryPreferenceError,
                    "memory-suppression-path-invalid",
                ):
                    memory_ledger.suppress_derived_memory(private, "hedef")
            pin.assert_called_once()

    def test_repeated_suppression_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            first = memory_ledger.suppress_derived_memory(private, "unutulacak")
            second = memory_ledger.suppress_derived_memory(private, "unutulacak")
            lines = (private / "controls" / "suppressions.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        self.assertEqual(first, second)
        self.assertEqual(len(lines), 1)

    def test_suppression_rejects_foreign_view_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            views = private / "views"
            views.mkdir(parents=True)
            (views / "sahipsiz.md").write_text("x", encoding="utf-8")
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.suppress_derived_memory(private, "hedef")

        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            views = private / "views"
            views.mkdir(parents=True)
            gercek = private / "gercek.md"
            gercek.write_text("x", encoding="utf-8")
            os.symlink(gercek, views / ("a" * 64 + ".md"))
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.suppress_derived_memory(private, "hedef")


class MemoryViewEdges(unittest.TestCase):
    def _vault(self, temporary: Path) -> Path:
        vault = temporary / "vault"
        (vault / "daily").mkdir(parents=True)
        return vault

    def test_excluded_source_and_profile_return_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            daily = vault / "daily" / "2026-09-01.md"
            daily.write_text("Gizlenecek satır.", encoding="utf-8")
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "daily/2026-09-01.md"
            )
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", memory_ledger.PROFILE_RELATIVE
            )
            with memory_ledger.memory_read(vault) as memory:
                _relative, text = memory.read_source(daily)
                self.assertIsNone(text)
                self.assertEqual(memory.profile_issues(), ())

    def test_inactive_memory_short_circuits_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            with memory_ledger.memory_read(vault) as memory:
                self.assertFalse(memory.active)
                self.assertEqual(memory.views([]), {})
                self.assertEqual(memory.render_views([]), ({}, {}))

    def test_view_failures_map_to_preference_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            (vault / "daily" / "2026-09-01.md").write_text("satır", encoding="utf-8")
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "alakasız"
            )
            with memory_ledger.memory_read(vault) as memory:
                self.assertTrue(memory.active)
                with mock.patch.object(
                    memory_ledger, "materialize_memory_views", side_effect=OSError
                ):
                    with self.assertRaises(memory_ledger.MemoryPreferenceError):
                        memory.views([("daily/2026-09-01.md", "Gün")])
                with mock.patch.object(
                    memory_ledger, "_render_memory_views", side_effect=OSError
                ):
                    with self.assertRaises(memory_ledger.MemoryPreferenceError):
                        memory.render_views([("daily/2026-09-01.md", "Gün")])

    def test_private_memory_junction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            (vault / ".codex").mkdir()
            outside = Path(temporary) / "dis"
            outside.mkdir()
            _junction(vault / ".codex" / "private-memory", outside)
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.materialize_memory_views(vault, [], frozenset())

    def test_http_markdown_links_survive_view_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            source = vault / "daily" / "2026-09-01.md"
            source.write_text(
                "Bkz [dış kaynak](https://example.com/sayfa) ve [iç](daily/2026-09-02.md).",
                encoding="utf-8",
            )
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "alakasız"
            )
            hashes = memory_ledger.load_suppressed_hashes(
                vault / ".codex/private-memory"
            )
            rendered = memory_ledger.materialize_memory_views(
                vault, [("daily/2026-09-01.md", "Gün")], hashes
            )
            text = "\n".join(
                (vault / view_relative).read_text(encoding="utf-8")
                for view_relative in rendered.values()
            )
        self.assertIn("https://example.com/sayfa", text)

    def test_ledger_scanner_and_view_stragglers(self) -> None:
        # Derin köşeli parantez: sınırlı özyinelemede decode RecursionError'ı
        # doğrulanamaz kimlik hatasına çevrilir.
        limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(60)
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                list(memory_ledger._json_regions("[" * 200))
        finally:
            sys.setrecursionlimit(limit)

        # TOKEN_PREFIX bölge sonrası eşleşme: bölge atlanır, token redakte edilir.
        cleaned, redactions = memory_ledger.sanitize_text(
            '{"x": "deger"} sonra ghp_abcdefghijklmnop123456 son'
        )
        self.assertIn("credential", redactions)
        self.assertNotIn("ghp_abcdefghijklmnop123456", cleaned)

        # Companion alias bastırması kaynağın kendisini de kapatır.
        source, alias = next(iter(memory_ledger._COMPANION_SOURCE_ALIASES.items()))
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", alias
            )
            with memory_ledger.memory_read(vault) as memory:
                self.assertIsNone(
                    memory.project_text(source, "metin", resolved_relative=source)
                )

        # Aktif hafızada bastırılmış kaynağın görünüm kimliği: kaynak görünür
        # listede yoktur, başa eklenir ve boş metin döner.
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            (vault / "daily" / "2026-09-01.md").write_text("Gün.", encoding="utf-8")
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "daily/2026-09-01.md"
            )
            digest = memory_ledger._sha256_text("daily/2026-09-01.md")
            view = vault / ".codex" / "private-memory" / "views" / f"{digest}.md"
            self.assertEqual(memory_ledger.read_memory_source(vault, view), "")

        # Görünüm hedefi symlink ise materialize reddeder; render'ın kendi
        # junction korkuluğu da bağımsız çalışır.
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            (vault / "daily" / "2026-09-01.md").write_text("Gün.", encoding="utf-8")
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "alakasız"
            )
            hashes = memory_ledger.load_suppressed_hashes(
                vault / ".codex/private-memory"
            )
            digest = memory_ledger._sha256_text("daily/2026-09-01.md")
            views = vault / ".codex" / "private-memory" / "views"
            views.mkdir(parents=True, exist_ok=True)
            gercek = vault / "gercek-view.md"
            gercek.write_text("x", encoding="utf-8")
            os.symlink(gercek, views / f"{digest}.md")
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.materialize_memory_views(
                    vault, [("daily/2026-09-01.md", "Gün")], hashes
                )

        with tempfile.TemporaryDirectory() as temporary:
            vault2 = Path(temporary) / "vault2"
            (vault2 / ".codex").mkdir(parents=True)
            outside = Path(temporary) / "dis"
            outside.mkdir()
            _junction(vault2 / ".codex" / "private-memory", outside)
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger._render_memory_views(vault2, [], frozenset())

    def test_read_memory_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            source = vault / "daily" / "2026-09-01.md"
            source.write_text("Gün içeriği.", encoding="utf-8")
            with self.assertRaises(memory_ledger.MemorySourceError):
                memory_ledger.read_memory_source(vault, Path(temporary) / "dis.md")

            digest = memory_ledger._sha256_text("daily/2026-09-01.md")
            view_identifier = vault / ".codex" / "private-memory" / "views" / f"{digest}.md"

            # Pasif hafıza: görünüm kimliği kaynağa çözülür ve doğrudan okunur.
            text = memory_ledger.read_memory_source(vault, view_identifier)
            self.assertIn("Gün içeriği", text)

            self.assertIsNone(
                memory_ledger._resolve_memory_view_source(
                    vault, "views/gecersiz-ad.md"
                )
            )


if __name__ == "__main__":
    unittest.main()
