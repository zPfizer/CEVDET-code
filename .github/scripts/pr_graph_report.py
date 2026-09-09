"""Build a bounded Code Review Graph report for one pull request.

The script only asks Code Review Graph to parse source files. It never imports
or executes repository product code.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable


GRAPH_VERSION = "2.3.8"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
REPORT_MARKER = "<!-- cevdet-pr-graph-report -->"


class ReportError(RuntimeError):
    """A report could not be produced safely."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReportError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _sha(value: str, label: str) -> str:
    value = value.strip().lower()
    if not SHA_RE.fullmatch(value):
        raise ReportError(f"{label} is not a full commit SHA")
    return value


def _inline(value: Any) -> str:
    """Keep graph names and summaries safe in a Markdown comment."""
    return str(value).replace("`", "'").replace("\r", " ").replace("\n", " ").strip()


def _changed_files(repo: Path, base: str, head: str) -> list[str]:
    raw = _git(repo, "diff", "--name-only", "-z", base, head, "--")
    return [path for path in raw.split("\0") if path]


def _graph_db(repo: Path) -> Path:
    return repo / ".code-review-graph" / "graph.db"


def _summary_value(result: dict[str, Any], key: str, default: Any = 0) -> Any:
    value = result.get(key, default)
    return default if value is None else value


def _shown_count(result: dict[str, Any], rows_key: str, total_key: str, limit: int) -> str:
    rows = result.get(rows_key) or []
    total = result.get(total_key, len(rows))
    return f"gösterilen {min(limit, len(rows))} / toplam {_inline(total)}"


def _without_context_savings(result: dict[str, Any]) -> dict[str, Any]:
    """Do not present the upstream optional estimate as measured benefit."""
    visible = dict(result)
    visible.pop("context_savings", None)
    return visible


