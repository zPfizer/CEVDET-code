from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Iterator, Sequence
import unicodedata

from file_lock import locked
from compile_state import PolicyError, PublicationSnapshot, require_publication_snapshot
from state_store import atomic_write_text
from profile_guard import PROFILE_RELATIVE, check_profile
from user_evidence import filter_evidence, USER_LINK, proof_for_link


MAX_EVENT_CHARS = 65_536
SUPPRESSION_SCHEMA = 1
PERSISTENT_TURNS_VERSION = "persistent-turns-v1"
_COMPANION_SOURCE_ALIASES = {
    '🔮 850-Companion/Sources/Last-Session.md': '🔮 850-Companion/Last-Session.md',
    '🔮 850-Companion/Sources/Journal.md': '🔮 850-Companion/Journal.md',
    '🔮 850-Companion/Sources/Threads.md': '🔮 850-Companion/Threads.md',
}
_COMPANION_CANONICAL = 'daily/companion-sessions.json'
MEMORY_READ_RULE = (
    'Unutma tercihleri etkin. Ham notlara veya eski önbelleğe geçme; tam kaynakları '
    'memory_ledger.read_memory_source(vault_root, path) ile süzülmüş olarak oku.'
)

PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
AUTHORIZATION = re.compile(
    r'''(?im)(?P<prefix>(?P<key_quote>["']?)\bauthorization(?P=key_quote)\s*:\s*)'''
    r'''(?:"Bearer\s+(?:\\.|[^"\\\r\n])+"|'''
    r"""'Bearer\s+(?:\\.|[^'\\\r\n])+'|Bearer\s+[^\s\r\n]+)"""
)
CREDENTIAL = re.compile(
    r'''(?im)(?P<prefix>(?P<key_quote>["']?)\b(?P<key>api[_-]?key|password|secret|token)'''
    r'''(?P=key_quote)\s*[:=]\s*)'''
    r'''(?P<value>"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s\r\n]+)'''
)
TOKEN_PREFIX = re.compile(r"\b(?:sk|ghp|github_pat|AKIA)[-_A-Za-z0-9]{12,}\b")
PERSONAL_CREDENTIAL = re.compile(
    r"(?i)\b(?:api\s+anahtarım|parolam|şifrem|tokenım)\b"
    r"(?:\s*[:=]\s*|\s+)(?:şu\s+|bu\s+)?\S[^\r\n]*"
)
CONTROL_TRAILING = re.compile(r"[\s.!?]+\Z")
FORGET_WITH_TARGET = re.compile(
    r"(?is)^\s*(?:şunu|bu\s+bilgiyi)?\s*unut\s*[:：]\s*(.+?)\s*[.!?]*\s*$"
)
FORGET_SUFFIX = re.compile(
    r"(?is)^\s*(.+?)\s+(?:bilgisini\s+|bilgiyi\s+)?unut(?:ur\s+musun)?\s*[.!?]*\s*$"
)
AMBIGUOUS_TARGETS = {"bunu", "şunu", "onu", "bu", "bu bilgi"}
NON_PERSISTENT_DIRECTIVES = {
    "secret",
    "forget",
    "forget-ambiguous",
    "do-not-save",
    "session-only",
    "what-known",
}

