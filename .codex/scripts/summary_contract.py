"""Canonical session-summary headings and validation."""
from __future__ import annotations

from knowledge_schema import markdown_headings


EXPECTED_SECTIONS = (
    "Bağlam",
    "Önemli Konuşmalar",
    "Alınan Kararlar",
    "Öğrenilenler",
    "Yapılacaklar",
)


def validate_summary(summary: str) -> bool:
    """Require exactly the five v2 headings, once and in contract order."""
    stripped = summary.strip()
    matches = markdown_headings(stripped)
    expected = [("##", section) for section in EXPECTED_SECTIONS]
    actual = [(match[0], match[1]) for match in matches]
    if actual != expected:
        return False
    return not stripped[: matches[0][2]].strip()
