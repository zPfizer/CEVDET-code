from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import datetime as dt
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tokenize
from typing import Callable, Iterator, Sequence
import unicodedata

from file_lock import locked
from compile_state import PolicyError, PublicationSnapshot, require_publication_snapshot
from state_store import atomic_write_text
from profile_guard import PROFILE_RELATIVE, check_profile
from user_evidence import filter_evidence, USER_LINK, proof_for_link


MAX_EVENT_CHARS = 65_536
SUPPRESSION_SCHEMA = 1
PERSISTENT_TURNS_VERSION = "persistent-turns-v2"
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
_ENV_CREDENTIAL_NAME_BODY = (
    r'(?:[A-Z][A-Z0-9]*_)*(?:API_KEY|ACCESS_KEY(?:_ID)?|PASSWORD|'
    r'SECRET(?:_(?:ACCESS_)?KEY)?|(?:ACCESS|API|AUTH|CLIENT|CSRF|GITHUB|'
    r'MY|OAUTH|REFRESH|SESSION|SERVICE)_TOKEN)'
)
_ENV_CREDENTIAL_NAME = rf'(?-i:{_ENV_CREDENTIAL_NAME_BODY})'
CREDENTIAL_NAME = rf'api[_-]?key|password|secret|token|{_ENV_CREDENTIAL_NAME}'
CREDENTIAL_NAME_RE = re.compile(rf'(?i)^(?:{CREDENTIAL_NAME})$')
CREDENTIAL = re.compile(
    r'''(?im)(?P<prefix>(?P<key_quote>["']?)\b(?P<key>''' + CREDENTIAL_NAME + r''')'''
    r'''(?P=key_quote)\s*[:=]\s*)'''
    r'''(?P<value>[{\[]|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|(?:\\[\s\S]|[^\s\\])+)'''
)
BATCH_CREDENTIAL_NAME = rf'(?i:api[_-]?key|password|secret|token|{_ENV_CREDENTIAL_NAME_BODY})'
_BATCH_IF_CONDITION = (
    r'if\b[ \t]+(?:/i[ \t]+)?(?:not[ \t]+)?(?:'
    r'(?:exist|defined|errorlevel|cmdextversion)\b[^\r\n]*?|'
    r'[^\r\n]*?(?:==|equ\b|neq\b|lss\b|leq\b|gtr\b|geq\b)[^\r\n]*?)'
    r'[ \t]+'
)
_BATCH_FOR_COMMAND = (
    r'for\b[ \t]+(?=[^\r\n]*%{1,2}[A-Za-z_][A-Za-z0-9_]*\b)'
    r'[^\r\n]*?\bdo[ \t]+'
)
_BATCH_CONTROL_PREFIX = (
    r'(?:^[ \t]*|(?<=[&|<>()])[ \t]*)@?(?:'
    + _BATCH_IF_CONDITION
    + r'|'
    + _BATCH_FOR_COMMAND
    + r')@?[ \t]*'
)
_BATCH_CALL_PREFIX = r'(?:^[ \t]*|(?<=[&|<>()])[ \t]*)@?call[ \t]+@?[ \t]*'
_BATCH_ELSE_PREFIX = r'(?<=\))[ \t]*@?else[ \t]+@?[ \t]*'
_BATCH_COMMAND_PREFIX = (
    r'(?:^[ \t]*@?[ \t]*|(?<=[&|<>()])[ \t]*@?[ \t]*|'
    + _BATCH_CALL_PREFIX
    + r'|'
    + _BATCH_ELSE_PREFIX
    + r'|'
    + _BATCH_CONTROL_PREFIX
    + r')set[ \t]+'
)
BATCH_CREDENTIAL = re.compile(
    r'''(?im)(?P<prefix>''' + _BATCH_COMMAND_PREFIX + r'''(?P<quote>["']))(?P<key>''' + BATCH_CREDENTIAL_NAME + r''')\s*=\s*'''
    r'''(?P<value>(?:(?!(?P=quote))[^\r\n])*)(?P=quote)'''
)
BATCH_CREDENTIAL_START = re.compile(
    r'''(?im)''' + _BATCH_COMMAND_PREFIX + r'''(?P<quote>["'])(?P<key>''' + BATCH_CREDENTIAL_NAME + r''')\s*=\s*'''
)
BATCH_CREDENTIAL_UNQUOTED = re.compile(
    r'''(?im)(?P<prefix>''' + _BATCH_COMMAND_PREFIX + r''')(?P<key>''' + BATCH_CREDENTIAL_NAME + r''')[ \t]*=[ \t]*'''
)
BATCH_ASSIGNMENT_PREFIX = re.compile(
    r'''(?im)''' + _BATCH_COMMAND_PREFIX + r'''["']?\Z'''
)
BATCH_CMD_WRAPPER_CREDENTIAL = re.compile(
    r'''(?im)(?:^[ \t]*|(?<=[&|<>()])[ \t]*)@?cmd[ \t]+/c[ \t]+"?set[ \t]+'''
    r'''(?P<key>''' + BATCH_CREDENTIAL_NAME + r''')[ \t]*=[ \t]*'''
)
POWERSHELL_CREDENTIAL = re.compile(
    r'''(?im)(?P<prefix>\$(?:(?i:env):[ \t]*|\{(?i:env):[ \t]*))'''
    r'''(?P<key>''' + BATCH_CREDENTIAL_NAME + r''')(?P<closing>\}?)(?P<assignment>[ \t]*=[ \t]*)'''
)
POWERSHELL_ASSIGNMENT_PREFIX = re.compile(r'''(?im)(?:\$(?i:env):|\$\{(?i:env):)[ \t]*\Z''')
TOKEN_PREFIX = re.compile(r"\b(?:sk(?=[-_])|ghp|github_pat|AKIA)[-_A-Za-z0-9]{12,}\b")
PERSONAL_CREDENTIAL = re.compile(
    r"(?i)\b(?:api\s+anahtarım|parolam|şifrem|tokenım)\b"
    r"(?:\s*[:=]\s*|\s+)(?:şu\s+|bu\s+)?\S[^\r\n]*"
)
CONTROL_TRAILING = re.compile(r"[\s.!?]+\Z")
CONTROL_SEPARATOR = re.compile(r"[\s.,:;!?…\u2012\u2013\u2014\-]+")
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
_QUOTED_CASE_SUFFIX = r"(?:(?i:['’]?y?[ıiuü])(?!\w))?"
_QUOTED_CASE_SUFFIX_RE = re.compile(_QUOTED_CASE_SUFFIX)
QUOTED_CONTENT = re.compile(
    r'(?:(?ms:^[ \t]*(?P<fence>(?P<fence_char>`|~)(?P=fence_char){2,})[^\r\n]*\r?\n'
    r'(?P<fenced_body>.*?)(?:^[ \t]*(?P=fence)(?P=fence_char)*[ \t]*\r?$|\Z))|'
    # Embedded multiline snippets remain data; only the named line-fence
    # branch can be unwrapped as a whole-message read-only restriction.
    r'```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|'
    r'(?m:^[ \t]*>[^\n]*|^(?: {4}|\t)[^\n]*)|'
    r'`[^`\n]*`|"[^"\n]*"|“[^”]*”|‘[^’]*’|«[^»]*»'
    r')' + _QUOTED_CASE_SUFFIX
)
_QUOTE_PAIRS = (
    ("'", "'"), ('"', '"'), ("“", "”"), ("‘", "’"),
    ("`", "`"), ("«", "»"),
)
_QUOTED_DIRECTORY_PREFIX = (
    r"(?:[a-z]:[\\/]?|\.{1,2}[\\/]|\\\\[\w.-]+[\\/][\w.-]+[\\/]?|[\w.-]+[\\/])"
)
_WRITE_EXTENSION = r"[\w-]+"
DO_NOT_SAVE = r'(?:(?:bunu|bu bilgiyi|bu ayrıntıyı)\s+)?(?:kaydetme|saklama|hafızana alma|hafızanda tutma|kaydetmeni istemiyorum)'
STANDALONE_DO_NOT_SAVE = (
    r'(?:lütfen\s+)?' + DO_NOT_SAVE
    + r'(?:\s+lütfen)?'
)
READ_ONLY_REQUEST = re.compile(
    r"\b(?:salt[ -]?okunur|read[ -]?only|sadece\s+incele|"
    r"hiçbir\s+dosyayı\s+değiştirme|dosyaları\s+değiştirme|"
    r"dosya\s+değiştirmeden|değişiklik\s+yapmadan|"
    r"do\s+not\s+(?:modify|change|touch)\s+files(?:\s+or\s+settings)?|"
    r"don['’]t\s+(?:modify|change|touch)\s+files(?:\s+or\s+settings)?|"
    r"no\s+(?:file|files)\s+(?:changes?|modifications?))\b"
)
SESSION_ONLY_REQUEST = re.compile(
    r"\b(?:bu (?:konuşmada|sohbette|oturumda|sohbet aramızda)|aramızda) kalsın\b"
)
FORGET_REQUEST = re.compile(
    r"\b(?:unut(?:ur\s+musun)?|hafızandan\s+(?:çıkar|sil)|"
    r"hatırlamanı\s+istemiyorum)\b"
)
DO_NOT_SAVE_REQUEST = re.compile(r"\b" + DO_NOT_SAVE + r"\b")
WHAT_KNOWN_REQUEST = re.compile(r"\bbenim\s+hakkımda\s+ne\s+biliyorsun\b")
CORRECT_REQUEST = re.compile(r"\bdüzelt\b")
_MEMORY_CONTROL_PATTERNS = (
    READ_ONLY_REQUEST,
    SESSION_ONLY_REQUEST,
    FORGET_REQUEST,
    DO_NOT_SAVE_REQUEST,
    WHAT_KNOWN_REQUEST,
    CORRECT_REQUEST,
)
_ACK_SEPARATOR = r"[,;:.!]"
EXPLICIT_WRITE_INTENT = re.compile(
    r"^\s*(?:"
    rf"(?:lütfen\s+)?(?:(?:ok|okay|tamam)\s*{_ACK_SEPARATOR}?\s*)?(?:lütfen\s+)?"
    r"(?:s[ıi]rayla(?:\s+hepsini)?|hepsini(?:\s+s[ıi]rayla)?)\s+(?:yap|uygula)|"
    rf"(?:ok|okay|tamam)\s*{_ACK_SEPARATOR}?\s*(?:yap|uygula)|"
    r"uygula|"
    r"bunu\s+düzelt|"
    r"gerekli\s+değişiklikleri\s+yap|"
    r"önerdiğin\s+değişiklikleri\s+uygula|"
    r"(?:bunu|öneriyi|değişikliği|değişiklikleri|önerilen\s+değişiklikleri)"
    r"\s+(?:yap|uygula)|"
    r"(?:dosyayı|dosyaları|ayarı|ayarları|kodu)\s+(?:değiştir|düzenle|uygula)|"
    r"(?:uygulamaya|yazmaya|değişikliğe)\s+geç|"
    r"(?:(?:dosyaları\s+)?(?:değiştirebilirsin|düzenleyebilirsin|"
    r"uygulayabilirsin)|dosyaları\s+yazabilirsin)"
    r")\s*[.!]*\s*$"
)

