from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Sequence

from file_lock import locked
from state_store import atomic_write_text
from vault_corpus import DAILY_ROOT, NoteIndex, vault_notes


SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_TAXONOMY_PATH = SCRIPT_DIR.parent / "tag-taxonomy.json"
TAGS_LINE = re.compile(r"^tags:\s*(.*?)\s*$")
BLOCK_TAG = re.compile(r"^\s+-\s+(.+?)\s*$")
INLINE_TAG = re.compile(r"(?<![\w#])#([^\s#\[\](){},;:!?]+)")
URL = re.compile(r"https?://[^\s)]+")
WIKILINK_ANCHOR = re.compile(r"\[\[#[^\]]+\]\]")
INLINE_CODE = re.compile(r"`[^`\r\n]*`")


class TaxonomyError(ValueError):
    """The taxonomy or a tag-bearing Markdown file is invalid."""


@dataclass(frozen=True)
class Taxonomy:
    canonical: frozenset[str]
    aliases: dict[str, str]
    scoped: dict[str, frozenset[str]]
    retired: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TagViolation:
    path: Path
    line: int
    tag: str
    canonical: str | None


@dataclass(frozen=True)
class InlineTagViolation:
    path: Path
    line: int
    tag: str


@dataclass(frozen=True)
class MigrationResult:
    changed: tuple[Path, ...]
    unknown: tuple[TagViolation, ...]
    patches: tuple[MigrationPatch, ...] = ()
    conflicts: tuple[MigrationConflict, ...] = ()


@dataclass(frozen=True)
class MigrationPatch:
    """One planned tag-only rewrite and the source identity it was based on."""

    path: Path
    source: str
    normalized: str
    source_sha256: str


@dataclass(frozen=True)
class MigrationConflict:
    """A planned rewrite whose source changed before its locked publish check."""

    path: Path
    expected_sha256: str
    current_sha256: str | None


@dataclass(frozen=True)
class _TagBlock:
    start: int
    end: int
    line: int
    tags: tuple[str, ...]
    style: str


def _valid_tag(tag: str) -> bool:
    return (
        bool(tag)
        and tag == tag.lower()
        and tag == tag.strip("-")
        and tag.replace("-", "").isalnum()
    )


def _valid_scoped_tag(tag: str) -> bool:
    parts = tag.split("/")
    return bool(parts) and all(_valid_tag(part) for part in parts)


def _valid_scope(scope: str) -> bool:
    normalized = scope.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    return bool(normalized) and not path.is_absolute() and ".." not in path.parts


def load_taxonomy(path: Path = DEFAULT_TAXONOMY_PATH) -> Taxonomy:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaxonomyError(f"taxonomy-unreadable:{exc.__class__.__name__}") from exc
    canonical_value = payload.get("canonical") if isinstance(payload, dict) else None
    aliases_value = payload.get("aliases") if isinstance(payload, dict) else None
    scoped_value = payload.get("scoped", {}) if isinstance(payload, dict) else None
    if (
        not isinstance(canonical_value, list)
        or not isinstance(aliases_value, dict)
        or not isinstance(scoped_value, dict)
    ):
        raise TaxonomyError("taxonomy-schema-invalid")
    if not all(isinstance(tag, str) and _valid_tag(tag) for tag in canonical_value):
        raise TaxonomyError("canonical-tag-invalid")
    canonical = frozenset(canonical_value)
    if len(canonical) != len(canonical_value):
        raise TaxonomyError("canonical-tag-duplicate")
    scoped: dict[str, frozenset[str]] = {}
    for raw_scope, raw_tags in scoped_value.items():
        if not isinstance(raw_scope, str) or not _valid_scope(raw_scope):
            raise TaxonomyError("scoped-path-invalid")
        if not isinstance(raw_tags, list) or not all(
            isinstance(tag, str) and _valid_scoped_tag(tag) for tag in raw_tags
        ):
            raise TaxonomyError(f"scoped-tag-invalid:{raw_scope}")
        tags = frozenset(raw_tags)
        if len(tags) != len(raw_tags):
            raise TaxonomyError(f"scoped-tag-duplicate:{raw_scope}")
        overlap = tags & canonical
        if overlap:
            raise TaxonomyError(f"scoped-tag-is-canonical:{sorted(overlap)[0]}")
        scope = PurePosixPath(raw_scope.replace("\\", "/").strip("/")).as_posix()
        scoped[scope] = tags
    aliases: dict[str, str] = {}
    scoped_tags = frozenset(tag for tags in scoped.values() for tag in tags)
    alias_targets = canonical | scoped_tags
    for alias, target in aliases_value.items():
        if not isinstance(alias, str) or not _valid_scoped_tag(alias):
            raise TaxonomyError("alias-tag-invalid")
        if not isinstance(target, str) or target not in alias_targets:
            raise TaxonomyError(f"alias-target-invalid:{alias}")
        if alias in alias_targets:
            raise TaxonomyError(f"alias-is-canonical:{alias}")
        aliases[alias] = target
    retired_value = payload.get("retired", [])
    if not isinstance(retired_value, list) or not all(
        isinstance(tag, str) and _valid_scoped_tag(tag) for tag in retired_value
    ):
        raise TaxonomyError("retired-tag-invalid")
    retired = frozenset(retired_value)
    if retired & (alias_targets | aliases.keys()):
        raise TaxonomyError("retired-tag-still-active")
    return Taxonomy(canonical, aliases, scoped, retired)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def _inline_tags(value: str) -> tuple[str, ...]:
    value = value.strip()
    if not value or value == "[]":
        return ()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
        return tuple(
            tag
            for item in value.split(",")
            if (tag := _strip_quotes(item))
        )
    tag = _strip_quotes(value)
    return (tag,) if tag else ()


