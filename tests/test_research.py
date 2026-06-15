"""Tests for the Research API.

Endpoints:
  POST /api/v1/research/ask
  GET  /api/v1/research/health

Auth: verify_key (X-Memory-Key). Tests blank MEMORY_API_KEY via monkeypatch
so the dependency short-circuits.
"""

from unittest.mock import MagicMock, patch

import pytest

import app.api.research as research


@pytest.fixture(autouse=True)
def _set_memory_auth(monkeypatch):
    # fail-closed güvenlik fix: key'i set et (client X-Memory-Key gönderir).
    from tests.conftest import TEST_MEMORY_KEY

    monkeypatch.setattr("app.api.memory.MEMORY_API_KEY", TEST_MEMORY_KEY)


@pytest.mark.anyio
async def test_routes_registered(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    assert "/api/v1/research/ask" in paths
    assert "/api/v1/research/health" in paths


@pytest.mark.anyio
async def test_health_all_deps(client):
    """Mock 3 dependency (ollama + qdrant + memory db)."""
    ol = MagicMock(ok=True)
    ol.json.return_value = {"version": "0.23.2"}
    qd = MagicMock(ok=True)
    qd.json.return_value = {"result": {"points_count": 9945}}

    def fake_get(url, **_):
        return ol if "/api/version" in url else qd

    # memory_db: gercek dosyaya yazmak istemiyoruz; sqlite3.connect'i mock'la
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (42,)
    with patch("app.api.research.requests.get", side_effect=fake_get), patch("app.api.research.sqlite3.connect", return_value=fake_conn):
        resp = await client.get("/api/v1/research/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama"]["ok"] is True
    assert body["qdrant"]["ok"] is True
    assert body["memory_db"]["ok"] is True


@pytest.mark.anyio
async def test_ask_returns_empty_answer_when_no_chunks(client, monkeypatch):
    """Tum kaynaklar bos donerse 'Yetersiz kaynak' don, LLM cagirma."""
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    # Bu durumda Ollama cagrilmamali — patch et ki cagrilirsa test patlasin
    with patch("app.api.research._ollama_generate") as gen:
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test sorgu", "include_rag": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "Yetersiz kaynak" in body["answer"]
    assert body["source_count"] == 0
    gen.assert_not_called()


@pytest.mark.anyio
async def test_ask_full_pipeline_with_citation_validation(client, monkeypatch):
    """Tek discovery dondurup synthesizer'in mock'lanmis cevabini validate et."""
    chunk = {
        "type": "discovery",
        "id": "323",
        "project": "petvet.panola.app",
        "subtype": "bug",
        "title": "self-pentest: eksik security header",
        "status": "active",
        "text": "Eksik header: HSTS CSP",
    }
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [chunk])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    fake_answer = "petvet.panola.app'da HSTS ve CSP header'lari eksik [discovery:323]."
    fake_hallu = "Ayrica [discovery:999] obsolete olarak isaretli."
    with patch("app.api.research._ollama_generate", return_value=f"{fake_answer} {fake_hallu}"):
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "petvet security headers", "include_rag": False},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_count"] == 1
    # 323 gercek -> used; 999 sahte -> hallucinated
    assert "discovery:323" in body["citations"]["used"]
    assert "discovery:999" in body["citations"]["hallucinated"]


@pytest.mark.anyio
async def test_ask_caps_chunks_at_max(client, monkeypatch):
    """max_chunks asilirsa kesilir — qwen 7B prompt boyu hassasiyeti."""
    big_list = [{"type": "discovery", "id": str(i), "title": f"d{i}", "text": "x"} for i in range(20)]
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: big_list)
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    with patch("app.api.research._ollama_generate", return_value="ok"):
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "max_chunks": 5, "include_rag": False},
        )
    assert resp.json()["source_count"] == 5


@pytest.mark.anyio
async def test_engine_claude_uses_anthropic_when_requested(client, monkeypatch):
    """engine='claude' -> _anthropic_generate cagrilir, _ollama_generate degil."""
    chunk = {"type": "discovery", "id": "5", "title": "x", "text": "y"}
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [chunk])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    # ANTHROPIC_API_KEY'i mock'la (modul yuklemesinde okunmus olabilir bos)
    monkeypatch.setattr("app.api.research.ANTHROPIC_API_KEY", "sk-test-fake")
    with (
        patch("app.api.research._anthropic_generate", return_value="cevap [discovery:5]") as cl,
        patch("app.api.research._ollama_generate") as ol,
    ):
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "engine": "claude", "include_rag": False},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "claude"
    cl.assert_called_once()
    ol.assert_not_called()
    assert "discovery:5" in body["citations"]["used"]