# These are quoted data, not requests. Keep the original text for target extraction.
QUOTED_CONTENT = re.compile(
    r'(?ms:^[ \t]*(?P<fence>(?P<fence_char>`|~)(?P=fence_char){2,})[^\r\n]*\r?\n'
    r'(?P<fenced_body>.*?)(?:^[ \t]*(?P=fence)(?P=fence_char)*[ \t]*\r?$|\Z))|'
    # Embedded multiline snippets remain data; only the named line-fence
    # branch can be unwrapped as a whole-message read-only restriction.
    r'```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|'
    r'(?m:^[ \t]*>[^\n]*|^(?: {4}|\t)[^\n]*)|'
    r'`[^`\n]*`|"[^"\n]*"|“[^”]*”|‘[^’]*’|«[^»]*»'
)
DO_NOT_SAVE = r'(?:(?:bunu|bu bilgiyi|bu ayrıntıyı)\s+)?(?:kaydetme|saklama|hafızana alma|hafızanda tutma|kaydetmeni istemiyorum)'
READ_ONLY_REQUEST = re.compile(
    r"\b(?:salt[ -]?okunur|read[ -]?only|sadece\s+incele|"
    r"hiçbir\s+dosyayı\s+değiştirme|dosyaları\s+değiştirme|"
    r"dosya\s+değiştirmeden|değişiklik\s+yapmadan|"
    r"do\s+not\s+(?:modify|change|touch)\s+files(?:\s+or\s+settings)?|"
    r"don['’]t\s+(?:modify|change|touch)\s+files(?:\s+or\s+settings)?|"
    r"no\s+(?:file|files)\s+(?:changes?|modifications?))\b"
)
EXPLICIT_WRITE_INTENT = re.compile(
    r"^\s*(?:"
    r"(?:lütfen\s+)?(?:(?:ok|okay|tamam)\s*[,;:]?\s*)?(?:lütfen\s+)?"
    r"(?:s[ıi]rayla(?:\s+hepsini)?|hepsini(?:\s+s[ıi]rayla)?)\s+(?:yap|uygula)|"
    r"(?:ok|okay|tamam)\s*[,;:]?\s*(?:yap|uygula)|"
    r"uygula|"
    r"bunu\s+düzelt|"
    r"gerekli\s+değişiklikleri\s+yap|"
    r"önerdiğin\s+değişiklikleri\s+uygula|"
    r"(?:bunu|öneriyi|değişikliği|değişiklikleri|önerilen\s+değişiklikleri)"
    r"\s+(?:yap|uygula)|"
    r"(?:dosyayı|dosyaları|ayarı|ayarları|kodu)\s+(?:değiştir|düzenle|uygula)|"
    r"(?:uygulamaya|yazmaya|değişikliğe)\s+geç|"
    r"(?:dosyaları\s+)?(?:değiştirebilirsin|düzenleyebilirsin|"
    r"uygulayabilirsin|yazabilirsin)"
    r")\s*[.!]*\s*$"
)


@dataclass(frozen=True)
class MemoryDirective:
    kind: str
    target: str = ""


class MemoryPreferenceError(ValueError):
    pass


class MemorySourceError(ValueError):
    pass


@dataclass(frozen=True)
class PersistentTurn:
    """One turn retained by the shared privacy reducer."""

    role: str
    text: str
    row_id: int | None = None


@dataclass(frozen=True)
class PersistentDecision:
    """Reducer result, including index rows removed by a late preference."""

    role: str
    text: str
    visible_text: str
    keep: bool
    classification: str
    removed_row_ids: tuple[int, ...] = ()


class PersistentTurnReducer:
    """Apply the memory policy once for text and indexed transcript rows."""

    def __init__(
        self,
        hashes: frozenset[str] = frozenset(),
        *,
        retained_rows: Sequence[tuple[int, str]] = (),
        skip_reply: bool = False,
        session_only: bool = False,
    ) -> None:
        self._hashes = hashes
        self._retained = [
            PersistentTurn(role, "", row_id)
            for row_id, role in retained_rows
        ]
        self.skip_reply = skip_reply
        self.session_only = session_only

    @property
    def retained(self) -> tuple[PersistentTurn, ...]:
        return tuple(self._retained)

    @property
    def retained_row_ids(self) -> tuple[int, ...]:
        return tuple(
            turn.row_id for turn in self._retained if turn.row_id is not None
        )

    def _decision(
        self,
        role: str,
        text: str,
        visible: str,
        keep: bool,
        classification: str,
        removed: Sequence[PersistentTurn] = (),
    ) -> PersistentDecision:
        return PersistentDecision(
            role,
            text,
            visible,
            keep,
            classification,
            tuple(
                turn.row_id
                for turn in removed
                if turn.row_id is not None
            ),
        )

    def add(
        self,
        role: str,
        text: str,
        *,
        row_id: int | None = None,
    ) -> PersistentDecision:
        if role not in {"user", "assistant"}:
            raise ValueError("persistent-turn-role-invalid")
        if self.session_only:
            self.skip_reply = True
            return self._decision(
                role, text, "", False, "session-only",
            )

        removed: list[PersistentTurn] = []
        if role == "user":
            directive = memory_directive(text)
            if directive.kind == "session-only":
                removed = list(self._retained)
                self._retained.clear()
                self.session_only = True
                self.skip_reply = True
                return self._decision(
                    role, text, "", False, "session-only", removed,
                )
            self.skip_reply = directive.kind in NON_PERSISTENT_DIRECTIVES
            if directive.kind == "do-not-save" and not directive.target:
                while self._retained and self._retained[-1].role == "assistant":
                    removed.append(self._retained.pop())
                if self._retained and self._retained[-1].role == "user":
                    removed.append(self._retained.pop())
            if self.skip_reply:
                return self._decision(
                    role, text, "", False, directive.kind, removed,
                )
        elif self.skip_reply:
            return self._decision(
                role, text, "", False, "reply-suppressed",
            )

        visible = filter_suppressed_text(text, self._hashes)
        if role == "user" and visible != text:
            self.skip_reply = True
        if not visible.strip():
            classification = "suppressed" if visible != text else "empty"
            return self._decision(
                role, text, visible, False, classification, removed,
            )
        self._retained.append(PersistentTurn(role, visible, row_id))
        classification = "suppressed" if visible != text else "ordinary"
        return self._decision(
            role, text, visible, True, classification, removed,
        )


