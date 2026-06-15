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
    def __init__(self, *, llm: LLMFn, search: SearchFn) -> None:
        self._llm = llm
        self._search = search

    # ── 1) Planlama ──
    def _generate_plan(self, topic: str, n: int) -> list[str]:
        prompt = (
            f'"{topic}" konusunu kapsamlı araştırmak için sorulması gereken {n} ODAKLI alt-soru üret. '
            f"Her satıra TEK soru yaz, numara/madde-işareti koyma, açıklama ekleme."
        )
        raw = self._llm(prompt) or ""
        subs: list[str] = []
        for line in raw.splitlines():
            # baştaki numara/madde-işareti/tırnağı ayıkla
            q = re.sub(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)", "", line).strip().strip("\"'")
            if len(q) >= 5:
                subs.append(q)
        # LLM boş/bozuk dönerse en azından konunun kendisini ara (degrade-gracefully)
        if not subs:
            subs = [topic]
        return subs[:n]

    # ── 2) Arama/Toplama ──
    def _execute_search(self, subquestions: list[str], depth: int, project: str | None) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for sq in subquestions:
            try:
                hits = self._search(sq, depth, project) or []
            except Exception:
                hits = []  # tek alt-soru aramasının patlaması tüm araştırmayı düşürmesin
            for h in hits:
                collected.append({**h, "_subq": sq})
        return collected

    # ── kaynak dedup + atıf-numarası ──
    @staticmethod
    def _dedup_sources(raw: list[dict[str, Any]]) -> list[ResearchSource]:
        best: dict[str, dict[str, Any]] = {}
        for h in raw:
            sid = str(h.get("id") or h.get("title") or h.get("text", "")[:40])
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
        out = self._llm(prompt) or ""
        # Özet = 'ÇIKARIMLAR:' öncesi; çıkarımlar = sonrası '- ' satırları
        parts = re.split(r"(?im)^\s*Ç?IKARIMLAR\s*:?\s*$", out, maxsplit=1)
        summary = parts[0].strip()
        findings: list[str] = []
        if len(parts) > 1:
            for line in parts[1].splitlines():
                m = re.sub(r"^\s*[-*•]\s*", "", line).strip()
                if m:
                    findings.append(m)
        if not summary:
            summary = out.strip() or f'"{topic}" için özet üretilemedi.'
        return summary, findings

    # ── güven puanı (heuristik) ──
    @staticmethod
    def _confidence(sources: list[ResearchSource], n_subq: int, depth: int) -> float:
        if not sources:
            return 0.0
        avg_rel = sum(s.relevance for s in sources) / len(sources)
        coverage = min(1.0, len(sources) / max(1, n_subq))  # alt-soru başına ≥1 kaynak ideali
        return round(min(1.0, 0.5 * avg_rel + 0.5 * coverage), 3)

    # ── orkestrasyon ──
    def run(self, config: ResearchConfig) -> ResearchReport:
        subqs = self._generate_plan(config.topic, config.max_iterations)
        raw = self._execute_search(subqs, config.depth, config.project)
        sources = self._dedup_sources(raw)
        summary, findings = self._synthesize(config.topic, sources)
        return ResearchReport(
            topic=config.topic,
            summary=summary,
            findings=findings,
            sources=sources,
            subquestions=subqs,
            confidence_score=self._confidence(sources, len(subqs), config.depth),
        )
