from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import TestCase, mock

import _fixtures  # noqa: F401  # sys.path seam

import knowledge_schema  # noqa: E402


def _concept(title: str, related: tuple[str, str]) -> str:
    return f"""---
title: {title}
aliases: []
tags: [doğrulama]
sources: [2026-08-27.md]
created: 2026-08-27
updated: 2026-08-27
---
# {title}

Çekirdek açıklama.

## Önemli Noktalar

- Bir
- İki
- Üç

## Detaylar

Detay.

## İlgili Kavramlar

- [[{related[0]}]] ilişkisi.
- [[{related[1]}]] ilişkisi.

## Kaynaklar

- 2026-08-27.md
"""


def _write_control_tree(root: Path) -> None:
    concepts = root / "knowledge" / "concepts"
    connections = root / "knowledge" / "connections"
    concepts.mkdir(parents=True)
    connections.mkdir()
    (concepts / "ornek.md").write_text(
        _concept("Örnek", ("ikinci", "ornek")), encoding="utf-8"
    )
    (concepts / "ikinci.md").write_text(
        _concept("İkinci", ("ornek", "ikinci")), encoding="utf-8"
    )
    (connections / "ikinci--ornek.md").write_text(
        """---
connects: [ikinci, ornek]
---
# İkinci ve Örnek

## Bağlantı

[[knowledge/concepts/ikinci\\|İkinci]] ↔ [[knowledge/concepts/ornek\\|Örnek]]

## Ana Fikir

Bağ.
""",
        encoding="utf-8",
    )
    (root / "knowledge" / "index.md").write_text(
        """# Bilgi Tabanı: İndeks

| Makale | Özet | Kaynak | Güncellendi |
| --- | --- | --- | --- |
| [[concepts/ikinci\\|İkinci]] | Özet. | 2026-08-27.md | 2026-08-27 |
| [[concepts/ornek\\|Örnek]] | Özet. | 2026-08-27.md | 2026-08-27 |
""",
        encoding="utf-8",
    )
    (root / "knowledge" / "log.md").write_text(
        "# Derleme Günlüğü\n", encoding="utf-8"
    )


def _record_reads() -> tuple[list[Path], mock._patch]:
    seen: list[Path] = []
    original = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        seen.append(path)
        return original(path, *args, **kwargs)

    return seen, mock.patch.object(Path, "read_text", new=read_text)


class KnowledgeSchemaPathTests(TestCase):
    def test_real_knowledge_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_control_tree(root)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }
            report = knowledge_schema.validate_knowledge_tree(root)
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }

        self.assertEqual(report.issues, ())
        self.assertEqual((report.concepts, report.connections, report.index_rows), (2, 1, 2))
        self.assertEqual(after, before)

    def test_linked_concept_is_rejected_before_external_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_control_tree(root)
            outside = root.parent / "outside-concept.md"
            outside.write_text("SENTINEL", encoding="utf-8")
            linked = root / "knowledge" / "concepts" / "escaped.md"
            try:
                linked.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            seen, reads = _record_reads()
            with reads:
                report = knowledge_schema.validate_knowledge_tree(root)

        self.assertIn("knowledge/concepts/escaped.md:source-symlink", report.issues)
        self.assertNotIn(linked, seen)
        self.assertNotIn(outside, seen)
        self.assertEqual(report.concepts, 2)

    def test_linked_source_files_are_rejected_before_read(self) -> None:
        cases = ("connections/escaped.md", "index.md", "log.md")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_control_tree(root)
                source = root / "knowledge" / case
                outside = root.parent / f"outside-{case.replace('/', '-')}-{root.name}.md"
                outside.write_text("SENTINEL", encoding="utf-8")
                if source.exists():
                    source.unlink()
                try:
                    source.symlink_to(outside)
                except OSError as exc:
                    self.skipTest(f"symlink unavailable: {exc}")

                seen, reads = _record_reads()
                with reads:
                    report = knowledge_schema.validate_knowledge_tree(root)

            relative = source.relative_to(root).as_posix()
            self.assertIn(f"{relative}:source-symlink", report.issues)
            self.assertNotIn(source, seen)
            self.assertNotIn(outside, seen)

    def test_linked_knowledge_directories_are_rejected_before_enumeration(self) -> None:
        for directory in ("concepts", "connections"):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_control_tree(root)
                source = root / "knowledge" / directory
                shutil.rmtree(source)
                outside = root.parent / f"outside-{directory}-{root.name}"
                outside.mkdir()
                sentinel = outside / "sentinel.md"
                sentinel.write_text("SENTINEL", encoding="utf-8")
                try:
                    source.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f"directory symlink unavailable: {exc}")

                seen, reads = _record_reads()
                with reads:
                    report = knowledge_schema.validate_knowledge_tree(root)

            relative = source.relative_to(root).as_posix()
            self.assertIn(f"{relative}:source-symlink", report.issues)
            self.assertNotIn(sentinel, seen)

    def test_linked_knowledge_root_is_rejected_before_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_control_tree(root)
            source = root / "knowledge"
            shutil.rmtree(source)
            outside = root.parent / f"outside-knowledge-{root.name}"
            outside.mkdir()
            sentinel = outside / "sentinel.md"
            sentinel.write_text("SENTINEL", encoding="utf-8")
            try:
                source.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            seen, reads = _record_reads()
            with reads:
                report = knowledge_schema.validate_knowledge_tree(root)

        self.assertIn("knowledge:source-symlink", report.issues)
        self.assertNotIn(sentinel, seen)
        self.assertNotIn("knowledge/index.md:missing", report.issues)

    def test_dangling_link_is_not_reported_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_control_tree(root)
            source = root / "knowledge" / "index.md"
            source.unlink()
            try:
                source.symlink_to(root.parent / "missing-index.md")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            issues = knowledge_schema.validate_knowledge_tree(root).issues

        self.assertIn("knowledge/index.md:source-symlink", issues)
        self.assertNotIn("knowledge/index.md:missing", issues)

    def test_unreadable_source_is_reported_without_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_control_tree(root)
            source = root / "knowledge" / "index.md"
            original_lstat = Path.lstat

            def lstat(path: Path) -> object:
                if path == source:
                    raise OSError("blocked")
                return original_lstat(path)

            with mock.patch.object(Path, "lstat", new=lstat):
                report = knowledge_schema.validate_knowledge_tree(root)

        self.assertIn("knowledge/index.md:source-unreadable", report.issues)

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_junctioned_knowledge_directory_is_rejected_before_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_control_tree(root)
            source = root / "knowledge" / "concepts"
            shutil.rmtree(source)
            outside = root.parent / f"outside-junction-{root.name}"
            outside.mkdir()
            sentinel = outside / "sentinel.md"
            sentinel.write_text("SENTINEL", encoding="utf-8")
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(source), str(outside)],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                self.skipTest(f"junction unavailable: {result.stdout} {result.stderr}")

            seen, reads = _record_reads()
            with reads:
                report = knowledge_schema.validate_knowledge_tree(root)

        self.assertIn("knowledge/concepts:source-symlink", report.issues)
        self.assertNotIn(sentinel, seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