_CONCRETE_WRITE_MUTATION = (
    r"(?:oluştur(?:un(?:uz)?)?|güncelle(?:yin(?:iz)?)?|yaz(?:ın(?:ız)?)?)"
)
_WRITE_MUTATION = (
    r"(?:düzelt(?:in(?:iz)?)?|değiştir(?:in(?:iz)?)?|"
    r"düzenle(?:yin(?:iz)?)?|uygula(?:yın(?:ız)?)?|onar(?:ın(?:ız)?)?|"
    rf"{_CONCRETE_WRITE_MUTATION})"
)
_WRITE_QUESTION_VERB = (
    r"(?:düzeltebilir|düzeltir|değiştirebilir|değiştirir|"
    r"düzenleyebilir|düzenler|uygulayabilir|uygular|onarabilir|onarır)"
)
_CONCRETE_WRITE_QUESTION_VERB = (
    r"(?:oluşturabilir|oluşturur|güncelleyebilir|günceller|yazabilir|yazar)"
)
_CONCRETE_WRITE_PERMISSION_VERB = (
    r"(?:düzeltebilir|değiştirebilir|düzenleyebilir|uygulayabilir|onarabilir|"
    r"oluşturabilir|güncelleyebilir|yazabilir)sin(?:iz)?"
)
_TARGETED_WRITE_QUESTION_VERB = (
    rf"(?:{_WRITE_QUESTION_VERB}|{_CONCRETE_WRITE_QUESTION_VERB})"
)
_QUESTION_SUFFIX = r"m[ıiuü]s[ıiuü]n(?:iz|ız|uz|üz)?"
_WRITE_FOLDER_OBJECT = r"klasör(?:ü|ünü|leri|lerini)?"
_WRITE_FILE_OBJECT = r"dosya(?:yı|sını|ları|larını|mı|nı)?"
_WRITE_SETTING_OBJECT = r"ayar(?:ı|ını|ları|larını|ımı)?"
_WRITE_FILENAME = rf"(?:[\w.-]+\.{_WRITE_EXTENSION}|\.{_WRITE_EXTENSION})"
_WRITE_LOCATIVE_SUFFIX = r"['’][dt][ae]ki"
_WRITE_TARGET_OBJECT = (
    r"(?:hata(?:yı|sını|ları)?|sorun(?:u|unu|ları)?|bug(?:ı|u|unu|ları)?|"
    rf"{_WRITE_FILE_OBJECT}|"
    rf"{_WRITE_FOLDER_OBJECT}|"
    r"kod(?:u|unu|ları|larını)?|değişiklik(?:i|ini|leri|lerini)?|"
    rf"{_WRITE_SETTING_OBJECT})"
)
_WRITE_FILE_MEMBER = rf"(?:dosya(?:daki|deki|sındaki|sindeki)|{_WRITE_FILENAME}{_WRITE_LOCATIVE_SUFFIX})\s+{_WRITE_TARGET_OBJECT}"
_WRITE_FOLDER_MEMBER = rf"klasör(?:deki|ündeki)\s+{_WRITE_TARGET_OBJECT}"
_WRITE_FILE_TARGET = (
    rf"(?:[\w.-]+\s+)?(?:{_WRITE_FILE_OBJECT}|{_WRITE_FILE_MEMBER}|"
    rf"{_WRITE_FOLDER_MEMBER}|{_WRITE_FOLDER_OBJECT})"
)
_WRITE_PROJECT_MEMBER = rf"proje(?:sindeki|deki)\s+{_WRITE_TARGET_OBJECT}"
_WRITE_PROJECT_TARGET = rf"(?:[\w.-]+\s+)?{_WRITE_PROJECT_MEMBER}"
_WRITE_PROJECT_OBJECT = r"proje(?:yi|si(?:ni)?|ler(?:i(?:ni)?)?)?"
_WRITE_PROJECT_OBJECT_TARGET = rf"(?:[\w.-]+\s+)?{_WRITE_PROJECT_OBJECT}"
_WRITE_MODULE_TARGET = (
    rf"[\w.-]+\s+modül(?:deki|ündeki)\s+{_WRITE_TARGET_OBJECT}"
)
_QUOTED_STRAIGHT_BODY = r"(?:[^'\r\n]|(?<=\w)'(?=\w))"
_QUOTED_PATH = (
    "(?:"
    + "|".join(
        rf"{re.escape(opening)}"
        + (
            rf"{_QUOTED_STRAIGHT_BODY}*"
            if opening == "'"
            else rf"[^{re.escape(closing)}\r\n]*"
        )
        + rf"\.{_WRITE_EXTENSION}{re.escape(closing)}{_QUOTED_CASE_SUFFIX}"
        for opening, closing in _QUOTE_PAIRS
    )
    + ")"
)
_QUOTED_FILENAME = (
    "(?:"
    + "|".join(
        rf"{re.escape(opening)}"
        + (
            rf"{_QUOTED_STRAIGHT_BODY}+"
            if opening == "'"
            else rf"[^{re.escape(closing)}\r\n]+"
        )
        + rf"{re.escape(closing)}{_QUOTED_CASE_SUFFIX}"
        for opening, closing in _QUOTE_PAIRS
    )
    + ")"
)
_QUOTED_DIRECTORY = (
    "(?:"
    + "|".join(
        rf"{re.escape(opening)}{_QUOTED_DIRECTORY_PREFIX}"
        + (
            rf"{_QUOTED_STRAIGHT_BODY}*?"
            if opening == "'"
            else rf"[^{re.escape(closing)}\r\n]*?"
        )
        + rf"{re.escape(closing)}{_QUOTED_CASE_SUFFIX}"
        for opening, closing in _QUOTE_PAIRS
    )
    + ")"
)
_WRITE_CASE_SUFFIX = r"['’]y?[ıiuü]"
_WRITE_TARGET_SUFFIX = rf"(?:\s+(?:{_WRITE_TARGET_OBJECT}|{_WRITE_FILE_MEMBER}|{_WRITE_FOLDER_MEMBER})|{_WRITE_CASE_SUFFIX})?"
_WRITE_PATH_COMPONENT = r"[^\\/\s<>:\"|?*\x00-\x1f]+"
_WRITE_PATH = (
    rf"(?:[a-z]:[\\/]?|\.{{1,2}}[\\/]|[\\/](?![\\/])|"
    rf"\\\\{_WRITE_PATH_COMPONENT}[\\/]{_WRITE_PATH_COMPONENT}[\\/]|"
    rf"{_WRITE_PATH_COMPONENT}[\\/])"
    rf"{_WRITE_PATH_COMPONENT}(?:[\\/]{_WRITE_PATH_COMPONENT})*"
)
_WRITE_DIRECTORY_PATH = rf"(?:{_WRITE_PATH}[\\/]|{_WRITE_PATH_COMPONENT}[\\/])"
_WRITE_DIRECTORY_ROOT = (
    rf"(?:[a-z]:[\\/]|[\\/](?![\\/])|"
    rf"\\\\{_WRITE_PATH_COMPONENT}[\\/]{_WRITE_PATH_COMPONENT}[\\/]?)"
)
_EXPLICIT_WRITE_PATH_PREFIX = re.compile(
    r"(?:[a-z]:|\.{1,2}[\\/]|[\\/](?![\\/])|"
    rf"\\\\{_WRITE_PATH_COMPONENT}[\\/]{_WRITE_PATH_COMPONENT}[\\/]?)"
)
_MEMORY_TARGET_TOKEN = re.compile(
    rf"(?:[a-z]:[\\/]?|\.{{1,2}}[\\/]|[\\/]{{1,2}})?"
    rf"{_WRITE_PATH_COMPONENT}(?:[\\/]{_WRITE_PATH_COMPONENT})*"
)
_CONCRETE_WRITE_TARGET_PATTERN = (
    rf"(?:{_WRITE_PROJECT_TARGET}|{_WRITE_MODULE_TARGET}|"
    rf"{_WRITE_DIRECTORY_ROOT}\s+{_WRITE_FOLDER_OBJECT}|"
    rf"{_WRITE_DIRECTORY_PATH}\s+{_WRITE_FOLDER_OBJECT}|"
    rf"{_WRITE_PROJECT_OBJECT_TARGET}|{_WRITE_FILE_TARGET}|"
    rf"{_WRITE_PATH}{_WRITE_TARGET_SUFFIX}|"
    rf"{_WRITE_FILENAME}{_WRITE_TARGET_SUFFIX}|"
    rf"{_QUOTED_DIRECTORY}\s+(?:{_WRITE_FOLDER_OBJECT}|{_WRITE_FOLDER_MEMBER})|"
    rf"{_QUOTED_PATH}{_WRITE_TARGET_SUFFIX}|"
    rf"{_QUOTED_FILENAME}\s+(?:{_WRITE_FOLDER_OBJECT}|{_WRITE_FOLDER_MEMBER}|"
    rf"{_WRITE_FILE_OBJECT}|{_WRITE_FILE_MEMBER}))"
)
_WRITE_TARGET = (
    rf"(?:bunu|bunları|şunu|şunları|onu|onları|"
    rf"(?:bu|şu|o)\s+{_WRITE_TARGET_OBJECT}|"
    rf"{_CONCRETE_WRITE_TARGET_PATTERN}|"
    rf"{_WRITE_PROJECT_OBJECT_TARGET}|"
    rf"{_WRITE_TARGET_OBJECT}|"
    rf"{_WRITE_PATH}"
    rf"{_WRITE_TARGET_SUFFIX}|"
    rf"{_WRITE_FILENAME}{_WRITE_TARGET_SUFFIX})"
)
_CONCRETE_WRITE_TARGET = re.compile(_CONCRETE_WRITE_TARGET_PATTERN)
_CONCRETE_WRITE_VERB = re.compile(
    rf"(?:{_CONCRETE_WRITE_MUTATION}|{_CONCRETE_WRITE_QUESTION_VERB}|"
    rf"{_CONCRETE_WRITE_PERMISSION_VERB})"
)
_TARGETED_WRITE_PREFIX = rf"(?:(?:acaba|lütfen|ok|okay|tamam|sonra)(?:{_ACK_SEPARATOR}\s++|\s++))*"
_TRAILING_POLITENESS = r"(?:(?:\s*+,\s*+|\s++)lütfen)?"
# ponytail: only this test-running suffix; broader compound sentences need shared sentence parsing.
_WRITE_FOLLOWUP = r"(?:\s+ve\s+testleri\s+çalıştır|[.!]\s+sonra\s+testleri\s+çalıştır)?"
TARGETED_WRITE_COMMAND = re.compile(
    rf"^\s*{_TARGETED_WRITE_PREFIX}(?P<target>{_WRITE_TARGET})\s+"
    rf"(?P<mutation>{_WRITE_MUTATION})"
    rf"{_TRAILING_POLITENESS}{_WRITE_FOLLOWUP}\s*+[.!]*\s*+$"
)
TARGETED_WRITE_QUESTION = re.compile(
    rf"^\s*{_TARGETED_WRITE_PREFIX}(?P<target>{_WRITE_TARGET})\s+"
    rf"(?P<mutation>{_TARGETED_WRITE_QUESTION_VERB})\s+"
    rf"{_QUESTION_SUFFIX}{_TRAILING_POLITENESS}\?\s*+$"
)
TARGETED_WRITE_PERMISSION = re.compile(
    rf"^\s*{_TARGETED_WRITE_PREFIX}(?P<target>{_WRITE_TARGET})\s+"
    rf"(?P<mutation>{_CONCRETE_WRITE_PERMISSION_VERB})"
    rf"{_TRAILING_POLITENESS}\s*+[.!]*\s*+$"
)
BARE_WRITE_QUESTION = re.compile(
    rf"^\s*{_TARGETED_WRITE_PREFIX}{_WRITE_QUESTION_VERB}\s+{_QUESTION_SUFFIX}"
    rf"{_TRAILING_POLITENESS}\?\s*+$"
)
NON_COMMITTAL_WRITE = re.compile(
    r"\b(?:eğer|şayet|uygunsa|mümkünse|istersen(?:iz)?|gerekirse|"
    r"olursa|belki|san[ıi]r[ıi]m|sak[ıi]n|asla|hiç(?:bir)?|"
    r"galiba|muhtemelen|herhalde)\b"
)
_CONDITIONAL_PERSON = (
    r"(?:sa|se|sam|sem|san|sen|sak|sek|salar|seler|sınız|siniz|"
    r"sunuz|sünüz|sanız|seniz)"
)
CONDITIONAL_WRITE = re.compile(
    r"\b(?:var|yok)(?:sa|se)\b|"
    r"\b\w+(?:(?:[ıiuü]r|[ae]r|[uü]r|m[ae]z|acak|ecek|iyor|ıyor|uyor|"
    r"miş|mış|muş|müş)(?:sa|se|sam|sem|san|sen|sak|sek|salar|seler|"
    r"sınız|siniz|sunuz|sünüz|sanız|seniz)|d[iıuü]y?(?:sa|se|sam|sem|"
    r"san|sen|sak|sek|salar|seler|sınız|siniz|sunuz|sünüz|sanız|seniz)|"
    r"y(?:sa|se|sam|sem|san|sen|sak|sek|salar|seler|sınız|siniz|sunuz|"
    r"sünüz|sanız|seniz)|"
    r"(?:ince|ınca|unca|ünce|diğinde|dığında|duğunda|düğünde|ken)|"
    r"madan|meden|madıkça|medikçe|s[ıiuü]z(?:sa|se))\b|"
    rf"\b\w+m[ae]{_CONDITIONAL_PERSON}\b|"
    r"\b(?:takdirde|halinde|durumunda|sonra|kadar)\b"
)
_QUESTION_PLURAL_SUFFIX = (
    r"ler(?:inin|ine|ini|inde|inden|indeki|in(?:de|den)?|e|i|de|den)?"
)
_QUESTION_NE = (
    rf"ne(?:yin|ye|yi|de|den|{_QUESTION_PLURAL_SUFFIX}(?:ki)?|"
    r"si(?:nin|ne|ni|nde|nden|ndeki)?)?"
)
_QUESTION_HANGI = (
    rf"hangi(?:si(?:nin|ne|ni|nde|nden|ndeki)?|"
    rf"{_QUESTION_PLURAL_SUFFIX}(?:ki)?|nin|ne|yi|de|den|deki)?"
)
_QUESTION_KIM = (
    rf"kim(?:in|e|i|de|den|{_QUESTION_PLURAL_SUFFIX}(?:ki)?)?(?:ki)?"
)
_QUESTION_NERE = (
    rf"nere(?:si(?:nin|ne|ni|nde|nden|ndeki)?|"
    rf"{_QUESTION_PLURAL_SUFFIX}(?:ki)?|nin|ye|yi|de|den|deki)?"
)
_QUESTION_KAC = (
    r"kaç(?:ıncı(?:sı(?:nın|na|nı|nda|ndan|ndaki)?|"
    r"lar(?:ın|a|ı|da|dan)?(?:ki)?)?|ın|a|ı|ta|tan)?"
)
ACTION_QUESTION_WORD = re.compile(
    rf"\b(?:{_QUESTION_NE}|nasıl|niçin|niye|sence|sizce|{_QUESTION_HANGI}|"
    rf"{_QUESTION_KIM}|{_QUESTION_NERE}|{_QUESTION_KAC}|ne\s+zaman)\b"
)
_NAMED_TARGET = re.compile(
    rf"^(?P<name>[\w.-]+)\s+(?:{_WRITE_PROJECT_OBJECT}|"
    rf"{_WRITE_PROJECT_MEMBER}|"
    rf"modül(?:deki|ündeki)\s+{_WRITE_TARGET_OBJECT}|"
    rf"{_WRITE_FILE_OBJECT}|{_WRITE_FILE_MEMBER}|{_WRITE_FOLDER_MEMBER}|"
    rf"{_WRITE_FOLDER_OBJECT})$"
)
_NAMED_FILENAME = re.compile(_WRITE_FILENAME)
_WRITE_FILENAME_WITH_CASE_SUFFIX = re.compile(
    rf"(?P<filename>{_WRITE_FILENAME})(?:{_WRITE_CASE_SUFFIX}|{_WRITE_LOCATIVE_SUFFIX})"
)
_SIMPLE_CONDITIONAL = re.compile(rf"\b\w+{_CONDITIONAL_PERSON}\b")


