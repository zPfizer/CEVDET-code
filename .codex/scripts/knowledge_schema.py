from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import unicodedata
from markdown_boundary import (
    FENCE_LINE as _FENCE_LINE,
    WIKILINK,
    _blank_inline_code,
    is_escaped as _is_escaped,
    markdown_body as _markdown_body,
    wikilinks as _wikilinks,
)
from user_evidence import USER_ANCHOR, USER_LINK, proof_for_link
from compile_state import PolicyError, require_publication_snapshot


CONCEPT_FIELDS = ("title", "aliases", "tags", "sources", "created", "updated")
CONCEPT_HEADINGS = (
    "## Önemli Noktalar",
    "## Detaylar",
    "## İlgili Kavramlar",
    "## Kaynaklar",
)
CONNECTION_HEADINGS = ("## Bağlantı", "## Ana Fikir")
IMPORTANT_POINTS = (3, 5)
RELATED_LINKS_MIN = 2
INDEX_HEADER = "| Makale | Özet | Kaynak | Güncellendi |"
INDEX_ROW = re.compile(r"\| \[\[concepts/([^\\|\]]+)\\\|")
LOG_HEADER = "# Derleme Günlüğü"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
DAILY_SOURCE = re.compile(r"\d{4}-\d{2}-\d{2}\.md\Z")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
CONCEPT_TARGET_PREFIX = "knowledge/concepts/"
DERIVED_SCHEMA = "knowledge-v2"
CLAIM_KINDS = (
    "kullanici-dusuncesi",
    "dis-gorus",
    "dogrulanmis-bilgi",
    "cevo-cikarimi",
)
CLAIM_STATES = ("gecerli", "gecmis")
CLAIM_FRESHNESS = ("guncel", "eski", "belirsiz")
CLAIM_HEADING = "## Kayıtlar"
CLAIM_ROW = re.compile(
    r"^- `(" + "|".join(CLAIM_STATES) + r")` "
    r"`(" + "|".join(CLAIM_KINDS) + r")` "
    r"`(" + "|".join(CLAIM_FRESHNESS) + r")` "
    r"(\d{4}-\d{2}-\d{2}) "
    r"\[\[daily/(\d{4}-\d{2}-\d{2})" + USER_ANCHOR + r"(?:\|[^\]]+)?\]\] — (\S(?:.*\S)?)$"
)
SOURCE_LINK = re.compile(
    r"\[\[daily/(\d{4}-\d{2}-\d{2})" + USER_ANCHOR + r"(?:\|[^\]]+)?\]\]"
)
_HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
DERIVED_RULES = (
    'Yeni kullanıcı düşüncesi kayıtları, daily içindeki sistem üretimi user-evidence kaydının '
    'claim metnini aynen ve [[daily/YYYY-MM-DD#user-ID|Kaynak]] bağlantısıyla taşımalı. '
    'Dayanağı olmayan özeti cevo-cikarimi ve belirsiz olarak sınıflandır; kullanıcı onayı uydurma. '
    'Eski değişmemiş kayıtların kaynaklarını ve tarihini koru. Aynı kayıt yeniden geçerli '
    'yapılacaksa yeni kullanıcı dayanağı gerekir.',
    f"Yeni veya güncellenen kavram ve bağlantı frontmatter'ında `schema: {DERIVED_SCHEMA}` kullan.",
    (
        f"Her kavramda {CLAIM_HEADING} altında her iddiayı tek satırda "
        "`- `<durum>` `<tür>` `<güncellik>` <iddia-tarihi> "
        "[[daily/<kaynak-tarihi>|Kaynak]] — <iddia>` biçiminde yaz. "
        f"Durum: {', '.join(f'`{value}`' for value in CLAIM_STATES)}; "
        f"tür: {', '.join(f'`{value}`' for value in CLAIM_KINDS)}; "
        f"güncellik: {', '.join(f'`{value}`' for value in CLAIM_FRESHNESS)}."
    ),
    (
        "Kayıtları tarih, kaynak, tür ve normalize iddia sırasıyla yaz; aynı normalize "
        "iddiayı çoğaltma. Aynı iddia yeni bir günlükte tekrarlanırsa yalnız yeni "
        "kaynağı sources ve Kaynaklar bölümüne ekle."
    ),
    (
        "Çelişkiyi kaynağın türüne göre ayır. Önceki kullanıcı kararını yalnız "
        "günlükte doğrulanmış `user-evidence` kaydı ve `#user-ID` bağlantısı yeni "
        "kullanıcı değişikliğini açıkça destekliyorsa eski kaydı koru ve `gecmis` "
        "yap; tarih ve kaynağı koruyarak yeni kullanıcı kaydını `gecerli` olarak "
        "ekle. Yeni doğrulanmış bir olgu önceki doğrulanmış olguyla çelişiyorsa "
        "önceki kaydı tarih ve kaynağıyla koru ve `gecmis`, yeni olguyu `gecerli` "
        "olarak ekle. Farklı dış kaynakların görüş ayrılığını kullanıcının açık karar "
        "değişikliğinden ayır; mevcut kullanıcı kararını otomatik olarak `gecmis` "
        "yapma; kaynak sahipleri ve koşullarıyla "
        "ortak sonucu, ayrışan iddiayı ve karar açısından eksik bilgiyi ayrı tut. "
        "Dış görüş kullanıcının kararını tek başına geçersiz kılmaz. Kaynakta olmayan "
        "açıklamayı `cevo-cikarimi` ve belirsiz olarak ayır; önceden kaynaklanmış bir "
        "kaydı silme. Gerekçe, alternatif, koşul ve taahhüt ayrımını koru."
    ),
    (
        "sources değerlerini tekrarsız ve sıralı tut; her kayıt kendi kaynağına, "
        "kavram ve bağlantıdaki her kaynak da Kaynaklar bölümünden "
        "[[daily/YYYY-MM-DD|Kaynak]] wikilink'iyle geri bağlansın."
    ),
    (
        "Yeni bağlantıda alfabetik slug sırasını tercih et. Mevcut bağlantının "
        "dosya adını ve connects sırasını koru; aynı kavram çiftini ikinci kez oluşturma."
    ),
)


