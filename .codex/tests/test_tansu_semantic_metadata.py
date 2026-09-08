from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


VAULT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VAULT / ".codex" / "scripts"))

import _fixtures  # noqa: E402
import doctor  # noqa: E402
import tansu_semantic_metadata  # noqa: E402


FIXTURE_SCHEMA = {
    "max_tags_per_note": 5,
    "module_tags": {"E4-01": "tansu/teknik/sinyal-strateji"},
    "topic_rules": [{"patterns": ["formasyon"], "tag": "tansu/teknik/formasyon"}],
    "symbol_allowlist": ["ALARK"],
}

FIXTURE_ROWS = (
    {
        "record_id": "1",
        "source_file": "notes/001.md",
        "title": "ALARK formasyon taraması",
        "focus_lanes": "SIGNAL_STRATEGY",
        "supporting_lanes": "BACKTEST_VALIDATION",
        "workflow_roles": "BASELINE|VALIDATION_RULE",
        "primary_module_slot": "E4-01",
        "supporting_module_slots": "E7-01+E7-02",
    },
    {
        "record_id": "2",
        "source_file": "notes/002.md",
        "title": "Likidite notu",
        "focus_lanes": "SIGNAL_STRATEGY",
        "supporting_lanes": "",
        "workflow_roles": "BASELINE|VALIDATION_RULE",
        "primary_module_slot": "E4-01",
        "supporting_module_slots": "",
    },
)


def _note(
    *,
    title: str,
    tags: str,
    symbols: str,
    maturity: str,
    supporting_lanes: str,
    supporting_modules: str,
) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "record_kind: kaynak\n"
        "source_type: user-text\n"
        "timeframe: günlük\n"
        f"tags: {tags}\n"
        f"symbols: {symbols}\n"
        "timeframes: [günlük]\n"
        "evidence_types: [kaynak-metni]\n"
        "data_sources: [tansu-x]\n"
        f"maturity: {maturity}\n"
        "focus_lanes: [SIGNAL_STRATEGY]\n"
        f"supporting_lanes: {supporting_lanes}\n"
        "workflow_roles: [BASELINE, VALIDATION_RULE]\n"
        "primary_module: E4-01\n"
        f"supporting_modules: {supporting_modules}\n"
        "---\n"
        f"# {title}\n"
    )


def write_semantic_fixture(root: Path, *, drift: bool = False) -> tuple[Path, Path]:
    """Sentetik CSV + schema + iki not yazar; (csv, schema) döner."""
    schema_path = root / "schema.json"
    schema_path.write_text(
        json.dumps(FIXTURE_SCHEMA, ensure_ascii=False), encoding="utf-8"
    )
    notes = root / "notes"
    notes.mkdir()
    (notes / "001.md").write_text(
        _note(
            title="ALARK formasyon taraması",
            tags="[tansu/teknik/sinyal-strateji, tansu/teknik/formasyon]",
            symbols="[ALARK]",
            maturity="kaynak",
            supporting_lanes="[BACKTEST_VALIDATION]",
            supporting_modules="[E7-01, E7-02]",
        ),
        encoding="utf-8",
    )
    (notes / "002.md").write_text(
        _note(
            title="Likidite notu",
            tags="[tansu/teknik/sinyal-strateji]",
            symbols="[]",
            maturity="spec" if drift else "kaynak",
            supporting_lanes="[]",
            supporting_modules="[]",
        ),
        encoding="utf-8",
    )
    csv_path = root / "map.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIXTURE_ROWS[0]))
        writer.writeheader()
        writer.writerows(FIXTURE_ROWS)
    return csv_path, schema_path


