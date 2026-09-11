"""Ek kaynak (attachment) hattının savunma dallarını sürer.

%100 kapsam sözleşmesinin beşinci dilimi: zarf ayrıştırma, eşleme kaydı,
not bütünlüğü ve yayın kapısı ihlalleri gerçek dosya düzenleriyle üretilir.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import attachment_memory
import flush
from memory_ledger import load_suppressed_hashes, suppress_derived_memory


UUID1 = "11111111-1111-4111-8111-111111111111"
NOW = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.timezone.utc)
SUMMARY = "\n\n".join("## " + h + "\nKaynak dersi." for h in flush.EXPECTED_SECTIONS)


def _envelope(*sources: Path) -> str:
    body = "\n\n".join(f'## "Örnek": {source}' for source in sources)
    return f"# Files pasted by the user:\n\n{body}\n\n## My request:\n"


def _seed(root: Path, content: str = "Kalıcı kaynak dersi.", uuid: str = UUID1) -> Path:
    source = root / "home" / "attachments" / uuid / "pasted-text.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    return source


def _capture(root: Path, text: str, summarize=None, hashes: frozenset = frozenset()):
    with mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "home")}):
        return attachment_memory.capture_sources(
            [("user", text)], root, NOW, hashes,
            summarize or (lambda _s: SUMMARY),
        )


class AttachmentPathGuards(unittest.TestCase):
    def test_regular_path_rejects_outside_link_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "kok"
            root.mkdir()
            with self.assertRaises(ValueError):
                attachment_memory._regular_path(Path("C:/dis/dosya.txt"), root)
            hedef = Path(temporary) / "hedef.txt"
            hedef.write_text("x", encoding="utf-8")
            link = root / "link.txt"
            os.symlink(hedef, link)
            with self.assertRaises(ValueError):
                attachment_memory._regular_path(link, root)
            klasor = root / "klasor"
            klasor.mkdir()
            with self.assertRaises(ValueError):
                attachment_memory._regular_path(klasor, root)

    def test_mapping_and_note_path_identity_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with self.assertRaises(ValueError):
                attachment_memory._mapping_path(state, "kisa-id")
            with self.assertRaises(ValueError):
                attachment_memory._mapping_path(state, UUID1, "KISA")
            with self.assertRaises(ValueError):
                attachment_memory._note_path(Path("."), "yanlis/yol.md", UUID1, "a" * 64)
        self.assertFalse(attachment_memory._valid_date("2026-13-99"))

    def test_destination_parent_junction_is_rejected(self) -> None:
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            outside = Path(temporary) / "dis"
            outside.mkdir()
            subprocess.run(
                ["cmd", "/c", "mklink", "/J",
                 str(vault / attachment_memory.SOURCE_DIR.parts[0]), str(outside)],
                check=True, capture_output=True,
            )
            destination = vault / attachment_memory.SOURCE_DIR / "not.md"
            with self.assertRaises(ValueError):
                attachment_memory._validate_destination_parent(vault, destination)


class AttachmentMappingGuards(unittest.TestCase):
    def _valid_mapping(self) -> dict:
        return {
            "schema_version": attachment_memory.MAPPING_SCHEMA_VERSION,
            "attachment_id": UUID1,
            "status": "prepared",
            "source_attachment": f"{UUID1}/pasted-text.txt",
            "envelope_sha256": "a" * 64,
            "source_sanitized_sha256": "b" * 64,
            "source_sha256": "c" * 64,
            "note_sha256": "d" * 64,
            "suppression_revision": "e" * 64,
            "note_relative": (
                attachment_memory._note_relative(UUID1, "c" * 64).as_posix()
            ),
            "event_date": "2026-09-11",
            "redactions": [],
        }

    def test_load_mapping_rejects_each_corruption(self) -> None:
        base = self._valid_mapping()
        cases = [
            "{bozuk",
            [1],
            {**base, "schema_version": 99},
            {**base, "status": "zombi"},
            {**base, "source_attachment": "baska/pasted-text.txt"},
            {**base, "note_sha256": "KISA"},
            {**base, "note_relative": 5},
            {**base, "event_date": "2026-13-99"},
            {**base, "redactions": [1]},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:60]):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "eslesme.json"
                    path.write_text(
                        payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        attachment_memory._load_mapping(path, UUID1)

    def test_symlinked_mapping_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hedef = root / "gercek.json"
            hedef.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            os.symlink(hedef, link)
            with self.assertRaises(ValueError):
                attachment_memory._load_mapping(link, UUID1)


class AttachmentNoteGuards(unittest.TestCase):
    def test_read_note_missing_and_invalid_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = vault / "not.md"
            with self.assertRaises(FileNotFoundError):
                attachment_memory._read_note(
                    note, vault, expected_hash=None,
                    source_digest="a" * 64,
                    source_attachment=f"{UUID1}/pasted-text.txt",
                )
            note.write_bytes(bytes([255, 254, 250]))
            with self.assertRaises(ValueError):
                attachment_memory._read_note(
                    note, vault, expected_hash=None,
                    source_digest="a" * 64,
                    source_attachment=f"{UUID1}/pasted-text.txt",
                )
            for text in (
                "gövdesiz metin",
                "başlık" + attachment_memory.SOURCE_BODY + "fence yok",
                "başlık" + attachment_memory.SOURCE_BODY + "```\niçerik\nkapanış yok",
            ):
                note.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    attachment_memory._read_note(
                        note, vault, expected_hash=None,
                        source_digest="a" * 64,
                        source_attachment=f"{UUID1}/pasted-text.txt",
                    )

    def test_summary_from_model_enforces_budget(self) -> None:
        with self.assertRaises(ValueError):
            attachment_memory._summary_from_model(lambda _p: "u" * 8001, "kaynak")


class AttachmentCaptureGuards(unittest.TestCase):
    def test_envelope_shape_violations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            with self.assertRaises(ValueError):
                _capture(root, "# Files pasted by the user:\n\nistek bölümü yok")
            with self.assertRaises(ValueError):
                _capture(
                    root,
                    "# Files pasted by the user:\n\nref yok\n\n## My request:\n",
                )

    def test_source_path_shape_violations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            outside = Path(temporary) / "dis" / "pasted-text.txt"
            outside.parent.mkdir()
            outside.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                _capture(root, _envelope(outside))

            derin = root / "home" / "attachments" / UUID1 / "alt" / "pasted-text.txt"
            derin.parent.mkdir(parents=True)
            derin.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                _capture(root, _envelope(derin))

    def test_duplicate_refs_deduplicate_and_budget_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            summarize = mock.Mock(return_value=SUMMARY)
            result = _capture(root, _envelope(source, source), summarize)
            self.assertEqual(len(result), 1)
            self.assertEqual(summarize.call_count, 1)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = []
            for index in range(attachment_memory.MAX_SOURCES + 1):
                uuid = f"{index:08d}-1111-4111-8111-111111111111"
                sources.append(_seed(root, content=f"içerik {index}", uuid=uuid))
            with self.assertRaises(ValueError):
                _capture(root, _envelope(*sources))

    def test_oversized_source_and_blank_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root, content="a" * (attachment_memory.MAX_SOURCE_CHARS + 1))
            with self.assertRaises(ValueError):
                _capture(root, _envelope(
                    root / "home" / "attachments" / UUID1 / "pasted-text.txt"
                ))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root, content="   \n\t\n")
            summarize = mock.Mock(return_value=SUMMARY)
            self.assertEqual(_capture(root, _envelope(source), summarize), [])
            summarize.assert_not_called()

    def test_committed_mapping_without_note_or_source_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            first = _capture(root, _envelope(source))
            self.assertTrue(first)
            note = root / (first[0][0] + ".md")
            note.unlink()
            source.unlink()
            with self.assertRaises(ValueError):
                _capture(root, _envelope(source))

    def test_destination_conflicts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            first = _capture(root, _envelope(source))
            note = root / (first[0][0] + ".md")

            # Eşleme silinir, not klasöre dönüşür: not-geçersiz.
            for mapping in (root / "state" if (root / "state").exists() else root).rglob("*.json"):
                pass
            state_files = list(root.rglob("attachment-*.json"))
            for mapping_file in state_files:
                mapping_file.unlink()
            note_bytes = note.read_bytes()
            note.unlink()
            note.mkdir()
            with self.assertRaises(ValueError):
                _capture(root, _envelope(source))
            note.rmdir()

            # Not symlink olursa: hedef-bağlantısı reddi.
            gercek = root / "gercek-not.md"
            gercek.write_bytes(note_bytes)
            os.symlink(gercek, note)
            with self.assertRaises(ValueError):
                _capture(root, _envelope(source))
            note.unlink()

            # Eşleme yokken yerinde duran geçerli not miras alınır: model
            # çıktısı yok sayılır, saklanan özet döner (tasarım gereği).
            for mapping_file in root.rglob("attachment-memory-*.json"):
                mapping_file.unlink()
            note.write_bytes(note_bytes)
            different = SUMMARY.replace("Kaynak dersi.", "Bambaşka ders.")
            adopted = _capture(root, _envelope(source), summarize=lambda _s: different)
            self.assertIn("Kaynak dersi.", adopted[0][1])

            # Yayın penceresinde hedefe klasör/symlink beliren yarışlar.
            for mapping_file in root.rglob("attachment-memory-*.json"):
                mapping_file.unlink()
            note_backup = note.read_bytes()
            note.unlink()

            def klasor_diken(_prompt: str) -> str:
                note.mkdir()
                return SUMMARY

            with self.assertRaises(ValueError):
                _capture(root, _envelope(source), summarize=klasor_diken)
            note.rmdir()

            for mapping_file in root.rglob("attachment-memory-*.json"):
                mapping_file.unlink()
            gercek2 = root / "yaris-not.md"
            gercek2.write_bytes(note_backup)

            def link_diken(_prompt: str) -> str:
                os.symlink(gercek2, note)
                return SUMMARY

            with self.assertRaises(ValueError):
                _capture(root, _envelope(source), summarize=link_diken)

    def test_fully_suppressed_generated_summary_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            private = root / ".codex/private-memory"
            for heading in flush.EXPECTED_SECTIONS:
                suppress_derived_memory(private, "## " + heading)
            suppress_derived_memory(private, "Kaynak dersi.")
            hashes = load_suppressed_hashes(private)
            self.assertEqual(
                _capture(root, _envelope(source), hashes=hashes), []
            )

    def test_capture_core_path_guards_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attachment_root = root / "home" / "attachments"
            attachment_root.mkdir(parents=True)
            state = root / "state"
            state.mkdir()
            for value in (
                str(root / "dis" / "pasted-text.txt"),
                str(attachment_root / UUID1 / "alt" / "pasted-text.txt"),
            ):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        attachment_memory._capture_one_core(
                            value, attachment_root, root, NOW, frozenset(),
                            lambda _s: SUMMARY, state, None, "a" * 64, None,
                        )

    def test_committed_mapping_with_missing_note_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            first = _capture(root, _envelope(source))
            (root / (first[0][0] + ".md")).unlink()
            with self.assertRaises(ValueError):
                _capture(root, _envelope(source))

    def test_new_envelope_refreshes_committed_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            first = _capture(root, _envelope(source))
            # Aynı kaynak, farklı zarf: eşleme yeni zarf imzasıyla tazelenir.
            second_text = _envelope(source) + "\nek istek satırı\n"
            second = _capture(root, second_text)
        self.assertEqual(first[0][0], second[0][0])

    def test_recovered_source_fully_suppressed_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root, content="Bastırılacak kaynak satırı.")
            first = _capture(root, _envelope(source))
            self.assertTrue(first)
            source.unlink()
            suppress_derived_memory(
                root / ".codex/private-memory", "Bastırılacak kaynak satırı."
            )
            hashes = load_suppressed_hashes(root / ".codex/private-memory")
            self.assertEqual(_capture(root, _envelope(source), hashes=hashes), [])

    def test_note_planted_during_summary_is_adopted_when_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            first = _capture(root, _envelope(source))
            note = root / (first[0][0] + ".md")
            note_bytes = note.read_bytes()
            note.unlink()
            for mapping_file in root.rglob("attachment-memory-*.json"):
                mapping_file.unlink()

            def ayni_dosyayi_diken(_prompt: str) -> str:
                note.write_bytes(note_bytes)
                return SUMMARY

            result = _capture(root, _envelope(source), summarize=ayni_dosyayi_diken)
            self.assertTrue(result)

    def test_read_note_rejects_tail_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            first = _capture(root, _envelope(source))
            note = root / (first[0][0] + ".md")
            text = note.read_text(encoding="utf-8")
            import re as _re
            digest = _re.search(r"source_sha256: ([0-9a-f]{64})", text).group(1)

            def read(tampered: str):
                note.write_text(tampered, encoding="utf-8", newline="\n")
                return attachment_memory._read_note(
                    note, root, expected_hash=None,
                    source_digest=digest,
                    source_attachment=f"{UUID1}/pasted-text.txt",
                )

            self.assertTrue(read(text))
            with self.assertRaises(ValueError):
                read(text.rstrip("\n") + "\nfence sonrası kuyruk")
            with self.assertRaises(ValueError):
                read(text.replace(attachment_memory.SOURCE_BODY, "\n## Başka gövde\n"))
            with self.assertRaises(ValueError):
                read(text.replace("Kalıcı kaynak dersi.", "Değişmiş kaynak dersi!"))

    def test_prepared_note_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            first = _capture(root, _envelope(source))
            self.assertTrue(first)
            mapping_files = list(root.rglob("attachment-*.json"))
            self.assertTrue(mapping_files)
            for mapping_file in mapping_files:
                payload = json.loads(mapping_file.read_text(encoding="utf-8"))
                payload["status"] = "prepared"
                payload["note_sha256"] = "0" * 64
                mapping_file.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                _capture(root, _envelope(source))


if __name__ == "__main__":
    unittest.main()