def _find_tag_block(text: str) -> tuple[list[str], str, bool, _TagBlock | None]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return lines, newline, trailing_newline, None
    closing = next((index for index in range(1, len(lines)) if lines[index] == "---"), None)
    if closing is None:
        return lines, newline, trailing_newline, None
    for index in range(1, closing):
        match = TAGS_LINE.fullmatch(lines[index])
        if match is None:
            continue
        inline = match.group(1)
        if inline:
            return lines, newline, trailing_newline, _TagBlock(
                index,
                index + 1,
                index + 1,
                _inline_tags(inline),
                "inline",
            )
        tags: list[str] = []
        end = index + 1
        while end < closing:
            item = BLOCK_TAG.fullmatch(lines[end])
            if item is None:
                break
            tags.append(_strip_quotes(item.group(1)))
            end += 1
        return lines, newline, trailing_newline, _TagBlock(
            index,
            end,
            index + 1,
            tuple(tag for tag in tags if tag),
            "block",
        )
    return lines, newline, trailing_newline, None


def _allowed_tags(
    taxonomy: Taxonomy,
    relative_path: Path | PurePosixPath | None = None,
) -> frozenset[str]:
    allowed = set(taxonomy.canonical)
    if relative_path is None:
        return frozenset(allowed)
    relative = PurePosixPath(relative_path.as_posix())
    for scope, tags in taxonomy.scoped.items():
        scope_path = PurePosixPath(scope)
        if relative == scope_path or scope_path in relative.parents:
            allowed.update(tags)
    return frozenset(allowed)


def tag_violations(
    taxonomy: Taxonomy,
    relative_path: Path | PurePosixPath | None,
    tags: Sequence[str],
) -> tuple[str, ...]:
    """Tags that are not allowed for ``relative_path`` under ``taxonomy``."""
    allowed = _allowed_tags(taxonomy, relative_path)
    return tuple(tag for tag in tags if tag not in allowed)


def _canonical_tag(
    tag: str,
    taxonomy: Taxonomy,
    allowed: frozenset[str],
) -> str | None:
    if tag in allowed:
        return tag
    target = taxonomy.aliases.get(tag)
    return target if target in allowed else None


def normalize_markdown(
    text: str,
    taxonomy: Taxonomy,
    *,
    relative_path: Path | PurePosixPath | None = None,
) -> tuple[str, list[str]]:
    lines, newline, trailing_newline, block = _find_tag_block(text)
    if block is None or not block.tags:
        return text, []
    allowed = _allowed_tags(taxonomy, relative_path)
    normalized: list[str] = []
    unknown: list[str] = []
    for tag in block.tags:
        if tag in taxonomy.retired:
            continue
        canonical = _canonical_tag(tag, taxonomy, allowed)
        if canonical is None:
            if tag not in unknown:
                unknown.append(tag)
            continue
        if canonical not in normalized:
            normalized.append(canonical)
    if unknown:
        return text, unknown
    if tuple(normalized) == block.tags:
        return text, []
    if block.style == "inline":
        replacement = [f"tags: [{', '.join(normalized)}]"]
    else:
        replacement = ["tags:", *(f"  - {tag}" for tag in normalized)]
    updated = newline.join([*lines[: block.start], *replacement, *lines[block.end :]])
    if trailing_newline:
        updated += newline
    return updated, []