def _build_payload(
    repo: Path,
    base_sha: str,
    head_sha: str,
    *,
    build_func: Callable[..., dict[str, Any]] | None = None,
    detect_func: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base_sha = _sha(base_sha, "event base.sha")
    head_sha = _sha(head_sha, "event head.sha")

    checked_out = _sha(_git(repo, "rev-parse", "HEAD"), "checked-out HEAD")
    if checked_out != head_sha:
        raise ReportError(
            f"checked-out HEAD {checked_out} does not match event head.sha {head_sha}"
        )
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ReportError("checkout has unexpected working-tree changes")

    merge_base = _sha(_git(repo, "merge-base", base_sha, head_sha), "merge-base")
    changed_files = _changed_files(repo, merge_base, head_sha)
    if not changed_files:
        raise ReportError(
            f"no changed files between merge-base {merge_base} and head {head_sha}"
        )

    if build_func is None or detect_func is None:
        try:
            import code_review_graph
            from code_review_graph import tools
        except ImportError as exc:  # pragma: no cover - CI setup failure
            raise ReportError(f"code-review-graph import failed: {exc}") from exc
        if getattr(code_review_graph, "__version__", None) != GRAPH_VERSION:
            raise ReportError(
                "unexpected code-review-graph version: "
                f"{getattr(code_review_graph, '__version__', None)!r}"
            )
        build_func = tools.build_or_update_graph
        detect_func = tools.detect_changes_func

    build = build_func(
        full_rebuild=True,
        repo_root=str(repo),
        postprocess="full",
    )
    if not isinstance(build, dict) or build.get("status") != "ok":
        raise ReportError(f"graph build failed: {build!r}")
    if not isinstance(build.get("files_parsed"), int) or build["files_parsed"] <= 0:
        raise ReportError(f"graph build parsed no files: {build!r}")
    if not isinstance(build.get("total_nodes"), int) or build["total_nodes"] <= 0:
        raise ReportError(f"graph build produced no nodes: {build!r}")
    if build.get("errors"):
        raise ReportError(f"graph build reported parse errors: {build['errors']!r}")
    graph_db = _graph_db(repo)
    if not graph_db.is_file() or graph_db.stat().st_size <= 0:
        raise ReportError(f"graph database missing or empty: {graph_db}")

    analysis = detect_func(
        base=merge_base,
        changed_files=changed_files,
        repo_root=str(repo),
        detail_level="standard",
        max_results=50,
        max_flows=20,
    )
    if not isinstance(analysis, dict) or analysis.get("status") != "ok":
        raise ReportError(f"detect_changes failed: {analysis!r}")

    visible_analysis = _without_context_savings(analysis)
    return {
        "status": "ok",
        "graph_version": GRAPH_VERSION,
        "repo_root": repo.as_posix(),
        "event_base_sha": base_sha,
        "event_head_sha": head_sha,
        "merge_base": merge_base,
        "changed_files": changed_files,
        "build": build,
        "analysis": visible_analysis,
    }


def _render_failure(error: str, *, base_sha: str, head_sha: str) -> str:
    error = _inline(error)
    if len(error) > 4000:
        error = error[:4000] + " … hata çıktısı kesildi; tam hata JSON artifact içinde."
    return "\n".join(
        (
            REPORT_MARKER,
            "## Code Review Graph PR raporu",
            "",
            "- Durum: **başarısız**",
            f"- Hata: `{error}`",
            f"- Event base.sha: `{_inline(base_sha)}`",
            f"- Event head.sha: `{_inline(head_sha)}`",
            "",
            "Graph raporu tamamlanmadı; bu çıktı başarılı analiz olarak yorumlanmamalıdır.",
        )
    ) + "\n"


def render_markdown(payload: dict[str, Any]) -> str:
    """Render only bounded, human-reviewable fields from a successful result."""
    analysis = payload["analysis"]
    prefix = payload.get("repo_root", "").rstrip("/") + "/" if payload.get("repo_root") else ""
    lines = [
        REPORT_MARKER,
        "## Code Review Graph PR raporu",
        "",
        "- Durum: **başarılı**",
        f"- Graph sürümü: `{_inline(payload['graph_version'])}`",
        f"- HEAD: `{_inline(payload['event_head_sha'])}`",
        f"- Karşılaştırma tabanı: `{_inline(payload['merge_base'])}`",
        "",
        "Bu çıktı kaynak koddan üretilmiş **statik aday** sinyalleridir; insan incelemesi ve gerçek test sonucu yerine geçmez.",
        "Risk skoru bilgilendiricidir; merge gate değildir.",
        "Bu rapor model tokenı, kota veya gerçek dünya tasarrufu ölçmez.",
        "",
        "### Değişiklik sinyali",
        f"- Dosyalar: `{_inline(_summary_value(analysis, 'changed_file_count', len(payload['changed_files'])))}`",
        f"- Fonksiyon/sınıf toplamı: `{_inline(analysis.get('changed_functions_total', len(analysis.get('changed_functions', []))))}`",
        f"- Etkilenen akış: `{_shown_count(analysis, 'affected_flows', 'affected_flows_total', 5)}`",
        f"- Risk skoru: `{_inline(_summary_value(analysis, 'risk_score', 0))}`",
    ]
    if analysis.get("truncated") or analysis.get("functions_truncated"):
        lines.append("- Graph sonucu sınırlandı; yukarıdaki listeler tam sonuç değildir.")

    priorities = analysis.get("review_priorities") or []
    if priorities:
        lines.extend(("", "### Statik adaylar (" + _shown_count(analysis, "review_priorities", "review_priorities_total", 10) + ")"))
        for row in priorities[:10]:
            if isinstance(row, dict):
                name = row.get("qualified_name") or row.get("name") or "(adı yok)"
                score = row.get("risk_score", "?")
            else:
                name, score = row, "?"
            lines.append(f"- `{_inline(str(name).removeprefix(prefix))}` — risk `{_inline(score)}`")
    else:
        lines.extend(("", "### Statik adaylar", "- Graph değişen sembol için aday üretmedi."))

    flows = analysis.get("affected_flows") or []
    if flows:
        lines.extend(("", "### Etkilenen akışlar"))
        for row in flows[:5]:
            if isinstance(row, dict):
                name = row.get("name") or row.get("id") or "(adı yok)"
                entry_point = row.get("entry_point_id")
                suffix = (
                    f" — entry_point_id `{_inline(entry_point)}`"
                    if entry_point is not None else ""
                )
            else:
                name, suffix = row, ""
            lines.append(f"- `{_inline(name)}`{suffix}")

    gaps = analysis.get("test_gaps") or []
    lines.extend(("", "### Test sinyali (" + _shown_count(analysis, "test_gaps", "test_gaps_total", 10) + ")"))
    if gaps:
        for row in gaps[:10]:
            if isinstance(row, dict):
                name = row.get("qualified_name") or row.get("name") or "(adı yok)"
            else:
                name = row
            lines.append(f"- `{_inline(str(name).removeprefix(prefix))}` — tests_for ilişkisi bulunamadı (statik aday).")
    lines.append("- tests_for ilişkisinin bulunmaması test yokluğunun kanıtı değildir.")

    lines.extend(
        (
            "",
            "Silinen veya tamamen kaldırılmış semboller HEAD grafında görünmeyebilir; bu nedenle yokluk sonucu silme kanıtı değildir.",
        )
    )
    return "\n".join(lines) + "\n"


def _write_outputs(payload: dict[str, Any], markdown_path: Path, json_path: Path) -> None:
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = (
        render_markdown(payload)
        if payload.get("status") == "ok"
        else _render_failure(
            str(payload.get("error", "unknown error")),
            base_sha=str(payload.get("event_base_sha", "unknown")),
            head_sha=str(payload.get("event_head_sha", "unknown")),
        )
    )
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    json_path = args.json_output or args.output.with_suffix(".json")

    try:
        payload = _build_payload(
            args.repo_root.resolve(), args.base_sha, args.head_sha,
        )
    except Exception as exc:
        payload = {
            "status": "error",
            "error": str(exc),
            "event_base_sha": args.base_sha,
            "event_head_sha": args.head_sha,
        }
        _write_outputs(payload, args.output, json_path)
        return 1

    _write_outputs(payload, args.output, json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
