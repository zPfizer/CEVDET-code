from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sys


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import pr_graph_report as report  # noqa: E402


SHA_BASE = "a" * 40
SHA_HEAD = "b" * 40
SHA_MERGE = "c" * 40


class PullRequestGraphReportTests(unittest.TestCase):
    def test_failure_comment_bounds_large_parse_errors(self) -> None:
        rendered = report._render_failure("error\n" * 20000, base_sha=SHA_BASE, head_sha=SHA_HEAD)
        self.assertLess(len(rendered), 5000)
        self.assertIn("hata çıktısı kesildi", rendered)
        self.assertIn("başarısız", rendered)

    def test_renderer_labels_static_candidates_without_upstream_savings(self) -> None:
        payload = {
            "status": "ok",
            "graph_version": "2.3.8",
            "event_head_sha": SHA_HEAD,
            "merge_base": SHA_MERGE,
            "changed_files": ["src/example.py"],
            "analysis": {
                "changed_file_count": 1,
                "changed_functions_total": 3,
                "changed_functions": [{"name": "changed", "risk_score": 0.7}],
                "affected_flows_total": 6,
                "affected_flows": [{"name": f"Request flow {i}", "entry_point_id": i} for i in range(6)],
                "truncated": True,
                "risk_score": 0.7,
                "review_priorities": [
                    {"qualified_name": "src/example.py::changed", "risk_score": 0.7}
                ],
                "test_gaps": [{"qualified_name": "src/example.py::changed"}],
                "context_savings": {"saved_tokens": 999999, "saved_percent": 99},
            },
        }

        rendered = report.render_markdown(payload)

        self.assertIn("statik aday", rendered)
        self.assertIn("gösterilen 5 / toplam 6", rendered)
        self.assertNotIn("Request flow 5", rendered)
        self.assertIn("Request flow", rendered)
        self.assertIn("entry_point_id", rendered)
        self.assertIn("sınırlandı", rendered)
        self.assertIn("tests_for ilişkisi", rendered)
        self.assertIn("model tokenı, kota", rendered)
        self.assertNotIn("999999", rendered)
        self.assertNotIn("context_savings", rendered)

    def test_renderer_explains_tests_for_absence_is_not_test_absence(self) -> None:
        payload = {
            "status": "ok",
            "graph_version": "2.3.8",
            "event_head_sha": SHA_HEAD,
            "merge_base": SHA_MERGE,
            "changed_files": ["src/example.py"],
            "analysis": {
                "changed_file_count": 1,
                "changed_functions": [],
                "affected_flows": [],
                "risk_score": 0.0,
                "review_priorities": [],
                "test_gaps": [],
            },
        }

        self.assertIn(
            "tests_for ilişkisinin bulunmaması test yokluğunun kanıtı değildir",
            report.render_markdown(payload),
        )

    def test_empty_diff_is_a_report_failure(self) -> None:
        build = mock.Mock()
        detect = mock.Mock()
        with mock.patch.object(
            report,
            "_git",
            side_effect=[SHA_HEAD, "", SHA_MERGE, ""],
        ):
            with self.assertRaisesRegex(report.ReportError, "no changed files"):
                report._build_payload(
                    Path(tempfile.gettempdir()),
                    SHA_BASE,
                    SHA_HEAD,
                    build_func=build,
                    detect_func=detect,
                )
        build.assert_not_called()
        detect.assert_not_called()

    def test_graph_status_error_does_not_become_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "src").mkdir()
            (repo / "src/example.py").write_text("print('x')\n", encoding="utf-8")
            (repo / ".code-review-graph").mkdir()
            (repo / ".code-review-graph/graph.db").write_bytes(b"graph")

            with mock.patch.object(
                report,
                "_git",
                side_effect=[SHA_HEAD, "", SHA_MERGE, "src/example.py\0"],
            ):
                with self.assertRaisesRegex(report.ReportError, "graph build failed"):
                    report._build_payload(
                        repo,
                        SHA_BASE,
                        SHA_HEAD,
                        build_func=lambda **_: {"status": "error"},
                        detect_func=mock.Mock(),
                    )

    def test_detect_status_error_does_not_become_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "src").mkdir()
            (repo / "src/example.py").write_text("print('x')\n", encoding="utf-8")
            (repo / ".code-review-graph").mkdir()
            (repo / ".code-review-graph/graph.db").write_bytes(b"graph")

            with mock.patch.object(
                report,
                "_git",
                side_effect=[SHA_HEAD, "", SHA_MERGE, "src/example.py\0"],
            ):
                with self.assertRaisesRegex(report.ReportError, "detect_changes failed"):
                    report._build_payload(
                        repo,
                        SHA_BASE,
                        SHA_HEAD,
                        build_func=lambda **_: {
                            "status": "ok", "files_parsed": 1, "total_nodes": 1,
                        },
                        detect_func=lambda **_: {"status": "error", "error": "missing graph"},
                    )

    def test_empty_native_graph_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "src").mkdir()
            (repo / "src/example.py").write_text("print('x')\n", encoding="utf-8")
            (repo / ".code-review-graph").mkdir()
            (repo / ".code-review-graph/graph.db").write_bytes(b"sqlite")

            with mock.patch.object(
                report,
                "_git",
                side_effect=[SHA_HEAD, "", SHA_MERGE, "src/example.py\0"],
            ):
                with self.assertRaisesRegex(report.ReportError, "parsed no files"):
                    report._build_payload(
                        repo,
                        SHA_BASE,
                        SHA_HEAD,
                        build_func=lambda **_: {
                            "status": "ok", "files_parsed": 0, "total_nodes": 0,
                        },
                        detect_func=mock.Mock(),
                    )

    def test_success_payload_uses_native_result_without_context_savings_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "src").mkdir()
            (repo / "src/example.py").write_text("print('x')\n", encoding="utf-8")
            (repo / ".code-review-graph").mkdir()
            (repo / ".code-review-graph/graph.db").write_bytes(b"graph")

            with mock.patch.object(
                report,
                "_git",
                side_effect=[SHA_HEAD, "", SHA_MERGE, "src/example.py\0"],
            ):
                payload = report._build_payload(
                    repo,
                    SHA_BASE,
                    SHA_HEAD,
                    build_func=lambda **_: {
                        "status": "ok", "build_type": "full",
                        "files_parsed": 1, "total_nodes": 1,
                    },
                    detect_func=lambda **_: {
                        "status": "ok",
                        "risk_score": 0.2,
                        "changed_functions": [],
                        "affected_flows": [],
                        "test_gaps": [],
                        "review_priorities": [],
                        "context_savings": {"saved_tokens": 1, "saved_percent": 99},
                    },
                )

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["merge_base"], SHA_MERGE)
            self.assertNotIn("context_savings", payload["analysis"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