@dataclass(frozen=True)
class MemoryDirective:
    kind: str
    target: str = ""


class MemoryPreferenceError(ValueError):
    pass


class MemorySourceError(ValueError):
    pass


def _targeted_write_matches(pattern: re.Pattern[str], folded: str) -> bool:
    match = pattern.fullmatch(folded)
    if match is None:
        return False
    if _CONCRETE_WRITE_VERB.fullmatch(match.group("mutation")) is not None:
        if _CONCRETE_WRITE_TARGET.fullmatch(match.group("target")) is None:
            return False
    named_target = _NAMED_TARGET.fullmatch(match.group("target"))
    if named_target is None:
        return True
    name = named_target.group("name")
    # A dot-qualified name is target data only when it matches the filename
    # grammar; punctuation such as an ellipsis remains prose.
    if "." in name:
        return _NAMED_FILENAME.fullmatch(name) is not None
    if _SIMPLE_CONDITIONAL.fullmatch(name) is not None:
        return False
    return not (
        NON_COMMITTAL_WRITE.fullmatch(name) is not None
        or CONDITIONAL_WRITE.fullmatch(name) is not None
        or ACTION_QUESTION_WORD.fullmatch(name) is not None
    )


def _mask_bounded_memory_controls(folded: str) -> str:
    # Control words can be part of a concrete path or filename. Remove only
    # those bounded target tokens; controls elsewhere in the message remain.
    masked = list(folded)
    for token_match in _MEMORY_TARGET_TOKEN.finditer(folded):
        target = token_match.group()
        final_component = re.split(r"[\\/]", target)[-1]
        if ":" in target and _EXPLICIT_WRITE_PATH_PREFIX.match(target) is None:
            continue
        filename = _NAMED_FILENAME.fullmatch(final_component)
        if filename is None:
            with_suffix = _WRITE_FILENAME_WITH_CASE_SUFFIX.fullmatch(final_component)
            if with_suffix is not None:
                filename = _NAMED_FILENAME.fullmatch(with_suffix.group("filename"))
        if filename is None and _EXPLICIT_WRITE_PATH_PREFIX.match(target) is None:
            continue
        for pattern in _MEMORY_CONTROL_PATTERNS:
            for match in pattern.finditer(target):
                start = token_match.start() + match.start()
                end = token_match.start() + match.end()
                masked[start:end] = " " * (end - start)
    return "".join(masked)