def tagged_notes(
    root: Path,
    notes: Sequence[NoteIndex] | None = None,
) -> list[NoteIndex]:
    """Notes the taxonomy governs: everything except the daily log."""
    corpus = vault_notes(root) if notes is None else notes
    _ensure_notes_readable(corpus)
    return [note for note in corpus if note.root != DAILY_ROOT]


def _ensure_notes_readable(notes: Sequence[NoteIndex]) -> None:
    unreadable = next((note for note in notes if note.error), None)
    if unreadable is not None:
        raise OSError(
            f"note-unreadable:{unreadable.key}:{unreadable.error}"
        )


def _violations(note: NoteIndex, taxonomy: Taxonomy) -> list[TagViolation]:
    _lines, _newline, _trailing, block = _find_tag_block(note.text)
    if block is None:
        return []
    relative = note.relative
    allowed = _allowed_tags(taxonomy, relative)
    violations = []
    for tag in tag_violations(taxonomy, relative, block.tags):
        target = taxonomy.aliases.get(tag)
        violations.append(
            TagViolation(note.path, block.line, tag, target if target in allowed else None)
        )
    return violations


def audit_vault(
    root: Path,
    taxonomy: Taxonomy,
    notes: Sequence[NoteIndex] | None = None,
) -> list[TagViolation]:
    violations = []
    for note in tagged_notes(root, notes):
        violations.extend(_violations(note, taxonomy))
    return violations


def _inline_tag_violations(note: NoteIndex) -> list[InlineTagViolation]:
    path = note.path
    lines = note.text.splitlines()
    violations: list[InlineTagViolation] = []
    in_frontmatter = bool(lines and lines[0] == "---")
    in_fence = False
    for line_number, source in enumerate(lines, start=1):
        stripped = source.strip()
        if in_frontmatter:
            if line_number > 1 and stripped == "---":
                in_frontmatter = False
            continue
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or re.match(r"^\s*#{1,6}\s+", source):
            continue
        scrubbed = INLINE_CODE.sub("", source)
        scrubbed = WIKILINK_ANCHOR.sub("", scrubbed)
        scrubbed = URL.sub("", scrubbed)
        for match in INLINE_TAG.finditer(scrubbed):
            tag = match.group(1).rstrip(".")
            if tag and any(char.isalpha() for char in tag):
                violations.append(InlineTagViolation(path, line_number, tag))
    return violations


def audit_inline_tags(
    root: Path,
    notes: Sequence[NoteIndex] | None = None,
) -> list[InlineTagViolation]:
    root = root.resolve(strict=True)
    ignored: tuple[str, ...] = ()
    app_config = root / ".obsidian" / "app.json"
    if app_config.is_file():
        try:
            payload = json.loads(app_config.read_text(encoding="utf-8"))
            configured = payload.get("userIgnoreFilters", [])
            if isinstance(configured, list):
                ignored = tuple(
                    item.replace("\\", "/").strip("/")
                    for item in configured
                    if isinstance(item, str) and item.strip("/\\")
                )
        except (OSError, json.JSONDecodeError):
            ignored = ()
    corpus = vault_notes(root) if notes is None else notes
    _ensure_notes_readable(corpus)
    violations: list[InlineTagViolation] = []
    for note in corpus:
        relative = note.key
        if any(
            relative == prefix or relative.startswith(f"{prefix}/")
            for prefix in ignored
        ):
            continue
        violations.extend(_inline_tag_violations(note))
    return violations


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_notes_current(root: Path, notes: Sequence[NoteIndex]) -> None:
    _ensure_notes_readable(notes)
    current_notes = vault_notes(root)
    _ensure_notes_readable(current_notes)
    captured = {note.key: note for note in notes}
    current = {note.key: note for note in current_notes}
    if captured.keys() != current.keys():
        raise OSError("note-set-changed")
    for key, note in captured.items():
        current_note = current[key]
        if _sha256_text(current_note.text) != _sha256_text(note.text):
            raise OSError(f"note-changed:{key}")


def _migration_plans(
    root: Path,
    taxonomy: Taxonomy,
    notes: Sequence[NoteIndex] | None = None,
) -> tuple[list[MigrationPatch], list[TagViolation]]:
    plans: list[MigrationPatch] = []
    unknown: list[TagViolation] = []
    for note in tagged_notes(root, notes):
        source = note.text
        normalized, missing = normalize_markdown(
            source,
            taxonomy,
            relative_path=note.relative,
        )
        if missing:
            _lines, _newline, _trailing, block = _find_tag_block(source)
            line = block.line if block is not None else 1
            unknown.extend(TagViolation(note.path, line, tag, None) for tag in missing)
        elif normalized != source:
            plans.append(
                MigrationPatch(
                    note.path,
                    source,
                    normalized,
                    _sha256_text(source),
                )
            )
    return plans, unknown


