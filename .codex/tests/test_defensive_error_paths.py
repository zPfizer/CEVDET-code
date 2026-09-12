"""Çekirdek depo/şema modüllerinin savunma dallarını gerçek girdilerle sürer.

Kapsam %100 sözleşmesinin parçası: her hata dalı ya burada gerçek bir
girdiyle tetiklenir ya da kaynakta gerekçeli `pragma: no cover` taşır.
İşletim sistemi hatası taşınabilir biçimde üretilemeyen yerlerde (yalnız
oralarda) mock ile hata enjekte edilir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import codex_runner
import compile_state
import graph_integrity
import state_store
import user_evidence
import vault_corpus


def _junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


class StateStoreErrorPaths(unittest.TestCase):
    def test_replace_retry_rejects_invalid_timeout_and_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "a"
            source.write_text("x", encoding="utf-8")
            target = Path(temporary) / "b"
            with self.assertRaises(ValueError):
                state_store.replace_with_retry(source, target, timeout="bozuk")
            with self.assertRaises(ValueError):
                state_store.replace_with_retry(source, target, deadline="bozuk")

    def test_load_health_rejects_entry_with_unknown_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "generation": 3,
                        "components": {"flush:s": {"status": "tuhaf"}},
                    }
                ),
                encoding="utf-8",
            )
            loaded = state_store._load_health(path)
        self.assertEqual(loaded["components"], {})
        self.assertEqual(loaded["generation"], 0)

    def test_write_health_requires_full_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                state_store.write_health(
                    Path(temporary), component="", error="x", scope_key="s"
                )

    def test_write_health_normalizes_non_list_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "health.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "generation": 1,
                        "components": {
                            "flush:s": {"status": "error", "warnings": "bozuk"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = state_store.write_health(
                state, component="flush", error="yeni", warning=True, scope_key="s"
            )
        self.assertEqual(payload["components"]["flush:s"]["warnings"], ["yeni"])

    def test_clear_health_keeps_entry_when_expected_error_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            state_store.write_health(state, component="flush", error="gercek", scope_key="s")
            payload = state_store.clear_health(
                state, component="flush", scope_key="s", expected_error="baska"
            )
        self.assertIn("flush:s", payload["components"])


class VaultCorpusEdges(unittest.TestCase):
    def test_junction_root_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "hedef"
            target.mkdir()
            (target / "not.md").write_text("x", encoding="utf-8")
            junction = root / "kestirme"
            _junction(junction, target)
            self.assertEqual(list(vault_corpus.markdown_paths(junction)), [])

    def test_missing_root_raises_walk_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "yok"
            with self.assertRaises(OSError):
                list(vault_corpus.markdown_paths(missing))

    def test_non_regular_md_entry_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            root.mkdir()
            (root / "gercek.md").write_text("x", encoding="utf-8")
            hedef = Path(temporary) / "dis.md"
            hedef.write_text("y", encoding="utf-8")
            os.symlink(hedef, root / "baglanti.md")
            names = [path.name for path in vault_corpus.markdown_paths(root)]
        self.assertEqual(names, ["gercek.md"])

    def test_unreadable_note_records_error_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_note = root / "dizin.md"
            fake_note.mkdir()
            notes = vault_corpus.vault_notes(root, paths=[fake_note])
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].error)
        self.assertEqual(notes[0].text, "")


class CodexRunnerDiscovery(unittest.TestCase):
    def _environment(self, temporary: Path, path_dir: Path) -> dict[str, str]:
        return {
            "LOCALAPPDATA": str(temporary / "lad"),
            "PATH": str(path_dir),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        }

    def test_configured_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "codex.exe"
            configured.write_bytes(b"")
            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(configured)}):
                self.assertEqual(codex_runner.find_codex(), str(configured))

    def test_wrapper_resolves_to_native_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrap = root / "wrap"
            wrap.mkdir()
            (wrap / "codex.bat").write_text("@echo off\r\n", encoding="utf-8")
            native = (
                wrap / "node_modules" / "@openai" / "codex" / "node_modules"
                / "@openai" / "codex-win32-x64" / "vendor"
                / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
            )
            native.parent.mkdir(parents=True)
            native.write_bytes(b"")
            with mock.patch.dict(
                os.environ, self._environment(root, wrap), clear=True
            ):
                self.assertEqual(codex_runner.find_codex(), str(native))

    def test_wrapper_without_native_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrap = root / "wrap"
            wrap.mkdir()
            wrapper = wrap / "codex.bat"
            wrapper.write_text("@echo off\r\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ, self._environment(root, wrap), clear=True
            ):
                self.assertEqual(
                    Path(codex_runner.find_codex()).resolve(), wrapper.resolve()
                )

    def test_missing_cli_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = root / "bos"
            empty.mkdir()
            with mock.patch.dict(
                os.environ, self._environment(root, empty), clear=True
            ):
                with self.assertRaises(FileNotFoundError):
                    codex_runner.find_codex()

    def test_within_is_false_across_drives(self) -> None:
        self.assertFalse(codex_runner._within(Path("Q:/gecici/x"), Path("C:/")))


class CompileStateGuards(unittest.TestCase):
    def test_publication_with_invalid_utf8_is_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / compile_state.PUBLICATION_NAME).write_bytes(b"\xff\xfe\xfa\x00")
            with self.assertRaises(compile_state.PolicyError):
                compile_state.load_publication(state)

    def test_publication_directory_is_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / compile_state.PUBLICATION_NAME).mkdir()
            with self.assertRaises(compile_state.PolicyError):
                compile_state.load_publication(state)

    def test_publication_read_oserror_is_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / compile_state.PUBLICATION_NAME).write_text("{}", encoding="utf-8")
            # Windows'ta izin hatası taşınabilir kurulamıyor; OSError'ı enjekte et.
            with mock.patch.object(
                compile_state, "_read_bounded_json", side_effect=OSError("disk")
            ):
                with self.assertRaises(compile_state.PolicyError):
                    compile_state.load_publication(state)

    def test_token_directory_and_oserror_are_policy_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / compile_state.PUBLICATION_TOKEN_NAME).mkdir()
            with self.assertRaises(compile_state.PolicyError):
                compile_state.load_publication_token(state)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / compile_state.PUBLICATION_TOKEN_NAME).write_text(
                "{}", encoding="utf-8"
            )
            with mock.patch.object(
                compile_state, "_read_bounded_json", side_effect=OSError("disk")
            ):
                with self.assertRaises(compile_state.PolicyError):
                    compile_state.load_publication_token(state)

    def test_token_with_invalid_id_is_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / compile_state.PUBLICATION_TOKEN_NAME).write_text(
                json.dumps({"schema_version": 1, "publication_id": "BÜYÜK"}),
                encoding="utf-8",
            )
            with self.assertRaises(compile_state.PolicyError):
                compile_state.load_publication_token(state)

    def test_save_token_rejects_invalid_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(compile_state.PolicyError):
                compile_state.save_publication_token(Path(temporary), "kisa")

    def test_clear_publication_missing_is_noop_and_directory_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            compile_state.clear_publication(state)
            (state / compile_state.PUBLICATION_NAME).mkdir()
            with self.assertRaises(compile_state.PolicyError):
                compile_state.clear_publication(state)

    def test_state_must_be_schema_valid_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            target = state / compile_state.STATE_NAME
            target.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(compile_state.PolicyError):
                compile_state.load(state)
            target.write_text(json.dumps({"ingested": []}), encoding="utf-8")
            with self.assertRaises(compile_state.PolicyError):
                compile_state.load(state)

    def test_invalid_calendar_date_in_name_sorts_last_not_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = vault / "daily"
            daily.mkdir()
            (daily / "2026-13-01.md").write_text("a", encoding="utf-8")
            (daily / "2026-01-01.md").write_text("b", encoding="utf-8")
            changed = compile_state.changed_dailies(
                vault, state=compile_state.CompileState()
            )
        self.assertEqual(
            [path.name for path, _digest in changed],
            ["2026-01-01.md", "2026-13-01.md"],
        )

    def test_daily_junction_escaping_vault_is_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            outside = root / "dis-daily"
            outside.mkdir()
            _junction(vault / "daily", outside)
            with self.assertRaises(compile_state.PolicyError):
                compile_state.changed_dailies(vault, state=compile_state.CompileState())


class GraphIntegrityGuards(unittest.TestCase):
    def _connection_vault(self, root: Path) -> Path:
        connections = root / "knowledge" / "connections"
        connections.mkdir(parents=True)
        (root / "knowledge" / "concepts").mkdir()
        return connections

    def test_multi_backtick_inline_code_is_blanked(self) -> None:
        body = graph_integrity._markdown_body("önce ``gizli-kod`` sonra")
        self.assertNotIn("gizli-kod", body)
        self.assertIn("sonra", body)

    def test_missing_daily_file_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(
                graph_integrity.ensure_daily_graph_link(Path(temporary) / "yok.md")
            )

    def test_missing_roots_short_circuit_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(graph_integrity.normalize_connection_links(root), 0)
            (root / "knowledge").mkdir()
            self.assertEqual(graph_integrity.normalize_connection_links(root), 0)

    def test_knowledge_and_concepts_must_be_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "knowledge").write_text("dosya", encoding="utf-8")
            with self.assertRaises(graph_integrity.GraphPolicyError):
                graph_integrity.normalize_connection_links(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "knowledge" / "connections").mkdir(parents=True)
            (root / "knowledge" / "concepts").write_text("dosya", encoding="utf-8")
            with self.assertRaises(graph_integrity.GraphPolicyError):
                graph_integrity.normalize_connection_links(root)

    def test_symlinked_connection_note_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            connections = self._connection_vault(root)
            hedef = Path(temporary) / "dis.md"
            hedef.write_text("---\nconnects:\n  - a\n  - b\n---\n", encoding="utf-8")
            os.symlink(hedef, connections / "baglanti.md")
            with self.assertRaises(graph_integrity.GraphPolicyError):
                graph_integrity.normalize_connection_links(root)

    def test_connection_without_connects_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            connections = self._connection_vault(root)
            (connections / "eksik.md").write_text("içerik", encoding="utf-8")
            with self.assertRaises(graph_integrity.GraphPolicyError):
                graph_integrity.normalize_connection_links(root)

    def test_concept_slug_backed_by_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            connections = self._connection_vault(root)
            (connections / "k.md").write_text(
                "---\nconnects:\n  - bir\n  - iki\n---\n\n## Bağlantı\n",
                encoding="utf-8",
            )
            (root / "knowledge" / "concepts" / "bir.md").mkdir()
            with self.assertRaises(graph_integrity.GraphPolicyError):
                graph_integrity.normalize_connection_links(root)

    def test_revalidation_before_write_catches_swapped_note(self) -> None:
        # TOCTOU korkuluğu: ilk geçişte geçen dosya, yazımdan hemen önce
        # yeniden doğrulanır; arada değişen dosya yazımı iptal etmeli.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            connections = self._connection_vault(root)
            (connections / "k.md").write_text(
                "---\nconnects:\n  - bir\n  - iki\n---\n\n## Bağlantı\n",
                encoding="utf-8",
            )
            for slug in ("bir", "iki"):
                (root / "knowledge" / "concepts" / f"{slug}.md").write_text(
                    f"---\ntitle: {slug}\n---\n", encoding="utf-8"
                )
            real = graph_integrity._source_status
            seen: dict[str, int] = {}

            def racing(path, root_arg, expected=None):
                if path.name == "k.md":
                    seen["k.md"] = seen.get("k.md", 0) + 1
                    if seen["k.md"] > 1:
                        return "source-type"
                return real(path, root_arg, expected)

            with mock.patch.object(graph_integrity, "_source_status", racing):
                with self.assertRaises(graph_integrity.GraphPolicyError):
                    graph_integrity.normalize_connection_links(root)

    def test_empty_wikilink_target_is_ignored_in_summary(self) -> None:
        note = vault_corpus.NoteIndex(
            Path("a.md"), PurePosixPath("a.md"), "[[#bölüm]] metin", {}, ""
        )
        count, isolated = graph_integrity.graph_summary([note])
        self.assertEqual((count, isolated), (1, ["a.md"]))


class UserEvidenceEdges(unittest.TestCase):
    def test_non_referential_start_needs_no_context(self) -> None:
        self.assertFalse(user_evidence.needs_context("Bunun nedeni performans."))

    def test_json_object_rejects_non_dict_and_non_str_keys(self) -> None:
        self.assertIsNone(user_evidence._json_object([1]))
        self.assertIsNone(user_evidence._json_object({1: "x"}))

    def test_invalid_scope_citation_becomes_uncertain(self) -> None:
        body = (
            '- Karar. <!-- user-source: {"quote": "Karar verdim.", "scope": "bozuk"} -->'
        )
        sections = {"Alınan Kararlar": body, "Öğrenilenler": ""}
        output = user_evidence.bind_evidence(
            sections, [("user", "Karar verdim.")], "2026-09-11T10:00:00+03:00"
        )
        self.assertIn("belirsiz", output["Öğrenilenler"])

    def test_project_scope_is_inferred_from_message(self) -> None:
        request = "Bu projede hız her şeyden önce gelir."
        body = (
            "- Hız önceliği. <!-- user-source: "
            + json.dumps({"quote": request, "scope": "general"}, ensure_ascii=False)
            + " -->"
        )
        output = user_evidence.bind_evidence(
            {"Alınan Kararlar": body}, [("user", request)], "2026-09-11T10:00:00+03:00"
        )
        self.assertIn("kapsam: project", output["Alınan Kararlar"])

    def test_oversized_result_hits_budget(self) -> None:
        with self.assertRaises(ValueError):
            user_evidence.bind_evidence(
                {"Bağlam": "a" * 66_000}, [], "2026-09-11T10:00:00+03:00"
            )

    def test_evidence_for_rejects_schema_and_timestamp_drift(self) -> None:
        bad_schema = (
            "<!-- user-evidence: "
            + json.dumps({"id": "x", "schema": 2})
            + " -->"
        )
        self.assertIsNone(user_evidence.evidence_for(bad_schema, "x", "iddia"))

        unsigned = {
            "schema": 1,
            "claim": "İddia",
            "quote": "alıntı",
            "scope": "general",
            "message_hash": "a" * 64,
            "previous_assistant": "",
            "captured_at": "bozuk-tarih",
        }
        record = {**unsigned, "id": user_evidence._identity(unsigned)}
        daily = "<!-- user-evidence: " + user_evidence._encoded(record) + " -->"
        self.assertIsNone(user_evidence.evidence_for(daily, record["id"], "İddia"))

    def test_filter_evidence_drops_malformed_records(self) -> None:
        typed_wrong = (
            "<!-- user-evidence: "
            + json.dumps({"id": "x", "claim": 1, "quote": "q", "previous_assistant": ""})
            + " -->"
        )
        self.assertEqual(user_evidence.filter_evidence(typed_wrong, lambda _t: False), "")
        broken_json = "<!-- user-evidence: {bozuk} -->"
        self.assertEqual(user_evidence.filter_evidence(broken_json, lambda _t: False), "")

    def test_proof_for_link_rejects_symlinked_daily_and_none_reader(self) -> None:
        line = "- İddia [[daily/2026-09-01#user-" + "a" * 64 + "|Kullanıcı dayanağı]]"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "daily").mkdir()
            hedef = root / "gercek.md"
            hedef.write_text("x", encoding="utf-8")
            os.symlink(hedef, root / "daily" / "2026-09-01.md")
            self.assertIsNone(user_evidence.proof_for_link(root, line))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-01.md").write_text("x", encoding="utf-8")
            self.assertIsNone(
                user_evidence.proof_for_link(root, line, reader=lambda _p: None)
            )


if __name__ == "__main__":
    unittest.main()