def _read_only_scan_request(text: str) -> str:
    return _mask_bounded_memory_controls(_folded_request(text))


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
    index = 0
    while index < len(text):
        character = text[index]
        if character == '\n':
            start = None
        elif character == "'":
            previous_word = index > 0 and (text[index - 1].isalnum() or text[index - 1] == '_')
            next_word = index + 1 < len(text) and (text[index + 1].isalnum() or text[index + 1] == '_')
            if start is None and not previous_word:
                start = index
            elif start is not None:
                suffix = _QUOTED_CASE_SUFFIX_RE.match(text, index + 1)
                end = suffix.end() if suffix is not None else index + 1
                if end > index + 1 or not next_word:
                    output[start:end] = ' ' * (end - start)
                    start = None
                    index = end
                    continue
        index += 1
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
    folded = _read_only_scan_request(text)
    # A question elsewhere in the message does not revoke an explicit restriction.
    return any(
        not re.match(r"\s+(?:kuralı|ifadesi)\b", folded[match.end():])
        for match in READ_ONLY_REQUEST.finditer(folded)
    )


def is_explicit_write_intent(text: str) -> bool:
    if is_read_only_request(text):
        return False
    folded = unicodedata.normalize("NFKC", text).casefold().replace("i\u0307", "i")
    # Targeted forms have a bounded prefix and must not inspect the target token.
    if (
        _targeted_write_matches(TARGETED_WRITE_COMMAND, folded)
        or _targeted_write_matches(TARGETED_WRITE_QUESTION, folded)
        or _targeted_write_matches(TARGETED_WRITE_PERMISSION, folded)
    ):
        return True
    # Quoted data is never authorization unless the quoted target was accepted above.
    if _unquoted_request(text) != text.strip():
        return False
    if (
        NON_COMMITTAL_WRITE.search(folded)
        or CONDITIONAL_WRITE.search(folded)
        or _SIMPLE_CONDITIONAL.search(folded)
        or ACTION_QUESTION_WORD.search(folded)
    ):
        return False
    return (
        EXPLICIT_WRITE_INTENT.fullmatch(folded) is not None
        or BARE_WRITE_QUESTION.fullmatch(folded) is not None
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def memory_view_relative_path(relative: str) -> str:
    return f'.codex/private-memory/views/{_sha256_text(relative)}.md'


def _balanced_value_end(text: str, start: int) -> int | None:
    """Use the stdlib lexer for non-JSON brace values; never evaluate them."""
    pairs = {')': '(', ']': '[', '}': '{'}
    length = min(256, len(text) - start)
    while True:
        # Grow only the value window, not the remaining document for every field.
        fragment = text[start:start + length]
        stack: list[str] = []
        try:
            for token in tokenize.generate_tokens(io.StringIO(fragment).readline):
                if token.type == tokenize.ERRORTOKEN and token.string in {'"', "'", '\\'}:
                    break
                if token.type != tokenize.OP:
                    continue
                if token.string in '([{':
                    stack.append(token.string)
                elif token.string in pairs:
                    if not stack or stack.pop() != pairs[token.string]:
                        return None
                    if not stack:
                        line, column = token.end
                        return start + sum(len(part) + 1 for part in fragment.split('\n')[:line - 1]) + column
        except (tokenize.TokenError, SyntaxError):
            pass
        if start + length >= len(text):
            return None
        length = min(length * 2, len(text) - start)


# ponytail: consume only adjacent shell fragments; a full shell parser adds no
# sanitizer guarantee and would broaden this bounded input contract.
def _quoted_credential_value_end(
    text: str,
    start: int,
    *,
    shell_segments: bool = False,
) -> int | None:
    ansi_c = text[start:start + 2] == "$'"
    if not ansi_c and text[start:start + 1] not in {'"', "'"}:
        return None
    cursor = start + 1 if ansi_c else start
    while cursor < len(text):
        quote = text[cursor]
        escaped = False
        index = cursor + 1
        while index < len(text):
            character = text[index]
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == quote:
                break
            index += 1
        if index >= len(text):
            return None
        if not shell_segments:
            return index + 1
        cursor = index + 1
        while cursor < len(text):
            character = text[cursor]
            if character in {'"', "'"}:
                break
            if character == '\\':
                if cursor + 1 >= len(text):
                    return None
                cursor += 2
                continue
            if character.isspace() or character in {';', '&', '|', '<', '>', '(', ')'}:
                return cursor
            cursor += 1
    return cursor


def _batch_quoted_credential_value_end(text: str, start: int) -> int | None:
    quote = text[start:start + 1]
    if quote not in {'"', "'"}:
        return None
    for index in range(start + 1, len(text)):
        character = text[index]
        if character in {'\r', '\n'}:
            return None
        if character == quote:
            return index + 1
    return None


def _batch_unquoted_credential_value_end(text: str, start: int) -> int | None:
    """Find a Windows batch value boundary while honoring quote and caret state."""
    quoted = False
    index = start
    while index < len(text):
        character = text[index]
        if character in {'\r', '\n'}:
            if quoted:
                return None
            end = index
            while end > start and text[end - 1] in {' ', '\t'}:
                end -= 1
            return end
        if character == '^':
            if index + 1 >= len(text) or text[index + 1] in {'\r', '\n'}:
                return None
            index += 2
            continue
        if character == '"':
            quoted = not quoted
            index += 1
            continue
        if not quoted and character in {'&', '|', '<', '>', '(', ')'}:
            end = index
            while end > start and text[end - 1] in {' ', '\t'}:
                end -= 1
            return end
        index += 1
    if quoted:
        return None
    end = index
    while end > start and text[end - 1] in {' ', '\t'}:
        end -= 1
    return end


def _powershell_credential_value_end(text: str, start: int) -> int | None:
    def statement_end(end: int) -> int | None:
        cursor = end
        while cursor < len(text) and text[cursor] in {' ', '\t'}:
            cursor += 1
        if cursor == len(text) or text[cursor] in {'\r', '\n', ';', '#'}:
            return end
        return None

    if text[start:start + 2] in {"@'", '@"'}:
        terminator = text[start + 1] + '@'
        closing = re.compile(rf'(?m)^[ \t]*{re.escape(terminator)}[ \t]*\r?$').search(
            text, start + 2,
        )
        return None if closing is None else statement_end(closing.end())
    quote = text[start:start + 1]
    if quote in {'"', "'"}:
        index = start + 1
        while index < len(text):
            character = text[index]
            if quote == '"' and character == '`':
                if index + 1 >= len(text):
                    return None
                index += 2
                continue
            if quote == "'" and character == "'" and text[index:index + 2] == "''":
                index += 2
                continue
            if character == quote:
                return statement_end(index + 1)
            index += 1
        return None
    index = start
    while index < len(text):
        character = text[index]
        if character in {'\r', '\n'}:
            return index
        if character == '`':
            if index + 1 >= len(text):
                return None
            index += 2
            continue
        if character.isspace() or character in {';', '&', '|', '<', '>', '(', ')'}:
            return statement_end(index)
        index += 1
    return index


def _contains_unsupported_shell_expansion(text: str, start: int) -> bool:
    quote: str | None = None
    index = start
    while index < len(text):
        character = text[index]
        if character in {'\r', '\n'}:
            return False
        if character == '\\' and quote != "'":
            if index + 1 >= len(text):
                return False
            index += 2
            continue
        if character in {'"', "'"}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            index += 1
            continue
        if quote != "'" and character == '$' and text[index + 1:index + 2] in {'{', '('}:
            return True
        if quote is None and character in {';', '&', '|', '<', '>'}:
            return False
        index += 1
    return False


def _replace_powershell_credential(match: re.Match[str]) -> str:
    if match.group('prefix').startswith('${') and not match.group('closing'):
        raise MemoryPreferenceError('memory-credential-container-unverifiable')
    return (
        f'{match.group("prefix")}{match.group("key")}'
        f'{match.group("closing")}{match.group("assignment")}<REDACTED>'
    )


def _json_regions(text: str) -> Iterator[tuple[int, int]]:
    """Validated JSON containers and complete top-level JSON strings."""
    decoder = json.JSONDecoder()
    start = len(text) - len(text.lstrip())
    if text[start:start + 1] == '"':
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            pass
        else:
            if isinstance(value, str) and not text[end:].strip():
                yield start, end
                return
    offset = 0
    # ponytail: bound malformed-input retries to linear parse work; a streaming
    # parser is only needed if legitimate inputs exhaust this budget.
    remaining = 8 * len(text)
    for opening in re.finditer(r'[\[{]', text):
        if opening.start() < offset:
            continue
        try:
            _, end = decoder.raw_decode(text, opening.start())
        except json.JSONDecodeError as exc:
            remaining -= max(1, exc.pos - opening.start())
            if remaining < 0 or _decoded_credential_key(text[opening.start():exc.pos]):
                raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
            offset = opening.start() + 1
            continue
        except (ValueError, RecursionError):
            raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
        offset = end
        boundary = end
        while (boundary < len(text) and not text[boundary].isspace()
               and not (text[boundary].isalnum() or text[boundary] == '_')):
            boundary += 1
        if boundary == len(text) or text[boundary].isspace():
            yield opening.start(), end


def _json_string_regions(
    text: str,
) -> Iterator[tuple[int, int, bool, str, int | None, int | None]]:
    """Yield JSON string spans, decoded values, and key value spans."""
    decoder = json.JSONDecoder()
    previous_end = 0
    for region_start, region_end in _json_regions(text):
        if _decoded_credential_key(text[previous_end:region_start], escaped_only=True):
            raise MemoryPreferenceError('memory-credential-container-unverifiable')
        previous_end = region_end
        cursor = region_start
        while cursor < region_end:
            opening = text.find('"', cursor, region_end)
            if opening < 0:
                break
            try:
                value, end = decoder.raw_decode(text, opening)
            except (ValueError, RecursionError):
                cursor = opening + 1
                continue
            if end > region_end:
                break
            tail = end
            while tail < region_end and text[tail].isspace():
                tail += 1
            is_value = tail >= region_end or text[tail] != ':'
            value_start = value_end = None
            # Only credential fields need their complete value span. Decoding
            # every ordinary key's subtree repeats work at each nesting level.
            if not is_value and (CREDENTIAL_NAME_RE.fullmatch(value) or value.casefold() == 'authorization'):
                value_start = tail + 1
                while value_start < region_end and text[value_start].isspace():
                    value_start += 1
                try:
                    _, value_end = decoder.raw_decode(text, value_start)
                except (ValueError, RecursionError):
                    cursor = tail + 1
                    continue
            yield opening, end, is_value, value, value_start, value_end
            cursor = tail + 1 if tail < region_end and text[tail] == ':' else tail
    if _decoded_credential_key(text[previous_end:], escaped_only=True):
        raise MemoryPreferenceError('memory-credential-container-unverifiable')


def _decoded_credential_key(text: str, *, escaped_only: bool = False) -> bool:
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(text):
        opening = text.find('"', cursor)
        if opening < 0:
            return False
        try:
            value, end = decoder.raw_decode(text, opening)
        except (ValueError, RecursionError):
            cursor = opening + 1
            continue
        tail = end
        while tail < len(text) and text[tail].isspace():
            tail += 1
        if (not escaped_only or '\\' in text[opening:end]) and tail < len(text) and text[tail] == ':' and (
            CREDENTIAL_NAME_RE.fullmatch(value) or value.casefold() == 'authorization'
        ):
            return True
        if '\\' in text[opening:end] and contains_secret(value):
            return True
        cursor = end
    return False


def _redact_decoded_json(text: str) -> tuple[str, tuple[str, ...]]:
    replacements: list[tuple[int, int, str]] = []
    redactions: list[str] = []

    def note(category: str) -> None:
        if category not in redactions:
            redactions.append(category)

    skip_until = 0
    for start, end, is_value, decoded, value_start, value_end in _json_string_regions(text):
        if start < skip_until:
            continue
        if not is_value:
            if (
                value_start is not None
                and value_end is not None
                and CREDENTIAL_NAME_RE.fullmatch(decoded)
            ):
                replacements.append((value_start, value_end, json.dumps("<REDACTED>")))
                note('credential')
                skip_until = value_end
            elif (
                value_start is not None
                and value_end is not None
                and decoded.casefold() == 'authorization'
            ):
                try:
                    decoded_value, decoded_end = json.JSONDecoder().raw_decode(text, value_start)
                except (ValueError, RecursionError):
                    continue
                if decoded_end == value_end and isinstance(decoded_value, str):
                    parts = decoded_value.split(None, 1)
                    if len(parts) == 2 and parts[0].casefold() == 'bearer':
                        replacements.append((value_start, value_end, json.dumps('Bearer <REDACTED>')))
                        note('authorization')
                        skip_until = value_end
            continue
        try:
            nested, nested_redactions = sanitize_text(decoded, max_chars=None)
        except RecursionError:
            raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
        except MemoryPreferenceError:
            replacements.append((start, end, json.dumps("<REDACTED>")))
            note('credential')
            continue
        for category in nested_redactions:
            note(category)
        if nested != decoded:
            replacements.append((start, end, json.dumps(nested)))
        elif _decoded_credential_key(decoded) or CREDENTIAL.search(decoded):
            replacements.append((start, end, json.dumps("<REDACTED>")))
            note('credential')
    if not replacements:
        return text, tuple(redactions)
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements):
        if start < cursor:
            continue
        pieces.extend((text[cursor:start], replacement))
        cursor = end
    pieces.append(text[cursor:])
    return ''.join(pieces), tuple(redactions)


