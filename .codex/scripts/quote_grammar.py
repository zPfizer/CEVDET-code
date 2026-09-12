"""Shared quote-detection grammar.

Leaf module: both the privacy classifier (memory_ledger) and the evidence
binder (user_evidence) consume this grammar. Keeping it dependency-free lets
both import it at module level instead of deferring imports around the
memory_ledger <-> user_evidence cycle.
"""
import re


# These are quoted data, not requests. Keep the original text for target extraction.
QUOTED_CASE_SUFFIX = r"(?:(?i:['’]?y?[ıiuü])(?!\w))?"
QUOTED_CONTENT = re.compile(
    r'(?:(?ms:^[ \t]*(?P<fence>(?P<fence_char>`|~)(?P=fence_char){2,})[^\r\n]*\r?\n'
    r'(?P<fenced_body>.*?)(?:^[ \t]*(?P=fence)(?P=fence_char)*[ \t]*\r?$|\Z))|'
    # Embedded multiline snippets remain data; only the named line-fence
    # branch can be unwrapped as a whole-message read-only restriction.
    r'```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|'
    r'(?m:^[ \t]*>[^\n]*|^(?: {4}|\t)[^\n]*)|'
    r'`[^`\n]*`|"[^"\n]*"|“[^”]*”|‘[^’]*’|«[^»]*»'
    r')' + QUOTED_CASE_SUFFIX
)
