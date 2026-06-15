"""Otonom çok-aşamalı araştırma ajanı.

4 aşama: planla (alt-sorular) → ara (canlı Qdrant-RAG) → sentezle (özet+çıkarımlar)
→ raporla (kaynak dedup + atıf-numarası + güven puanı).

TASARIM (Codex/incelemeden çıkan kararlar):
- RAG = **Qdrant** (canlı). ChromaDB RAGEngine ÖLÜ (:8100 down) → kullanılmaz.
- Bağımlılık-enjeksiyonu: `llm` ve `search` callable olarak verilir → core API'ye
  bağımlı olmaz (döngüsel-import yok) + testler GERÇEK ajan-mantığını sahte-fn'lerle
  çalıştırır (her-şey-mock false-green DEĞİL).
- Senkron: /ask ile tutarlı; helper'lar (requests) sync; FastAPI sync-endpoint'i
  threadpool'da koşar → event-loop bloklamaz.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.models.schemas import ResearchConfig, ResearchReport, ResearchSource

# llm: (prompt) -> metin ; search: (soru, top_k, project) -> [{title,id,score,text,...}]
LLMFn = Callable[[str], str]
SearchFn = Callable[..., list[dict[str, Any]]]


class ResearchAgent:
    def __init__(
        self,
        *,
        llm: LLMFn,
        search: SearchFn,
        synth_llm: LLMFn | None = None,
        web_search: SearchFn | None = None,
    ) -> None:
        self._llm = llm  # planlama: hızlı/ucuz (Ollama qwen) — plan basit
        # sentez: GÜÇLÜ model (Haiku). FAZ1: ayrı synth_llm; verilmezse plan-llm'e düşer
        # (geriye-dönük uyum + test-DI). Sentez kalite-darboğazı → ayrı model değer.
        self._synth_llm = synth_llm or llm
        self._search = search  # RAG (Qdrant)
        self._web_search = web_search  # FAZ2: opt-in web kaynağı (None = yalnız RAG)

    # ── 1) Planlama ──
    def _generate_plan(self, topic: str, n: int) -> list[str]:
        prompt = (
            f'"{topic}" konusunu kapsamlı araştırmak için sorulması gereken {n} ODAKLI alt-soru üret. '
            f"Her satıra TEK soru yaz, numara/madde-işareti koyma, açıklama ekleme."
        )
        subs = self._parse_questions(self._llm(prompt), n)
        # LLM boş/bozuk dönerse en azından konunun kendisini ara (degrade-gracefully)
        return subs or [topic]

    @staticmethod
    def _parse_questions(raw: str | None, n: int) -> list[str]:
        """LLM çıktısından temiz alt-soru listesi (numara/madde/markdown/Soru:-önek ayıkla)."""
        subs: list[str] = []
        for line in (raw or "").splitlines():
            q = ResearchAgent._clean_line(line)
            # küçük-model "Soru:"/"Madde:"/markdown-başlık ekleyebilir (canlı-smoke'ta görüldü)
            q = re.sub(r"(?i)^\s*(?:soru|madde|alt-?soru|question)\s*\d*\s*[:：]\s*", "", q).strip()
            if len(q) >= 5:
                subs.append(q)
        return subs[:n]

    # ── FAZ3: multi-hop — bulgulara göre EKSİK kalan yeni alt-sorular ──
    def _refine(self, topic: str, sources: list[ResearchSource], n: int, asked: list[str] | None = None) -> list[str]:
        if not sources:
            return []
        # BAŞLIK YETERSİZ (RAG başlıkları 'memory'/'discovery' gibi jenerik) → SNIPPET ver.
        # Böylece LLM ne bulunduğunu GÖRÜP gerçek-boşluğu hedefler; aksi halde 'metodoloji
        # nedir' gibi meta/süreç sorusu üretiyordu (canlı-smoke: bilge-arena 2. hop zayıftı).
        found = "\n".join(f"- {s.snippet[:160]}" for s in sources[:8] if s.snippet)
        asked = asked or []
        # ÖNCEKİ SORULAR'ı ver → çapraz-hop tekrar biter (3-hop smoke: hop3, hop2'yi tekrarladı).
        asked_block = ("ZATEN SORULDU (BUNLARI TEKRARLAMA):\n" + "\n".join(f"- {q}" for q in asked[-15:]) + "\n\n") if asked else ""
        prompt = (
            f'Konu: "{topic}"\n\nŞu ana dek bulunan bilgiler:\n{found or "(içerik yok)"}\n\n'
            f"{asked_block}"
            f"Yukarıdakilerin DEĞİNMEDİĞİ, {topic} ile ilgili {n} SOMUT ve SPESİFİK alt-soru üret. "
            "Doğrudan teknik/konu-özel alt-başlıkları sor (belirli bir açık türü, bileşen, senaryo). "
            "METODOLOJİ/SÜREÇ/'hangi kaynak' sorusu SORMA. Her satıra tek soru."
        )
        # refine GÜÇLÜ modelde (synth_llm=Sonnet): boşluk-tespiti+keskin-soru akıl-yürütme işi;
        # qwen meta-soru üretiyordu. Plan (ilk-tur) hâlâ hızlı _llm'de (basit bölme).
        new = self._parse_questions(self._synth_llm(prompt), n)
        # ÇİFT-KEMER: LLM yine tekrarlarsa kod-tarafı near-dup ele (Jaccard token-overlap).
        return self._novel_questions(new, asked, n)

    @staticmethod
    def _novel_questions(new: list[str], asked: list[str], n: int) -> list[str]:
        """asked'a (≥0.6 Jaccard token-overlap) çok benzeyenleri ele → çapraz-hop tekrar yok."""

        def toks(q: str) -> set[str]:
            return set(re.findall(r"\w{4,}", q.lower()))

        asked_t = [t for t in (toks(a) for a in asked) if t]
        out: list[str] = []
        for q in new:
            qt = toks(q)
            if not qt:
                continue
            if any(len(qt & at) / len(qt | at) >= 0.6 for at in asked_t):
                continue  # near-dup → atla
            out.append(q)
            asked_t.append(qt)  # aynı turdaki kendi-tekrarını da önle
        return out[:n]

    @staticmethod
    def _clean_line(line: str) -> str:
        """Satır başı gürültüsünü ayıkla: markdown başlık (#), bullet/numara, ** vurgu, tırnak."""
        s = re.sub(r"^\s*#+\s*", "", line.strip())  # markdown başlık
        s = re.sub(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)", "", s)  # numara/madde-işareti
        return s.strip().strip("*").strip().strip("\"'").strip()

    # ── 2) Arama/Toplama ──
    def _execute_search(self, subquestions: list[str], depth: int, project: str | None, topic: str = "") -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for sq in subquestions:
            try:
                hits = self._search(sq, depth, project) or []
            except Exception:
                hits = []  # tek alt-soru aramasının patlaması tüm araştırmayı düşürmesin
            # FAZ2: web kaynağı (opt-in) — RAG sonuçlarına ekle. Web fail → RAG'la devam.
            if self._web_search is not None:
                # ALAKA: web sorgusunu KONU ile çapala → alt-soru genel olsa da konuda kalır
                # (canlı-smoke: 'Hangi kaynaklar...' Apple/macOS getirdi). RAG zaten alt-soruda.
                wq = f"{topic} {sq}".strip() if topic else sq
                try:
                    hits = hits + (self._web_search(wq, depth) or [])
                except Exception:
                    pass
            for h in hits:
                collected.append({**h, "_subq": sq})
        return collected

    @staticmethod
    def _sid(h: dict[str, Any]) -> str:
        """Kaynak kimliği — dedup + hop-arası yeni-kaynak tespiti için tek-kaynak."""
        return str(h.get("id") or h.get("title") or h.get("text", "")[:40])

    # ── kaynak dedup + atıf-numarası ──
    @staticmethod
    def _dedup_sources(raw: list[dict[str, Any]]) -> list[ResearchSource]:
        best: dict[str, dict[str, Any]] = {}
        for h in raw:
            sid = ResearchAgent._sid(h)
            score = float(h.get("score", 0) or 0)
            if sid not in best or score > float(best[sid].get("score", 0) or 0):
                best[sid] = h
        ordered = sorted(best.values(), key=lambda h: float(h.get("score", 0) or 0), reverse=True)
        return [
            ResearchSource(
                ref=i + 1,
                title=str(h.get("title", "?")),
                source_id=str(h.get("id", "?")),
                snippet=str(h.get("text", ""))[:300],
                relevance=round(float(h.get("score", 0) or 0), 3),
            )
            for i, h in enumerate(ordered)
        ]

    # ── 3) Sentezleme ──
    def _synthesize(self, topic: str, sources: list[ResearchSource]) -> tuple[str, list[str]]:
        if not sources:
            return (f'"{topic}" için kaynak bulunamadı; araştırma sonuçsuz.', [])
        context = "\n".join(f"[{s.ref}] {s.title}: {s.snippet}" for s in sources)
        prompt = (
            f'Konu: "{topic}"\n\nKaynaklar:\n{context}\n\n'
            "Yukarıdaki kaynaklara dayanarak (1) kapsamlı bir ÖZET paragrafı yaz, "
            "sonra (2) 'ÇIKARIMLAR:' satırı ardından madde-madde (- ile) bulguları listele. "
            "İddiaları [1], [2] gibi kaynak numaralarıyla atıfla."
        )
        out = (self._synth_llm(prompt) or "").strip()
        # FORMAT-TOLERANSLI parse (canlı-smoke: 3B model 'ÇIKARIMLAR:' yerine '### Özet'
        # markdown verdi → eski split 0-findings buluyordu). Kural: bullet/numara satırları
        # = findings; başlık (#) ve 'ÇIKARIMLAR:' satırları atlanır; kalan prose = summary.
        findings: list[str] = []
        summary_parts: list[str] = []
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            if re.match(r"(?i)^#*\s*\**\s*Ç?IKARIMLAR\b", s) or re.match(r"^#+\s+\S", s):
                continue  # başlık/etiket satırı: ne summary ne finding
            # Sembol-bullet'ta boşluk OPSİYONEL (-bulgu da kabul, Codex); ama sayıda
            # boşluk ZORUNLU → "3.14 önemli" gibi ondalığı yanlış-finding sayma.
            m = re.match(r"^(?:[-*•]\s*|\d+[.)]\s+)(.+)$", s)
            if m:
                findings.append(m.group(1).strip().strip("*").strip())
            else:
                summary_parts.append(s)
        summary = " ".join(summary_parts).strip()
        if not summary:
            summary = out or f'"{topic}" için özet üretilemedi.'
        return summary, findings

    # ── güven puanı (heuristik) ──
    @staticmethod
    def _confidence(sources: list[ResearchSource], n_subq: int, depth: int) -> float:
        if not sources:
            return 0.0
        avg_rel = sum(s.relevance for s in sources) / len(sources)
        coverage = min(1.0, len(sources) / max(1, n_subq))  # alt-soru başına ≥1 kaynak ideali
        return round(min(1.0, 0.5 * avg_rel + 0.5 * coverage), 3)

    # ── orkestrasyon (FAZ3: multi-hop otonom döngü) ──
    def run(self, config: ResearchConfig) -> ResearchReport:
        subqs_all: list[str] = []
        raw_all: list[dict[str, Any]] = []
        seen: set[str] = set()
        # 1. hop = plan; sonraki hop'lar = bulgu-boşluğuna göre refine
        next_subqs = self._generate_plan(config.topic, config.max_iterations)
        for hop in range(config.max_hops):
            if not next_subqs:
                break
            subqs_all.extend(next_subqs)
            raw = self._execute_search(next_subqs, config.depth, config.project, config.topic)
            new = [h for h in raw if self._sid(h) not in seen]
            for h in new:
                seen.add(self._sid(h))
            raw_all.extend(new)
            # OTONOM DURMA: yeni kaynak gelmediyse derinleşmenin anlamı yok
            if not new:
                break
            # son hop değilse: mevcut bulgulara göre EKSİK alanlar için yeni sorular
            # (asked=subqs_all → refine önceki TÜM soruları görüp tekrarlamaz)
            if hop + 1 < config.max_hops:
                next_subqs = self._refine(config.topic, self._dedup_sources(raw_all), config.max_iterations, subqs_all)
            else:
                next_subqs = []
        sources = self._dedup_sources(raw_all)
        summary, findings = self._synthesize(config.topic, sources)
        return ResearchReport(
            topic=config.topic,
            summary=summary,
            findings=findings,
            sources=sources,
            subquestions=subqs_all,
            confidence_score=self._confidence(sources, len(subqs_all), config.depth),
        )
