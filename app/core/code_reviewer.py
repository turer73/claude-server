"""Read-only yazılım-mühendisi ajanı — sürekli kod incelemesi + öğrenme + (web) yeni-yapı.

KOD DEĞİŞTİRMEZ. Bulguları discoveries (bug-tracker, dedup'lı) + opsiyonel Telegram'a
yazar; insan review eder. 3 mod (hepsi read-only):
  1) review  — qwen2.5-coder:7b ile dosya/diff incele → 'bug' bulgusu
  2) learn   — tekrar-eden bulgu desenlerini 'learning' dersine sentezle
  3) research— (Faz 3) research-agent ile web'den yeni-yapı/best-practice tespiti

Güvenlik/dayanıklılık: yerel Ollama (ücretsiz), timeout'lu, FAIL-SILENT, ENV-gated,
dedup (unique-active index BEDAVA), conservative-prompt (FP-sel önlenir), bounded.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import httpx

from app.core.config import read_env_var

logger = logging.getLogger(__name__)

ROOT = Path("/opt/linux-ai-server")
MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"
PROJECT = "code-review"  # tüm ajan-bulguları bu proje altında (dedup + filtre)

_ENABLED = (read_env_var("CODE_REVIEW_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
_OLLAMA = (read_env_var("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
_MODEL = read_env_var("CODE_REVIEW_MODEL") or "qwen2.5-coder:7b"
_TIMEOUT = int(read_env_var("CODE_REVIEW_TIMEOUT") or "60")
_MAX_BYTES = 12000  # dosya başına LLM'e gönderilecek max (büyük dosyada baş kısmı)

_SEVERITIES = {"P1", "P2", "P3"}

# Conservative prompt — sürekli-ajan için FP-sel KRİTİK. Sadece GERÇEK sorun, stil-nit YOK,
# emin değilse boş dön. Katı-JSON (parse güvenli).
_REVIEW_PROMPT = """Sen kıdemli bir güvenlik+correctness odaklı kod gözden geçiricisin. Aşağıdaki {lang} kodunu incele.

SADECE gerçek, somut sorunları bildir: güvenlik açığı (injection/auth-bypass/secret-sızıntı), correctness-bug (race/None-deref/yanlış-mantık), kaynak-sızıntı (OOM/handle), veri-kaybı. Stil/isimlendirme/biçim/öneri YAZMA. Emin değilsen ATLA — yanlış-pozitif yasak.

Yanıtı YALNIZ şu JSON dizisi olarak ver (başka metin yok), sorun yoksa boş dizi []:
[{{"line": <satır-no>, "severity": "P1|P2|P3", "title": "<=60 kar özet", "detail": "<niçin sorun + somut kanıt, 1-2 cümle>"}}]

Dosya: {path}
```{lang}
{code}
```"""


def _lang(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".sh": "bash", ".sql": "sql"}.get(ext, "")


async def _ask_coder(prompt: str) -> list[dict]:
    """qwen-coder'a sor, katı-JSON parse et. Hata/timeout → [] (fail-silent)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{_OLLAMA}/api/generate",
                json={"model": _MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.1}},
            )
        if r.status_code != 200:
            return []
        raw = (r.json() or {}).get("response", "").strip()
        # JSON dizisini ayıkla (model bazen ```json sarması ekler)
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        parsed = json.loads(raw[start : end + 1])
        out = []
        for f in parsed if isinstance(parsed, list) else []:
            if not isinstance(f, dict) or not f.get("title"):
                continue
            sev = str(f.get("severity", "P3")).upper()
            out.append(
                {
                    "line": int(f["line"]) if str(f.get("line", "")).isdigit() else 0,
                    "severity": sev if sev in _SEVERITIES else "P3",
                    "title": str(f["title"])[:60],
                    "detail": str(f.get("detail", ""))[:400],
                }
            )
        return out
    except Exception:
        return []


