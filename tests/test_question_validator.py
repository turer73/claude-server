"""Tests for Bilge Arena question validator.

Covers: validation rules (15+ rules), batch validation, API endpoints.
Uses mock Supabase responses — no real network calls.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import httpx

# Helper to create httpx.Response with a request set (needed for raise_for_status)
def _mock_response(status_code: int, json_data, headers: dict | None = None) -> httpx.Response:
    resp = httpx.Response(
        status_code,
        json=json_data,
        headers=headers or {},
        request=httpx.Request("GET", "https://test.supabase.co/rest/v1/questions"),
    )
    return resp
import pytest

from app.core.question_validator import (
    VALID_CATEGORIES,
    VALID_GAMES,
    QuestionValidator,
    ValidationError,
    ValidationReport,
)


# --- Helpers ---

def make_question(**overrides) -> dict:
    """Create a valid question dict with optional overrides."""
    base = {
        "id": "test-uuid-001",
        "external_id": "v001",
        "game": "matematik",
        "category": "sayilar",
        "subcategory": None,
        "difficulty": 3,
        "level_tag": None,
        "content": {
            "question": "2 + 2 kaçtır?",
            "options": ["3", "4", "5", "6"],
            "answer": 1,
        },
        "is_active": True,
        "is_boss": False,
        "times_answered": 0,
        "times_correct": 0,
        "source": "original",
        "exam_ref": None,
    }
    base.update(overrides)
    return base


def make_wordquest(**content_overrides) -> dict:
    content = {
        "question": "The most powerful ---- to parachuting is fear.",
        "options": ["resemblance", "adjustment", "deterrent", "submission", "adherence"],
        "answer": 2,
    }
    content.update(content_overrides)
    return make_question(
        id="test-wq-001", game="wordquest", category="vocabulary",
        level_tag="C1", content=content,
    )


@pytest.fixture
def validator():
    return QuestionValidator("https://test.supabase.co", "test-token")


# ============================================================
# 1. Valid questions — no errors
# ============================================================

class TestValidQuestions:
    def test_valid_matematik(self, validator):
        q = make_question()
        errors = validator.validate_question(q)
        assert errors == []

    def test_valid_wordquest(self, validator):
        q = make_wordquest()
        errors = validator.validate_question(q)
        assert errors == []

    def test_valid_turkce(self, validator):
        q = make_question(game="turkce", category="paragraf")
        errors = validator.validate_question(q)
        assert errors == []

    def test_valid_fen(self, validator):
        q = make_question(game="fen", category="fizik")
        errors = validator.validate_question(q)
        assert errors == []

    def test_valid_sosyal(self, validator):
        q = make_question(game="sosyal", category="tarih")
        errors = validator.validate_question(q)
        assert errors == []

    def test_valid_5_options(self, validator):
        q = make_question(content={
            "question": "Hangisi doğrudur?",
            "options": ["A", "B", "C", "D", "E"],
            "answer": 4,
        })
        errors = validator.validate_question(q)
        assert errors == []


# ============================================================
# 2. Game validation
# ============================================================

class TestGameValidation:
    def test_invalid_game(self, validator):
        q = make_question(game="biology")
        errors = validator.validate_question(q)
        assert len(errors) == 1
        assert errors[0].rule == "invalid_game"
        assert errors[0].severity == "critical"

    def test_none_game(self, validator):
        q = make_question(game=None)
        errors = validator.validate_question(q)
        assert any(e.rule == "invalid_game" for e in errors)

    def test_all_valid_games(self, validator):
        for game in VALID_GAMES:
            cats = VALID_CATEGORIES[game]
            q = make_question(game=game, category=cats[0])
            errors = validator.validate_question(q)
            assert errors == [], f"{game}/{cats[0]} should be valid"


# ============================================================
# 3. Category validation
# ============================================================

class TestCategoryValidation:
    def test_category_mismatch(self, validator):
        q = make_question(game="matematik", category="fizik")
        errors = validator.validate_question(q)
        assert any(e.rule == "category_mismatch" for e in errors)
        assert all(e.severity != "critical" for e in errors if e.rule == "category_mismatch")

    def test_valid_categories_per_game(self, validator):
        for game, cats in VALID_CATEGORIES.items():
            for cat in cats:
                q = make_question(game=game, category=cat)
                errors = validator.validate_question(q)
                cat_errors = [e for e in errors if e.rule == "category_mismatch"]
                assert cat_errors == [], f"{game}/{cat} should be valid"


# ============================================================
# 4. Content validation
# ============================================================

class TestContentValidation:
    def test_missing_content(self, validator):
        q = make_question(content=None)
        errors = validator.validate_question(q)
        assert any(e.rule == "missing_content" for e in errors)
        assert any(e.severity == "critical" for e in errors)

    def test_empty_content(self, validator):
        q = make_question(content={})
        errors = validator.validate_question(q)
        # Empty dict is falsy → treated as missing content
        assert any(e.rule == "missing_content" for e in errors)

    def test_content_not_dict(self, validator):
        q = make_question(content="just a string")
        errors = validator.validate_question(q)
        assert any(e.rule == "missing_content" for e in errors)

    def test_missing_question_text(self, validator):
        q = make_question(content={"options": ["A", "B", "C", "D"], "answer": 0})
        errors = validator.validate_question(q)
        assert any(e.rule == "missing_question_text" for e in errors)

    def test_sentence_field_accepted(self, validator):
        """WordQuest uses 'sentence' instead of 'question'."""
        q = make_question(content={
            "sentence": "The ---- of the project was delayed significantly.",
            "options": ["completion", "compete", "complex", "compact"],
            "answer": 0,
        })
        errors = validator.validate_question(q)
        assert not any(e.rule == "missing_question_text" for e in errors)

    def test_short_question(self, validator):
        q = make_question(content={"question": "Kaç?", "options": ["1", "2", "3", "4"], "answer": 0})
        errors = validator.validate_question(q)
        assert any(e.rule == "too_short" for e in errors)
        assert all(e.severity == "info" for e in errors if e.rule == "too_short")

    def test_long_question(self, validator):
        q = make_question(content={
            "question": "X" * 2001,
            "options": ["A", "B", "C", "D"],
            "answer": 0,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "too_long" for e in errors)


# ============================================================
# 5. Options validation
# ============================================================

class TestOptionsValidation:
    def test_options_not_list(self, validator):
        q = make_question(content={"question": "Test sorusu?", "options": "A,B,C,D", "answer": 0})
        errors = validator.validate_question(q)
        assert any(e.rule == "options_not_list" for e in errors)
        assert any(e.severity == "critical" for e in errors)

    def test_too_few_options(self, validator):
        q = make_question(content={
            "question": "Test sorusu?",
            "options": ["A", "B", "C"],
            "answer": 0,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "too_few_options" for e in errors)

    def test_too_many_options(self, validator):
        q = make_question(content={
            "question": "Test sorusu?",
            "options": ["A", "B", "C", "D", "E", "F"],
            "answer": 0,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "too_many_options" for e in errors)

    def test_empty_option(self, validator):
        q = make_question(content={
            "question": "Test sorusu?",
            "options": ["A", "", "C", "D"],
            "answer": 0,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "empty_option" for e in errors)

    def test_duplicate_options(self, validator):
        q = make_question(content={
            "question": "Test sorusu?",
            "options": ["Aynı", "Farklı", "Aynı", "Başka"],
            "answer": 1,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "duplicate_options" for e in errors)

    def test_duplicate_case_insensitive(self, validator):
        q = make_question(content={
            "question": "Test sorusu?",
            "options": ["hello", "HELLO", "world", "test"],
            "answer": 2,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "duplicate_options" for e in errors)


# ============================================================
# 6. Answer validation
# ============================================================

class TestAnswerValidation:
    def test_missing_answer(self, validator):
        q = make_question(content={"question": "Test?", "options": ["A", "B", "C", "D"]})
        errors = validator.validate_question(q)
        assert any(e.rule == "missing_answer" for e in errors)
        assert any(e.severity == "critical" for e in errors)

    def test_answer_not_int(self, validator):
        q = make_question(content={
            "question": "Test?", "options": ["A", "B", "C", "D"], "answer": "B",
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "answer_not_int" for e in errors)

    def test_negative_answer(self, validator):
        q = make_question(content={
            "question": "Test?", "options": ["A", "B", "C", "D"], "answer": -1,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "negative_answer" for e in errors)

    def test_answer_out_of_bounds(self, validator):
        q = make_question(content={
            "question": "Test?", "options": ["A", "B", "C", "D"], "answer": 4,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "answer_out_of_bounds" for e in errors)

    def test_answer_exactly_at_boundary(self, validator):
        """answer=3 with 4 options is valid (0-indexed)."""
        q = make_question(content={
            "question": "Test sorusu?", "options": ["A", "B", "C", "D"], "answer": 3,
        })
        errors = validator.validate_question(q)
        assert not any(e.rule in ("answer_out_of_bounds", "negative_answer") for e in errors)


# ============================================================
# 7. Difficulty validation
# ============================================================

class TestDifficultyValidation:
    def test_difficulty_zero(self, validator):
        q = make_question(difficulty=0)
        errors = validator.validate_question(q)
        assert any(e.rule == "invalid_difficulty" for e in errors)

    def test_difficulty_six(self, validator):
        q = make_question(difficulty=6)
        errors = validator.validate_question(q)
        assert any(e.rule == "invalid_difficulty" for e in errors)

    def test_difficulty_valid_range(self, validator):
        for d in range(1, 6):
            q = make_question(difficulty=d)
            errors = validator.validate_question(q)
            assert not any(e.rule == "invalid_difficulty" for e in errors), f"difficulty={d}"


# ============================================================
# 8. Active-but-broken detection
# ============================================================

class TestActiveBroken:
    def test_active_with_missing_content(self, validator):
        q = make_question(is_active=True, content=None)
        errors = validator.validate_question(q)
        assert any(e.rule == "active_but_broken" for e in errors)

    def test_inactive_with_missing_content(self, validator):
        q = make_question(is_active=False, content=None)
        errors = validator.validate_question(q)
        assert not any(e.rule == "active_but_broken" for e in errors)

    def test_active_with_bad_answer(self, validator):
        q = make_question(is_active=True, content={
            "question": "Test sorusu?", "options": ["A", "B", "C", "D"], "answer": 10,
        })
        errors = validator.validate_question(q)
        assert any(e.rule == "active_but_broken" for e in errors)


# ============================================================
# 9. Source validation
# ============================================================

class TestSourceValidation:
    def test_invalid_source(self, validator):
        q = make_question(source="unknown_source")
        errors = validator.validate_question(q)
        assert any(e.rule == "invalid_source" for e in errors)
        assert all(e.severity == "info" for e in errors if e.rule == "invalid_source")

    def test_valid_sources(self, validator):
        for src in ("original", "derived", "ai_generated", "tyt_full_bank"):
            q = make_question(source=src)
            errors = validator.validate_question(q)
            assert not any(e.rule == "invalid_source" for e in errors)


# ============================================================
# 10. Batch validation & report
# ============================================================

class TestBatchValidation:
    @pytest.mark.anyio
    async def test_run_full_validation(self, validator):
        questions = [
            make_question(id="q1"),
            make_question(id="q2", content=None),  # broken
            make_question(id="q3", game="fen", category="fizik"),
        ]

        mock_resp = _mock_response(200, questions, {"content-range": f"0-2/{len(questions)}"})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            report = await validator.run_full_validation()

        assert report.total_questions == 3
        assert report.valid_count == 2
        assert report.error_count == 1  # q2 has critical errors
        assert report.duration_ms > 0
        assert report.timestamp != ""
        assert "matematik" in report.by_game
        assert report.by_game["matematik"]["total"] == 2

    @pytest.mark.anyio
    async def test_empty_database(self, validator):
        mock_resp = _mock_response(200, [], {"content-range": "*/0"})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            report = await validator.run_full_validation()

        assert report.total_questions == 0
        assert report.valid_count == 0
        assert report.errors == []

    @pytest.mark.anyio
    async def test_game_filter(self, validator):
        questions = [make_question(id="q1", game="fen", category="fizik")]
        mock_resp = _mock_response(200, questions, {"content-range": f"0-0/{len(questions)}"})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get:
            await validator.run_full_validation(game="fen")
            call_kwargs = mock_get.call_args
            assert "eq.fen" in str(call_kwargs)


# ============================================================
# 11. Fetch & pagination
# ============================================================

class TestFetch:
    @pytest.mark.anyio
    async def test_fetch_questions(self, validator):
        mock_resp = _mock_response(200, [make_question()], {"content-range": "0-0/1"})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            rows, total = await validator.fetch_questions()
            assert len(rows) == 1
            assert total == 1

    @pytest.mark.anyio
    async def test_fetch_all_pagination(self, validator):
        page1 = [make_question(id=f"q{i}") for i in range(1000)]
        page2 = [make_question(id=f"q{i}") for i in range(1000, 1500)]

        resp1 = _mock_response(200, page1, {"content-range": "0-999/1500"})
        resp2 = _mock_response(200, page2, {"content-range": "1000-1499/1500"})

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return resp1 if call_count == 1 else resp2

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            all_rows = await validator.fetch_all_questions()
            assert len(all_rows) == 1500
            assert call_count == 2

    @pytest.mark.anyio
    async def test_network_error(self, validator):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.ConnectError("fail")):
            with pytest.raises(httpx.ConnectError):
                await validator.fetch_questions()


# ============================================================
# 12. Summary
# ============================================================

class TestSummary:
    @pytest.mark.anyio
    async def test_get_summary(self, validator):
        async def mock_get(*args, **kwargs):
            return _mock_response(200, [], {"content-range": "*/100"})

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
            summary = await validator.get_summary()
            assert "matematik" in summary
            assert "total" in summary
            assert summary["total"] == 500  # 5 games * 100


# ============================================================
# 13. API endpoint tests
# ============================================================

class TestAPIEndpoints:
    @pytest.mark.anyio
    async def test_run_requires_auth(self, client):
        resp = await client.post("/api/v1/validation/run")
        assert resp.status_code in (401, 403)

    @pytest.mark.anyio
    async def test_summary_requires_auth(self, client):
        resp = await client.get("/api/v1/validation/summary")
        assert resp.status_code in (401, 403)

    @pytest.mark.anyio
    async def test_errors_requires_auth(self, client):
        resp = await client.get("/api/v1/validation/errors")
        assert resp.status_code in (401, 403)

    @pytest.mark.anyio
    async def test_question_requires_auth(self, client):
        resp = await client.get("/api/v1/validation/question/test-id")
        assert resp.status_code in (401, 403)

    @pytest.mark.anyio
    async def test_errors_no_report(self, client, auth_headers):
        # Reset cached report
        import app.api.validation as val_mod
        val_mod._last_report = None

        resp = await client.get("/api/v1/validation/errors", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []


# ============================================================
# 14. Edge cases
# ============================================================

class TestEdgeCases:
    def test_question_with_all_fields_valid(self, validator):
        q = make_question(
            external_id="math_001",
            game="matematik",
            category="geometri",
            subcategory="ucgenler",
            difficulty=5,
            source="ai_generated",
            is_boss=True,
            content={
                "question": "Bir üçgenin iç açıları toplamı kaç derecedir?",
                "options": ["90", "180", "270", "360"],
                "answer": 1,
                "solution": "Üçgenin iç açıları toplamı 180 derecedir.",
                "hint": "Düz açı ile ilişkilidir.",
            },
        )
        errors = validator.validate_question(q)
        assert errors == []

    def test_content_with_extra_fields(self, validator):
        """Extra fields in content should not cause errors."""
        q = make_question(content={
            "question": "Normal soru?",
            "options": ["A", "B", "C", "D"],
            "answer": 0,
            "extra_field": "should be ignored",
            "metadata": {"custom": True},
        })
        errors = validator.validate_question(q)
        assert errors == []

    def test_answer_zero_is_valid(self, validator):
        q = make_question(content={
            "question": "İlk seçenek doğru olan soru?",
            "options": ["Doğru", "Yanlış1", "Yanlış2", "Yanlış3"],
            "answer": 0,
        })
        errors = validator.validate_question(q)
        assert not any(e.rule in ("missing_answer", "negative_answer", "answer_out_of_bounds") for e in errors)

    def test_external_id_not_string(self, validator):
        q = make_question(external_id=12345)
        errors = validator.validate_question(q)
        assert any(e.rule == "invalid_external_id" for e in errors)

    def test_multiple_errors_on_same_question(self, validator):
        """A single question can have multiple validation errors."""
        q = make_question(
            difficulty=0,
            source="bad_source",
            content={
                "question": "Q?",  # too short
                "options": ["A", "A", "C"],  # too few + duplicate
                "answer": 5,  # out of bounds
            },
        )
        errors = validator.validate_question(q)
        rules = {e.rule for e in errors}
        assert "invalid_difficulty" in rules
        assert "too_short" in rules
        assert "too_few_options" in rules
        assert "duplicate_options" in rules
        assert "answer_out_of_bounds" in rules
