"""Secret strip helpers — memory'e yazilmadan once token/key'leri maskele.

Memory inserts (discoveries, memories, notes) Bash hook output'larindan veya
shell exec sonuclarinden gelen token'lari yanlislikla kaydedebilir. Bu modul
bilinen API key/token formatlarini [REDACTED:label] ile degistirir.

Calisma prensibi: pattern listesinde yer alan regex'ler text'te aranir,
match her zaman degistirilir. False positive kabul edilir (legitimate
"ghp_example" gibi documentation snippet'leri de redact olur) cunku
false negative — gercek secret leak — daha pahali.

usage:
    from app.core.privacy import redact
    clean, labels = redact("ghp_abc...")
    # clean = "[REDACTED:github_pat_classic]"
    # labels = ["github_pat_classic"]
"""

from __future__ import annotations

import re

# Patterns ordered by specificity (longer/more-specific first; sk-ant before sk-)
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("github_pat_fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("github_pat_classic", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{90,}\b")),
    ("openai", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{48}\b")),  # sk-ant-* zaten ustte
    ("google_api", re.compile(r"\bAIza[A-Za-z0-9_\-]{35}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[bpars]-[A-Za-z0-9\-]{10,}\b")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")),
    # generic_hex_long (40+ hex) cikartildi — git SHA'lar ve hash'ler de match eder,
    # false positive cok yuksek. Spesifik pattern'lerle kal.
]


def redact(text: str | None) -> tuple[str | None, list[str]]:
    """Bilinen secret pattern'lerini [REDACTED:label] ile degistirir.

    Returns:
        (redacted_text, found_labels): orijinal text yoksa (None, []);
        match yoksa text unchanged + [].
    """
    if not text:
        return text, []
    out = text
    found: set[str] = set()
    for label, pat in PATTERNS:
        if pat.search(out):
            found.add(label)
            out = pat.sub(f"[REDACTED:{label}]", out)
    return out, sorted(found)
