"""Reject known private path markers in tracked text, without echoing content."""

import os
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATTERNS = {
    "windows-user-path": r"[a-z]:[\\/]+Users[\\/]+[^\\/\s]+",
    "macos-user-path": r"/[U]sers/[^/\s]+",
    "linux-home-path": r"/[h]ome/[^/\s]+",
    "private-workspace": r"\bYON[E]TIM\b",
    "private-project-memory": r"\.claude[\\/]+projects",
    "private-recovery": r"CEVDET[-]Recovery",
}


def scan_tracked_text(root):
    paths = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.split(b"\0")
    patterns = [(name, re.compile(pattern, re.IGNORECASE))
                for name, pattern in PATTERNS.items()]
    findings = []
    for raw_path in paths:
        if not raw_path:
            continue
        relative = os.fsdecode(raw_path)
        path = root / relative
        # Do not follow a tracked symlink into a private directory.
        data = (os.fsencode(os.readlink(path)) if path.is_symlink()
                else path.read_bytes())
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            text = data.decode("utf-32")
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        elif b"\0" in data:
            continue  # Binary files are outside this text-only check.
        else:
            text = data.decode("utf-8", errors="surrogateescape")
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern in patterns:
                if pattern.search(line):
                    findings.append(f"{relative}:{number}: {name}")
    return findings


class PublicRepositoryPrivacyTests(unittest.TestCase):
    def test_tracked_text_has_no_private_path_markers(self):
        findings = scan_tracked_text(ROOT)
        self.assertFalse(findings, "\n" + "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