async def review_source(rel_path: str, code: str) -> list[dict]:
    """Tek dosyayı incele → bulgu listesi (read-only)."""
    if not _ENABLED or not code.strip():
        return []
    snippet = code[:_MAX_BYTES]
    prompt = _REVIEW_PROMPT.format(lang=_lang(rel_path) or "text", path=rel_path, code=snippet)
    return await _ask_coder(prompt)


async def review_file(abs_path: Path) -> list[dict]:
    try:
        rel = str(abs_path.relative_to(ROOT)) if abs_path.is_relative_to(ROOT) else abs_path.name
        return await review_source(rel, abs_path.read_text(errors="replace"))
    except Exception:
        return []


def _record_finding(rel_path: str, f: dict) -> bool:
    """Bulguyu discoveries'e yaz (dedup: unique-active index). Yeni-kayıt ise True."""
    title = f"{rel_path}:{f['line']} {f['title']}"[:120]
    details = f"[{f['severity']}] {f['detail']}"
    try:
        conn = sqlite3.connect(MEMORY_DB)
        conn.execute("PRAGMA busy_timeout=5000")
        # unique-active (project,type,title) → çift INSERT 2067/UNIQUE ihlali = zaten-var
        cur = conn.execute(
            "INSERT OR IGNORE INTO discoveries (project, type, title, details, device_name, rationale, status) "
            "VALUES (?, 'bug', ?, ?, 'klipper', 'auto: code-reviewer (qwen2.5-coder) — read-only, doğrula', 'active')",
            (PROJECT, title, details),
        )
        conn.commit()
        new = cur.rowcount > 0
        conn.close()
        return new
    except Exception:
        return False


def record_findings(rel_path: str, findings: list[dict]) -> dict:
    """Bulguları kaydet (dedup'lı). {new, dup, p1_titles} döner — çağıran Telegram'a karar verir."""
    new = dup = 0
    p1 = []
    for f in findings:
        if _record_finding(rel_path, f):
            new += 1
            if f["severity"] == "P1":
                p1.append(f"{rel_path}:{f['line']} {f['title']}")
        else:
            dup += 1
    return {"new": new, "dup": dup, "p1_titles": p1}


# ── Öğrenme (learn): tekrar-eden bulgu desenini 'learning' dersine sentezle ──


def synthesize_lesson() -> bool:
    """Aktif code-review bulgularında tekrar-eden başlık-desenini (≥3 kez aynı sorun-türü)
    'learning' kaydına çevir — 'bu codebase X anti-pattern'ini tekrarlıyor' dersi. Read-only,
    dedup'lı. Yeni-ders ise True."""
    try:
        conn = sqlite3.connect(MEMORY_DB)
        conn.execute("PRAGMA busy_timeout=5000")
        # title formatı 'path:line <özet>' → '<özet>' (sorun-türü) bazında grupla
        rows = conn.execute("SELECT title FROM discoveries WHERE project=? AND type='bug' AND status='active'", (PROJECT,)).fetchall()
        from collections import Counter

        kinds = Counter()
        for (t,) in rows:
            kind = t.split(" ", 1)[1].lower() if " " in t else t.lower()
            kinds[kind] += 1
        recurring = [(k, n) for k, n in kinds.items() if n >= 3]
        new = False
        for kind, n in recurring:
            ltitle = f"Tekrar-eden bulgu: {kind}"[:120]
            cur = conn.execute(
                "INSERT OR IGNORE INTO discoveries (project, type, title, details, device_name, rationale, status) "
                "VALUES (?, 'learning', ?, ?, 'klipper', 'auto: code-reviewer öğrenme — read-only', 'active')",
                (PROJECT, ltitle, f"'{kind}' sorunu {n} farklı yerde tespit edildi — sistemik desen, kök-neden/lint-kuralı düşün."),
            )
            new = new or cur.rowcount > 0
        conn.commit()
        conn.close()
        return new
    except Exception:
        return False
