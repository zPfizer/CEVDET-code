from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import json
import ntpath
from pathlib import Path
import re
from collections.abc import Iterable, Mapping
from typing import NamedTuple, Sequence, TypedDict
import stat
import unicodedata

from knowledge_schema import parse_frontmatter


SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parent.parent
SCHEMA_PATH = SCRIPT_DIR.parent / "tansu-semantic-schema.json"
DEFAULT_SEMANTIC_CSV = SCRIPT_DIR.parent / "tansu-semantic-map.csv"
EXPECTED_RECORD_COUNT = 226
SEMANTIC_KEYS = (
    "symbols",
    "timeframes",
    "evidence_types",
    "data_sources",
    "maturity",
    "focus_lanes",
    "supporting_lanes",
    "workflow_roles",
    "primary_module",
    "supporting_modules",
)


class TopicRule(TypedDict):
    tag: str
    patterns: list[str]


class SemanticSchema(TypedDict):
    max_tags_per_note: int
    module_tags: dict[str, str]
    topic_rules: list[TopicRule]
    symbol_allowlist: list[str]


@dataclass(frozen=True)
class RecordPlan:
    record_id: str
    path: Path
    tags: tuple[str, ...]
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    evidence_types: tuple[str, ...]
    data_sources: tuple[str, ...]
    maturity: str
    focus_lanes: tuple[str, ...]
    supporting_lanes: tuple[str, ...]
    workflow_roles: tuple[str, ...]
    primary_module: str
    supporting_modules: tuple[str, ...]
    source_sha256: str


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(
        char for char in folded if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_marks.replace("ı", "i")).strip()


def _split_pipe(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split("|") if item.strip())


def _split_modules(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in re.split(r"[|+]", value) if item.strip())


def _frontmatter(text: str) -> tuple[dict[str, str | tuple[str, ...]], int]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("frontmatter-missing")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("frontmatter-unclosed") from exc
    values: dict[str, str | tuple[str, ...]] = {
        key: tuple(value) if isinstance(value, list) else value
        for key, value in parse_frontmatter(text).items()
    }
    return values, closing


def _source_scope(text: str) -> str:
    return text.split("## İlgili kayıtlar", 1)[0]


def _source_excerpt(text: str) -> str:
    if "## Kaynak" not in text:
        return ""
    excerpt = text.split("## Kaynak", 1)[1]
    return excerpt.split("\n## ", 1)[0]


def _evidence_types(frontmatter: Mapping[str, object]) -> tuple[str, ...]:
    source_type = str(frontmatter.get("source_type", ""))
    code_status = str(frontmatter.get("code_status", ""))
    backtest_status = str(frontmatter.get("backtest_status", ""))
    math_status = str(frontmatter.get("math_spec_status", ""))
    source_url = str(frontmatter.get("source_url", ""))
    values: list[str] = []

    def add(value: str) -> None:
        if value not in values:
            values.append(value)

    if any(token in source_type for token in ("text", "tweet", "strategy")):
        add("kaynak-metni")
    if "image" in source_type:
        add("kaynak-görseli")
    if "pdf" in source_type:
        add("pdf")
    if source_type in {"primary-source-research", "official-site-research"}:
        add("resmî-kaynak")
    if code_status in {
        "reference-implemented",
        "source-snippet-validated-not-integrated",
        "reference-only",
        "illustrative-snippets",
        "illustrative-snippet-only",
    }:
        add("kod")
    if backtest_status == "first-pass-mixed-cost-sensitive":
        add("backtest-sonucu")
    elif backtest_status == "source-reported-unverified":
        add("backtest-iddiası")
    if math_status and math_status not in {"not-provided", "not-applicable"}:
        add("formül")
    if source_url.startswith("http"):
        add("kaynak-url")
    return tuple(values)


def _data_sources(
    frontmatter: Mapping[str, object],
    normalized_source: str,
) -> tuple[str, ...]:
    source_type = str(frontmatter.get("source_type", ""))
    provider = str(frontmatter.get("data_provider", ""))
    source_url = str(frontmatter.get("source_url", ""))
    values: list[str] = []

    def add(value: str) -> None:
        if value not in values:
            values.append(value)

    if source_type.startswith("user-"):
        add("tansu-x")
    if "kap" in normalized_source.split():
        add("kap")
    if "yfinance" in normalized_source or "yfinance" in provider:
        add("yfinance")
    if "yahoo chart" in normalized_source:
        add("yahoo-chart")
    if "cmegroup.com" in source_url or source_url == "https://www.cmegroup.com/":
        add("cme")
    if source_type == "official-site-research":
        add("resmî-site")
    return tuple(values)