def _unquoted_request(text: str) -> str:
    text = QUOTED_CONTENT.sub(' ', text)
    output = list(text)
    start = None
    for index, character in enumerate(text):
        if character == '\n':
            start = None
        elif character == "'":
            previous_word = index > 0 and (text[index - 1].isalnum() or text[index - 1] == '_')
            next_word = index + 1 < len(text) and (text[index + 1].isalnum() or text[index + 1] == '_')
            if start is None and not previous_word:
                start = index
            elif start is not None and not next_word:
                output[start:index + 1] = ' ' * (index + 1 - start)
                start = None
    return ''.join(output).strip()


def _folded_request(text: str) -> str:
    stripped = text.strip()
    # Reuse the quote grammar's first match: fullmatch could skip an earlier
    # closing fence and swallow a restriction between two separate blocks.
    fenced = QUOTED_CONTENT.match(stripped)
    if fenced is not None and fenced.group("fence") and fenced.end() == len(stripped):
        request = _unquoted_request(fenced.group("fenced_body"))
    else:
        request = _unquoted_request(text)
    return unicodedata.normalize("NFKC", request).casefold().replace("i\u0307", "i")


def is_read_only_request(text: str) -> bool:
    folded = _folded_request(text)
    # A question elsewhere in the message does not revoke an explicit restriction.
    return any(
        not re.match(r"\s+(?:kuralı|ifadesi)\b", folded[match.end():])
        for match in READ_ONLY_REQUEST.finditer(folded)
    )


def is_explicit_write_intent(text: str) -> bool:
    # Removing quoted data must not turn a quoted rule into write authorization.
    folded = unicodedata.normalize("NFKC", text).casefold().replace("i\u0307", "i")
    return EXPLICIT_WRITE_INTENT.fullmatch(folded) is not None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def memory_view_relative_path(relative: str) -> str:
    return f'.codex/private-memory/views/{_sha256_text(relative)}.md'