@dataclass(frozen=True)
class KnowledgeSchemaReport:
    concepts: int
    connections: int
    index_rows: int
    issues: tuple[str, ...]


@dataclass(frozen=True)
class _Claim:
    state: str
    kind: str
    freshness: str
    claim_date: str
    source: str
    text: str
    normalized: str
    raw_line: str
    user_anchor: str | None


@dataclass(frozen=True)
class StructuralRule:
    """One document-shape rule, stated once for the writer and the validator.

    ``prompt`` is the sentence the compile prompt renders; ``holds`` is the
    enforcement the validator runs. A rule cannot exist in only one of them.
    """

    key: str
    scope: str
    prompt: str
    holds: Callable[[Path, str], bool]


def parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    result: dict[str, str | list[str]] = {}
    active_list: str | None = None
    for line in lines[1:]:
        if line == "---":
            return result
        item = re.match(r"^\s+-\s+(.+?)\s*$", line)
        if item and active_list:
            value = result.setdefault(active_list, [])
            if isinstance(value, list):
                value.append(item.group(1).strip().strip("'\""))
            continue
        if ":" not in line or line[0].isspace():
            active_list = None
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        active_list = None
        if raw.startswith("[") and raw.endswith("]"):
            result[key] = [
                part.strip().strip("'\"")
                for part in raw[1:-1].split(",")
                if part.strip()
            ]
        elif raw:
            result[key] = raw.strip("'\"")
        else:
            result[key] = []
            active_list = key
    return {}


def wikilink_target(inner: str) -> str:
    """Vault-relative target of a wikilink body (alias, anchor and escaping stripped)."""
    target = re.split(r"\\\||\|", inner, maxsplit=1)[0]
    return target.split("#", 1)[0].strip().replace("\\", "/")


def _link_slugs(text: str) -> set[str]:
    return {
        target
        for match in _wikilinks(_markdown_body(text))
        if (target := wikilink_target(match.group(1)))
    }


