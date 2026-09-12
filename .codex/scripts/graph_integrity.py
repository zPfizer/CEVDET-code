from __future__ import annotations

import stat
from pathlib import Path
import re
from typing import Sequence

from markdown_boundary import (
    FENCE_LINE as _FENCE_LINE,
    _blank,
    _blank_inline_code,
    is_escaped as _is_escaped,
    markdown_body as _markdown_body,
    wikilinks as _wikilinks,
)
from knowledge_schema import (
    _connects,
    _source_status,
    canonical_concept_target,
    parse_frontmatter,
    wikilink_target,
)
from state_store import atomic_write_text
from vault_corpus import NoteIndex, by_key, by_stem, resolve_link


DAILY_GRAPH_LINK = "[[knowledge/index|Bilgi Tabanı]]"
# Infrastructure markdown that lives in the vault but is not a graph node.
GRAPH_EXCLUDED_DIRS = frozenset({".agents", ".scratch", "docs", "tasks"})


class GraphPolicyError(ValueError):
    pass


_DAILY_HEADING = re.compile(r"(?m)^[ \t]{0,3}#[ \t]+Günlük Log:[^\r\n]*\r?$")
_CONNECTION_HEADING = re.compile(
    r"(?m)^[ \t]{0,3}##[ \t]+Bağlantı[ \t]*(?:#+[ \t]*)?\r?$"
)


def _insert_after_heading(
    text: str,
    match: re.Match[str],
    newline: str,
    content: str,
) -> str:
    line_end = text.find("\n", match.end())
    line_end = len(text) if line_end < 0 else line_end + 1
    return text[:line_end] + newline + content + newline + text[line_end:]


def daily_with_graph_link(text: str, date_text: str, newline: str) -> str:
    if not text:
        return (
            f"# Günlük Log: {date_text}{newline}{newline}"
            f"{DAILY_GRAPH_LINK}{newline}{newline}## Oturumlar{newline}"
        )
    body = _markdown_body(text)
    heading = _DAILY_HEADING.search(body)
    if heading is None:
        raise GraphPolicyError(f"daily-heading-invalid:{date_text}.md")
    if any(match.group(0) == DAILY_GRAPH_LINK for match in _wikilinks(body)):
        return text
    return _insert_after_heading(text, heading, newline, DAILY_GRAPH_LINK)


def ensure_daily_graph_link(path: Path) -> bool:
    if not path.is_file():
        return False
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    updated = daily_with_graph_link(text, path.stem, newline)
    if updated == text:
        return False
    atomic_write_text(path, updated, newline="\n")
    return True


def normalize_connection_links(root: Path) -> int:
    knowledge = root / "knowledge"
    knowledge_status = _source_status(knowledge, root, stat.S_ISDIR)
    if knowledge_status == "missing":
        return 0
    if knowledge_status:
        raise GraphPolicyError(f"{knowledge_status}:{knowledge.relative_to(root).as_posix()}")
    connections = root / "knowledge" / "connections"
    connections_status = _source_status(connections, root, stat.S_ISDIR)
    if connections_status == "missing":
        return 0
    if connections_status:
        raise GraphPolicyError(
            f"{connections_status}:{connections.relative_to(root).as_posix()}"
        )
    concepts = root / "knowledge" / "concepts"
    concepts_status = _source_status(concepts, root, stat.S_ISDIR)
    if concepts_status not in ("", "missing"):
        raise GraphPolicyError(
            f"{concepts_status}:{concepts.relative_to(root).as_posix()}"
        )
    pending: list[tuple[Path, str]] = []
    for path in sorted(connections.rglob("*.md")):
        path_status = _source_status(path, root, stat.S_ISREG)
        if path_status:
            raise GraphPolicyError(
                f"{path_status}:{path.relative_to(root).as_posix()}"
            )
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        newline = "\r\n" if b"\r\n" in raw else "\n"
        slugs = _connects(text)
        if slugs is None:
            raise GraphPolicyError(f"connection-connects:{path.name}")
        for slug in slugs:
            concept = concepts / f"{slug}.md"
            concept_status = _source_status(concept, root, stat.S_ISREG)
            if concept_status == "missing":
                raise GraphPolicyError(
                    f"connection-concept-missing:{path.name}:{slug}"
                )
            if concept_status:
                raise GraphPolicyError(
                    f"{concept_status}:{concept.relative_to(root).as_posix()}"
                )
        body = _markdown_body(text)
        heading = _CONNECTION_HEADING.search(body)
        if heading is None:
            raise GraphPolicyError(f"connection-heading-missing:{path.name}")
        linked = {
            wikilink_target(match.group(1)) for match in _wikilinks(body)
        }
        missing = [
            slug for slug in slugs if canonical_concept_target(slug) not in linked
        ]
        if not missing:
            continue
        links = []
        for slug in missing:
            concept = concepts / f"{slug}.md"
            title_value = parse_frontmatter(
                concept.read_text(encoding="utf-8")
            ).get("title")
            title = title_value if isinstance(title_value, str) and title_value else slug
            links.append(f"[[knowledge/concepts/{slug}|{title}]]")
        updated = _insert_after_heading(
            text,
            heading,
            newline,
            " ↔ ".join(links),
        )
        pending.append((path, updated))
    for path, _updated in pending:
        path_status = _source_status(path, root, stat.S_ISREG)
        if path_status:
            raise GraphPolicyError(
                f"{path_status}:{path.relative_to(root).as_posix()}"
            )
    for path, updated in pending:
        atomic_write_text(path, updated, newline="\n")
    return len(pending)


def graph_notes(notes: Sequence[NoteIndex]) -> list[NoteIndex]:
    return [
        note
        for note in notes
        if not GRAPH_EXCLUDED_DIRS.intersection(note.relative.parts)
    ]


def graph_summary(notes: Sequence[NoteIndex]) -> tuple[int, list[str]]:
    files = graph_notes(notes)
    keyed = by_key(files)
    stems = by_stem(files)
    degree = {note.key: 0 for note in files}
    for source in files:
        for match in _wikilinks(
            _markdown_body(source.text, mask_frontmatter=False)
        ):
            target = wikilink_target(match.group(1))
            if not target:
                continue
            note = resolve_link(target, keyed, stems)
            if note is None or note is source:
                continue
            degree[source.key] += 1
            degree[note.key] += 1
    isolated = sorted(key for key, count in degree.items() if count == 0)
    return len(files), isolated