@pytest.mark.anyio
async def test_engine_auto_picks_claude_when_chunks_high(client, monkeypatch):
    """8+ kaynak ve ANTHROPIC_API_KEY varsa auto -> claude."""
    many = [{"type": "discovery", "id": str(i), "title": "x", "text": "y"} for i in range(10)]
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: many)
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research.ANTHROPIC_API_KEY", "sk-test")
    with patch("app.api.research._anthropic_generate", return_value="ok") as cl, patch("app.api.research._ollama_generate") as ol:
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "engine": "auto", "include_rag": False, "max_chunks": 10},
        )
    assert resp.json()["engine"] == "claude"
    cl.assert_called_once()
    ol.assert_not_called()


@pytest.mark.anyio
async def test_engine_auto_falls_back_to_local_without_key(client, monkeypatch):
    """ANTHROPIC_API_KEY yoksa auto -> local."""
    many = [{"type": "discovery", "id": str(i), "title": "x", "text": "y"} for i in range(10)]
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: many)
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research.ANTHROPIC_API_KEY", "")  # key yok
    with patch("app.api.research._ollama_generate", return_value="ok") as ol, patch("app.api.research._anthropic_generate") as cl:
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "engine": "auto", "include_rag": False, "max_chunks": 10},
        )
    assert resp.json()["engine"] == "local"
    ol.assert_called_once()
    cl.assert_not_called()