def markdown_headings(text: str) -> list[tuple[str, str, int, int]]:
    """Return real Markdown headings outside fenced code blocks."""
    headings: list[tuple[str, str, int, int]] = []
    masked = _markdown_body(text, mask_inline_code=False)
    chars = list(masked)
    _blank_inline_code(chars, masked, multiline_only=True)
    masked = ''.join(chars)
    offset = 0
    for raw_line in masked.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        heading = _HEADING_LINE.fullmatch(line)
        if heading:
            title = heading.group(2).strip()
            if title:
                headings.append((heading.group(1), title, offset, offset + len(line)))
        offset += len(raw_line)
    return headings


def _heading_matches(text: str, heading: str) -> list[tuple[str, str, int, int]]:
    level, _, title = heading.partition(" ")
    return [
        match
        for match in markdown_headings(text)
        if match[0] == level and match[1] == title
    ]


def _ordered(text: str, headings: tuple[str, ...]) -> bool:
    matches = [_heading_matches(text, heading) for heading in headings]
    if any(len(found) != 1 for found in matches):
        return False
    positions = [found[0][2] for found in matches]
    return positions == sorted(positions)


def _section(text: str, start: str, end: str) -> str:
    start_match = _heading_matches(text, start)
    end_match = _heading_matches(text, end)
    if not start_match or not end_match or start_match[0][3] > end_match[0][2]:
        return ""
    return text[start_match[0][3] : end_match[0][2]]


def _heading_section(text: str, heading: str) -> str:
    matches = _heading_matches(text, heading)
    if not matches:
        return ""
    target = matches[0]
    next_heading = next(
        (
            match
            for match in markdown_headings(text)
            if match[0] == "##" and match[2] > target[2]
        ),
        None,
    )
    end = next_heading[2] if next_heading else len(text)
    return text[target[3] : end]


def _normalized_claim(text: str) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    return normalized.casefold().replace("i\u0307", "i")


def _parse_claim(row: str) -> _Claim | None:
    match = CLAIM_ROW.fullmatch(row)
    if match is None:
        return None
    state, kind, freshness, claim_date, source_date, claim = match.groups()
    link = USER_LINK.search(row)
    return _Claim(
        state=state,
        kind=kind,
        freshness=freshness,
        claim_date=claim_date,
        source=f"{source_date}.md",
        text=claim,
        normalized=_normalized_claim(claim),
        raw_line=row,
        user_anchor=link.group(2) if link else None,
    )


def _claims(text: str) -> tuple[list[_Claim], bool]:
    section = _heading_section(text, CLAIM_HEADING)
    rows = [line for line in section.splitlines() if line.strip()]
    claims: list[_Claim] = []
    malformed = not rows
    for row in rows:
        claim = _parse_claim(row)
        if claim is None:
            malformed = True
            continue
        claims.append(claim)
    return claims, malformed


def _claim_identity(claim: _Claim) -> tuple[str, ...]:
    return claim.kind, claim.claim_date, claim.source, claim.normalized


def _claim_key(claim: _Claim) -> tuple[str, ...]:
    return claim.kind, claim.normalized


def _claim_sort_key(claim: _Claim) -> tuple[str, ...]:
    return claim.claim_date, claim.source, claim.kind, claim.normalized


def normalize_claim_order(text: str) -> str:
    """Reorder valid derived claim rows; retain every row and all other text."""
    if parse_frontmatter(text).get('schema') != DERIVED_SCHEMA:
        return text
    claims, malformed = _claims(text)
    if malformed:
        return text  # Leave malformed content for the validator to reject.
    section = _heading_section(text, CLAIM_HEADING)
    lines = section.splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if line.strip()]
    ordered = sorted(claims, key=_claim_sort_key)
    for index, claim in zip(positions, ordered):
        line = lines[index]
        ending = line[len(line.splitlines()[0]):]
        lines[index] = claim.raw_line + ending
    heading = _heading_matches(text, CLAIM_HEADING)
    # Başlık yoksa _claims bölümü boş bulur ve malformed erken döndürür; savunma hattı.
    if not heading:  # pragma: no cover
        return text
    start = heading[0][3]
    return text[:start] + ''.join(lines) + text[start + len(section):]