def _timeframes(frontmatter: Mapping[str, object], normalized_source: str) -> tuple[str, ...]:
    explicit = str(frontmatter.get("timeframe", ""))
    haystack = f"{_normalize(explicit)} {normalized_source}"
    rules = (
        ("30-dakika", (r"\b30m\b", r"\b30 dakika\b")),
        ("4-saat", (r"\b4h\b", r"\bh4\b", r"\b4 saat\b")),
        ("günlük", (r"\bgunluk\b", r"\bdaily\b")),
        ("haftalık", (r"\bhaftalik\b", r"\bweekly\b")),
        ("aylık", (r"\baylik\b", r"\bmonthly\b")),
        ("intraday", (r"\bintraday\b", r"\bgun ici\b")),
    )
    return tuple(
        value
        for value, patterns in rules
        if any(re.search(pattern, haystack) for pattern in patterns)
    )


def _maturity(frontmatter: Mapping[str, object]) -> str:
    if str(frontmatter.get("backtest_status", "")) == "first-pass-mixed-cost-sensitive":
        return "backtest-sonucu"
    if str(frontmatter.get("code_status", "")) in {
        "reference-implemented",
        "source-snippet-validated-not-integrated",
    }:
        return "kod-referansı"
    if str(frontmatter.get("math_spec_status", "")) in {
        "ready",
        "validated-fixture",
        "corrected",
    }:
        return "spec"
    if str(frontmatter.get("status", "")) == "planned":
        return "araştırma-planı"
    if "plan" in str(frontmatter.get("record_kind", "")):
        return "araştırma-planı"
    return "kaynak"


def _symbols(
    title: str,
    frontmatter: Mapping[str, str | tuple[str, ...]],
    allowlist: set[str],
) -> tuple[str, ...]:
    values: list[str] = []
    explicit = frontmatter.get("symbols", frontmatter.get("symbol", ()))
    candidates = explicit if isinstance(explicit, tuple) else (str(explicit),)
    for value in candidates:
        normalized = value.strip().upper()
        if normalized in allowlist and normalized not in values:
            values.append(normalized)
    for match in re.finditer(r"(?<![A-Za-z0-9ÇĞİÖŞÜ])[A-ZÇĞİÖŞÜ][A-Z0-9ÇĞİÖŞÜ]{2,7}(?![A-Za-z0-9ÇĞİÖŞÜ])", title):
        value = match.group(0)
        if value in allowlist and value not in values:
            values.append(value)
    return tuple(values)