@pytest.mark.anyio
async def test_engine_invalid_rejected(client, monkeypatch):
    chunk = {"type": "discovery", "id": "1", "title": "x", "text": "y"}
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [chunk])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    resp = await client.post(
        "/api/v1/research/ask",
        json={"q": "test", "engine": "gpt5", "include_rag": False},
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_fts_query_hyphens_normalised():
    """'bilge-arena' -> 'bilge OR arena' (FTS5 column-prefix patlamasini engeller)."""
    from app.api.research import _fts_q

    out = _fts_q("bilge-arena security header")
    # Tire kelimeleri ayirir, kucuk kelimeler (<3 char) atilir
    assert "bilge" in out
    assert "arena" in out
    assert "OR" in out
    # FTS5'in column-prefix sentaksini tetikleyecek karakterler yok
    for forbidden in ('"', "*", ":"):
        assert forbidden not in out


# ───────── /run (otonom araştırma ajanı) ─────────


@pytest.mark.anyio
async def test_run_route_registered(client):
    resp = await client.get("/openapi.json")
    assert "/api/v1/research/run" in resp.json()["paths"]


@pytest.mark.anyio
async def test_run_returns_report(client):
    """/run → ResearchReport (plan+ara+sentez), Ollama+Qdrant mock'lu."""
    plan = "Mimari nedir?\nBellek yönetimi nasıl?"
    synth = "Kapsamlı özet metni [1].\nÇIKARIMLAR:\n- ilk bulgu [1]\n- ikinci bulgu [2]"

    def fake_chunks(q, top_k=5, project=None):
        return [{"id": f"doc-{q[:4]}", "title": q, "score": 0.85, "text": f"{q} içerik"}]

    with (
        patch("app.api.research._ollama_generate", side_effect=[plan, synth]),
        patch("app.api.research._qdrant_chunks", side_effect=fake_chunks),
    ):
        # synth_model=ollama → plan VE sentez _ollama_generate'ten (mock'lanabilir tek-yol)
        resp = await client.post(
            "/api/v1/research/run",
            json={"topic": "linux kernel", "max_iterations": 2, "depth": 3, "synth_model": "ollama", "max_hops": 1},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["topic"] == "linux kernel"
    assert body["subquestions"] == ["Mimari nedir?", "Bellek yönetimi nasıl?"]
    assert "Kapsamlı özet" in body["summary"]
    assert body["findings"] == ["ilk bulgu [1]", "ikinci bulgu [2]"]
    assert len(body["sources"]) == 2
    assert body["sources"][0]["ref"] == 1
    assert 0.0 < body["confidence_score"] <= 1.0


@pytest.mark.anyio
async def test_run_rejects_wrong_memory_key(client):
    """Router-level verify_key: yanlış X-Memory-Key → reddedilir."""
    resp = await client.post(
        "/api/v1/research/run",
        json={"topic": "x" * 5},
        headers={"X-Memory-Key": "wrong-key"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_run_validates_short_topic(client):
    """topic min_length=3 → 422 (Ollama/Qdrant çağrılmadan)."""
    resp = await client.post("/api/v1/research/run", json={"topic": "x"})
    assert resp.status_code == 422


# ───────── synth-model seçici (FAZ1: Haiku) ─────────


def test_synth_llm_ollama_uses_aya():
    with patch("app.api.research._ollama_generate", return_value="x") as gen:
        research._synth_llm("ollama")("prompt")
    gen.assert_called_once()
    assert gen.call_args.kwargs.get("model") == research.LLM_MODEL_HI  # aya:8b


def test_synth_llm_haiku_uses_anthropic():
    with patch("app.api.research._anthropic_generate", return_value="haiku-çıktı") as a:
        out = research._synth_llm("haiku")("p")
    assert out == "haiku-çıktı"
    a.assert_called_once()
    assert a.call_args.kwargs.get("model") == research.ANTHROPIC_MODEL  # Haiku model'i


def test_synth_llm_sonnet_uses_sonnet_model():
    with patch("app.api.research._anthropic_generate", return_value="sonnet-çıktı") as a:
        out = research._synth_llm("sonnet")("p")
    assert out == "sonnet-çıktı"
    assert a.call_args.kwargs.get("model") == research.ANTHROPIC_MODEL_SONNET  # Sonnet model'i


def test_synth_llm_sonnet_falls_back_to_aya_on_error():
    with (
        patch("app.api.research._anthropic_generate", side_effect=RuntimeError("api down")),
        patch("app.api.research._ollama_generate", return_value="aya-çıktı") as o,
    ):
        out = research._synth_llm("sonnet")("p")
    assert out == "aya-çıktı"  # Sonnet fail → aya:8b fallback
    o.assert_called_once()


def test_synth_llm_haiku_falls_back_to_aya_on_error():
    # Haiku patlarsa (anahtar-yok/API-hata) → aya:8b yerel fallback (araştırma düşmez)
    with (
        patch("app.api.research._anthropic_generate", side_effect=RuntimeError("no key")),
        patch("app.api.research._ollama_generate", return_value="aya-çıktı") as o,
    ):
        out = research._synth_llm("haiku")("p")
    assert out == "aya-çıktı"
    o.assert_called_once()


@pytest.mark.anyio
async def test_run_haiku_synth_path(client):
    """synth_model=haiku → sentez Haiku'dan, plan Ollama'dan."""
    with (
        patch("app.api.research._ollama_generate", return_value="Soru bir?\nSoru iki?"),
        patch("app.api.research._anthropic_generate", return_value="Haiku özeti.\n- bulgu A") as haiku,
        patch("app.api.research._qdrant_chunks", side_effect=lambda q, **k: [{"id": "a", "title": "A", "score": 0.8, "text": "t"}]),
    ):
        resp = await client.post(
            "/api/v1/research/run",
            json={"topic": "konu testi", "max_iterations": 1, "depth": 2, "synth_model": "haiku", "max_hops": 1},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "Haiku özeti" in body["summary"]
    assert body["findings"] == ["bulgu A"]
    haiku.assert_called_once()  # sentez gerçekten Haiku'dan geçti


# ───────── web arama (FAZ2: DDG-lite) ─────────

_WEB_HIT = {"type": "web", "id": "https://w.com", "title": "W", "score": 0.6, "text": "web"}


def test_strip_html():
    assert research._strip_html("<b>Linux</b> &amp; kernel") == "Linux & kernel"


def test_web_search_parses_ddg_lite(monkeypatch):
    # Parse testi (alaka filtresi DEĞİL) → embed'i sabit-vektöre mock'la (cosine=1 → hepsi tutulur).
    monkeypatch.setattr(research.rag_module, "_embed", lambda text: [1.0, 0.0])
    page = (
        '<a rel="nofollow" href="https://kernel.org/mm" class=\'result-link\'>Memory Mgmt</a>'
        '<td class="result-snippet">Linux &amp; bellek yönetimi</td>'
        '<a rel="nofollow" href="https://example.com" class=\'result-link\'>İkinci</a>'
    )
    resp = MagicMock(ok=True)
    resp.text = page
    with patch("app.api.research.requests.post", return_value=resp):
        out = research._web_search("bellek", n=5)
    assert len(out) == 2
    assert out[0]["type"] == "web"
    assert out[0]["id"] == "https://kernel.org/mm"
    assert out[0]["title"] == "Memory Mgmt"
    assert "bellek yönetimi" in out[0]["text"]
    assert out[0]["score"] > out[1]["score"]  # rank-tabanlı


def test_web_search_network_fail_returns_empty():
    with patch("app.api.research.requests.post", side_effect=Exception("timeout")):
        assert research._web_search("x") == []


@pytest.mark.anyio
async def test_run_include_web_wires_web_search(client):
    """include_web=true → _web_search çağrılır + sonuçları rapora girer."""
    with (
        patch("app.api.research._ollama_generate", side_effect=["soru bir?", "Özet.\n- bulgu"]),
        patch("app.api.research._qdrant_chunks", side_effect=lambda q, **k: [{"id": "rag-1", "title": "R", "score": 0.8, "text": "t"}]),
        patch("app.api.research._web_search", return_value=[_WEB_HIT]) as web,
        patch("app.api.research._synth_llm", return_value=lambda p: "Özet.\n- bulgu"),
    ):
        resp = await client.post(
            "/api/v1/research/run",
            json={"topic": "konu testi", "max_iterations": 1, "depth": 2, "include_web": True},
        )
    assert resp.status_code == 200
    web.assert_called()  # web arama tetiklendi
    ids = {s["source_id"] for s in resp.json()["sources"]}
    assert "https://w.com" in ids  # web kaynağı rapora girdi
    assert "rag-1" in ids  # RAG kaynağı da


@pytest.mark.anyio
async def test_health_exposes_selectable_synth_models(client):
    """Codex: /health hem Haiku hem Sonnet sentez-model-id'lerini + varsayılanı bildirir."""
    ol = MagicMock(ok=True)
    ol.json.return_value = {"version": "0.23.2"}
    qd = MagicMock(ok=True)
    qd.json.return_value = {"result": {"points_count": 1}}
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (1,)
    with (
        patch("app.api.research.requests.get", side_effect=lambda url, **_: ol if "/api/version" in url else qd),
        patch("app.api.research.sqlite3.connect", return_value=fake_conn),
    ):
        resp = await client.get("/api/v1/research/health")
    a = resp.json()["anthropic"]
    assert a["synth_models"]["sonnet"] == research.ANTHROPIC_MODEL_SONNET
    assert a["synth_models"]["haiku"] == research.ANTHROPIC_MODEL
    assert a["default_synth_model"] == "sonnet"


# ───────── web alaka filtresi (off-topic ele + çapraz-dil güvenliği) ─────────


def test_filter_relevant_drops_off_topic_semantic(monkeypatch):
    # Anlamsal kapı: query ile hizalı = tut, dik = at. Embed mock'lanır (Ollama'sız).
    def fake_embed(text):
        t = text.lower()
        return [0.0, 1.0] if ("macos" in t or "apple" in t) else [1.0, 0.0]

    monkeypatch.setattr(research.rag_module, "_embed", fake_embed)
    cands = [
        {"url": "a", "title": "Linux kernel güvenlik", "text": "sertleştirme teknikleri"},
        {"url": "b", "title": "Linux kernel modül", "text": "yükleme"},
        {"url": "c", "title": "Apple macOS", "text": "tamamen alakasız konu"},
    ]
    out = research._filter_relevant(cands, "linux kernel güvenlik", 5)
    urls = [c["url"] for c in out]
    assert "a" in urls
    assert "b" in urls
    assert "c" not in urls  # cosine 0.0 < eşik → elendi


def test_filter_relevant_drops_homonym(monkeypatch):
    # ASIL BUG (06-15): "klipper" hem bu-sunucu hem 3D-yazıcı firmware'i → token-örtüşmesi
    # ayıramıyordu (printer sayfaları geçiyordu). Anlamsal kapı ayırır.
    def fake_embed(text):
        t = text.lower()
        return [0.2, 1.0] if ("yazıcı" in t or "firmware" in t) else [1.0, 0.1]

    monkeypatch.setattr(research.rag_module, "_embed", fake_embed)
    cands = [
        {"url": "printer", "title": "Klipper 3D Yazıcı Firmware Kurulum", "text": "ayar rehberi"},
        {"url": "server", "title": "Klipper sunucu liveness", "text": "meta-monitor bekçi"},
    ]
    out = research._filter_relevant(cands, "klipper sunucu liveness meta-monitor bekçi", 5)
    urls = [c["url"] for c in out]
    assert "server" in urls
    assert "printer" not in urls  # homonim off-topic anlamsal olarak elendi


def test_filter_relevant_falls_back_to_tokens_on_embed_error(monkeypatch):
    # Embed/Ollama fail → token-örtüşme fallback (hard-fail yok). Sıfır-örtüşme atılır.
    def boom(text):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(research.rag_module, "_embed", boom)
    cands = [
        {"url": "a", "title": "Linux kernel", "text": "güvenlik"},
        {"url": "c", "title": "Apple macOS", "text": "alakasız"},
    ]
    out = research._filter_relevant(cands, "linux kernel güvenlik", 5)
    urls = [c["url"] for c in out]
    assert "a" in urls
    assert "c" not in urls


def test_token_filter_empty_when_no_overlap():
    # Davranış değişikliği: hiç örtüşme yoksa ham-listeye DÖNMEZ (off-topic kirliliği
    # geri sokardı) — dürüst boş döner.
    cands = [{"url": "x", "title": "zzz", "text": "qqq"}]
    assert research._token_filter(cands, "linux kernel güvenlik", 5) == []