def normalize_source_links(text: str) -> str:
    """Add missing links for declared dates; do not invent sources or remove prose."""
    metadata = parse_frontmatter(text)
    sources = metadata.get('sources')
    if metadata.get('schema') != DERIVED_SCHEMA or not isinstance(sources, list):
        return text
    if any(not isinstance(source, str) or not DAILY_SOURCE.fullmatch(source) for source in sources):
        return text
    heading_matches = _heading_matches(text, "## Kaynaklar")
    start = heading_matches[0][3] if heading_matches else len(text)
    next_heading = next(
        (
            match
            for match in markdown_headings(text)
            if match[0] == "##" and match[2] > start
        ),
        None,
    )
    end = next_heading[2] if next_heading else len(text)
    missing = sorted(set(sources) - _source_links(text[start:end]))
    if not missing:
        return text
    links = '\n'.join(f'- [[daily/{source.removesuffix(".md")}|Kaynak]]' for source in missing)
    prefix = text[:end].rstrip('\r\n') + ('\n\n## Kaynaklar' if not heading_matches else '')
    return prefix + '\n\n' + links + '\n\n' + text[end:]


def source_link_details(text: str) -> str:
    sources = parse_frontmatter(text).get('sources', [])
    declared = {value for value in sources if isinstance(value, str) and DAILY_SOURCE.fullmatch(value)} if isinstance(sources, list) else set()
    linked = _source_links(_heading_section(text, '## Kaynaklar'))
    claims, _ = _claims(text)
    return (f'missing-footer={sorted(declared - linked)}; '
            f'extra-footer={sorted(linked - declared)}; '
            f'undeclared-claims={sorted({claim.source for claim in claims} - declared)}')


def _source_links(text: str) -> set[str]:
    body = _markdown_body(text)
    return {
        f"{source.group(1)}.md"
        for match in _wikilinks(body)
        if (source := SOURCE_LINK.fullmatch(match.group(0))) is not None
    }


def _connects(text: str) -> list[str] | None:
    value = parse_frontmatter(text).get("connects")
    if (
        not isinstance(value, list)
        or len(value) != 2
        or len(set(value)) != 2
        or any(not isinstance(slug, str) or SLUG.fullmatch(slug) is None for slug in value)
    ):
        return None
    return value


def canonical_concept_target(slug: str) -> str:
    return f"{CONCEPT_TARGET_PREFIX}{slug}"


def _index_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("| [[")]


def _concept_points_ok(path: Path, text: str) -> bool:
    if not _ordered(text, CONCEPT_HEADINGS):
        return True  # the headings rule already owns this failure
    points = _section(text, CONCEPT_HEADINGS[0], CONCEPT_HEADINGS[1])
    low, high = IMPORTANT_POINTS
    return low <= len(re.findall(r"(?m)^-\s+", points)) <= high


def _concept_related_ok(path: Path, text: str) -> bool:
    if not _ordered(text, CONCEPT_HEADINGS):
        return True  # the headings rule already owns this failure
    related = _section(text, CONCEPT_HEADINGS[2], CONCEPT_HEADINGS[3])
    return sum(
        1
        for _ in _wikilinks(_markdown_body(related, mask_frontmatter=False))
    ) >= RELATED_LINKS_MIN


def _connection_path_ok(path: Path, text: str) -> bool:
    connects = _connects(text)
    return connects is None or path.name == f"{connects[0]}--{connects[1]}.md"


def _connection_links_ok(path: Path, text: str) -> bool:
    connects = _connects(text)
    if connects is None:
        return True  # the connects rule already owns this failure
    targets = _link_slugs(text)
    return all(canonical_concept_target(slug) in targets for slug in connects)