def rewrite_source_file(csv_path: Path, source_file: str) -> None:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = tuple(rows[0])
    rows[0]["source_file"] = source_file
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class TansuSemanticFixtureTests(unittest.TestCase):
    """Makine-bağımsız: sentetik CSV manifest + audit'i uçtan uca sürer."""

    def test_numeric_tag_limit_compatibility_preserves_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path, schema_path = write_semantic_fixture(root)
            expected = tansu_semantic_metadata.build_manifest(root,
                semantic_csv=csv_path, schema_path=schema_path, expected_records=2)
            for value in ("5", 5.0):
                with self.subTest(value=value):
                    schema_path.write_text(json.dumps(dict(FIXTURE_SCHEMA,
                        max_tags_per_note=value)), encoding='utf-8')
                    actual = tansu_semantic_metadata.build_manifest(root,
                        semantic_csv=csv_path, schema_path=schema_path, expected_records=2)
                    self.assertEqual(actual, expected)

    def test_synthetic_csv_drives_manifest_and_clean_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            semantic_csv, schema_path = write_semantic_fixture(root)
            plans = tansu_semantic_metadata.build_manifest(
                root,
                semantic_csv=semantic_csv,
                schema_path=schema_path,
                expected_records=2,
            )
            audit = tansu_semantic_metadata.audit_manifest(
                root,
                semantic_csv=semantic_csv,
                schema_path=schema_path,
                expected_records=2,
            )

        self.assertEqual([plan.record_id for plan in plans], ["001", "002"])
        self.assertEqual(plans[0].path, root / "notes" / "001.md")
        self.assertEqual(
            plans[0].tags,
            ("tansu/teknik/sinyal-strateji", "tansu/teknik/formasyon"),
        )
        self.assertEqual(plans[0].symbols, ("ALARK",))
        self.assertEqual(plans[0].timeframes, ("günlük",))
        self.assertEqual(plans[0].supporting_modules, ("E7-01", "E7-02"))
        self.assertEqual(plans[1].tags, ("tansu/teknik/sinyal-strateji",))
        self.assertEqual((audit.matched, audit.total, audit.mismatches), (2, 2, ()))

    def test_drifted_note_lowers_the_measured_match_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            semantic_csv, schema_path = write_semantic_fixture(root, drift=True)
            audit = tansu_semantic_metadata.audit_manifest(
                root,
                semantic_csv=semantic_csv,
                schema_path=schema_path,
                expected_records=2,
            )

        self.assertEqual(audit.mismatches, ("002:maturity",))
        self.assertEqual((audit.matched, audit.total), (1, 2))

    def test_missing_live_csv_produces_an_explicit_skip_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "yok.csv"
            present = Path(temporary) / "var.csv"
            present.write_text("", encoding="utf-8")
            reason = _fixtures.tansu_csv_skip_reason(missing)
            self.assertIsNone(_fixtures.tansu_csv_skip_reason(present))

        self.assertIsNotNone(reason)
        self.assertIn(str(missing), reason or "")

    def test_manifest_preserves_valid_unicode_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            semantic_csv, schema_path = write_semantic_fixture(root)
            source = root / "notes" / "ünicode kaynak.md"
            source.write_text(_note(
                title="Unicode kaynak",
                tags="[tansu/teknik/sinyal-strateji]",
                symbols="[]",
                maturity="kaynak",
                supporting_lanes="[]",
                supporting_modules="[]",
            ), encoding="utf-8")
            rewrite_source_file(semantic_csv, "notes/ünicode kaynak.md")

            plans = tansu_semantic_metadata.build_manifest(
                root,
                semantic_csv=semantic_csv,
                schema_path=schema_path,
                expected_records=2,
            )

        self.assertEqual(plans[0].path, source)

    def test_manifest_rejects_source_path_escape_before_read_or_patch(self) -> None:
        for label in ("absolute", "parent", "symlink", "junction"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "vault"
                root.mkdir()
                semantic_csv, schema_path = write_semantic_fixture(root)
                outside = root.parent / f"{label}-outside.md"
                outside.write_bytes(b"\xff")
                if label == "absolute":
                    source_file = str(outside)
                elif label == "parent":
                    source_file = "../" + outside.name
                elif label == "symlink":
                    link = root / "notes" / "kaçış.md"
                    link.symlink_to(outside)
                    source_file = "notes/kaçış.md"
                else:
                    outside_dir = root.parent / "junction-outside"
                    outside_dir.mkdir()
                    (outside_dir / "001.md").write_bytes(b"\xff")
                    junction = root / "notes" / "junction"
                    if os.name == "nt":
                        result = subprocess.run(
                            ["cmd", "/c", "mklink", "/J", str(junction), str(outside_dir)],
                            capture_output=True,
                            text=True,
                        )
                        if result.returncode != 0:
                            self.fail(
                                "Windows junction creation unavailable: "
                                + (result.stderr or result.stdout)
                            )
                    else:
                        junction.symlink_to(outside_dir, target_is_directory=True)
                    source_file = "notes/junction/001.md"
                rewrite_source_file(semantic_csv, source_file)

                with self.assertRaisesRegex(ValueError, "semantic-source"):
                    plans = tansu_semantic_metadata.build_manifest(
                        root,
                        semantic_csv=semantic_csv,
                        schema_path=schema_path,
                        expected_records=2,
                    )
                    tansu_semantic_metadata.render_patch(plans)


class TansuDoctorEvidenceTests(unittest.TestCase):
    """Doctor kanıtı ölçümden gelir; sabit 226/226 string'i yok."""

    def _run_check(self, audit: object) -> doctor.Check:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            semantic_map = (
                vault
                / "🏰 300-Projects"
                / "Tansu X Veri Havuzu"
                / "tansu-semantik-kullanim-haritasi.md"
            )
            semantic_map.parent.mkdir(parents=True)
            semantic_map.write_text("# harita\n", encoding="utf-8")
            with mock.patch.object(
                doctor,
                "audit_tansu_semantic_manifest",
                return_value=audit,
            ) as audited:
                check = doctor._tansu_semantic_metadata_check(doctor.Context(vault))
            audited.assert_called_once_with(vault)
        return check

    def test_clean_audit_reports_the_measured_match_count(self) -> None:
        check = self._run_check(
            tansu_semantic_metadata.ManifestAudit(226, 226, ())
        )

        self.assertEqual(check.status, "OK")
        self.assertEqual(check.evidence, "226/226 manifest eşleşiyor")

    def test_missing_match_is_printed_as_the_real_ratio(self) -> None:
        check = self._run_check(
            tansu_semantic_metadata.ManifestAudit(225, 226, ("007:tags",))
        )

        self.assertEqual(check.status, "FAIL")
        self.assertEqual(check.evidence, "225/226 manifest eşleşiyor; ilk 007:tags")


if __name__ == "__main__":
    unittest.main(verbosity=2)
