from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
import re
import unicodedata

from graph_integrity import _markdown_body
from knowledge_schema import CLAIM_ROW, WIKILINK, markdown_headings
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
_DeadlineCheck = Callable[[], None]


def _check_deadline(check_deadline: _DeadlineCheck | None) -> None:
    if check_deadline is not None:
        check_deadline()


def _heading(
    text: str,
    title: str,
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> list[tuple[str, str, int, int]]:
    _check_deadline(check_deadline)
    matches: list[tuple[str, str, int, int]] = []
    for match in markdown_headings(_markdown_body(text, mask_inline_code=False)):
        _check_deadline(check_deadline)
        if match[0] == "##" and match[1] == title:
            matches.append(match)
    return matches


def _section(
    text: str,
    match: tuple[str, str, int, int],
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> str:
    _check_deadline(check_deadline)
    start = match[3]
    masked = _markdown_body(text, mask_inline_code=False)
    boundaries: list[int] = []
    for heading in markdown_headings(masked):
        _check_deadline(check_deadline)
        if heading[0] in ("##", "###") and heading[2] > match[2]:
            boundaries.append(heading[2])
    if empty := _EMPTY_SECTION.search(masked, start):
        boundaries.append(empty.start())
    end = min(boundaries, default=len(text))
    return text[start:end]


def _structured_section(
    text: str,
    match: tuple[str, str, int, int],
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> str:
    _check_deadline(check_deadline)
    return _markdown_body(
        _section(text, match, check_deadline=check_deadline),
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


def _frontmatter_updated(
    text: str,
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> date | None:
    _check_deadline(check_deadline)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    values = []
    for line in lines[1:end]:
        _check_deadline(check_deadline)
        match = re.fullmatch(r"\s*updated\s*:\s*(.*?)\s*", line)
        if match:
            values.append(match.group(1).strip("'\""))
    return _iso(values[0]) if len(values) == 1 else None


def _read(
    root: Path,
    path: Path,
    reader: _ReadSource | None,
    missing: str,
    unavailable: str,
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> tuple[str | None, str | None]:
    _check_deadline(check_deadline)
    try:
        if _reparse(path, root) or not path.resolve().is_relative_to(root):
            return None, unavailable
    except (OSError, RuntimeError):
        return None, unavailable
    if not path.is_file():
        return None, missing
    if reader is not None:
        try:
            _check_deadline(check_deadline)
            value = reader(path)
            _check_deadline(check_deadline)
        except TimeoutError:
            raise
        except (OSError, UnicodeError, ValueError, RuntimeError):
            return None, unavailable
        return (value, None) if isinstance(value, str) else (None, unavailable)
    try:
        _check_deadline(check_deadline)
        value = path.read_text(encoding="utf-8")
        _check_deadline(check_deadline)
        return value, None
    except TimeoutError:
        raise
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


def _check_links(
    text: str,
    root: Path,
    issues: list[str],
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> None:
    _check_deadline(check_deadline)
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError):
        issues.append("profile-link-invalid")
        return
    for match in WIKILINK.finditer(text):
        _check_deadline(check_deadline)
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


def portrait(
    text: str,
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> str:
    _check_deadline(check_deadline)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = _heading(text, _PORTRAIT_TITLE, check_deadline=check_deadline)
    if len(matches) != 1:
        return ""
    body = _section(text, matches[0], check_deadline=check_deadline).strip()
    card = f"## {_PORTRAIT_TITLE}\n\n{body}" if body else ""
    return card if card and len(card) <= PROFILE_CONTEXT_LIMIT else ""


def _claim_rows(
    text: str,
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> list[tuple[str, ...]]:
    _check_deadline(check_deadline)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    headings = _heading(text, "Kayıtlar", check_deadline=check_deadline)
    if len(headings) != 1:
        return []
    rows: list[tuple[str, ...]] = []
    for line in _structured_section(
        text,
        headings[0],
        check_deadline=check_deadline,
    ).splitlines():
        _check_deadline(check_deadline)
        if (match := CLAIM_ROW.fullmatch(line)) is not None:
            rows.append(match.groups())
    return rows


def _check_preference(
    root: Path,
    claim: str,
    slug: str,
    claim_date: str,
    updated: date,
    reader: _ReadSource | None,
    issues: list[str],
    *,
    check_deadline: _DeadlineCheck | None = None,
) -> None:
    _check_deadline(check_deadline)
    parsed_date = _iso(claim_date)
    if parsed_date is None or parsed_date > updated:
        issues.append("profile-claim-date")
        return
    source_path = root / "knowledge" / "concepts" / f"{slug}.md"
    source, error = _read(
        root,
        source_path,
        reader,
        "profile-source-missing",
        "profile-source-unavailable",
        check_deadline=check_deadline,
    )
    if error:
        issues.append(error)
        return
    rows = _claim_rows(source or "", check_deadline=check_deadline)
    qualified: list[tuple[str, ...]] = []
    for row in rows:
        _check_deadline(check_deadline)
        if (
            row[0] == "gecerli"
            and row[1] == "kullanici-dusuncesi"
            and row[2] == "guncel"
            and row[3] == claim_date
        ):
            qualified.append(row)
    if not qualified:
        issues.append("profile-claim-provenance")
        return
    matching: list[tuple[str, ...]] = []
    for row in qualified:
        _check_deadline(check_deadline)
        if _normal_claim(row[5]) == _normal_claim(claim):
            matching.append(row)
    if not matching:
        issues.append("profile-claim-mismatch")
        return
    for row in matching:
        _check_deadline(check_deadline)
        source_date = _iso(row[4])
        if source_date is None or source_date > updated:
            issues.append("profile-source-date")
            continue
        daily_path = root / "daily" / f"{row[4]}.md"
        _daily, error = _read(
            root,
            daily_path,
            reader,
            "profile-daily-missing",
            "profile-daily-unavailable",
            check_deadline=check_deadline,
        )
        if error:
            issues.append(error)
        for line in _markdown_body(
            source or '',
            mask_inline_code=False,
        ).splitlines():
            _check_deadline(check_deadline)
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
    check_deadline: _DeadlineCheck | None = None,
) -> tuple[str, ...]:
    """Return stable, content-free diagnostics for the active profile snapshot."""
    issues: list[str] = []
    _check_deadline(check_deadline)
    try:
        root = Path(vault_root).resolve()
    except (OSError, RuntimeError):
        return ("profile-unavailable",)
    profile_path = root / PROFILE_RELATIVE
    if text is None:
        text, error = _read(
            root,
            profile_path,
            read_source,
            "profile-missing",
            "profile-unavailable",
            check_deadline=check_deadline,
        )
        if error:
            return (error,)
    if not text:
        return ("profile-empty",)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    _check_deadline(check_deadline)
    updated = _frontmatter_updated(text, check_deadline=check_deadline)
    if updated is None:
        issues.append("profile-frontmatter")
    portrait_matches = _heading(
        text,
        _PORTRAIT_TITLE,
        check_deadline=check_deadline,
    )
    if len(portrait_matches) != 1 or not _section(
        text,
        portrait_matches[0],
        check_deadline=check_deadline,
    ).strip():
        issues.append("profile-portrait")
    elif (
        len(portrait(text, check_deadline=check_deadline)) > PROFILE_CONTEXT_LIMIT
        or not portrait(text, check_deadline=check_deadline)
    ):
        issues.append("profile-context-limit")
    style_matches = _heading(text, _STYLE_TITLE, check_deadline=check_deadline)
    if len(style_matches) != 1:
        issues.append("profile-style-section")
    _check_links(text, root, issues, check_deadline=check_deadline)
    if updated is None or len(style_matches) != 1:
        return tuple(dict.fromkeys(issues))
    bullets: list[str] = []
    for line in _structured_section(
        text,
        style_matches[0],
        check_deadline=check_deadline,
    ).splitlines():
        _check_deadline(check_deadline)
        if re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line):
            bullets.append(line)
    if not bullets:
        issues.append("profile-preference-missing")
    claims: set[str] = set()
    for line in bullets:
        _check_deadline(check_deadline)
        match = _PREFERENCE.fullmatch(line)
        if match is None:
            issues.append("profile-preference-format")
            continue
        values = match.groupdict()
        normalized = _normal_claim(values["claim"])
        if normalized in claims:
            issues.append("profile-claim-duplicate")
        claims.add(normalized)
        _check_preference(
            root,
            values["claim"],
            values["slug"],
            values["date"],
            updated,
            read_source,
            issues,
            check_deadline=check_deadline,
        )
    return tuple(dict.fromkeys(issues))


profile_card = portrait