STRUCTURAL_RULES: tuple[StructuralRule, ...] = (
    StructuralRule(
        key="slug",
        scope="concept-identity",
        prompt="Kavram dosyası knowledge/concepts/<ascii-kebab-slug>.md yolunda olmalı.",
        holds=lambda path, text: SLUG.fullmatch(path.stem) is not None,
    ),
    StructuralRule(
        key="headings",
        scope="concept",
        prompt=(
            "Kavram gövdesi sırasıyla # Title, 2-4 cümlelik çekirdek açıklama ve\n"
            f"  {', '.join(CONCEPT_HEADINGS)}\n"
            "  başlıklarını bu sırayla içermeli."
        ),
        holds=lambda path, text: _ordered(text, CONCEPT_HEADINGS),
    ),
    StructuralRule(
        key="important-points",
        scope="concept",
        prompt=(
            f"{CONCEPT_HEADINGS[0]} altında `- ` ile başlayan "
            f"{IMPORTANT_POINTS[0]}-{IMPORTANT_POINTS[1]} madde bulunmalı."
        ),
        holds=_concept_points_ok,
    ),
    StructuralRule(
        key="related-links",
        scope="concept",
        prompt=(
            f"{CONCEPT_HEADINGS[2]} altında en az {RELATED_LINKS_MIN} wikilink ve\n"
            "  her bağlantının nasıl ilişkili olduğunu anlatan bir cümle bulunmalı."
        ),
        holds=_concept_related_ok,
    ),
    StructuralRule(
        key="connects",
        scope="connection-identity",
        prompt=(
            "Anlamlı kavram bağlantıları knowledge/connections altında tutulmalı;\n"
            "  connects frontmatter alanı tam olarak iki farklı kavram slug'ı içermeli."
        ),
        holds=lambda path, text: _connects(text) is not None,
    ),
    StructuralRule(
        key="path",
        scope="connection",
        prompt=(
            "Bağlantı dosyasının adı connects sırasını izleyerek\n"
            "  knowledge/connections/<a>--<b>.md olmalı."
        ),
        holds=_connection_path_ok,
    ),
    StructuralRule(
        key="concept-links",
        scope="connection",
        prompt=(
            "Bağlantı dosyası her iki kavrama da\n"
            "  [[knowledge/concepts/<slug>|<başlık>]] biçiminde gerçek wikilink içermeli."
        ),
        holds=_connection_links_ok,
    ),
    StructuralRule(
        key="headings",
        scope="connection",
        prompt=(
            f"Bağlantı dosyası {' ve '.join(CONNECTION_HEADINGS)} başlıklarını\n"
            "  bu sırayla içermeli."
        ),
        holds=lambda path, text: _ordered(text, CONNECTION_HEADINGS),
    ),
    StructuralRule(
        key="header",
        scope="index",
        prompt=(
            f"knowledge/index.md tablosu `{INDEX_HEADER}`\n"
            "  başlık satırını içermeli."
        ),
        holds=lambda path, text: INDEX_HEADER in text,
    ),
    StructuralRule(
        key="row",
        scope="index",
        prompt=(
            "Her makale satırı\n"
            "  `| [[concepts/<slug>\\|<başlık>]] | <özet> | <kaynak> | <güncellendi> |`\n"
            "  biçiminde olmalı; wikilink hücresindeki dikey çizgi `\\|` olarak\n"
            "  kaçırılmalı, yoksa satır tabloyu bozar."
        ),
        holds=lambda path, text: all(
            INDEX_ROW.match(row) for row in _index_rows(text)
        ),
    ),
    StructuralRule(
        key="header",
        scope="log",
        prompt=f"knowledge/log.md dosyası `{LOG_HEADER}` satırıyla başlamalı.",
        holds=lambda path, text: text.startswith(LOG_HEADER),
    ),
)


def schema_rules_text() -> str:
    """The declared contract as prompt bullets — the only place prompts state it."""
    return "\n".join(
        [*(f"- {rule.prompt}" for rule in STRUCTURAL_RULES), *(f"- {rule}" for rule in DERIVED_RULES)]
    )


def _issue_path(path: Path) -> str:
    if path.name == "knowledge":
        return "knowledge"
    if path.parent.name in {"concepts", "connections"}:
        return f"knowledge/{path.parent.name}/{path.name}"
    return f"knowledge/{path.name}"


