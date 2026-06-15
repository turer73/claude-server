"""ResearchAgent birim testleri — GERÇEK ajan-mantığı, sahte llm/search ile.

False-green değil: dış-bağımlılık (Ollama/Qdrant) sahte-callable, ama plan-parse,
dedup, sentez-parse, güven-puanı, degrade yolları GERÇEK kodla çalışır.
"""

from __future__ import annotations

from app.core.research_agent import ResearchAgent
from app.models.schemas import ResearchConfig


def _agent(llm, search):
    return ResearchAgent(llm=llm, search=search)


def test_plan_parse_strips_numbering_and_caps():
    def llm(_p):
        return "1. Mimari nedir?\n2) Bellek yönetimi\n- Zamanlayıcı\n\nfazladan soru burada"

    agent = _agent(llm, lambda *a: [])
    subs = agent._generate_plan("linux kernel", n=3)
    assert subs == ["Mimari nedir?", "Bellek yönetimi", "Zamanlayıcı"]  # numara/madde ayıklandı + cap=3


def test_plan_empty_falls_back_to_topic():
    agent = _agent(lambda _p: "   \n  ", lambda *a: [])
    assert agent._generate_plan("konu", n=5) == ["konu"]


def test_dedup_keeps_highest_score_and_numbers_refs():
    raw = [
        {"id": "a", "title": "A", "score": 0.5, "text": "x"},
        {"id": "a", "title": "A", "score": 0.9, "text": "x"},  # aynı id, yüksek skor kazanır
        {"id": "b", "title": "B", "score": 0.7, "text": "y"},
    ]
    sources = ResearchAgent._dedup_sources(raw)
    assert [s.source_id for s in sources] == ["a", "b"]  # skor-sıralı (0.9, 0.7)
    assert [s.ref for s in sources] == [1, 2]
    assert sources[0].relevance == 0.9


def test_synthesize_splits_summary_and_findings():
    out = "Bu bir özet paragrafı [1].\nÇIKARIMLAR:\n- birinci bulgu [1]\n- ikinci bulgu [2]"
    agent = _agent(lambda _p: out, lambda *a: [])
    from app.models.schemas import ResearchSource

    srcs = [ResearchSource(ref=1, title="A", source_id="a", snippet="x", relevance=0.9)]
    summary, findings = agent._synthesize("konu", srcs)
    assert summary == "Bu bir özet paragrafı [1]."
    assert findings == ["birinci bulgu [1]", "ikinci bulgu [2]"]


def test_synthesize_no_sources_returns_inconclusive():
    agent = _agent(lambda _p: "ÇAĞRILMAMALI", lambda *a: [])
    summary, findings = agent._synthesize("konu", [])
    assert "kaynak bulunamadı" in summary
    assert findings == []


def test_confidence_zero_without_sources():
    assert ResearchAgent._confidence([], 5, 5) == 0.0


def test_search_exception_does_not_crash():
    def boom(*_a, **_k):
        raise RuntimeError("qdrant down")

    agent = _agent(lambda _p: "soru1\nsoru2", boom)
    out = agent._execute_search(["soru1", "soru2"], depth=5, project=None)
    assert out == []  # patlama yutuldu, araştırma düşmedi


def test_run_end_to_end_with_fakes():
    # 1. çağrı = plan, 2. çağrı = sentez (sıra-bağımlı sahte llm)
    calls = {"n": 0}

    def llm(_p):
        calls["n"] += 1
        if calls["n"] == 1:
            return "Mimari?\nBellek?"
        return "Kapsamlı özet.\nÇIKARIMLAR:\n- bulgu A\n- bulgu B"

    def search(q, k, p):
        return [{"id": f"doc-{q[:3]}", "title": q, "score": 0.8, "text": f"{q} içeriği"}]

    agent = _agent(llm, search)
    report = agent.run(ResearchConfig(topic="linux kernel", max_iterations=2, depth=3))
    assert report.topic == "linux kernel"
    assert report.subquestions == ["Mimari?", "Bellek?"]
    assert "Kapsamlı özet" in report.summary
    assert report.findings == ["bulgu A", "bulgu B"]
    assert len(report.sources) == 2  # 2 farklı alt-soru → 2 farklı doc
    assert all(s.ref == i + 1 for i, s in enumerate(report.sources))
    assert 0.0 < report.confidence_score <= 1.0


def test_synthesize_blank_llm_with_sources_falls_back():
    # Codex: kaynak VAR ama LLM boş dönerse summary fallback'e düşer (satır 106).
    from app.models.schemas import ResearchSource

    agent = _agent(lambda _p: "   \n  ", lambda *a: [])
    srcs = [ResearchSource(ref=1, title="A", source_id="a", snippet="x", relevance=0.9)]
    summary, findings = agent._synthesize("konu", srcs)
    assert "üretilemedi" in summary  # boş çıktı → fallback metni
    assert findings == []