_MEMORY_VIEW_IDENTIFIER = re.compile(
    r'^\.codex/private-memory/views/(?P<digest>[0-9a-f]{64})\.md$'
)


def _resolve_memory_view_source(vault_root: Path, relative: str) -> Path | None:
    match = _MEMORY_VIEW_IDENTIFIER.fullmatch(relative)
    if match is None:
        return None
    from companion_memory import CANONICAL_RELATIVE, VIEW_NAMES
    from vault_corpus import COMPANION_ROOT, markdown_paths

    root = vault_root.resolve(strict=True)
    digest = match.group('digest')
    for candidate in markdown_paths(root, excluded_root_dirs=frozenset({'tmp'})):
        candidate_relative = candidate.relative_to(root).as_posix()
        if _sha256_text(candidate_relative) == digest:
            return candidate
    if (root / CANONICAL_RELATIVE).is_file():
        for name in VIEW_NAMES:
            candidate_relative = f'{COMPANION_ROOT}/{name}'
            if _sha256_text(candidate_relative) == digest:
                return root / candidate_relative
    return None


def sanitize_text(
    text: str,
    *,
    max_chars: int | None = MAX_EVENT_CHARS,
) -> tuple[str, tuple[str, ...]]:
    redactions: list[str] = []

    def replace_outside_json_values(
        pattern: re.Pattern[str],
        replacement: str | Callable[[re.Match[str]], str],
        category: str,
        end_resolver: Callable[[re.Match[str]], int | None] | None = None,
    ) -> None:
        nonlocal text
        spans = [(record[0], record[1]) for record in _json_string_regions(text) if record[2]]
        pieces: list[str] = []
        cursor = span_index = 0
        for match in pattern.finditer(text):
            if match.start() < cursor:
                continue
            while span_index < len(spans) and spans[span_index][1] <= match.start():
                span_index += 1
            if span_index < len(spans) and spans[span_index][0] <= match.start():
                continue
            end = end_resolver(match) if end_resolver is not None else match.end()
            if end is None or end < match.start():
                raise MemoryPreferenceError('memory-credential-container-unverifiable')
            opening = re.search(r'[\[{]', match.group())
            if opening is not None and pattern is not PRIVATE_KEY and end_resolver is None:
                value_end = _balanced_value_end(text, match.start() + opening.start())
                if value_end is None:
                    raise MemoryPreferenceError('memory-credential-container-unverifiable')
                end = max(end, value_end)
                while end < len(text) and not text[end].isspace():
                    end += 1
            value = replacement(match) if callable(replacement) else match.expand(replacement)
            pieces.extend((text[cursor:match.start()], value))
            cursor = end
        if pieces:
            text = ''.join(pieces) + text[cursor:]
        if pieces and category not in redactions:
            redactions.append(category)

    text, decoded_redactions = _redact_decoded_json(text)
    for category in decoded_redactions:
        if category not in redactions:
            redactions.append(category)
    replace_outside_json_values(PRIVATE_KEY, "<REDACTED>", "private-key")
    replace_outside_json_values(AUTHORIZATION,
            lambda match: (f'{match.group("prefix")}"Bearer <REDACTED>"' if match.group('key_quote')
                           else "Authorization: Bearer <REDACTED>"),
            "authorization")
    for match in BATCH_CREDENTIAL_START.finditer(text):
        if _batch_quoted_credential_value_end(text, match.start('quote')) is None:
            raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
    replace_outside_json_values(
        BATCH_CMD_WRAPPER_CREDENTIAL,
        '',
        'credential',
        lambda _match: None,
    )
    replace_outside_json_values(
        BATCH_CREDENTIAL,
        lambda match: f'{match.group("prefix")}{match.group("key")}=<REDACTED>{match.group("quote")}',
        "credential",
    )
    replace_outside_json_values(
        BATCH_CREDENTIAL_UNQUOTED,
        lambda match: f'{match.group("prefix")}{match.group("key")}=<REDACTED>',
        "credential",
        lambda match: _batch_unquoted_credential_value_end(text, match.end()),
    )
    replace_outside_json_values(
        POWERSHELL_CREDENTIAL,
        _replace_powershell_credential,
        "credential",
        lambda match: _powershell_credential_value_end(text, match.end()),
    )
    regions = _json_regions(text)
    json_strings = _json_string_regions(text)
    region: tuple[int, int] | None = None
    json_string: tuple[int, int, bool, str, int | None, int | None] | None = None
    pieces: list[str] = []
    cursor = 0
    search_cursor = 0
    while match := CREDENTIAL.search(text, search_cursor):
        line_start = text.rfind('\n', 0, match.start()) + 1
        assignment_prefix = text[line_start:match.start()]
        if (BATCH_ASSIGNMENT_PREFIX.search(assignment_prefix)
                or POWERSHELL_ASSIGNMENT_PREFIX.search(assignment_prefix)):
            search_cursor = match.end()
            continue
        while json_string is None or json_string[1] <= match.start():
            json_string = next(json_strings, None)
            if json_string is None:
                break
        if json_string is not None and json_string[0] < match.start() < json_string[1]:
            if not json_string[2]:
                raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
            pieces.extend((text[cursor:json_string[0]], text[json_string[0]:json_string[1]]))
            cursor = json_string[1]
            search_cursor = json_string[1]
            continue
        end = match.end()
        value_start = match.start('value')
        if (match.group('prefix').rstrip().endswith('=')
                and _contains_unsupported_shell_expansion(text, value_start)):
            raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
        if (text[value_start] in {'"', "'"}
                or (match.group('prefix').rstrip().endswith('=')
                    and text[value_start:value_start + 2] == "$'")):
            quoted_end = _quoted_credential_value_end(
                text,
                value_start,
                shell_segments=match.group('prefix').rstrip().endswith('='),
            )
            if quoted_end is None:
                raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
            end = max(end, quoted_end)
        if text[match.start('value')] in '{[':
            quoted_field = False
            try:
                _, end = json.JSONDecoder().raw_decode(text, match.start('value'))
            except (ValueError, RecursionError):
                end = _balanced_value_end(text, match.start('value'))
                if end is None:
                    raise MemoryPreferenceError('memory-credential-container-unverifiable') from None
            else:
                while region is None or region[1] <= match.start():
                    region = next(regions, None)
                    if region is None:
                        break
                quoted_field = (region is not None and region[0] <= match.start() and end <= region[1]
                                and match.group('key_quote') == '"' and match.group('prefix').rstrip().endswith(':'))
            if (not quoted_field and match.group('key_quote')
                    and match.group('prefix').rstrip().endswith(':') and text[end:].strip()):
                raise MemoryPreferenceError('memory-credential-container-unverifiable')
            if not quoted_field:
                while end < len(text) and not text[end].isspace():
                    end += 1
        replacement = (f'{match.group("prefix")}"<REDACTED>"' if match.group('key_quote')
                       else f"{match.group('key')}=<REDACTED>")
        pieces.extend((text[cursor:match.start()], replacement))
        cursor = end
        search_cursor = end
    if pieces:
        text = ''.join(pieces) + text[cursor:]
    if pieces and 'credential' not in redactions:
        redactions.append('credential')
    replace_outside_json_values(TOKEN_PREFIX, "<REDACTED>", "credential")
    replace_outside_json_values(PERSONAL_CREDENTIAL, "<REDACTED>", "credential")

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
    try:
        return bool(sanitize_text(text, max_chars=None)[1])
    except MemoryPreferenceError:
        return True