def _linked_path(path: Path, root: Path) -> str | None:
    """Return the first source-boundary failure without opening ``path``."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "source-escape"
    current = root
    try:
        for part in relative.parts:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return "source-symlink"
            is_junction = getattr(current, "is_junction", None)
            if is_junction is not None and is_junction():
                return "source-symlink"
            if getattr(info, "st_file_attributes", 0) & 0x400:
                return "source-symlink"
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve(strict=True)):
            return "source-escape"
    except (OSError, RuntimeError):
        return "source-unreadable"
    return None


def _source_status(
    path: Path,
    root: Path,
    expected: Callable[[int], bool] | None = None,
) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "source-unreadable"
    reason = _linked_path(path, root)
    if reason is not None:
        return reason
    if expected is not None and not expected(info.st_mode):
        return "source-type"
    return ""


def _knowledge_sources(
    directory: Path,
    root: Path,
    issues: list[str],
) -> list[Path]:
    status = _source_status(directory, root, stat.S_ISDIR)
    if status == "missing":
        return []
    if status:
        issues.append(f"{_issue_path(directory)}:{status}")
        return []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        issues.append(f"{_issue_path(directory)}:source-unreadable")
        return []
    sources: list[Path] = []
    for path in entries:
        is_markdown = path.suffix.lower() == ".md"
        status = _source_status(path, root, stat.S_ISREG if is_markdown else None)
        if status:
            if status != "missing":
                issues.append(f"{_issue_path(path)}:{status}")
            continue
        if is_markdown:
            sources.append(path)
    return sources


def _knowledge_file(
    path: Path,
    root: Path,
    issues: list[str],
) -> bool:
    status = _source_status(path, root, stat.S_ISREG)
    if status == "missing":
        issues.append(f"{_issue_path(path)}:missing")
        return False
    if status:
        issues.append(f"{_issue_path(path)}:{status}")
        return False
    return True


def _apply_rules(scope: str, path: Path, text: str, issues: list[str]) -> bool:
    """Append this scope's violations; return True when the scope held."""
    before = len(issues)
    for rule in STRUCTURAL_RULES:
        if rule.scope == scope and not rule.holds(path, text):
            issues.append(f"{_issue_path(path)}:{rule.key}")
    return len(issues) == before