def _string_list(value: object, error: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(error)
    return list(value)


def _string_map(value: object, error: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(error)
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(error)
        result[key] = item
    return result


def _topic_rules(value: object) -> list[TopicRule]:
    if not isinstance(value, list):
        raise ValueError("schema-topic-rules")
    result: list[TopicRule] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("schema-topic-rule")
        tag = item.get("tag")
        patterns = item.get("patterns")
        if not isinstance(tag, str):
            raise ValueError("schema-topic-rule-tag")
        result.append({"tag": tag, "patterns": _string_list(patterns, "schema-topic-rule-patterns")})
    return result


def _load_schema(path: Path = SCHEMA_PATH) -> SemanticSchema:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("schema-not-object")
    max_tags = payload.get("max_tags_per_note")
    if not isinstance(max_tags, (str, int, float)):
        raise ValueError("schema-max-tags")
    return {
        "max_tags_per_note": int(max_tags),
        "module_tags": _string_map(payload.get("module_tags"), "schema-module-tags"),
        "topic_rules": _topic_rules(payload.get("topic_rules")),
        "symbol_allowlist": _string_list(payload.get("symbol_allowlist"), "schema-symbol-allowlist"),
    }


def _source_path(source_root: Path, source_file: object) -> Path:
    if not isinstance(source_file, str):
        raise ValueError("semantic-source-path-invalid")
    relative = Path(source_file.replace("\\", "/"))
    if (
        not relative.parts
        or relative.is_absolute()
        or relative.anchor
        or ntpath.isabs(source_file)
        or ".." in relative.parts
    ):
        raise ValueError("semantic-source-path-invalid")
    root = Path(source_root)
    resolved_root = root.resolve(strict=True)
    path = root / relative
    for part in (root, *reversed(path.parents[: len(relative.parts) - 1]), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("semantic-source-link-rejected")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("semantic-source-path-escape")
    return path


def build_manifest(
    source_root: Path = VAULT_ROOT,
    *,
    semantic_csv: Path = DEFAULT_SEMANTIC_CSV,
    schema_path: Path = SCHEMA_PATH,
    expected_records: int = EXPECTED_RECORD_COUNT,
) -> tuple[RecordPlan, ...]:
    """CSV satırlarını plana çevirir.

    ``source_root`` her ``source_file`` satırının çözüldüğü köktür; kaynak yolu
    göreli kalmalı ve bu kökün içinde olmalıdır.
    """
    schema = _load_schema(schema_path)
    module_tags = schema["module_tags"]
    topic_rules = schema["topic_rules"]
    max_tags = int(schema["max_tags_per_note"])
    allowlist = set(schema["symbol_allowlist"])
    facet_values = {
        "symbols": allowlist,
        "tags": set(module_tags.values()) | {rule["tag"] for rule in topic_rules},
        "timeframes": {"30-dakika", "4-saat", "günlük", "haftalık", "aylık", "intraday"},
        "evidence_types": {"kaynak-metni", "kaynak-görseli", "pdf", "resmî-kaynak", "kod", "backtest-sonucu", "backtest-iddiası", "formül", "kaynak-url"},
        "data_sources": {"tansu-x", "kap", "yfinance", "yahoo-chart", "cme", "resmî-site"},
    }
    plans: list[RecordPlan] = []

    with semantic_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key, allowed in facet_values.items():
            if key not in row:
                continue
            if not isinstance(row[key], str):
                raise ValueError(f"semantic-facet-invalid:{key}")
            values = _split_pipe(row[key])
            if (not set(values) <= allowed or len(values) != len(set(values))
                    or (key == "tags" and not 1 <= len(values) <= max_tags)):
                raise ValueError(f"semantic-facet-invalid:{key}")
        path = _source_path(source_root, row.get("source_file"))
        text = path.read_text(encoding="utf-8")
        frontmatter, _closing = _frontmatter(text)
        title = str(frontmatter.get("title", row.get("title", "")))
        record_kind = str(frontmatter.get("record_kind", ""))
        normalized_basis = _normalize(f"{title} {record_kind}")
        normalized_source = _normalize(_source_scope(text))
        normalized_source_excerpt = _normalize(
            f"{title} {record_kind} {_source_excerpt(text)}"
        )
        primary_module = row["primary_module_slot"].strip()
        module_tag = module_tags.get(primary_module)
        if not isinstance(module_tag, str):
            raise ValueError(f"module-tag-missing:{row['record_id']}:{primary_module}")
        tags = [module_tag]
        for rule in topic_rules:
            if any(re.search(pattern, normalized_basis) for pattern in rule["patterns"]):
                tag = rule["tag"]
                if tag not in tags:
                    tags.append(tag)
            if len(tags) >= max_tags:
                break
        plans.append(
            RecordPlan(
                record_id=row["record_id"].zfill(3),
                path=path,
                # Reviewed source facets outrank title/text heuristics, including empty values.
                tags=_split_pipe(row["tags"]) if "tags" in row else tuple(tags),
                symbols=_split_pipe(row["symbols"]) if "symbols" in row else _symbols(title, frontmatter, allowlist),
                timeframes=_split_pipe(row["timeframes"]) if "timeframes" in row else _timeframes(frontmatter, normalized_source_excerpt),
                evidence_types=_split_pipe(row["evidence_types"]) if "evidence_types" in row else _evidence_types(frontmatter),
                data_sources=_split_pipe(row["data_sources"]) if "data_sources" in row else _data_sources(frontmatter, normalized_source),
                maturity=_maturity(frontmatter),
                focus_lanes=_split_pipe(row["focus_lanes"]),
                supporting_lanes=_split_pipe(row["supporting_lanes"]),
                workflow_roles=_split_pipe(row["workflow_roles"]),
                primary_module=primary_module,
                supporting_modules=_split_modules(row["supporting_module_slots"]),
                source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    unique_paths = len({plan.path for plan in plans})
    if len(plans) != expected_records or unique_paths != len(plans):
        raise ValueError("semantic-manifest-coverage")
    return tuple(plans)


def _yaml_list(values: Iterable[str]) -> str:
    return f"[{', '.join(values)}]"


def updated_frontmatter(plan: RecordPlan) -> tuple[str, str]:
    text = plan.path.read_text(encoding="utf-8")
    lines = text.splitlines()
    _values, closing = _frontmatter(text)
    old = "\n".join(lines[: closing + 1])
    frontmatter = lines[: closing + 1]
    semantic_prefixes = tuple(f"{key}:" for key in SEMANTIC_KEYS)
    filtered: list[str] = []
    index = 0
    while index < len(frontmatter):
        line = frontmatter[index]
        if line.startswith(semantic_prefixes):
            index += 1
            while index < len(frontmatter) and re.fullmatch(
                r"\s+-\s+.+", frontmatter[index]
            ):
                index += 1
            continue
        if line.startswith("tags:"):
            filtered.append(f"tags: {_yaml_list(plan.tags)}")
            index += 1
            while index < len(frontmatter) and re.fullmatch(
                r"\s+-\s+.+", frontmatter[index]
            ):
                index += 1
            continue
        filtered.append(line)
        index += 1
    closing_index = len(filtered) - 1
    semantic_lines = [
        f"symbols: {_yaml_list(plan.symbols)}",
        f"timeframes: {_yaml_list(plan.timeframes)}",
        f"evidence_types: {_yaml_list(plan.evidence_types)}",
        f"data_sources: {_yaml_list(plan.data_sources)}",
        f"maturity: {plan.maturity}",
        f"focus_lanes: {_yaml_list(plan.focus_lanes)}",
        f"supporting_lanes: {_yaml_list(plan.supporting_lanes)}",
        f"workflow_roles: {_yaml_list(plan.workflow_roles)}",
        f"primary_module: {plan.primary_module}",
        f"supporting_modules: {_yaml_list(plan.supporting_modules)}",
    ]
    new_lines = [*filtered[:closing_index], *semantic_lines, filtered[closing_index]]
    return old, "\n".join(new_lines)


def render_patch(plans: Sequence[RecordPlan]) -> str:
    chunks = ["*** Begin Patch"]
    for plan in plans:
        old, new = updated_frontmatter(plan)
        if old == new:
            continue
        chunks.extend(
            [
                f"*** Update File: {plan.path.as_posix()}",
                "@@",
                *(f"-{line}" for line in old.splitlines()),
                *(f"+{line}" for line in new.splitlines()),
            ]
        )
    chunks.append("*** End Patch")
    return "\n".join(chunks) + "\n"


class ManifestAudit(NamedTuple):
    """Tek denetim koşusunun ölçümü: kaç kayıt eşleşti, kaç kayıt denetlendi."""

    matched: int
    total: int
    mismatches: tuple[str, ...]


def audit_manifest(
    source_root: Path = VAULT_ROOT,
    *,
    semantic_csv: Path = DEFAULT_SEMANTIC_CSV,
    schema_path: Path = SCHEMA_PATH,
    expected_records: int = EXPECTED_RECORD_COUNT,
) -> ManifestAudit:
    mismatches: list[str] = []
    matched = 0
    list_fields = (
        "tags",
        "symbols",
        "timeframes",
        "evidence_types",
        "data_sources",
        "focus_lanes",
        "supporting_lanes",
        "workflow_roles",
        "supporting_modules",
    )
    plans = build_manifest(
        source_root,
        semantic_csv=semantic_csv,
        schema_path=schema_path,
        expected_records=expected_records,
    )
    for plan in plans:
        frontmatter, _closing = _frontmatter(plan.path.read_text(encoding="utf-8"))
        before = len(mismatches)
        for key in list_fields:
            actual = frontmatter.get(key, ())
            actual_values = actual if isinstance(actual, tuple) else (str(actual),)
            if tuple(actual_values) != tuple(getattr(plan, key)):
                mismatches.append(f"{plan.record_id}:{key}")
        if frontmatter.get("maturity") != plan.maturity:
            mismatches.append(f"{plan.record_id}:maturity")
        if frontmatter.get("primary_module") != plan.primary_module:
            mismatches.append(f"{plan.record_id}:primary_module")
        if len(mismatches) == before:
            matched += 1
    return ManifestAudit(matched, len(plans), tuple(mismatches))


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic-csv", type=Path, default=DEFAULT_SEMANTIC_CSV)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--patch", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    plans = build_manifest(
        VAULT_ROOT,
        semantic_csv=args.semantic_csv,
        schema_path=args.schema,
    )
    if args.patch:
        print(render_patch(plans), end="")
        return 0
    print(
        json.dumps(
            {
                "records": len(plans),
                "tags": len({tag for plan in plans for tag in plan.tags}),
                "symbols": len({symbol for plan in plans for symbol in plan.symbols}),
                "timeframes": len(
                    {timeframe for plan in plans for timeframe in plan.timeframes}
                ),
                "sources": len(
                    {source for plan in plans for source in plan.data_sources}
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
