"""Read-only kod-mühendisi ajanı testleri. LLM mock'lu; tmp-DB (prod kirletilmez)."""

import sqlite3

import pytest

from app.core import code_reviewer as cr

DISCOVERIES_SCHEMA = """
CREATE TABLE discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, project TEXT,
    type TEXT CHECK(type IN ('bug','fix','learning','config','workaround','architecture','plan')),
    title TEXT NOT NULL, details TEXT, resolved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')), device_name TEXT DEFAULT 'klipper',
    status TEXT DEFAULT 'active' CHECK(status IN ('active','completed','obsolete','superseded')),
    last_read_at TEXT, read_count INTEGER DEFAULT 0, rationale TEXT);
CREATE UNIQUE INDEX idx_disc_unique_active ON discoveries(project, type, title) WHERE status='active';
"""


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(DISCOVERIES_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(cr, "MEMORY_DB", str(db))
    monkeypatch.setattr(cr, "_ENABLED", True)
    return db


def _rows(db, **w):
    conn = sqlite3.connect(db)
    q = "SELECT type, title, details FROM discoveries WHERE 1=1"
    p = []
    for k, v in w.items():
        q += f" AND {k}=?"
        p.append(v)
    out = conn.execute(q, p).fetchall()
    conn.close()
    return out


def test_record_findings_dedup(tmp_db):
    """Aynı bulgu 2 kez → 1 new + 1 dup (unique-active index)."""
    f = [{"line": 42, "severity": "P1", "title": "SQL injection", "detail": "user input concat"}]
    r1 = cr.record_findings("app/x.py", f)
    assert r1 == {"new": 1, "dup": 0, "p1_titles": ["app/x.py:42 SQL injection"]}
    r2 = cr.record_findings("app/x.py", f)  # tekrar
    assert r2["new"] == 0
    assert r2["dup"] == 1
    assert len(_rows(tmp_db, type="bug")) == 1  # tek kayıt


def test_record_findings_p1_surfaced(tmp_db):
    f = [
        {"line": 1, "severity": "P3", "title": "minor", "detail": "x"},
        {"line": 2, "severity": "P1", "title": "auth bypass", "detail": "y"},
    ]
    r = cr.record_findings("app/y.py", f)
    assert r["new"] == 2
    assert r["p1_titles"] == ["app/y.py:2 auth bypass"]  # yalnız P1 yüzeye çıkar


def test_synthesize_lesson_recurring(tmp_db):
    """Aynı sorun-türü ≥3 yerde → 'learning' dersi (read-only sentez)."""
    for i in range(3):
        cr.record_findings(f"app/f{i}.py", [{"line": i, "severity": "P2", "title": "missing busy_timeout", "detail": "d"}])
    assert cr.synthesize_lesson() is True
    lessons = _rows(tmp_db, type="learning")
    assert len(lessons) == 1
    assert "missing busy_timeout" in lessons[0][1].lower()
    # idempotent: tekrar çağrı yeni-ders üretmez
    assert cr.synthesize_lesson() is False


def test_synthesize_lesson_below_threshold(tmp_db):
    cr.record_findings("app/a.py", [{"line": 1, "severity": "P2", "title": "rare issue", "detail": "d"}])
    assert cr.synthesize_lesson() is False  # <3 → ders yok
    assert len(_rows(tmp_db, type="learning")) == 0


def _stub_llm(monkeypatch, raw: str):
    """cr.llm_core.generate'i sabit ham-yanıtla değiştir (LLMCore boundary mock)."""

    async def fake_generate(prompt, **kw):
        return raw

    monkeypatch.setattr(cr.llm_core, "generate", fake_generate)


async def test_ask_coder_parses_and_filters(monkeypatch):
    """Mock LLM yanıtı → katı-JSON parse + alan-filtre (read-only LLM boundary)."""
    _stub_llm(monkeypatch, 'blah ```json\n[{"line": 5, "severity": "P1", "title": "race", "detail": "concurrent insert"}]\n``` end')
    out = await cr._ask_coder("prompt")
    assert len(out) == 1
    assert out[0]["severity"] == "P1"
    assert out[0]["line"] == 5
    assert out[0]["title"] == "race"


async def test_ask_coder_empty_on_no_json(monkeypatch):
    _stub_llm(monkeypatch, "Kod temiz, sorun yok.")  # JSON-dizi yok
    assert await cr._ask_coder("p") == []


async def test_ask_coder_empty_on_llm_fail(monkeypatch):
    """LLMCore fail-silent → '' → _ask_coder boş döner (ajan döngüsü bozulmaz)."""
    _stub_llm(monkeypatch, "")
    assert await cr._ask_coder("p") == []


# ── #4 Adversarial-verify (FP-eleme) ──

_F1 = {"severity": "P1", "title": "injection", "line": 5, "detail": "os.system"}


async def test_verify_one_real_kept(monkeypatch):
    _stub_llm(monkeypatch, "REAL")
    assert await cr._verify_one("x.py", "code", _F1) is True


async def test_verify_one_fp_dropped(monkeypatch):
    """Yanıt 'FP' ile başlıyor → net-FP → elenir (False)."""
    _stub_llm(monkeypatch, "FP — yakında shlex.quote var, mitige")
    assert await cr._verify_one("x.py", "code", _F1) is False


async def test_verify_one_uncertain_kept(monkeypatch):
    """Belirsiz/garbage (FP ile başlamıyor) → KORUNUR (gerçek-kaçırma > FP-survivor; insan review eder)."""
    _stub_llm(monkeypatch, "emin değilim, belki")
    assert await cr._verify_one("x.py", "code", _F1) is True


async def test_verify_one_empty_kept(monkeypatch):
    """claude-down → boş → KORU (kör-bırakma yok)."""
    _stub_llm(monkeypatch, "")
    assert await cr._verify_one("x.py", "code", _F1) is True


async def test_verify_findings_filters_p1p2_keeps_p3(monkeypatch):
    async def fake_verify(rel, code, f):
        return f["title"] == "real-bug"

    monkeypatch.setattr(cr, "_verify_one", fake_verify)
    findings = [
        {"severity": "P1", "title": "real-bug", "line": 1, "detail": ""},
        {"severity": "P2", "title": "fake-bug", "line": 2, "detail": ""},
        {"severity": "P3", "title": "nit", "line": 3, "detail": ""},  # P3 → verify atlanır, kalır
    ]
    kept = await cr._verify_findings("x.py", "code", findings)
    assert {f["title"] for f in kept} == {"real-bug", "nit"}  # fake-bug elendi


async def test_review_source_applies_verify(monkeypatch):
    """review_source verify'ı uygular: bulgu var ama hepsi FP-elenirse boş döner."""
    monkeypatch.setattr(cr, "_ENABLED", True)
    monkeypatch.setattr(cr, "_VERIFY_ENABLED", True)

    async def fake_ask(p):
        return [dict(_F1)]

    async def fake_vf(rel, code, findings):
        return []  # tümü FP elendi

    monkeypatch.setattr(cr, "_ask_coder", fake_ask)
    monkeypatch.setattr(cr, "_verify_findings", fake_vf)
    assert await cr.review_source("x.py", "kod var") == []


async def test_review_source_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(cr, "_ENABLED", False)
    assert await cr.review_source("x.py", "code") == []


async def test_research_records_architecture_finding(tmp_db, monkeypatch):
    """Faz 3: web+LLM yeni-yapı önerirse 'architecture' bulgusu yazılır (read-only)."""
    import app.api.research as research

    monkeypatch.setattr(cr, "_RESEARCH_ENABLED", True)
    monkeypatch.setattr(research, "_web_search", lambda q, n=5: [{"title": "FastAPI 0.115 lifespan DB pool", "text": "new pattern"}])
    monkeypatch.setattr(
        research, "_ollama_generate", lambda p, **k: "Lifespan-scoped DB havuzu benimse\nBağlantı-başı yerine havuz daha verimli."
    )
    assert await cr.research_new_structure("FastAPI") is True
    arch = _rows(tmp_db, type="architecture")
    assert len(arch) == 1
    assert "FastAPI" in arch[0][1]


async def test_research_yok_no_finding(tmp_db, monkeypatch):
    import app.api.research as research

    monkeypatch.setattr(cr, "_RESEARCH_ENABLED", True)
    monkeypatch.setattr(research, "_web_search", lambda q, n=5: [{"title": "x", "text": "y"}])
    monkeypatch.setattr(research, "_ollama_generate", lambda p, **k: "YOK")
    assert await cr.research_new_structure("FastAPI") is False
    assert len(_rows(tmp_db, type="architecture")) == 0


async def test_research_no_web_results(tmp_db, monkeypatch):
    import app.api.research as research

    monkeypatch.setattr(cr, "_RESEARCH_ENABLED", True)
    monkeypatch.setattr(research, "_web_search", lambda q, n=5: [])
    assert await cr.research_new_structure("X") is False


async def test_agent_drain_queue(tmp_db, tmp_path, monkeypatch):
    """Agent commit-kuyruğunu okur, inceler, temizler (event-trigger)."""
    from app.core import code_review_agent as cra

    monkeypatch.setattr(cra.cr, "_ENABLED", True)
    agent = cra.CodeReviewAgent()
    # kuyruk + sahte dosya
    qf = tmp_path / "queue.txt"
    target = cr.ROOT  # gerçek root; var olan bir dosyayı kuyruğa koy
    agent._queue = qf
    qf.write_text("app/main.py\napp/main.py\n")  # dup → uniq

    seen = []

    async def fake_review_file(p):
        seen.append(str(p))
        return [{"line": 1, "severity": "P2", "title": "test", "detail": "d"}]

    monkeypatch.setattr(cra.cr, "review_file", fake_review_file)
    monkeypatch.setattr(cra.cr, "record_findings", lambda rel, f: {"new": 1, "dup": 0, "p1_titles": []})

    await agent._drain_queue()
    assert len(seen) == 1  # dup-dosya tek kez incelendi
    assert "app/main.py" in seen[0]
    assert qf.read_text() == ""  # kuyruk drenaj edildi
    _ = target