def test_plan_strips_markdown_and_label_prefixes():
    # Canlı-smoke: 3B model "**Madde: X**", "Soru: Y", "### Z" döndürdü
    def llm(_p):
        return "**Madde: Linux Kernel Modülleri**\nSoru: Bellek nasıl yönetilir?\n### Mimari katmanları"

    agent = _agent(llm, lambda *a: [])
    subs = agent._generate_plan("konu", n=5)
    assert subs == ["Linux Kernel Modülleri", "Bellek nasıl yönetilir?", "Mimari katmanları"]


def test_synthesize_markdown_format_without_cikarimlar_header():
    # Canlı-smoke kök-sorun: model 'ÇIKARIMLAR:' yerine '### Özet' + bullet verdi
    from app.models.schemas import ResearchSource

    out = "### Özet Paragraf\n\nLinux AI server 3 kernel modülü tanımlar.\n\n- proc_linux_ai CPU/RAM sağlar\n- nf_linux_ai IP firewall"
    agent = _agent(lambda _p: out, lambda *a: [])
    srcs = [ResearchSource(ref=1, title="A", source_id="a", snippet="x", relevance=0.9)]
    summary, findings = agent._synthesize("konu", srcs)
    assert "3 kernel modülü" in summary  # başlık atlandı, prose summary'de
    assert "### Özet" not in summary  # markdown başlık summary'ye girmedi
    assert findings == ["proc_linux_ai CPU/RAM sağlar", "nf_linux_ai IP firewall"]  # bullet'lar findings


def test_synthesize_compact_bullet_and_decimal_safety():
    # Codex: boşluksuz '-bulgu' finding olmalı. AMA ondalık '3.14 ...' finding OLMAMALI.
    from app.models.schemas import ResearchSource

    out = "Özet metni burada.\n-kompakt bulgu\nOran 3.14 değerindedir ve önemlidir."
    agent = _agent(lambda _p: out, lambda *a: [])
    srcs = [ResearchSource(ref=1, title="A", source_id="a", snippet="x", relevance=0.9)]
    summary, findings = agent._synthesize("konu", srcs)
    assert findings == ["kompakt bulgu"]  # -kompakt → finding; 3.14-satırı → finding DEĞİL
    assert "3.14 değerindedir" in summary  # ondalık prose summary'de kaldı


def test_synth_uses_separate_synth_llm():
    # FAZ1: plan→llm, sentez→synth_llm (AYRI modeller)
    plan_calls, synth_calls = [], []

    def plan(p):
        plan_calls.append(p)
        return "soru bir\nsoru iki"

    def synth(p):
        synth_calls.append(p)
        return "Güçlü-model özeti.\n- bulgu X"

    agent = ResearchAgent(
        llm=plan,
        synth_llm=synth,
        search=lambda q, k, pr: [{"id": "a", "title": "A", "score": 0.8, "text": "t"}],
    )
    rep = agent.run(ResearchConfig(topic="konu xyz", max_iterations=2, depth=2))
    assert len(plan_calls) == 1  # plan tek-çağrı (qwen)
    assert len(synth_calls) == 1  # sentez tek-çağrı (Haiku) — AYRI llm
    assert "Güçlü-model özeti" in rep.summary
    assert rep.findings == ["bulgu X"]


def test_synth_llm_defaults_to_plan_llm():
    # synth_llm verilmezse plan-llm'e düşer (geriye-uyum)
    calls = []

    def llm(p):
        calls.append(p)
        return "soru\n- bulgu" if len(calls) == 1 else "Özet.\n- bulgu"

    agent = ResearchAgent(llm=llm, search=lambda *a: [{"id": "a", "title": "A", "score": 0.7, "text": "t"}])
    agent.run(ResearchConfig(topic="konu xyz", max_iterations=1, depth=2))
    assert len(calls) == 2  # plan + sentez AYNI llm'e gitti (synth_llm=None → llm)


def test_web_search_merged_with_rag():
    # FAZ2: web_search verilirse RAG sonuçlarına EKLENİR (her ikisi de toplanır)
    def rag(q, k, pr):
        return [{"id": "rag-1", "title": "RAG kaynak", "score": 0.8, "text": "yerel"}]

    def web(q, k):
        return [{"id": "https://x.com", "title": "Web kaynak", "score": 0.6, "text": "web"}]

    agent = ResearchAgent(llm=lambda p: "soru bir", search=rag, web_search=web)
    out = agent._execute_search(["soru bir"], depth=3, project=None)
    ids = {h["id"] for h in out}
    assert ids == {"rag-1", "https://x.com"}  # RAG + web birleşti


def test_web_search_failure_does_not_crash():
    def web_boom(q, k):
        raise RuntimeError("ddg down")

    agent = ResearchAgent(
        llm=lambda p: "s",
        search=lambda *a: [{"id": "rag-1", "title": "R", "score": 0.7, "text": "t"}],
        web_search=web_boom,
    )
    out = agent._execute_search(["s"], depth=2, project=None)
    assert [h["id"] for h in out] == ["rag-1"]  # web patladı → RAG'la devam
