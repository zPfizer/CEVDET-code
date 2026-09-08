"""Validate newly created human content notes against the intake contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from typing import Sequence

import tag_taxonomy
from knowledge_schema import parse_frontmatter
from vault_corpus import INTAKE_CONTENT_ROOTS as _CONTENT_ROOTS


VAULT_ROOT = Path(__file__).resolve().parents[2]
_TYPE_ROOTS = {
    "source-note": frozenset(
        {"📥 000-Inbox", "🏰 300-Projects", "🛠️ 600-Arsenal"}
    ),
    "decision": frozenset(
        {
            "⚔️ 200-Goals",
            "🏰 300-Projects",
            "🔐 400-Vault",
            "🧠 500-Knowledge",
            "🛠️ 600-Arsenal",
            "📦 900-Archive",
        }
    ),
    "lesson": frozenset(
        {"🏰 300-Projects", "🧠 500-Knowledge", "🛠️ 600-Arsenal", "📦 900-Archive"}
    ),
    "project-marker": frozenset({"🏰 300-Projects"}),
    "work-packet": frozenset({"🎯 100-Command-Center"}),
    "work-index": frozenset({"🎯 100-Command-Center"}),
    "archive-index": frozenset({"📦 900-Archive"}),
    "policy": frozenset({"🛠️ 600-Arsenal"}),
    "reference": frozenset({"🛠️ 600-Arsenal"}),
    "playbook": frozenset({"🛠️ 600-Arsenal"}),
    "dashboard": frozenset({"🎯 100-Command-Center"}),
}
# The dashboard is the vault's root surface, so it has no upper index to link.
_UPPER_LINK_EXEMPT_TYPES = frozenset({"dashboard"})
_REQUIRED_FIELDS = ("title", "created", "type", "status", "tags")
_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")
_UPPER_LINK = re.compile(
    r"(?mi)^Üst\s+(?:kayıt|indeks|yüzey)\s*:\s*\[\[[^\]]+\]\]"
)


def _relative(vault_root: Path, path: Path) -> Path:
    if path.is_absolute():
        return path.resolve(strict=False).relative_to(vault_root.resolve(strict=False))
    return Path(*path.parts)


def should_validate_path(path: Path) -> bool:
    normalized = path.as_posix()
    parts = Path(normalized).parts
    if not parts or path.suffix.casefold() != ".md":
        return False
    if parts[0].casefold() == "daily":
        return False
    if len(parts) >= 2 and parts[0].casefold() == "knowledge" and parts[1].casefold() == "connections":
        return False
    if parts[0] == "🔮 850-Companion":
        return False
    return parts[0] in _CONTENT_ROOTS


def _tag_values(value: str | list[str]) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(value)
    return (value,) if value else ()


def validate_note(vault_root: Path, path: Path, text: str) -> tuple[str, ...]:
    relative = _relative(vault_root, path)
    if not should_validate_path(relative):
        return ()
    issues: list[str] = []
    values = parse_frontmatter(text)
    if not values:
        return ("frontmatter-missing",)
    for field in _REQUIRED_FIELDS:
        if not values.get(field):
            issues.append(f"frontmatter-field-missing:{field}")
    tags = values.get("tags")
    if tags and not isinstance(tags, list):
        issues.append("tags-empty-or-invalid")
    taxonomy_path = vault_root / ".codex" / "tag-taxonomy.json"
    try:
        taxonomy = tag_taxonomy.load_taxonomy(taxonomy_path)
    except tag_taxonomy.TaxonomyError as exc:
        issues.append(str(exc))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues.append(f"taxonomy-unreadable:{exc.__class__.__name__}")
    else:
        if tags:
            for tag in tag_taxonomy.tag_violations(taxonomy, relative, _tag_values(tags)):
                issues.append(f"tag-not-canonical:{tag}")
    raw_type = values.get("type")
    note_type = raw_type if isinstance(raw_type, str) else ""
    allowed_roots = _TYPE_ROOTS.get(note_type)
    if allowed_roots is None:
        issues.append(f"intake-type-unknown:{note_type or 'missing'}")
    elif relative.parts[0] not in allowed_roots:
        issues.append(f"intake-target-invalid:{note_type}")
    if note_type == "source-note":
        if not values.get("dedupe_key"):
            issues.append("dedupe-key-missing")
        if not values.get("source_type"):
            issues.append("source-type-missing")
    if note_type == "project-marker":
        if not values.get("canonical_link"):
            issues.append("project-canonical-link-missing")
        if not values.get("last_decision_summary"):
            issues.append("project-last-decision-summary-missing")
        if not values.get("decision_summary_mirroring"):
            issues.append("project-mirroring-decision-missing")
    if note_type not in _UPPER_LINK_EXEMPT_TYPES and _UPPER_LINK.search(text) is None:
        issues.append("upper-index-link-missing")

    images = tuple(_IMAGE.finditer(text))
    if images:
        if note_type != "source-note":
            issues.append("image-record-type-invalid")
        asset_folders: set[str] = set()
        for image in images:
            if not image.group("alt").strip():
                issues.append("image-alt-missing")
            target = image.group("target").strip().replace("\\", "/")
            target_parts = Path(target).parts
            if (
                len(target_parts) < 3
                or target_parts[0].casefold() != "assets"
                or target.startswith(("http://", "https://"))
            ):
                issues.append("image-assets-folder-invalid")
            else:
                asset_folders.add(target_parts[1].casefold())
        if len(asset_folders) > 1:
            issues.append("image-assets-folder-split")
        headings = {
            line.strip().casefold()
            for line in text.splitlines()
            if line.startswith("## ")
        }
        if "## görselde açıkça görülenler" not in headings:
            issues.append("image-observed-section-missing")
        if "## görselde verilmeyenler" not in headings:
            issues.append("image-unprovided-section-missing")
    return tuple(dict.fromkeys(issues))


def _git_new_paths(vault_root: Path) -> tuple[Path, ...]:
    commands = (
        ("git", "diff", "--name-only", "--diff-filter=A", "-z", "HEAD"),
        ("git", "ls-files", "--others", "--exclude-standard", "-z"),
    )
    paths: set[Path] = set()
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=vault_root,
                check=False,
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                raise ValueError("intake-git-status-failed")
            output = result.stdout.decode("utf-8", errors="strict")
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            raise ValueError("intake-git-status-failed") from exc
        for raw in output.split("\0"):
            if raw:
                paths.add(Path(raw))
    return tuple(sorted(paths, key=lambda item: item.as_posix()))


def main(
    argv: Sequence[str] | None = None,
    *,
    vault_root: Path = VAULT_ROOT,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if args.paths:
        paths = tuple(args.paths)
    else:
        try:
            paths = _git_new_paths(vault_root)
        except ValueError:
            print(json.dumps({
                "status": "fail",
                "checked": 0,
                "violations": [{
                    "path": "<git>",
                    "issues": ["intake-git-status-failed"],
                }],
            }, ensure_ascii=True, sort_keys=True))
            return 1
    findings: list[dict[str, object]] = []
    checked = 0
    for relative in paths:
        normalized = _relative(vault_root, relative)
        if not should_validate_path(normalized):
            continue
        checked += 1
        path = vault_root / normalized
        issues: tuple[str, ...]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            issues = ("note-unreadable",)
        else:
            issues = validate_note(vault_root, path, text)
        if issues:
            findings.append({"path": normalized.as_posix(), "issues": list(issues)})
    report = {
        "status": "ok" if not findings else "fail",
        "checked": checked,
        "violations": findings,
    }
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
