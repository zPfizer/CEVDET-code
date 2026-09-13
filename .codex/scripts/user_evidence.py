"""Bind model citations to allowed user text; this is provenance, not semantic proof."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
import hashlib
import json
import re
from typing import TypedDict

from markdown_boundary import (
    BLOCKQUOTE_PREFIX,
    FENCE_LINE,
    HTML_LITERAL_OPEN,
    HTML_TAG,
    is_escaped,
    markdown_body,
)
from quote_grammar import QUOTED_CONTENT


SOURCE = re.compile(r'<!-- user-source:\s*(\{[^\n]*\})\s*-->')
EVIDENCE = re.compile(r'<!-- user-evidence:\s*(\{[^\n]*\})\s*-->')
LIST_PREFIX = re.compile(r'^[ \t]*(?:[-+*]|\d+[.)])(?:[ \t]+|$)')
USER_ANCHOR = r'(?:#user-[a-f0-9]{64})?'
USER_LINK = re.compile(r'\[\[daily/(\d{4}-\d{2}-\d{2})#user-([a-f0-9]{64})(?:\|[^\]]+)?\]\]')
SCOPES = frozenset({'general', 'project', 'session', 'unspecified'})
EMPTY = frozenset({'', 'yok', 'belirtilmedi', 'karar yok', 'yeni karar yok', 'karar alınmadı', '-'})
# ponytail: bounded approval vocabulary; unfamiliar wording still needs semantic review.
SHORT_APPROVALS = frozenset({'yapalım', 'yap', 'evet', 'tamam', 'olur', 'devam', 'onaylıyorum',
                            'deneyelim', 'yap bakalım', 'uygula', 'uygulayalım'})
CONTEXTUAL_FOLLOWUP = re.compile(
    r'^(?:(?:evet|tamam|olur|yapalım|yap|devam)\s*[,;:]\s*)?'
    r'(?:bunu|bunun|bunları|bunlari|bunların|bunlarin|böyle|boyle)(?!\w)'
)
BARE_BU_FOLLOWUP = re.compile(
    r'^(?:(?:evet|tamam|olur|yapalım|yap|devam)\s*[,;:]\s*)?'
    r'bu(?=\s+(?:öner\w*|yöntem\w*|yaklaşım\w*|karar\w*|çözüm\w*|'
    r'davranış\w*|tercih\w*|özellik\w*|yetenek\w*|kayıt\w*|seçenek\w*|'
    r'plan\w*|gerçekten|olması|olmalı|gerekli|lazım)(?!\w))'
)
NON_REFERENTIAL_STARTS = ('bunun nedeni ', 'bununla birlikte ', 'bunun sonucunda ')


class EvidenceFields(TypedDict):
    schema: int
    claim: str
    quote: str
    scope: str
    message_hash: str
    previous_assistant: str
    captured_at: str


class EvidenceRecord(EvidenceFields):
    id: str


def normalize(text: str) -> str:
    return ' '.join(text.split()).strip().rstrip('.!?').casefold().replace('i\u0307', 'i')


def needs_context(text: str) -> bool:
    """Return whether the text needs the immediately preceding Assistant context."""
    normalized = normalize(text)
    if normalized.startswith(NON_REFERENTIAL_STARTS):
        return False
    return (normalized in SHORT_APPROVALS or normalized == 'bu'
            or bool(CONTEXTUAL_FOLLOWUP.search(normalized))
            or bool(BARE_BU_FOLLOWUP.search(normalized)))


def _encoded(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e')


def _identity(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_encoded(value).encode('utf-8')).hexdigest()


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _evidence_record(value: Mapping[str, object]) -> EvidenceRecord | None:
    schema = value.get('schema')
    claim = value.get('claim')
    quote = value.get('quote')
    scope = value.get('scope')
    message_hash = value.get('message_hash')
    previous_assistant = value.get('previous_assistant')
    captured_at = value.get('captured_at')
    identity = value.get('id')
    if (
        set(value) != {
            'schema', 'claim', 'quote', 'scope', 'message_hash',
            'previous_assistant', 'captured_at', 'id',
        }
        or schema != 1
        or not isinstance(claim, str)
        or not isinstance(quote, str)
        or not isinstance(scope, str)
        or scope not in SCOPES
        or not isinstance(message_hash, str)
        or not isinstance(previous_assistant, str)
        or not isinstance(captured_at, str)
        or not isinstance(identity, str)
    ):
        return None
    return {
        'schema': schema,
        'claim': claim,
        'quote': quote,
        'scope': scope,
        'message_hash': message_hash,
        'previous_assistant': previous_assistant,
        'captured_at': captured_at,
        'id': identity,
    }


def _authored_quote(message: str, quote: str) -> bool:
    # Shares the privacy classifier's quote grammar via the quote_grammar leaf module.
    spans = list(QUOTED_CONTENT.finditer(message)) + list(re.finditer(
        r'<(untrusted_text|quoted_text|message_from_agent|tool_result|assistant)\b[^>]*>[\s\S]*?</\1>'
        r"|(?<!\w)'[^'\n]*'(?!\w)", message, re.I))
    for occurrence in re.finditer(re.escape(quote), message):
        blocked = False
        for span in spans:
            container = span[0].lstrip().startswith(('```', '~~~', '>', '<')) or span[0].startswith(('    ', '\t'))
            enclosed = span.start() <= occurrence.start() and occurrence.end() <= span.end()
            overlaps = occurrence.start() < span.end() and span.start() < occurrence.end()
            if enclosed or (container and overlaps):
                blocked = True
                break
        if not blocked:
            return True
    return False


def _markdown_link_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] != '[' or is_escaped(text, index):
            index += 1
            continue
        label_depth = 1
        label_end = None
        cursor = index + 1
        while cursor < len(text):
            if text[cursor] == '\\':
                cursor += 2
                continue
            if text[cursor] == '[':
                label_depth += 1
            elif text[cursor] == ']':
                label_depth -= 1
                if label_depth == 0:
                    label_end = cursor
                    break
            cursor += 1
        if label_end is None or label_end + 1 >= len(text) or text[label_end + 1] != '(':
            index = max(cursor, index + 1)
            continue
        depth = 1
        cursor = label_end + 2
        while cursor < len(text):
            if text[cursor] == '\\':
                cursor += 2
                continue
            if text[cursor] == '(':
                depth += 1
            elif text[cursor] == ')':
                depth -= 1
                if depth == 0:
                    spans.append((index, cursor + 1))
                    index = cursor + 1
                    break
            cursor += 1
        else:
            index = label_end + 2
    return tuple(spans)


def _visible_source_body(text: str) -> str:
    """Keep visible source markers while hiding examples and code spans."""
    masked = markdown_body(text, mask_frontmatter=False)
    chars = list(masked)
    prefix = '<!-- user-source:'

    def is_lazy_paragraph(line: str) -> bool:
        stripped = line.lstrip(' \t')
        return bool(stripped) and not (
            FENCE_LINE.fullmatch(stripped) is not None
            or LIST_PREFIX.match(line) is not None
            or stripped.startswith('>')
            or re.match(r'^#{1,6}(?:[ \t]+|$)', stripped) is not None
            or re.fullmatch(r'(?:[-*_][ \t]*){3,}|=+[ \t]*', stripped) is not None
            or HTML_LITERAL_OPEN.match(stripped) is not None
        )

    lazy_blockquote_starts: set[int] = set()
    quote_paragraph = False
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip('\r\n')
        blockquote = BLOCKQUOTE_PREFIX.match(line)
        if blockquote is not None:
            remainder = line[blockquote.end():]
            while (nested := BLOCKQUOTE_PREFIX.match(remainder)) is not None:
                remainder = remainder[nested.end():]
            quote_paragraph = is_lazy_paragraph(remainder)
        elif not line.strip():
            quote_paragraph = False
        elif quote_paragraph and is_lazy_paragraph(line):
            lazy_blockquote_starts.add(offset)
        else:
            quote_paragraph = False
        offset += len(raw_line)
    link_spans = _markdown_link_spans(text)
    for match in SOURCE.finditer(text):
        line_start = text.rfind('\n', 0, match.start()) + 1
        line_prefix = text[line_start:match.start()]
        in_blockquote = False
        while True:
            if BLOCKQUOTE_PREFIX.match(line_prefix) is not None:
                in_blockquote = True
                break
            list_prefix = LIST_PREFIX.match(line_prefix)
            if list_prefix is None:
                break
            line_prefix = line_prefix[list_prefix.end():]
        if (
            line_start in lazy_blockquote_starts
            or in_blockquote
            or any(start < match.start() < end for start, end in link_spans)
            or any(
                tag.start() < match.start() < tag.end()
                for tag in HTML_TAG.finditer(text)
            )
        ):
            chars[match.start():match.end()] = ' ' * (match.end() - match.start())
            continue
        end = match.start() + len(prefix)
        if masked[match.start():end] == text[match.start():end]:
            chars[match.start():match.end()] = text[match.start():match.end()]
    return ''.join(chars)


def _record(
    claim: str,
    citation: Mapping[str, object],
    turns: Sequence[tuple[str, str]],
    captured_at: str,
) -> EvidenceRecord | None:
    quote, scope = citation.get('quote'), citation.get('scope')
    if not isinstance(quote, str) or not quote.strip() or scope not in SCOPES:
        return None
    found = [i for i, (role, text) in enumerate(turns) if role == 'user' and _authored_quote(text, quote)]
    if not found:
        return None
    index = found[-1]
    message = turns[index][1]
    previous = turns[index - 1][1] if index and turns[index - 1][0] == 'assistant' else ''
    context_dependent = needs_context(message) or needs_context(quote)
    if context_dependent and not previous:
        return None
    scope_text = normalize(message + ('\n' + previous if context_dependent else ''))
    if re.search(r'\b(?:bu (?:cevapta|yanıtta|yanitta|konuşmada|konusmada|oturumda|işte|iste|görevde)|şimdilik|simdilik)\b', scope_text):
        scope = 'session'
    elif re.search(r'\b(?:bu projede|projesinde|projemde)\b', scope_text):
        scope = 'project'
    unsigned: EvidenceFields = {
        'schema': 1,
        'claim': claim,
        'quote': quote,
        'scope': scope,
        'message_hash': hashlib.sha256(message.encode('utf-8')).hexdigest(),
        'previous_assistant': previous if context_dependent else '',
        'captured_at': captured_at,
    }
    record: EvidenceRecord = {**unsigned, 'id': _identity(unsigned)}
    return record


def bind_evidence(
    sections: dict[str, str], turns: Sequence[tuple[str, str]], captured_at: str,
    *, previous_summary: str = '', vault_root: Path | None = None,
    memory_reader: Callable[[Path], object] | None = None,
) -> dict[str, str]:
    """Only code creates evidence. Unbacked decisions remain explicitly uncertain synthesis.

    Önceki özet doğrulanacaksa okuyucu dışarıdan verilir (memory_ledger'ın
    memory_read'i); bu modül ledger'ı import etmez, bağımlılık oku tek yönde
    kalır (ledger → evidence).
    """
    day = datetime.fromisoformat(captured_at).date().isoformat()
    result: dict[str, str] = {}
    records: dict[str, EvidenceRecord] = {}
    uncertain: list[str] = []
    prior: dict[str, EvidenceRecord] = {}
    if previous_summary and vault_root is not None:
        if memory_reader is None:
            raise ValueError('user-evidence-memory-reader-required')
        with memory_reader(vault_root) as memory:
            for line in previous_summary.splitlines():
                proof = proof_for_link(vault_root, line, reader=lambda path: memory.read_source(path)[1])
                if proof:
                    prior[normalize(proof['claim'])] = proof
    for section, body in sections.items():
        lines: list[str] = []
        # Model output cannot mint a trusted record or reuse an old evidence link.
        body = EVIDENCE.sub('', body)
        body = USER_LINK.sub('', body)
        visible_body = _visible_source_body(body)
        logical_lines: list[tuple[str, str]] = []
        for line, visible_line in zip(body.splitlines(), visible_body.splitlines()):
            # Models sometimes put the citation directly below its list item.
            # Never cross a blank line/section or silently choose among citations.
            if (SOURCE.fullmatch(visible_line.strip()) and logical_lines
                    and logical_lines[-1][1].lstrip().startswith(('- ', '* ', '+ '))):
                raw, visible = logical_lines[-1]
                logical_lines[-1] = (
                    raw + ' ' + line.strip(),
                    visible + ' ' + visible_line.strip(),
                )
            else:
                logical_lines.append((line, visible_line))
        for line, visible_line in logical_lines:
            citations = list(SOURCE.finditer(visible_line))
            clean = SOURCE.sub('', line).strip()
            claim = re.sub(r'^[-*+]\s+', '', clean).strip()
            record = None
            if len(citations) == 1 and claim:
                try:
                    raw_marker = line[citations[0].start():citations[0].end()]
                    raw_match = SOURCE.fullmatch(raw_marker)
                    citation = (
                        _json_object(json.loads(raw_match.group(1)))
                        if raw_match is not None
                        else None
                    )
                    if citation is not None:
                        record = _record(claim, citation, turns, captured_at)
                except (ValueError, TypeError):
                    pass
            if record:
                records[record['id']] = record
                lines.append(f'- {claim} [[daily/{day}#user-{record["id"]}|Kullanıcı dayanağı; kapsam: {record["scope"]}]]')
            elif (
                visible_line.strip()
                and (retained := prior.get(normalize(claim))) is not None
            ):
                # Only an unchanged claim with a freshly verified source may survive
                # transcript compaction. Model-supplied evidence identities stay ignored.
                records[retained['id']] = retained
                original_day = retained['captured_at'][:10]
                lines.append(f'- {retained["claim"]} [[daily/{original_day}#user-{retained["id"]}|Kullanıcı dayanağı; kapsam: {retained["scope"]}]]')
            elif (section == 'Alınan Kararlar' and normalize(claim) not in EMPTY) or citations:
                if claim:
                    uncertain.append(f'- `cevo-cikarimi` `belirsiz` — {claim}')
            else:
                lines.append(clean)
        result[section] = '\n'.join(lines).strip()
    if uncertain:
        result['Öğrenilenler'] = '\n'.join(filter(None, (result.get('Öğrenilenler', ''), *uncertain)))
    if records:
        evidence_text = '\n'.join(f'<!-- user-evidence: {_encoded(record)} -->' for record in records.values())
        result['Önemli Konuşmalar'] = '\n'.join(filter(None, (result.get('Önemli Konuşmalar', ''), evidence_text)))
    if sum(map(len, result.values())) > 65_536:
        raise ValueError('user-evidence-budget')
    return result


def evidence_for(daily: str, identity: str, claim: str) -> EvidenceRecord | None:
    matches: list[EvidenceRecord] = []
    for match in EVIDENCE.finditer(daily):
        try:
            record = _json_object(json.loads(match.group(1)))
            if record is None or record.get('id') != identity:
                continue
            parsed = _evidence_record(record)
            if parsed is None:
                return None
            unsigned = {key: value for key, value in parsed.items() if key != 'id'}
            if (set(unsigned) != {'schema', 'claim', 'quote', 'scope', 'message_hash', 'previous_assistant', 'captured_at'}
                    or not parsed['quote'].strip() or not re.fullmatch('[a-f0-9]{64}', parsed['message_hash'])
                    or _identity(unsigned) != identity or normalize(parsed['claim']) != normalize(claim)):
                return None
            datetime.fromisoformat(parsed['captured_at'])
            matches.append(parsed)
        except (ValueError, TypeError):
            return None
    return matches[0] if len(matches) == 1 else None


def filter_evidence(text: str, excludes: Callable[[str], bool]) -> str:
    """Decode before forgetting: encoded JSON must not retain a hidden quotation."""
    hidden: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        try:
            record = _json_object(json.loads(match.group(1)))
            if record is None:  # pragma: no cover — EVIDENCE {...} deseni json nesnesi garantiler; savunma hattı.
                return ''
            record_id = record.get('id')
            claim = record.get('claim')
            quote = record.get('quote')
            previous_assistant = record.get('previous_assistant')
            if (
                not isinstance(record_id, str)
                or not isinstance(claim, str)
                or not isinstance(quote, str)
                or not isinstance(previous_assistant, str)
            ):
                return ''
            original_day = str(record.get('captured_at', ''))[:10]
            if (excludes(f'daily/{original_day}.md')
                    or excludes(claim) or excludes(quote) or excludes(previous_assistant)):
                hidden.add(record_id)
                return ''
        except (ValueError, TypeError):
            return ''
        return match.group(0)
    text = EVIDENCE.sub(replace, text)
    return ''.join(line for line in text.splitlines(keepends=True)
                   if not any(match.group(2) in hidden for match in USER_LINK.finditer(line)))


def proof_for_link(
    root: Path,
    line: str,
    reader: Callable[[Path], str | None] | None = None,
) -> EvidenceRecord | None:
    links = list(USER_LINK.finditer(line))
    if len(links) != 1:
        return None
    link = links[0]
    path = root / 'daily' / (link.group(1) + '.md')
    try:
        if path.resolve() != root.resolve() / 'daily' / path.name or path.is_symlink():
            return None
        text = path.read_text(encoding='utf-8') if reader is None else reader(path)
    except (OSError, UnicodeError, ValueError):
        return None
    if text is None:
        return None
    claim = line.partition(' — ')[2] if ' — ' in line else line[:link.start()]
    claim = re.sub(r'^\s*[-*+]\s+', '', claim).strip()
    record = evidence_for(text, link.group(2), claim)
    return record if record and record['captured_at'][:10] == link.group(1) else None
