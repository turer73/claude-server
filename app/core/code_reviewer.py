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

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from app.core.agents.llmcore import llm_core
from app.core.config import read_env_var

logger = logging.getLogger(__name__)

ROOT = Path("/opt/linux-ai-server")
MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"
PROJECT = "code-review"  # tüm ajan-bulguları bu proje altında (dedup + filtre)

_ENABLED = (read_env_var("CODE_REVIEW_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
_MODEL = read_env_var("CODE_REVIEW_MODEL") or "qwen2.5-coder:7b"
_TIMEOUT = int(read_env_var("CODE_REVIEW_TIMEOUT") or "60")
_MAX_BYTES = 12000  # dosya başına LLM'e gönderilecek max (büyük dosyada baş kısmı)
# Büyük dosya kısaltılınca modele AÇIK truncation-notu eklenir: yoksa model snippet'in
# ani-kesintisini "incomplete code / syntax error / unparseable" sanıp FP konfabüle eder
# (discovery #1137/#1139/#1140: test_code_reviewer.py 19KB→12KB-cut → 3× sahte syntax-FP;
# verify de AYNI kesik snippet'i gördüğü için elemiyordu). Scan+verify ikisi de bu notu görür.
_TRUNCATION_NOTE = (
    "\n\n# ⚠️ [REVIEW-NOTU: Dosya {total} byte; yalnız ilk {shown} byte gönderildi "
    "(büyük-dosya kısaltması). Snippet SONUNDAKİ ani kesinti TRUNCATION'dır, kod-kusuru "
    "DEĞİL → 'incomplete code'/'syntax error'/'unparseable'/'file ends abruptly'/'partial "
    "statement' TÜRÜ bulgu RAPORLAMA.]"
)
# #4 Adversarial-verify: her P1/P2 bulgu bağımsız skeptik 2. pass'ten geçer (FP'yi sistem eler).
_VERIFY_ENABLED = (read_env_var("CODE_REVIEW_VERIFY_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
# #3 Gerçek-öğrenme: learn-mode'un sentezlediği dersler review-prompt'a oto-beslenir (ajan kendi
# derslerini uygular). Gürültü-korumalı: sadece aktif code-review 'learning', cap'li, geri-alınabilir.
_LEARN_FEEDBACK_ENABLED = (read_env_var("CODE_REVIEW_LEARN_FEEDBACK_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
_LEARN_FEEDBACK_MAX = int(read_env_var("CODE_REVIEW_LEARN_FEEDBACK_MAX") or "5")
# Negatif/FP-feedback (surer #100203): pozitif-ders loop'u ölü (sentez >=3-aktif ister, hacim
# düşük → hiç ders yok). En yüksek kaldıraç FP-prone ajan için NEGATİF sinyal: bu codebase'de
# SIK obsolete/FP-edilen bulgu-tiplerini "şüpheci ol" olarak besle → FP-sel tekrarını + aynı-
# bulgu re-flag'ini azalt. >=MIN kez obsolete olan tip = sistemik-FP deseni (izole-duplicate
# eşiği geçmez). Advisory: mitigation-FP-guard'ı EZMEZ, sadece kanıt-barını yükseltir.
_FP_FEEDBACK_ENABLED = (read_env_var("CODE_REVIEW_FP_FEEDBACK_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
_FP_FEEDBACK_MIN = int(read_env_var("CODE_REVIEW_FP_FEEDBACK_MIN") or "3")  # tip >=N kez obsolete → FP-deseni
_FP_FEEDBACK_MAX = int(read_env_var("CODE_REVIEW_FP_FEEDBACK_MAX") or "5")  # en fazla N tip besle

_SEVERITIES = {"P1", "P2", "P3"}

# klipper #100224: Ollama structured-output (JSON-schema) — model'i geçerli bulgu-dizisine
# kısıtla → kırılgan substring-ayıklama (raw.find('[')...) yerine temiz json.loads(raw).
# Ollama yok-sayarsa / claude-route'ta → substring-fallback korunur (_ask_coder).
_FINDINGS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "line": {"type": "integer"},
            "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
            "title": {"type": "string"},
            "detail": {"type": "string"},
        },
        "required": ["line", "severity", "title", "detail"],
    },
}

# Conservative prompt — sürekli-ajan için FP-sel KRİTİK. Sadece GERÇEK sorun, stil-nit YOK,
# emin değilse boş dön. Katı-JSON (parse güvenli).
_REVIEW_PROMPT = """Sen kıdemli bir güvenlik+correctness odaklı kod gözden geçiricisin. Aşağıdaki {lang} kodunu incele.

SADECE gerçek, somut sorunları bildir: güvenlik açığı (injection/auth-bypass/secret-sızıntı), correctness-bug (race/None-deref/yanlış-mantık), kaynak-sızıntı (OOM/handle), veri-kaybı. Stil/isimlendirme/biçim/öneri YAZMA. Emin değilsen ATLA — yanlış-pozitif yasak.

MITIGATION-FARKINDALIĞI (yanlış-pozitif önle — KRİTİK): Kod sorunu ZATEN azaltıyorsa FLAG'LEME, çünkü güvenli:
- shlex.quote(...) / parametreli-sorgu (? veya %s placeholder, değer params'ta) / kaçışlama → injection MITIGATED, flag'leme.
- input-validation (regex fullmatch / allowlist / izinli-değer kontrolü) ve SONRA kullanım → MITIGATED.
- try/except, None/boş kontrolü, timeout/busy_timeout, with-context → ilgili risk MITIGATED.
f-string'de komut/SQL görmen TEK BAŞINA açık değildir — yakında bir guard (quote/validate/param) var mı BAK; varsa GÜVENLİDİR.
Aynı sorunu TEK kez bildir (P1+P2 olarak tekrarlama). Satır-no'yu yorum/import değil, sorunun GERÇEK satırına ver.{lessons}{fp_feedback}

Yanıtı YALNIZ şu JSON dizisi olarak ver (başka metin yok), sorun yoksa boş dizi []:
[{{"line": <satır-no>, "severity": "P1|P2|P3", "title": "<=60 kar özet", "detail": "<niçin sorun + somut kanıt, 1-2 cümle>"}}]

Dosya: {path}
```{lang}
{code}
```"""

# #4 Adversarial-verify prompt — ŞÜPHECİ 2. denetçi. SIKILAŞTIRILDI (2026-06-22): bu junior-ajan
# ~%98 FP (dashboard sinyal=%2). VARSAYILAN=FP; yalnız SOMUT-tetik-kurulabilen bulgu REAL. Belirsiz→FP
# (_verify_one parsing'i de yalnız net-REAL korur). Eski "belirsizde-koru" → FP-flood'a yol açtı.
_VERIFY_PROMPT = """Sen ŞÜPHECİ bir kıdemli güvenlik+correctness denetçisisin. FP-EĞİLİMLİ bir junior-ajan (geçmiş ~%98 yanlış-pozitif) aşağıdaki bulguyu raporladı. VARSAYILAN cevabın FP — bulguyu ancak SOMUT-KANIT'la onaylarsın.

DOSYA: {path}
BULGU: [{sev}] {title} — satır {line}
GEREKÇE: {detail}

KOD:
```{lang}
{code}
```

REAL demek için: bulgunun GERÇEKTEN tetiklendiği somut senaryoyu (tam girdi/çağrı/trace) zihninde kurabilmelisin. Kuramıyorsan = FP.

KESİN FP işaretleri (biri varsa FP de): mitigation var (shlex.quote / parametreli-sorgu ?/%s / None-kontrol / try-except / allowlist / regex-validation / busy_timeout); erişilen dict-key fonksiyon-başında GARANTİ init-edilmiş; fail-safe try/except tüm-gövdeyi sarıyor (asla raise etmez); satır yorum/import/boş/test-iskelet; değer operasyonel-eşik-altı (önemsiz); test-helper'da exception-path cleanup-leak (tmp+pytest-cleanup → anlamsız); "olabilir / potansiyel / riski-var" ama somut-tetik gösterilemiyor.

SADECE tek kelime yanıt ver: REAL veya FP. Emin değilsen FP."""


def _lang(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".sh": "bash", ".sql": "sql"}.get(ext, "")


async def _ask_coder(prompt: str) -> list[dict[str, Any]]:
    """Tarama modeline sor (LLMCore route='code-review'), katı-JSON parse et. Hata/timeout → [] (fail-silent).
    NOT: model'i explicit GEÇME — route backend'i (ollama/claude) kendi modelini seçer. Explicit ``model=_MODEL``
    (qwen2.5-coder:7b) geçilirse claude backend'ine qwen adı gider → ``claude cli rc=1`` → sessiz boş (tarama ölür)."""
    try:
        raw = await llm_core.generate(prompt, task="code-review", temperature=0.1, timeout=_TIMEOUT, fmt=_FINDINGS_SCHEMA)
        if not raw:
            return []
        # Structured-output: temiz JSON dizisi → doğrudan parse. Ollama yok-sayarsa / claude-
        # route'ta serbest-metin gelir → substring-ayıkla (eski davranış, fail-safe fallback).
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        # Structured JSON dizisi beklenir; ama dict-wrapper {"findings":[...]} (Claude-route
        # veya Ollama fmt'yi yok-sayınca yaygın) ya da serbest-metin gelebilir → iç diziyi
        # kurtar (Codex #232 P2: aksi halde temiz-parse-dict'te isinstance(list) kontrolü
        # TÜM bulguları sessizce düşürür, substring-fallback hiç çalışmaz).
        if isinstance(parsed, dict):
            for _k in ("findings", "results", "items", "issues"):
                if isinstance(parsed.get(_k), list):
                    parsed = parsed[_k]
                    break
        if not isinstance(parsed, list):
            start, end = raw.find("["), raw.rfind("]")
            if start == -1 or end == -1 or end < start:
                return []
            try:
                parsed = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return []
        out = []
        for f in parsed:
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


def _build_snippet(code: str) -> str:
    """LLM'e gönderilecek kod. _MAX_BYTES üstündeyse kısalt + truncation-notu ekle
    (model ani-kesintiyi syntax-hatası sanmasın — #1137/#1139/#1140 FP kök-fix)."""
    if len(code) <= _MAX_BYTES:
        return code
    return code[:_MAX_BYTES] + _TRUNCATION_NOTE.format(total=len(code), shown=_MAX_BYTES)


async def review_source(rel_path: str, code: str) -> list[dict[str, Any]]:
    """Tek dosyayı incele → bulgu listesi (read-only)."""
    if not _ENABLED or not code.strip():
        return []
    snippet = _build_snippet(code)
    prompt = _REVIEW_PROMPT.format(
        lang=_lang(rel_path) or "text", path=rel_path, code=snippet, lessons=_lessons_block(), fp_feedback=_fp_feedback_block()
    )
    findings = await _ask_coder(prompt)
    if _VERIFY_ENABLED and findings:
        findings = await _verify_findings(rel_path, snippet, findings)
    return findings


async def _verify_one(rel_path: str, code: str, f: dict[str, Any]) -> bool:
    """Bağımsız skeptik 2. pass (güçlü model = claude/Sonnet, task='verify'). Üç sonuç:
    - yanıt 'FP' ile başlar → net-FP → ELE (False).
    - yanıt geldi ama FP değil (REAL/belirsiz/boş-yanıt) → KORU (True; gerçek-kaçırma > FP-survivor).
    - verify ÇALIŞAMADI (claude-down/kota → generate istisna) → **FAIL-CLOSED, ELE (False)**.
      Gerekçe (2026-06-20 incident): eski fail-open'da claude-503'te HER qwen-FP korundu → 245-FP
      seli gerçek-sinyali boğdu, "insan zaten review eder" varsayımı çöktü (kimse 245 girdiyi elemez).
      Doğrulanamayan bulgu persist EDİLMEZ; claude geri gelince sonraki commit-review'da yeniden bulunur.
    qwen-coder kendi blind-spot'unu çürütemediği için verify güçlü-modele yönlendirilir."""
    prompt = _VERIFY_PROMPT.format(
        lang=_lang(rel_path) or "text",
        path=rel_path,
        sev=f["severity"],
        title=f["title"],
        line=f["line"],
        detail=f.get("detail", ""),
        code=code,
    )
    try:
        raw = await llm_core.generate(prompt, task="verify", temperature=0.1, timeout=_TIMEOUT, raise_on_error=True)
    except Exception:
        logger.warning("verify modeli erişilemez (claude-down/kota?) → fail-closed ELE: %s '%s'", rel_path, f.get("title"))
        return False
    out = (raw or "").strip().upper()
    first = out.split()[0] if out.split() else ""
    # SIKILAŞTIRILDI (2026-06-22, %98-FP): yalnız NET-REAL korunur; FP/belirsiz/boş → ELE.
    # Eski "first != FP" (belirsizi korur) → FP-survivor seli. Ajan ikincil-net (Codex + klipper-verify
    # + commit-already-merged) → emin-olmayan-bulguyu düşürmek FP-flood'u keser, gerçek-net bulgu kalır.
    return first.startswith("REAL")


async def _verify_findings(rel_path: str, code: str, findings: list[dict[str, Any]]) -> list[dict]:
    """TÜM bulguları skeptik-pass'ten geçir (FP ele). SIKILAŞTIRILDI (2026-06-22): P3 de verify edilir
    (eski P3-bypass FP-flood'a katkıydı — test_drift_check:20 gibi P3-FP'ler doğrudan geçiyordu)."""
    kept = []
    for f in findings:
        if await _verify_one(rel_path, code, f):
            kept.append(f)
    return kept


# ── #3 Gerçek-öğrenme: learn-mode dersleri → review-prompt oto-besleme ──


def _recent_lessons(limit: int = _LEARN_FEEDBACK_MAX) -> list[str]:
    """Aktif code-review 'learning' dersleri (tekrar-eden sistemik desenler). Read-only."""
    try:
        conn = sqlite3.connect(MEMORY_DB)
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            "SELECT title FROM discoveries WHERE project=? AND type='learning' AND status='active' ORDER BY id DESC LIMIT ?",
            (PROJECT, limit),
        ).fetchall()
        conn.close()
        return [t for (t,) in rows]
    except Exception:
        return []


def _lessons_block() -> str:
    """Dersleri review-prompt'a enjekte edilecek blok (boş = ders yok / kapalı). Gürültü-korumalı:
    cap'li, sadece aktif code-review learning'i; ders FP-guard'ını EZMEZ (prompt'ta açıkça belirtilir)."""
    if not _LEARN_FEEDBACK_ENABLED:
        return ""
    lessons = _recent_lessons()
    if not lessons:
        return ""
    items = "\n".join(f"- {t}" for t in lessons)
    return (
        "\n\nÖĞRENİLEN DERSLER (bu codebase'de TEKRAR etti — bu desenlere ÖZELLİKLE dikkat, "
        "AMA yine de mitigation-farkındalığını uygula; ders FP-guard'ı EZMEZ):\n" + items
    )


def _recent_fp_patterns(min_count: int = _FP_FEEDBACK_MIN, limit: int = _FP_FEEDBACK_MAX) -> list[tuple[str, int]]:
    """Bu codebase'de SIK obsolete-edilen bulgu-tipleri (sistemik-FP deseni). title 'path:line
    <özet>' → '<özet>' (tip) bazında grupla, >=min_count obsolete olanları döndür. Read-only.
    İzole duplicate (1-2 kez) eşiği geçmez → yalnız tekrar-eden FP-deseni yüzeye çıkar."""
    try:
        conn = sqlite3.connect(MEMORY_DB)
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            "SELECT title FROM discoveries WHERE project=? AND type='bug' AND status='obsolete'",
            (PROJECT,),
        ).fetchall()
        conn.close()
        from collections import Counter

        kinds: Counter = Counter()
        for (t,) in rows:
            kind = t.split(" ", 1)[1].lower() if " " in t else t.lower()
            kinds[kind] += 1
        return [(k, n) for k, n in kinds.most_common(limit) if n >= min_count]
    except Exception:
        return []


def _sanitize_pattern(s: str, maxlen: int = 80) -> str:
    """FP-pattern'i prompt'a enjekte etmeden önce temizle (Codex #220): title'lar model/API
    çıktısından gelir; kontrol-karakteri/newline bullet'tan kaçıp prompt'u yönlendirebilir
    (örn. '[]'e steer). Tüm whitespace/kontrol-karakterini tek boşluğa indir + cap."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in s)
    cleaned = " ".join(cleaned.split())  # newline/tab/çoklu-boşluk → tek boşluk
    return cleaned[:maxlen]


def _fp_feedback_block() -> str:
    """Negatif-feedback bloğu: sık-FP/obsolete tipleri 'şüpheci ol' uyarısı olarak prompt'a
    enjekte et (boş = desen yok / kapalı). Advisory — mitigation-FP-guard'ı EZMEZ, yalnız
    bu tipler için kanıt-barını yükseltir (re-flag + FP-sel tekrarını azaltır)."""
    if not _FP_FEEDBACK_ENABLED:
        return ""
    pats = _recent_fp_patterns()
    if not pats:
        return ""
    items = "\n".join(f"- {_sanitize_pattern(k)} ({n}× geçmişte FP/obsolete)" for k, n in pats)
    return (
        "\n\nGEÇMİŞ YANLIŞ-POZİTİFLER (bu codebase'de bu tipler SIK yanlış-pozitif/obsolete oldu — "
        "yalnızca SOMUT, satır-bazlı kanıtın varsa flag'le; şüpheli/teorik olanı ATLA):\n" + items
    )


async def review_file(abs_path: Path) -> list[dict[str, Any]]:
    try:
        rel = str(abs_path.relative_to(ROOT)) if abs_path.is_relative_to(ROOT) else abs_path.name
        return await review_source(rel, abs_path.read_text(errors="replace"))
    except Exception:
        return []


def _record_finding(rel_path: str, f: dict[str, Any]) -> bool:
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


def record_findings(rel_path: str, findings: list[dict[str, Any]]) -> dict:
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


# ── Faz 3: internet/yeni-yapı tespiti (research-agent reuse) ──

_RESEARCH_ENABLED = (read_env_var("CODE_REVIEW_RESEARCH_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
# Codebase stack'i — her research-tick'inde sıradaki topic araştırılır (rotating, bounded).
STACK_TOPICS = [
    "Python FastAPI",
    "Pydantic v2",
    "uvicorn asyncio",
    "aiosqlite SQLite WAL",
    "httpx async client",
    "structlog logging",
    "Ollama local LLM serving",
    "Qdrant vector search",
]


def _record_research(topic: str, headline: str, detail: str) -> bool:
    """Yeni-yapı bulgusunu 'architecture' kaydı olarak yaz (dedup: unique-active)."""
    title = f"Yeni-yapı [{topic}]: {headline}"[:120]
    try:
        conn = sqlite3.connect(MEMORY_DB)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            "INSERT OR IGNORE INTO discoveries (project, type, title, details, device_name, rationale, status) "
            "VALUES (?, 'architecture', ?, ?, 'klipper', 'auto: code-reviewer internet-research (web+LLM) — read-only, DEĞERLENDİR', 'active')",
            (PROJECT, title, detail[:600]),
        )
        conn.commit()
        new = cur.rowcount > 0
        conn.close()
        return new
    except Exception:
        return False


async def research_new_structure(topic: str) -> bool:
    """Bir stack-topic için web-araştır → benimsenmesi gereken yeni pattern/güvenlik var mı?
    → 'architecture' bulgusu (read-only, dedup'lı). research-agent web+LLM reuse. Yeni ise True."""
    if not _RESEARCH_ENABLED:
        return False
    try:
        from app.api.research import _web_search

        results = await asyncio.to_thread(_web_search, f"{topic} security best practices new pattern 2025 2026 CVE", 5)
        if not results:
            return False
        web = "\n".join(f"- {r.get('title', '')}: {r.get('text', '')[:200]}" for r in results[:5])
        prompt = (
            f"Projemiz {topic} kullanan tek-sahip Python admin-server. Aşağıdaki GÜNCEL web sonuçlarına göre: "
            f"benimsenmesi GEREKEN somut yeni bir güvenlik-güncellemesi / pattern / best-practice VAR MI? "
            f"Varsa İLK satıra <=60 karakter başlık, sonra 1-2 cümle neden yaz. Yoksa sadece 'YOK' yaz. "
            f"Spekülasyon/genel-tavsiye/var-olanı-tekrar YAZMA.\n\n{web}"
        )
        # SENTEZ = güçlü model (task='synthesis' → Sonnet); web-sonuçlarını derleyip karar verir.
        answer = (await llm_core.generate(prompt, task="synthesis", timeout=120) or "").strip()
        if not answer:
            # Codex#168: Claude-CLI down → sentez boş döner; ollama'ya (task=reasoning→qwen) düş
            # ki research her tick web-sonucu sessizce DÜŞÜRMESİN (degrade, fail-değil).
            answer = (await llm_core.generate(prompt, task="reasoning", timeout=60) or "").strip()
        if not answer or answer.upper().startswith("YOK") or len(answer) < 25:
            return False
        lines = [ln for ln in answer.splitlines() if ln.strip()]
        return await asyncio.to_thread(_record_research, topic, lines[0][:70], answer)
    except Exception:
        return False