def migrate_vault(
    root: Path,
    taxonomy: Taxonomy,
    *,
    apply: bool,
    notes: Sequence[NoteIndex] | None = None,
) -> MigrationResult:
    """Plan user-note patches without publishing them.

    ``apply`` is retained for the existing caller contract.  When true it
    performs the final locked source check, but the live vault remains
    patch-only; only ``normalize_tree`` may write its private staging tree.
    """
    plans, unknown = _migration_plans(root, taxonomy, notes)
    if unknown:
        return MigrationResult((), tuple(unknown))
    conflicts: list[MigrationConflict] = []
    if apply:
        for plan in plans:
            with locked(plan.path):
                try:
                    current = plan.path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    current = None
                current_sha256 = None if current is None else _sha256_text(current)
                if current_sha256 != plan.source_sha256:
                    conflicts.append(
                        MigrationConflict(
                            plan.path,
                            plan.source_sha256,
                            current_sha256,
                        )
                    )
    return MigrationResult((), (), tuple(plans), tuple(conflicts))


def normalize_tree(root: Path, taxonomy: Taxonomy) -> int:
    plans, unknown = _migration_plans(root, taxonomy)
    if unknown:
        first = unknown[0]
        raise TaxonomyError(f"unknown-tag:{first.path.name}:{first.tag}")
    for plan in plans:
        atomic_write_text(plan.path, plan.normalized, keep_mode=True)
    return len(plans)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=VAULT_ROOT)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY_PATH)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Prepare tag patches and check source identity; never writes user notes.",
    )
    return parser.parse_args(argv)


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        safe = message.encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe)


def _print_patch(patch: MigrationPatch) -> None:
    _safe_print(f"PATCH\t{patch.path}\tsource_sha256={patch.source_sha256}")
    for line in difflib.unified_diff(
        patch.source.splitlines(),
        patch.normalized.splitlines(),
        fromfile=f"{patch.path} (source)",
        tofile=f"{patch.path} (normalized)",
        lineterm="",
    ):
        _safe_print(f"PATCH\t{line}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    migration_conflicts = False
    try:
        taxonomy = load_taxonomy(args.taxonomy)
        if args.apply:
            result = migrate_vault(args.root, taxonomy, apply=True)
            if result.unknown:
                for item in result.unknown:
                    _safe_print(f"UNKNOWN\t{item.path}:{item.line}\t{item.tag}")
                return 1
            for patch in result.patches:
                _print_patch(patch)
            for conflict in result.conflicts:
                current = conflict.current_sha256 or "<unavailable>"
                _safe_print(
                    f"CONFLICT\t{conflict.path}\t"
                    f"expected_sha256={conflict.expected_sha256}\t"
                    f"current_sha256={current}"
                )
            if result.conflicts:
                migration_conflicts = True
                _safe_print(f"CONFLICTS\t{len(result.conflicts)}")
            else:
                _safe_print(f"PATCH_READY\t{len(result.patches)}")
        notes = vault_notes(args.root)
        violations = audit_vault(args.root, taxonomy, notes)
        inline_violations = audit_inline_tags(args.root, notes)
        _ensure_notes_current(args.root, notes)
    except (OSError, UnicodeError, TaxonomyError) as exc:
        _safe_print(f"ERROR\t{exc}")
        return 1
    for violation in violations:
        target = violation.canonical or "<unknown>"
        _safe_print(
            f"VIOLATION\t{violation.path}:{violation.line}\t"
            f"{violation.tag}\t{target}"
        )
    for inline_violation in inline_violations:
        _safe_print(
            f"INLINE_VIOLATION\t{inline_violation.path}:{inline_violation.line}\t"
            f"{inline_violation.tag}"
        )
    _safe_print(f"CANONICAL\t{len(taxonomy.canonical)}")
    _safe_print(
        f"SCOPED\t{sum(len(tags) for tags in taxonomy.scoped.values())}"
    )
    _safe_print(f"VIOLATIONS\t{len(violations)}")
    _safe_print(f"INLINE_VIOLATIONS\t{len(inline_violations)}")
    return 1 if migration_conflicts or violations or inline_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