def _validate_concept(path: Path, issues: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(text)
    _apply_rules("concept-identity", path, text, issues)
    missing = sorted(set(CONCEPT_FIELDS) - set(frontmatter))
    if missing:
        issues.append(f"{_issue_path(path)}:missing:{','.join(missing)}")
        return
    aliases = frontmatter.get("aliases")
    if not isinstance(aliases, list):
        issues.append(f"{_issue_path(path)}:aliases")
    for key in ("tags", "sources"):
        value = frontmatter.get(key)
        if not isinstance(value, list) or not value:
            issues.append(f"{_issue_path(path)}:{key}")
    for key in ("title", "created", "updated"):
        value = frontmatter.get(key)
        if not isinstance(value, str) or not value:
            issues.append(f"{_issue_path(path)}:{key}")
    for key in ("created", "updated"):
        value = frontmatter.get(key)
        if isinstance(value, str) and not DATE.fullmatch(value):
            issues.append(f"{_issue_path(path)}:{key}-format")
    sources = frontmatter.get("sources")
    if isinstance(sources, list) and any(
        not DAILY_SOURCE.fullmatch(source) for source in sources
    ):
        issues.append(f"{_issue_path(path)}:sources-format")
    title = frontmatter.get("title")
    lines = text.splitlines()
    try:
        frontmatter_end = lines.index("---", 1)
    except ValueError:  # pragma: no cover — kapanmamış frontmatter alan eksikliğinde erken döner.
        frontmatter_end = len(lines)
    first_body_line = next(
        (line for line in lines[frontmatter_end + 1 :] if line.strip()),
        "",
    )
    if isinstance(title, str) and first_body_line != f"# {title}":
        issues.append(f"{_issue_path(path)}:title-heading")
    _apply_rules("concept", path, text, issues)


def _validate_connection(path: Path, issues: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    if not _apply_rules("connection-identity", path, text, issues):
        return
    _apply_rules("connection", path, text, issues)


def _validate_derived_concept(
    path: Path,
    text: str,
    previous_text: str | None,
    issues: list[str],
    *,
    parsed: tuple[list[_Claim], bool] | None = None,
    parsed_previous: tuple[list[_Claim], bool] | None = None,
) -> list[_Claim]:
    frontmatter = parse_frontmatter(text)
    if frontmatter.get("schema") != DERIVED_SCHEMA:
        issues.append(f"{_issue_path(path)}:derived-schema")
        return []
    sources = frontmatter.get("sources")
    if not isinstance(sources, list):
        return []  # the legacy field validator owns this failure
    if sources != sorted(set(sources)):
        issues.append(f"{_issue_path(path)}:source-order")
    claims, malformed = parsed if parsed is not None else _claims(text)
    if malformed:
        issues.append(f"{_issue_path(path)}:claims")
    identities = [_claim_identity(claim) for claim in claims]
    keys = [_claim_key(claim) for claim in claims]
    if len(keys) != len(set(keys)):
        issues.append(f"{_issue_path(path)}:claim-duplicate")
    if claims != sorted(claims, key=_claim_sort_key):
        issues.append(f"{_issue_path(path)}:claim-order")
    claim_sources = {claim.source for claim in claims}
    source_set = set(sources)
    if not claim_sources.issubset(source_set) or _source_links(
        _heading_section(text, CONCEPT_HEADINGS[3])
    ) != source_set:
        issues.append(f"{_issue_path(path)}:source-links")
    updated = frontmatter.get("updated")
    dated = [claim.claim_date for claim in claims] + [source.removesuffix(".md") for source in sources]
    if isinstance(updated, str) and dated and updated < max(dated):
        issues.append(f"{_issue_path(path)}:updated-before-claim")
    current_dates = [claim.claim_date for claim in claims if claim.state == "gecerli"]
    if any(
        claim.state == "gecmis"
        and not any(current_date >= claim.claim_date for current_date in current_dates)
        for claim in claims
    ):
        issues.append(f"{_issue_path(path)}:claim-history")
    if previous_text is not None:
        previous_claims, _malformed = (
            parsed_previous
            if parsed_previous is not None
            else _claims(previous_text)
        )
        if not {_claim_identity(claim) for claim in previous_claims}.issubset(set(identities)):
            issues.append(f"{_issue_path(path)}:claim-history")
        previous_sources = parse_frontmatter(previous_text).get("sources")
        if isinstance(previous_sources, list) and not set(previous_sources).issubset(source_set):
            issues.append(f"{_issue_path(path)}:source-history")
    return claims


def _validate_derived_connection(
    path: Path,
    text: str,
    previous_text: str | None,
    issues: list[str],
) -> None:
    frontmatter = parse_frontmatter(text)
    if frontmatter.get("schema") != DERIVED_SCHEMA:
        issues.append(f"{_issue_path(path)}:derived-schema")
        return
    sources = frontmatter.get("sources")
    if not isinstance(sources, list) or not sources:
        issues.append(f"{_issue_path(path)}:sources")
        return
    if any(not DAILY_SOURCE.fullmatch(source) for source in sources):
        issues.append(f"{_issue_path(path)}:sources-format")
    if sources != sorted(set(sources)):
        issues.append(f"{_issue_path(path)}:source-order")
    if previous_text is not None:
        previous_sources = parse_frontmatter(previous_text).get("sources")
        if isinstance(previous_sources, list) and not set(previous_sources).issubset(set(sources)):
            issues.append(f"{_issue_path(path)}:source-history")
    if _source_links(_heading_section(text, "## Kaynaklar")) != set(sources):
        issues.append(f"{_issue_path(path)}:source-links")
    updated = frontmatter.get("updated")
    if not isinstance(updated, str) or not DATE.fullmatch(updated):
        issues.append(f"{_issue_path(path)}:updated")
    elif updated < max(source.removesuffix(".md") for source in sources):
        issues.append(f"{_issue_path(path)}:updated-before-source")


def validate_knowledge_tree(
    root: Path,
    *,
    changed_paths: Sequence[str] = (),
    previous_texts: Mapping[str, str] | None = None,
) -> KnowledgeSchemaReport:
    try:
        publication = require_publication_snapshot(root)
        report = _validate_knowledge_tree(root, changed_paths=changed_paths, previous_texts=previous_texts)
        require_publication_snapshot(root, publication)
        return report
    except PolicyError as exc:
        return KnowledgeSchemaReport(0, 0, 0, (f'knowledge:{exc}',))
    except (OSError, UnicodeError):
        return KnowledgeSchemaReport(0, 0, 0, ('knowledge:source-unreadable',))


def _validate_knowledge_tree(
    root: Path,
    *,
    changed_paths: Sequence[str] = (),
    previous_texts: Mapping[str, str] | None = None,
) -> KnowledgeSchemaReport:
    knowledge = root / "knowledge"
    issues: list[str] = []
    knowledge_status = _source_status(knowledge, root, stat.S_ISDIR)
    if knowledge_status == "missing":
        concepts: list[Path] = []
        connections: list[Path] = []
    elif knowledge_status:
        issues.append(f"{_issue_path(knowledge)}:{knowledge_status}")
        return KnowledgeSchemaReport(0, 0, 0, tuple(issues))
    else:
        concepts = _knowledge_sources(knowledge / "concepts", root, issues)
        connections = _knowledge_sources(knowledge / "connections", root, issues)
    for path in concepts:
        _validate_concept(path, issues)
    for path in connections:
        _validate_connection(path, issues)

    changed = set(changed_paths)
    previous = previous_texts or {}
    claim_owners: dict[tuple[str, ...], str] = {}
    connection_owners: dict[tuple[str, ...], str] = {}
    ordered_concepts = sorted(
        concepts,
        key=lambda path: (
            path.relative_to(root).as_posix() in changed,
            path.as_posix(),
        ),
    )
    for path in ordered_concepts:
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        parsed_claims = _claims(text)
        previous_text = previous.get(relative)
        parsed_previous = _claims(previous_text) if previous_text is not None else None
        if parse_frontmatter(text).get("schema") == DERIVED_SCHEMA or relative in changed:
            claims_for_path = _validate_derived_concept(
                path,
                text,
                previous_text,
                issues,
                parsed=parsed_claims,
                parsed_previous=parsed_previous,
            )
        else:
            claims_for_path = parsed_claims[0]
        if relative in changed:
            prior_rows = {
                _claim_identity(claim): claim
                for claim in (parsed_previous[0] if parsed_previous is not None else ())
            }
            for claim in parsed_claims[0]:
                if claim.kind != 'kullanici-dusuncesi':
                    continue
                previous_claim = prior_rows.get(_claim_identity(claim))
                promoting = previous_claim is not None and (
                    (previous_claim.state != 'gecerli' and claim.state == 'gecerli') or
                    (previous_claim.freshness != 'guncel' and claim.freshness == 'guncel'))
                if previous_claim is not None and previous_claim.user_anchor == claim.user_anchor and not promoting:
                    continue  # Carry an unchanged source identity; read-time checks still validate it.
                if (promoting and previous_claim is not None and previous_claim.user_anchor == claim.user_anchor) or proof_for_link(root, claim.raw_line) is None:
                    issues.append(f'{_issue_path(path)}:user-evidence')
        for claim in claims_for_path:
            identity = _claim_key(claim)
            owner = claim_owners.setdefault(identity, path.name)
            if owner != path.name:
                issues.append(f"{_issue_path(path)}:claim-duplicate")

    ordered_connections = sorted(
        connections,
        key=lambda path: (
            path.relative_to(root).as_posix() in changed,
            path.as_posix(),
        ),
    )
    for path in ordered_connections:
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if parse_frontmatter(text).get("schema") == DERIVED_SCHEMA or relative in changed:
            _validate_derived_connection(path, text, previous.get(relative), issues)
        connects = _connects(text)
        if connects is not None:
            missing_targets = [
                slug
                for slug in connects
                if not (knowledge / "concepts" / f"{slug}.md").is_file()
            ]
            if missing_targets:
                issues.append(f"{_issue_path(path)}:concept-targets")
            pair = tuple(sorted(connects))
            owner = connection_owners.setdefault(pair, path.name)
            if owner != path.name:
                issues.append(f"{_issue_path(path)}:connection-duplicate")

    index_path = knowledge / "index.md"
    log_path = knowledge / "log.md"
    index_rows: list[str] = []
    if _knowledge_file(index_path, root, issues):
        index = index_path.read_text(encoding="utf-8")
        _apply_rules("index", index_path, index, issues)
        index_rows = _index_rows(index)
        indexed = [
            match.group(1)
            for row in index_rows
            if (match := INDEX_ROW.match(row))
        ]
        expected = sorted(path.stem for path in concepts)
        if sorted(indexed) != expected:
            issues.append("knowledge/index.md:coverage")
    if _knowledge_file(log_path, root, issues):
        _apply_rules(
            "log",
            log_path,
            log_path.read_text(encoding="utf-8"),
            issues,
        )
    return KnowledgeSchemaReport(
        len(concepts),
        len(connections),
        len(index_rows),
        tuple(issues),
    )