def memory_directive(text: str) -> MemoryDirective:
    raw = text.strip()
    unquoted = _unquoted_request(text)
    folded = _mask_bounded_memory_controls(
        unicodedata.normalize("NFKC", unquoted).casefold().replace("i\u0307", "i")
    )
    raw_folded = unicodedata.normalize("NFKC", raw).casefold().replace("i\u0307", "i")
    if SESSION_ONLY_REQUEST.search(folded) is not None:
        return MemoryDirective("session-only")
    if contains_secret(raw):
        return MemoryDirective("secret")
    if WHAT_KNOWN_REQUEST.search(folded) is not None:
        return MemoryDirective("what-known")
    if DO_NOT_SAVE_REQUEST.search(folded) is not None:
        standalone_text = CONTROL_SEPARATOR.sub(" ", raw_folded).strip()
        standalone = re.fullmatch(STANDALONE_DO_NOT_SAVE, standalone_text)
        return MemoryDirective("do-not-save", "" if standalone else raw)
    if FORGET_REQUEST.search(folded) is not None:
        match = FORGET_WITH_TARGET.match(raw) or FORGET_SUFFIX.match(raw)
        if match is None:
            return MemoryDirective("forget-ambiguous", raw)
        target = CONTROL_TRAILING.sub("", match.group(1).strip().strip('\'"“”‘’«»'))
        if target.casefold().replace("i\u0307", "i") in AMBIGUOUS_TARGETS:
            return MemoryDirective("forget-ambiguous")
        return MemoryDirective("forget", target)
    if is_read_only_request(text):
        return MemoryDirective("read-only")
    if CORRECT_REQUEST.search(folded) is not None:
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
def memory_write_guard(
    state_dir: Path,
    session_id: str | None,
    *,
    timeout: float | None = None,
) -> Iterator[None]:
    """Serialize publication with scope changes, only for this session."""
    if not session_id:
        yield
        return
    with locked(_read_only_path(state_dir, session_id), timeout=timeout):
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
        root = vault_root.resolve(strict=True)
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise MemorySourceError('memory-source-outside-vault')
        relative = resolved.relative_to(root).as_posix()
        if _MEMORY_VIEW_IDENTIFIER.fullmatch(relative):
            source = _resolve_memory_view_source(root, relative)
            if source is None:
                raise MemorySourceError('memory-view-source-unavailable')
            source_relative = source.relative_to(root).as_posix()
            if memory.active:
                from vault_retrieval import _apply_memory_suppressions, build_vault_map

                indexed = build_vault_map(root, write_cache=False)
                visible = _apply_memory_suppressions(root, indexed, memory)
                # ponytail: render all visible sources for opaque link targets; if a large Vault makes this costly, use requested-only rendering with opaque alias mapping.
                sources = [(entry.path, entry.title) for entry in visible]
                if source_relative not in {relative for relative, _title in sources}:
                    sources.insert(0, (source_relative, PurePosixPath(source_relative).stem))
                rendered, _paths = memory.render_views(
                    sources,
                )
                text = rendered.get(source_relative)
            else:
                _relative, text = memory.read_source(source)
        else:
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
