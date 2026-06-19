"""Code-reviewer yetenekleri Action olarak (review/learn/research).

Action deseni: her yetenek isimli + uniform run(). Yeni-mod = yeni Action (registry'e
ekle), ajan döngüsü değişmez. code_reviewer saf-mantığı sarmalanır (read-only).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core import code_reviewer as cr
from app.core.agents.base import ActionRegistry


class ReviewAction:
    name = "review"
    description = "Bir dosyayı qwen-coder ile incele → bulgular (read-only, dedup'lı kaydet)"

    async def run(self, path: Path | None = None, **kwargs) -> dict:
        if path is None:
            return {"new": 0, "dup": 0, "p1_titles": []}
        findings = await cr.review_file(path)
        if not findings:
            return {"new": 0, "dup": 0, "p1_titles": []}
        rel = str(path.relative_to(cr.ROOT)) if path.is_relative_to(cr.ROOT) else path.name
        res = await asyncio.to_thread(cr.record_findings, rel, findings)
        return {"rel": rel, **res}


class LearnAction:
    name = "learn"
    description = "Tekrar-eden bulgu desenini 'learning' dersine sentezle"

    async def run(self, **kwargs) -> dict:
        created = await asyncio.to_thread(cr.synthesize_lesson)
        return {"created": created}


class ResearchAction:
    name = "research"
    description = "Bir stack-topic için web-araştır → benimsenecek yeni-yapı bulgusu"

    async def run(self, topic: str = "", **kwargs) -> dict:
        found = await cr.research_new_structure(topic)
        return {"found": found, "topic": topic}


def build_code_review_registry() -> ActionRegistry:
    reg = ActionRegistry()
    for action in (ReviewAction(), LearnAction(), ResearchAction()):
        reg.register(action)
    return reg
