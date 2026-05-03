"""Bilge Arena soru doğrulama REST API.

Supabase'deki soruların yapısal bütünlüğünü kontrol eder.
Tüm endpoint'ler auth gerektirir.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from app.core.config import get_settings
from app.core.question_validator import QuestionValidator, ValidationReport
from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/validation", tags=["validation"])

# Cache son raporu bellekte tut (sunucu yeniden başlayana kadar)
_last_report: ValidationReport | None = None


def _get_validator() -> QuestionValidator:
    s = get_settings()
    if not s.supabase_url or not s.supabase_token:
        raise ValueError("SUPABASE_URL ve SUPABASE_TOKEN ayarlanmalı")
    return QuestionValidator(s.supabase_url, s.supabase_token)


@router.post("/run", dependencies=[Depends(require_auth)])
async def run_validation(
    game: str | None = Query(None, description="Oyun filtresi: matematik, turkce, fen, sosyal, wordquest"),
):
    """Tam doğrulama çalıştır. Opsiyonel game filtresi."""
    global _last_report
    validator = _get_validator()
    report = await validator.run_full_validation(game=game)
    _last_report = report

    result = asdict(report)
    # Hata listesini sınırla (çok büyük olabilir)
    if len(result["errors"]) > 200:
        result["errors_truncated"] = True
        result["errors_total"] = len(result["errors"])
        result["errors"] = result["errors"][:200]

    return result


@router.get("/summary", dependencies=[Depends(require_auth)])
async def get_summary():
    """Oyun bazlı soru sayısı özeti + son doğrulama sonucu."""
    validator = _get_validator()
    counts = await validator.get_summary()

    result = {"question_counts": counts}
    if _last_report:
        result["last_validation"] = {
            "timestamp": _last_report.timestamp,
            "total": _last_report.total_questions,
            "valid": _last_report.valid_count,
            "errors": _last_report.error_count,
            "warnings": _last_report.warning_count,
            "duration_ms": round(_last_report.duration_ms, 1),
            "by_game": _last_report.by_game,
        }
    return result


@router.get("/errors", dependencies=[Depends(require_auth)])
async def get_errors(
    severity: str | None = Query(None, description="Filtre: critical, warning, info"),
    game: str | None = Query(None, description="Oyun filtresi"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Son doğrulamadaki hataları listele."""
    if not _last_report:
        return {"message": "Henüz doğrulama çalıştırılmadı. POST /validation/run kullanın.", "errors": []}

    errors = _last_report.errors
    if severity:
        errors = [e for e in errors if e.severity == severity]
    if game:
        # question_id üzerinden filtrelemek zor, by_rule kullan
        # Aslında errors'da game yok, ama validator'dan gelen veriye göre filtre
        pass

    total = len(errors)
    page = [asdict(e) for e in errors[offset : offset + limit]]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "by_rule": _last_report.by_rule,
        "errors": page,
    }


@router.get("/question/{question_id}", dependencies=[Depends(require_auth)])
async def validate_single_question(question_id: str):
    """Tek bir soruyu doğrula."""
    validator = _get_validator()
    # Supabase'den tek soru çek
    url = f"{validator._url}/rest/v1/questions"
    params = {
        "select": "id,external_id,game,category,subcategory,difficulty,level_tag,"
        "content,is_active,is_boss,times_answered,times_correct,source,exam_ref",
        "id": f"eq.{question_id}",
        "limit": "1",
    }
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=validator._headers, params=params)
        resp.raise_for_status()
        rows = resp.json()

    if not rows:
        return {"error": "Soru bulunamadı", "question_id": question_id}

    q = rows[0]
    errors = validator.validate_question(q)
    return {
        "question_id": question_id,
        "game": q.get("game"),
        "category": q.get("category"),
        "is_active": q.get("is_active"),
        "is_valid": len(errors) == 0,
        "error_count": len(errors),
        "errors": [asdict(e) for e in errors],
        "content_preview": {
            "question": (q.get("content", {}).get("question") or "")[:100],
            "options_count": len(q.get("content", {}).get("options", [])),
            "answer": q.get("content", {}).get("answer"),
        },
    }
