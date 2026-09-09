"""Single owner of "what counts as a vault note": one traversal, one exclusion set.

Every audit consumer (link, graph, metadata, tag, archive) takes the index this module
builds and filters it; nobody walks the vault on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import ntpath
import os
import posixpath
import stat
from typing import Iterable, Iterator, Sequence

from knowledge_schema import parse_frontmatter


# The one exclusion set: infrastructure directories that never hold vault notes.
EXCLUDED_DIRS = frozenset({".git", ".codex", ".obsidian", ".scratch", ".agents", ".code-review-graph", "node_modules", "__pycache__"})
HUMAN_NOTE_ROOTS = (
    "📥 000-Inbox",
    "🎯 100-Command-Center",
    "⚔️ 200-Goals",
    "🏰 300-Projects",
    "🔐 400-Vault",
    "🧠 500-Knowledge",
    "🛠️ 600-Arsenal",
    "💪 700-Body",
    "🧘 800-Mind",
    "📦 900-Archive",
    "📋 Templates",
)
DAILY_ROOT = "daily"
KNOWLEDGE_ROOT = "knowledge"
ARCHIVE_ROOT = "📦 900-Archive"
TEMPLATES_ROOT = "📋 Templates"
COMPANION_ROOT = "🔮 850-Companion"

# Şablonlar not değildir: intake sözleşmesi onları saymaz.
INTAKE_CONTENT_ROOTS = frozenset(HUMAN_NOTE_ROOTS) - {TEMPLATES_ROOT}

# 2026-09-05: geçmiş ve şablonlar da bulunabilir; durumları güncel kanıttan ayrılır.
RETRIEVAL_EXCLUDED_ROOTS = ()
RETRIEVAL_CONTENT_ROOTS = tuple(
    root for root in HUMAN_NOTE_ROOTS if root not in RETRIEVAL_EXCLUDED_ROOTS
) + (
    KNOWLEDGE_ROOT,
    COMPANION_ROOT,
    DAILY_ROOT,
)


@dataclass(frozen=True, eq=False)
class NoteIndex:
    """One markdown file, read and parsed once per run."""

    path: Path
    relative: PurePosixPath
    text: str
    frontmatter: dict[str, str | list[str]] = field(default_factory=dict)
    error: str = ""

    @property
    def root(self) -> str:
        """Top-level vault directory, or "" for a note in the vault root."""
        parts = self.relative.parts
        return parts[0] if len(parts) > 1 else ""

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def key(self) -> str:
        return self.relative.as_posix()


def markdown_paths(
    root: Path,
    *,
    excluded_root_dirs: frozenset[str] = frozenset(),
    suffixes: frozenset[str] = frozenset({".md"}),
) -> Iterator[Path]:
    """Shared traversal: never descend into infrastructure or linked directories."""
    if root.is_symlink() or root.is_junction():
        return
    accepted_suffixes = frozenset(item.casefold() for item in suffixes)
    def read_error(error: OSError) -> None:
        raise error
    for current, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=read_error):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if name.casefold() not in EXCLUDED_DIRS
            and not (current_path == root and name.casefold() in excluded_root_dirs)
            and not (current_path / name).is_symlink()
            and not (current_path / name).is_junction()
        ]
        for name in files:
            path = current_path / name
            if path.suffix.lower() not in accepted_suffixes:
                continue
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            yield path


def vault_notes(
    vault: Path,
    *,
    paths: Iterable[Path] | None = None,
) -> tuple[NoteIndex, ...]:
    """Every markdown note under ``vault``, read once, in path order."""
    root = vault.resolve()
    notes: list[NoteIndex] = []
    paths = (
        markdown_paths(root, excluded_root_dirs=frozenset({"tmp"}))
        if paths is None
        else paths
    )
    for path in paths:
        if path.suffix.casefold() != ".md":
            continue
        if path.is_relative_to(root / COMPANION_ROOT / "Sources"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            error = ""
        except (OSError, UnicodeError) as exc:
            text, error = "", exc.__class__.__name__
        notes.append(
            NoteIndex(
                path,
                PurePosixPath(path.relative_to(root).as_posix()),
                text,
                parse_frontmatter(text),
                error,
            )
        )
    notes.sort(key=lambda note: note.path)
    return tuple(notes)


def by_key(notes: Sequence[NoteIndex]) -> dict[str, NoteIndex]:
    return {note.key: note for note in notes}


def by_stem(notes: Sequence[NoteIndex]) -> dict[str, list[NoteIndex]]:
    index: dict[str, list[NoteIndex]] = {}
    for note in notes:
        index.setdefault(note.stem, []).append(note)
    return index


def link_key(target: str) -> str | None:
    """Vault-relative ``.md`` path a wikilink target names, or None if it leaves the vault."""
    cleaned = posixpath.normpath(target)
    if ntpath.isabs(cleaned) or cleaned == "." or cleaned.split("/", 1)[0] == "..":
        return None
    candidate = PurePosixPath(cleaned)
    if candidate.suffix.lower() != ".md":
        candidate = candidate.with_suffix(".md")
    return candidate.as_posix()


def resolve_link(
    target: str,
    keyed: dict[str, NoteIndex],
    stems: dict[str, list[NoteIndex]],
) -> NoteIndex | None:
    """The note a wikilink points at: exact path first, then an unambiguous name match."""
    key = link_key(target)
    if key is None:
        return None
    note = keyed.get(key)
    if note is not None:
        return note
    candidates = stems.get(PurePosixPath(target).name, ())
    return candidates[0] if len(candidates) == 1 else None
