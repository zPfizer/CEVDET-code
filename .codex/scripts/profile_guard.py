from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
import re
import unicodedata

from markdown_boundary import markdown_body as _markdown_body, wikilinks
from knowledge_schema import CLAIM_ROW, markdown_headings
from user_evidence import USER_LINK, proof_for_link


PROFILE_RELATIVE = "🔮 850-Companion/Profile.md"
PROFILE_CONTEXT_LIMIT = 300
_PORTRAIT_TITLE = "Oturum Portresi"
_STYLE_TITLE = "Vault'ta çalışma ve yanıt tarzı"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_PREFERENCE = re.compile(
    r"^\s*-\s+(?P<claim>\S(?:.*?\S)?)\s+Kaynak:\s+"
    r"\[\[knowledge/concepts/(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)(?:#[^\]|]*)?"
    r"\|(?P<alias>[^\]]+)\]\]\s+·\s+kullanıcı tercihi\s+·\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\.[ \t]*$"
)
_EMPTY_SECTION = re.compile(r"(?m)^#{2,3}[ \t]*\r?$")
_ReadSource = Callable[[Path], str | None]


def _heading(text: str, title: str) -> list[tuple[str, str, int, int]]:
    return [
        match
        for match in markdown_headings(_markdown_body(text, mask_inline_code=False))
        if match[0] == "##" and match[1] == title
    ]


def _section(text: str, match: tuple[str, str, int, int]) -> str:
    start = match[3]
    masked = _markdown_body(text, mask_inline_code=False)
    boundaries = [
        heading[2]
        for heading in markdown_headings(masked)
        if heading[0] in ("##", "###") and heading[2] > match[2]
    ]
    if empty := _EMPTY_SECTION.search(masked, start):
        boundaries.append(empty.start())
    end = min(boundaries, default=len(text))
    return text[start:end]


def _structured_section(text: str, match: tuple[str, str, int, int]) -> str:
    return _markdown_body(
        _section(text, match),
        mask_frontmatter=False,
        mask_inline_code=False,
    )


def _iso(value: str) -> date | None:
    if not _ISO_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _normal_claim(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().casefold().replace("i\u0307", "i")
    while value and unicodedata.category(value[-1]).startswith("P"):
        value = value[:-1].rstrip()
    return value


def _frontmatter_updated(text: str) -> date | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    values = []
    for line in lines[1:end]:
        match = re.fullmatch(r"\s*updated\s*:\s*(.*?)\s*", line)
        if match:
            values.append(match.group(1).strip("'\""))
    return _iso(values[0]) if len(values) == 1 else None


def _read(root: Path, path: Path, reader: _ReadSource | None, missing: str, unavailable: str) -> tuple[str | None, str | None]:
    try:
        if _reparse(path, root) or not path.resolve().is_relative_to(root):
            return None, unavailable
    except (OSError, RuntimeError):
        return None, unavailable
    if not path.is_file():
        return None, missing
    if reader is not None:
        try:
            value = reader(path)
        except (OSError, UnicodeError, ValueError, RuntimeError):
            return None, unavailable
        return (value, None) if isinstance(value, str) else (None, unavailable)
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeError):
        return None, unavailable


def _target(inner: str) -> str:
    path = re.split(r"\\\||\|", inner, maxsplit=1)[0]
    return path.split("#", 1)[0].strip().replace("\\", "/")


def _reparse(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except (OSError, ValueError):
        return False
    try:
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)()):
                return True
    except OSError:
        return True
    return False


def _check_links(text: str, root: Path, issues: list[str]) -> None:
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError):
        issues.append("profile-link-invalid")
        return
    for match in wikilinks(_markdown_body(text)):
        raw = _target(match.group(1))
        if not raw:  # [[#fragment]] is a safe intra-document link.
            continue
        if (
            raw.startswith(("/", "\\", "//"))
            or re.match(r"^[A-Za-z]:", raw)
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw)
        ):
            issues.append("profile-link-external")
            continue
        parts = raw.split("/")
        if ".." in parts:
            issues.append("profile-link-traversal")
            continue
        suffix = Path(parts[-1]).suffix.lower()
        if suffix and suffix != ".md":
            issues.append("profile-link-format")
            continue
        candidate = root.joinpath(*parts)
        if not suffix:
            candidate = candidate.with_name(candidate.name + ".md")
        if _reparse(candidate, root):
            issues.append("profile-link-reparse")
            continue
        try:
            resolved = candidate.resolve()
            inside = resolved.is_relative_to(resolved_root)
        except (OSError, RuntimeError):
            issues.append("profile-link-invalid")
            continue
        if not inside:
            issues.append("profile-link-traversal")
        elif not resolved.is_file():
            issues.append("profile-link-broken")