def sanitize_text(
    text: str,
    *,
    max_chars: int | None = MAX_EVENT_CHARS,
) -> tuple[str, tuple[str, ...]]:
    redactions: list[str] = []

    def replace(
        pattern: re.Pattern[str],
        replacement: str | Callable[[re.Match[str]], str],
        category: str,
    ) -> None:
        nonlocal text
        text, count = pattern.subn(replacement, text)
        if count and category not in redactions:
            redactions.append(category)

    replace(PRIVATE_KEY, "<REDACTED>", "private-key")
    replace(AUTHORIZATION,
            lambda match: (f'{match.group("prefix")}"Bearer <REDACTED>"' if match.group('key_quote')
                           else "Authorization: Bearer <REDACTED>"),
            "authorization")
    pieces: list[str] = []
    cursor = 0
    while match := CREDENTIAL.search(text, cursor):
        end = match.end()
        if text[match.start('value')] in '{[':
            try:
                _, end = json.JSONDecoder().raw_decode(text, match.start('value'))
                if end < len(text) and not (text[end].isspace() or text[end] in ',;}]'):
                    end = len(text)
            except (ValueError, RecursionError):
                # Unknown container boundaries must not expose the remaining payload.
                end = len(text)
        replacement = (f'{match.group("prefix")}"<REDACTED>"' if match.group('key_quote')
                       else f"{match.group('key')}=<REDACTED>")
        pieces.extend((text[cursor:match.start()], replacement))
        cursor = end
    if pieces:
        text = ''.join(pieces) + text[cursor:]
    if pieces:
        redactions.append('credential')
    replace(TOKEN_PREFIX, "<REDACTED>", "credential")
    replace(PERSONAL_CREDENTIAL, "<REDACTED>", "credential")

    if max_chars is not None and max_chars < 1:
        raise ValueError("max-event-chars-invalid")
    if max_chars is not None and len(text) > max_chars:
        marker = "\n<TRUNCATED_SAFE_LEDGER_EVENT>\n"
        if max_chars <= len(marker):
            text = marker[:max_chars]
        else:
            available = max_chars - len(marker)
            tail_chars = min(available // 4, 16_384)
            head_chars = available - tail_chars
            tail = text[-tail_chars:] if tail_chars else ""
            text = text[:head_chars] + marker + tail
        redactions.append("truncated")
    return text, tuple(redactions)


def contains_secret(text: str) -> bool:
    return any(
        pattern.search(text) is not None
        for pattern in (
            PRIVATE_KEY,
            AUTHORIZATION,
            CREDENTIAL,
            TOKEN_PREFIX,
            PERSONAL_CREDENTIAL,
        )
    )


def memory_directive(text: str) -> MemoryDirective:
    raw = text.strip()
    unquoted = _unquoted_request(text)
    folded = unicodedata.normalize("NFKC", unquoted).casefold().replace("i\u0307", "i")
    if re.search(r'\b(?:bu (?:konuşmada|sohbette|oturumda|sohbet aramızda)|aramızda) kalsın\b', folded):
        return MemoryDirective("session-only")
    if contains_secret(raw):
        return MemoryDirective("secret")
    if re.search(r"\bbenim\s+hakkımda\s+ne\s+biliyorsun\b", folded):
        return MemoryDirective("what-known")
    if re.search(r'\b' + DO_NOT_SAVE + r'\b', folded):
        standalone = re.fullmatch(
            r'(?:lütfen\s+)?' + DO_NOT_SAVE,
            CONTROL_TRAILING.sub("", folded),
        )
        return MemoryDirective("do-not-save", "" if standalone else raw)
    if re.search(r"\b(?:unut(?:ur\s+musun)?|hafızandan\s+(?:çıkar|sil)|hatırlamanı\s+istemiyorum)\b", folded):
        match = FORGET_WITH_TARGET.match(raw) or FORGET_SUFFIX.match(raw)
        if match is None:
            return MemoryDirective("forget-ambiguous", raw)
        target = CONTROL_TRAILING.sub("", match.group(1).strip().strip('\'"“”‘’«»'))
        if target.casefold().replace("i\u0307", "i") in AMBIGUOUS_TARGETS:
            return MemoryDirective("forget-ambiguous")
        return MemoryDirective("forget", target)
    if is_read_only_request(text):
        return MemoryDirective("read-only")
    if re.search(r"\bdüzelt\b", folded):
        return MemoryDirective("correct")
    if is_explicit_write_intent(text):
        return MemoryDirective("write-intent")
    return MemoryDirective("ordinary")


def persistent_turns(
    turns: Sequence[tuple[str, str]],
    hashes: frozenset[str] = frozenset(),
) -> list[tuple[str, str]]:
    reducer = PersistentTurnReducer(hashes)
    for role, text in turns:
        reducer.add(role, text)
    return [(turn.role, turn.text) for turn in reducer.retained]


def _session_only_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / f"memory-session-only-{_sha256_text(session_id)}"


def mark_session_only(state_dir: Path, session_id: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _session_only_path(state_dir, session_id)
    with locked(path):
        path.touch(exist_ok=True)


def is_session_only(state_dir: Path, session_id: str) -> bool:
    return _session_only_path(state_dir, session_id).is_file()


def _read_only_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / f"memory-read-only-{_sha256_text(session_id)}"


def mark_read_only_turn(state_dir: Path, session_id: str) -> None:
    """Salt okunur görev: bu turda otomatik kayıt, profil onarımı ve bakım yapılmaz.

    Oturum kapsamlıdır; sonraki kullanıcı mesajı veya tur sonu işareti kaldırmaz.
    Yalnız açık bir yazma isteği kapsamı kaldırabilir.
    Konuşma transkriptten düşürülmez; sonraki yetkili tur hatırlayabilir.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _read_only_path(state_dir, session_id)
    with locked(path):
        path.touch(exist_ok=True)


def is_read_only_turn(state_dir: Path, session_id: str) -> bool:
    return _read_only_path(state_dir, session_id).is_file()


class MemoryReadOnlyError(ValueError):
    pass


@contextmanager
def memory_write_guard(state_dir: Path, session_id: str | None) -> Iterator[None]:
    """Serialize publication with scope changes, only for this session."""
    if not session_id:
        yield
        return
    with locked(_read_only_path(state_dir, session_id)):
        if is_read_only_turn(state_dir, session_id):
            raise MemoryReadOnlyError('memory-read-only')
        yield


def clear_read_only_turn(state_dir: Path, session_id: str) -> None:
    path = _read_only_path(state_dir, session_id)
    if not path.exists():
        return
    with locked(path):
        path.unlink(missing_ok=True)


def memory_text_hash(text: str) -> str:
    value = text.split(" — ", 1)[-1]
    value = re.sub(r"^\s*[-*]\s+", "", value)
    value = CONTROL_TRAILING.sub("", value.strip())
    normalized = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", value).casefold().replace("i\u0307", "i"),
    )
    return _sha256_text(normalized)


def _suppression_path(private_root: Path) -> Path:
    return private_root / "controls" / "suppressions.jsonl"


def _suppression_hashes_from_lines(lines: Sequence[str]) -> frozenset[str]:
    hashes: set[str] = set()
    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryPreferenceError("memory-suppression-invalid") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema") != SUPPRESSION_SCHEMA
            or not isinstance(record.get("ts"), int)
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("target_sha256", "")))
        ):
            raise MemoryPreferenceError("memory-suppression-invalid")
        hashes.add(record["target_sha256"])
    return frozenset(hashes)


def load_suppressed_hashes(private_root: Path) -> frozenset[str]:
    path = _suppression_path(private_root)
    try:
        # Writers atomically replace the file; read-only callers need no lock file.
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeError) as exc:
        raise MemoryPreferenceError('memory-suppression-unreadable') from exc
    return _suppression_hashes_from_lines(lines)


def memory_syntactic_units(value: str) -> Iterator[str]:
    """Equivalent Markdown/metadata wrappers, not semantic similarity."""
    units = [value.strip(), re.sub(r'^\s*(?:#{1,6}|[-*>])\s+', '', value).strip()]
    metadata = re.match(r'^\s*[\w-]+:\s*(.+)$', value)
    if metadata:
        raw = metadata.group(1).strip()
        units.extend((raw, raw.strip('[]\'" ')))
        units.extend(part.strip('[]\'" ') for part in raw.split(','))
    for match in re.finditer(r'\[\[([^\]]+)\]\]', value):
        units.extend(match.group(1).replace('\\|', '|').split('|'))
    for match in re.finditer(r'(?<!!)\[([^\]]*)\]\(([^)]+)\)', value):
        units.extend(match.groups())
    for unit in units:
        unit = unit.strip('\'"“”‘’«»<> ')
        if not unit:
            continue
        yield unit
        path = PurePosixPath(unit.replace('\\', '/'))
        yield from path.parts
        yield path.stem
        yield path.stem.replace('-', ' ').replace('_', ' ')


def contains_suppressed_unit(value: str, hashes: frozenset[str]) -> bool:
    return bool(hashes) and any(memory_text_hash(unit) in hashes for unit in memory_syntactic_units(value))


def filter_suppressed_text(text: str, hashes: frozenset[str]) -> str:
    """Hide resolved memory units without changing their original source."""
    if not hashes:
        return text
    text = filter_evidence(text, lambda value: contains_suppressed_unit(value, hashes))
    kept = []
    for line in text.splitlines(keepends=True):
        if memory_text_hash(line) in hashes:
            continue
        # A paragraph may contain an unrelated sentence that must remain visible.
        sentences = re.split(r'(?<=[.!?])(?=\s+\S)', line)
        kept.append(''.join(part for part in sentences if not contains_suppressed_unit(part, hashes)))
    return ''.join(kept)


@dataclass(frozen=True)
class MemoryRead:
    """One preference snapshot for filtering, source targets and result validation."""

    _vault_root: Path
    _hashes: frozenset[str]
    _publication: PublicationSnapshot | None = field(default=None, init=False, repr=False, compare=False)

    def check_knowledge_snapshot(self) -> None:
        # Track only knowledge consumers; an interrupted compile must not block daily/Companion writes.
        try:
            current = require_publication_snapshot(self._vault_root, self._publication)
        except PolicyError as exc:
            raise MemoryPreferenceError(f'memory-{exc}') from exc
        object.__setattr__(self, '_publication', current)

    def _check_source_publication(self, relative: str) -> None:
        if self._publication is not None or relative.startswith(('knowledge/', '.codex/private-memory/views/')):
            self.check_knowledge_snapshot()

    @property
    def active(self) -> bool:
        return bool(self._hashes)

    def excludes(self, value: str) -> bool:
        return contains_suppressed_unit(value, self._hashes)

    def filter(self, text: str) -> str:
        return filter_suppressed_text(text, self._hashes)

    def project_text(
        self,
        relative: str,
        text: str,
        *,
        resolved_relative: str | None = None,
    ) -> str | None:
        """Apply this read's suppression, provenance and sanitization snapshot."""
        source_relative = resolved_relative or relative
        alias = _COMPANION_SOURCE_ALIASES.get(source_relative)
        if self.excludes(relative) or (alias is not None and self.excludes(alias)):
            return None
        text = self.filter(text)
        root = self._vault_root.resolve(strict=True)
        self._check_source_publication(source_relative)
        if PurePosixPath(source_relative).parts[0] != 'daily':
            lines = []
            for line in text.splitlines(keepends=True):
                if USER_LINK.search(line):
                    proof = proof_for_link(root, line, reader=lambda path: self.read_source(path)[1])
                    if proof is None:
                        continue
                    line = USER_LINK.sub(
                        lambda link: f'[[daily/{link[1]}#user-{link[2]}|Kullanıcı dayanağı; kapsam: {proof["scope"]}]]',
                        line,
                    )
                lines.append(line)
            text = ''.join(lines)
        if source_relative == PROFILE_RELATIVE and check_profile(
            root, text, read_source=lambda path: self.read_source(path)[1],
        ):
            self._check_source_publication(source_relative)
            return None
        self._check_source_publication(source_relative)
        return sanitize_text(text, max_chars=None)[0]

    def read_source(self, path: Path, *, relative: str | None = None) -> tuple[Path, str | None]:
        """Read a sanitized source inside the vault; None content means exclusion.

        Views retain their supplied lexical identity; other readers use the resolved path.
        Provenance filtering and validation run before sanitization; the source is never
        rewritten. Full source reads skip the bounded ledger-event truncation.
        """
        source = path.resolve(strict=False)
        root = self._vault_root.resolve(strict=True)
        if not source.is_relative_to(root):
            raise MemorySourceError('memory-source-outside-vault')
        source_relative = source.relative_to(root)
        resolved_identity = source_relative.as_posix()
        if resolved_identity == _COMPANION_CANONICAL:
            raise MemorySourceError('memory-source-internal')
        identity = resolved_identity if relative is None else relative
        if self.excludes(identity) or (
            _COMPANION_SOURCE_ALIASES.get(resolved_identity) is not None
            and self.excludes(_COMPANION_SOURCE_ALIASES[resolved_identity])
        ):
            return source_relative, None
        self._check_source_publication(resolved_identity)
        if resolved_identity in _COMPANION_SOURCE_ALIASES.values() and (root / _COMPANION_CANONICAL).is_file():
            from companion_memory import render_views
            text = render_views(root, hashes=self._hashes, memory=self).get(source.name)
            return source_relative, None if text is None else self.project_text(
                identity, text, resolved_relative=resolved_identity,
            )
        return source_relative, self.project_text(
            identity,
            source.read_text(encoding='utf-8'),
            resolved_relative=resolved_identity,
        )

    def profile_issues(self) -> tuple[str, ...]:
        if self.excludes(PROFILE_RELATIVE):
            return ()

        def read(path: Path) -> str | None:
            relative = path.resolve().relative_to(self._vault_root.resolve()).as_posix()
            self._check_source_publication(relative)
            text = None if self.excludes(relative) else self.filter(path.read_text(encoding='utf-8'))
            self._check_source_publication(relative)
            return text

        issues = check_profile(self._vault_root, read_source=read)
        self._check_source_publication(PROFILE_RELATIVE)
        return issues

    def views(
        self,
        sources: Sequence[tuple[str, str]],
        *,
        write: bool = True,
        alias_sources: Sequence[tuple[str, str]] | None = None,
    ) -> dict[str, str]:
        if not self.active or not write:
            return {}
        try:
            for relative, _title in sources:
                self._check_source_publication(relative)
            views = materialize_memory_views(
                self._vault_root,
                sources,
                self._hashes,
                alias_sources=alias_sources,
            )
            if self._publication is not None:
                self.check_knowledge_snapshot()
            return views
        except MemoryPreferenceError:
            raise
        except (OSError, ValueError) as exc:
            if str(exc) == 'memory-preferences-changed':
                raise MemoryPreferenceError('memory-preferences-changed') from exc
            raise MemoryPreferenceError('memory-view-unavailable') from exc

    def render_views(
        self,
        sources: Sequence[tuple[str, str]],
        *,
        alias_sources: Sequence[tuple[str, str]] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Render filtered sources in memory, without creating view files."""
        if not self.active:
            return {}, {}
        try:
            for relative, _title in sources:
                self._check_source_publication(relative)
            views = _render_memory_views(
                self._vault_root,
                sources,
                self._hashes,
                alias_sources=alias_sources,
            )
            if self._publication is not None:
                self.check_knowledge_snapshot()
            return views
        except MemoryPreferenceError:
            raise
        except (OSError, ValueError) as exc:
            raise MemoryPreferenceError('memory-view-unavailable') from exc


@contextmanager
def memory_read(vault_root: Path) -> Iterator[MemoryRead]:
    """Reject a completed read if a concurrent preference change made it stale."""
    private = vault_root / '.codex/private-memory'
    hashes = load_suppressed_hashes(private)
    memory = MemoryRead(vault_root, hashes)
    yield memory
    if memory._publication is not None:
        memory.check_knowledge_snapshot()
    if load_suppressed_hashes(private) != hashes:
        raise MemoryPreferenceError('memory-preferences-changed')


def read_memory_source(vault_root: Path, path: Path) -> str:
    with memory_read(vault_root) as memory:
        _relative, text = memory.read_source(path)
        return text or ''


def _checked_views_dir(private_root: Path) -> Path:
    views = private_root / 'views'
    if views.is_symlink() or views.resolve() != private_root.resolve() / 'views':
        raise MemoryPreferenceError('memory-view-path-invalid')
    return views


def _render_memory_views(
    vault_root: Path,
    sources: Sequence[tuple[str, str]],
    hashes: frozenset[str],
    *,
    alias_sources: Sequence[tuple[str, str]] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return filtered source text and its safe view identities without writing."""
    private = vault_root / '.codex/private-memory'
    if private.is_symlink() or private.resolve() != vault_root.resolve() / '.codex/private-memory':
        raise MemoryPreferenceError('memory-view-path-invalid')
    _checked_views_dir(private)
    targets = {
        relative: vault_root / memory_view_relative_path(relative)
        for relative, _ in sources
    }
    aliases: dict[str, Path | None] = {}
    for relative, title in alias_sources if alias_sources is not None else sources:
        path = PurePosixPath(relative)
        names = (relative.removesuffix('.md'), path.stem, title,
                 relative.removeprefix('knowledge/').removesuffix('.md'))
        target = targets.get(relative)
        for name in names:
            key = name.casefold()
            if key in aliases and aliases[key] != target:
                aliases[key] = None
            else:
                aliases[key] = target

    def link(match: re.Match[str]) -> str:
        parts = match.group(1).replace('\\|', '|').split('|', 1)
        target = parts[0].split('#', 1)[0].removesuffix('.md').casefold()
        label = parts[-1]
        resolved = aliases.get(target)
        return f'[{label}](<{resolved.as_posix()}>)' if resolved else label

    def markdown_link(match: re.Match[str]) -> str:
        label, destination = match.groups()
        destination = destination.strip('<> ')
        if re.match(r'https?://', destination, re.IGNORECASE):
            return match.group(0)
        key = destination.replace('\\', '/').split('#', 1)[0].removesuffix('.md').casefold()
        resolved = aliases.get(key) if key in aliases else aliases.get(PurePosixPath(key).stem)
        return f'[{label}](<{resolved.as_posix()}>)' if resolved else label

    memory = MemoryRead(vault_root, hashes)
    rendered: dict[str, str] = {}
    for relative, _title in sources:
        source = vault_root / relative
        if source.is_symlink():
            raise MemoryPreferenceError('memory-view-source-invalid')
        try:
            _source_relative, projected = memory.read_source(source, relative=relative)
        except MemorySourceError as exc:
            raise MemoryPreferenceError('memory-view-source-invalid') from exc
        projected = projected or ''
        projected = re.sub(r'(?<!!)\[([^\]]*)\]\(([^)]+)\)', markdown_link, projected)
        projected = re.sub(r'\[\[([^\]]+)\]\]', link, projected)
        rendered[relative] = projected
    return rendered, {
        relative: memory_view_relative_path(relative)
        for relative in targets
    }


def materialize_memory_views(
    vault_root: Path,
    sources: Sequence[tuple[str, str]],
    hashes: frozenset[str],
    *,
    alias_sources: Sequence[tuple[str, str]] | None = None,
) -> dict[str, str]:
    """Disposable filtered read targets; never point the agent back at raw notes."""
    private = vault_root / '.codex/private-memory'
    if private.is_symlink() or private.resolve() != vault_root.resolve() / '.codex/private-memory':
        raise MemoryPreferenceError('memory-view-path-invalid')
    views = _checked_views_dir(private)
    with suppression_guard(private, hashes):
        rendered, paths = _render_memory_views(
            vault_root,
            sources,
            hashes,
            alias_sources=alias_sources,
        )
        for relative, projected in rendered.items():
            target = vault_root / paths[relative]
            if target.is_symlink() or target.resolve() != views.resolve() / target.name:
                raise MemoryPreferenceError('memory-view-path-invalid')
            if not target.is_file() or target.read_text(encoding='utf-8') != projected:
                atomic_write_text(target, projected)
    return paths


@contextmanager
def suppression_guard(private_root: Path, expected: frozenset[str]) -> Iterator[None]:
    """Fence a short publication against a concurrent forget request."""
    path = _suppression_path(private_root)
    with locked(path):
        lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
        if _suppression_hashes_from_lines(lines) != expected:
            raise ValueError('memory-preferences-changed')
        yield


def suppress_derived_memory(
    private_root: Path,
    target: str,
    *,
    now: float | None = None,
) -> Path:
    target_hash = memory_text_hash(target)
    path = _suppression_path(private_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if target_hash in _suppression_hashes_from_lines(lines):
            return path
        record = {
            "schema": SUPPRESSION_SCHEMA,
            "ts": int(dt.datetime.now().timestamp() if now is None else now),
            "target_sha256": target_hash,
        }
        # Cached read targets must stop exposing the unit before accepting the rule.
        views = _checked_views_dir(private_root)
        for view in views.glob('*.md'):
            if view.is_symlink() or view.resolve() != views.resolve() / view.name:
                raise MemoryPreferenceError('memory-view-path-invalid')
            if not re.fullmatch(r'[0-9a-f]{64}\.md', view.name):
                raise MemoryPreferenceError('memory-view-owner-unknown')
            # Links were rewritten in views, so source hashes cannot safely edit them.
            # Invalidate; the next search rebuilds from the unchanged raw sources.
            atomic_write_text(view, '[Hafıza görünümü güncel değil; yeni arama gerekli.]\n')
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        atomic_write_text(path, '\n'.join(lines) + '\n')
    return path
