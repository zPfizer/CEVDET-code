"""Doctor kontrol hataları raporu durdurmadan izole edilmelidir."""

from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (sys.path seam)

import doctor  # noqa: E402


class DoctorFailureIsolationTests(unittest.TestCase):
    def test_malformed_hook_check_fails_and_next_check_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            hooks = vault / ".codex" / "hooks.json"
            hooks.parent.mkdir()
            hooks.write_text("[]", encoding="utf-8")

            with mock.patch.object(
                doctor,
                "CHECKS",
                (
                    ("Codex kancaları", doctor._codex_hooks_check),
                    ("Python", doctor._python_check),
                ),
            ):
                checks = doctor.run_checks(vault, project_root=vault)

        self.assertEqual([check.name for check in checks], ["Codex kancaları", "Python"])
        self.assertEqual([check.status for check in checks], ["FAIL", "OK"])
        self.assertIn("AttributeError", checks[0].evidence)
        self.assertLessEqual(len(checks[0].evidence), 128)
        self.assertEqual(checks[1].evidence, sys.version.split()[0])

    def test_main_isolates_invalid_version_and_exits_nonzero(self) -> None:
        marker = "PRIVATE_VERSION_MARKER_9b7c"
        payload = bytes([0xFF]) + marker.encode("ascii")
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            version = vault / ".beyin-version"
            version.write_bytes(payload)
            script = vault / ".codex" / "scripts" / "doctor.py"
            script.parent.mkdir(parents=True)

            with (
                mock.patch.object(doctor, "__file__", str(script)),
                mock.patch.object(
                    doctor,
                    "CHECKS",
                    (
                        ("Sürüm", doctor._version_check),
                        ("Python", doctor._python_check),
                    ),
                ),
                mock.patch.object(sys, "stdout", io.StringIO()) as stdout,
            ):
                exit_code = doctor.main([])
                output = stdout.getvalue()

            self.assertEqual(version.read_bytes(), payload)

        rows = [
            line
            for line in output.splitlines()
            if line.startswith("|") and not line.startswith(("| Parça", "| ---"))
        ]
        fields = [row.strip().strip("|").strip().split(" | ") for row in rows]
        self.assertEqual([field[:2] for field in fields], [["Sürüm", "FAIL"], ["Python", "OK"]])
        self.assertEqual(exit_code, 1)
        self.assertIn("UnicodeDecodeError", fields[0][2])
        self.assertLessEqual(len(fields[0][2]), 128)
        self.assertNotIn(marker, output)
        self.assertNotIn("invalid start byte", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