def portrait(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = _heading(text, _PORTRAIT_TITLE)
    if len(matches) != 1:
        return ""
    body = _section(text, matches[0]).strip()
    card = f"## {_PORTRAIT_TITLE}\n\n{body}" if body else ""
    return card if card and len(card) <= PROFILE_CONTEXT_LIMIT else ""


def _claim_rows(text: str) -> list[tuple[str, ...]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    headings = _heading(text, "Kayıtlar")
    if len(headings) != 1:
        return []
    return [
        match.groups()
        for line in _structured_section(text, headings[0]).splitlines()
        if (match := CLAIM_ROW.fullmatch(line)) is not None
    ]


def _check_preference(
    root: Path,
    claim: str,
    slug: str,
    claim_date: str,
    updated: date,
    reader: _ReadSource | None,
    issues: list[str],
) -> None:
    parsed_date = _iso(claim_date)
    if parsed_date is None or parsed_date > updated:
        issues.append("profile-claim-date")
        return
    source_path = root / "knowledge" / "concepts" / f"{slug}.md"
    source, error = _read(root, source_path, reader, "profile-source-missing", "profile-source-unavailable")
    if error:
        issues.append(error)
        return
    rows = _claim_rows(source or "")
    qualified = [
        row for row in rows
        if row[0] == "gecerli" and row[1] == "kullanici-dusuncesi"
        and row[2] == "guncel" and row[3] == claim_date
    ]
    if not qualified:
        issues.append("profile-claim-provenance")
        return
    matching = [row for row in qualified if _normal_claim(row[5]) == _normal_claim(claim)]
    if not matching:
        issues.append("profile-claim-mismatch")
        return
    for row in matching:
        source_date = _iso(row[4])
        if source_date is None or source_date > updated:
            issues.append("profile-source-date")
            continue
        daily_path = root / "daily" / f"{row[4]}.md"
        _daily, error = _read(root, daily_path, reader, "profile-daily-missing", "profile-daily-unavailable")
        if error:
            issues.append(error)
        for line in _markdown_body(
            source or '',
            mask_inline_code=False,
        ).splitlines():
            parsed = CLAIM_ROW.fullmatch(line)
            if parsed and parsed.groups() == row and USER_LINK.search(line):
                proof = proof_for_link(root, line, reader=reader)
                if proof is None:
                    issues.append('profile-user-evidence')
                elif proof['scope'] != 'general':
                    issues.append('profile-claim-scope')


def check_profile(
    vault_root: Path,
    text: str | None = None,
    *,
    read_source: _ReadSource | None = None,
) -> tuple[str, ...]:
    """Return stable, content-free diagnostics for the active profile snapshot."""
    issues: list[str] = []
    try:
        root = Path(vault_root).resolve()
    except (OSError, RuntimeError):
        return ("profile-unavailable",)
    profile_path = root / PROFILE_RELATIVE
    if text is None:
        text, error = _read(root, profile_path, read_source, "profile-missing", "profile-unavailable")
        if error:
            return (error,)
    if not text:
        return ("profile-empty",)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    updated = _frontmatter_updated(text)
    if updated is None:
        issues.append("profile-frontmatter")
    portrait_matches = _heading(text, _PORTRAIT_TITLE)
    if len(portrait_matches) != 1 or not _section(text, portrait_matches[0]).strip():
        issues.append("profile-portrait")
    elif len(portrait(text)) > PROFILE_CONTEXT_LIMIT or not portrait(text):
        issues.append("profile-context-limit")
    style_matches = _heading(text, _STYLE_TITLE)
    if len(style_matches) != 1:
        issues.append("profile-style-section")
    _check_links(text, root, issues)
    if updated is None or len(style_matches) != 1:
        return tuple(dict.fromkeys(issues))
    bullets = [line for line in _structured_section(text, style_matches[0]).splitlines()
               if re.match(r"^\s*(?:[-+*]|[0-9]{1,9}[.)])\s+", line)]
    if not bullets:
        issues.append("profile-preference-missing")
    claims: set[str] = set()
    for line in bullets:
        match = _PREFERENCE.fullmatch(line)
        if match is None:
            issues.append("profile-preference-format")
            continue
        values = match.groupdict()
        normalized = _normal_claim(values["claim"])
        if normalized in claims:
            issues.append("profile-claim-duplicate")
        claims.add(normalized)
        _check_preference(root, values["claim"], values["slug"], values["date"], updated, read_source, issues)
    return tuple(dict.fromkeys(issues))


profile_card = portrait
