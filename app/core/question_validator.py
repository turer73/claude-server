"""Bilge Arena soru doğrulama motoru.

Supabase'deki soruları çeker ve 15+ kural ile yapısal bütünlüğünü kontrol eder.
Kritik hatalar (bozuk content, geçersiz cevap) ve uyarılar (eksik alan, kısa metin)
ayrı severity seviyeleriyle raporlanır.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

# --- Bilge Arena oyun/kategori tanımları ---

VALID_GAMES = {"wordquest", "matematik", "turkce", "fen", "sosyal"}

VALID_CATEGORIES: dict[str, list[str]] = {
    "matematik": ["sayilar", "problemler", "geometri", "denklemler", "fonksiyonlar", "olasilik"],
    "turkce": ["paragraf", "dil_bilgisi", "sozcuk", "anlam_bilgisi", "yazim_kurallari"],
    "fen": ["fizik", "kimya", "biyoloji"],
    "sosyal": ["tarih", "cografya", "felsefe", "sosyoloji"],
    "wordquest": [
        "vocabulary",
        "grammar",
        "cloze_test",
        "dialogue",
        "restatement",
        "sentence_completion",
        "phrasal_verbs",
    ],
}

VALID_SOURCES = {"original", "derived", "ai_generated", "tyt_full_bank"}

MIN_QUESTION_LENGTH = 10
MAX_QUESTION_LENGTH = 2000
EXPECTED_OPTIONS_COUNT = (4, 5)


# --- Data classes ---


@dataclass
class ValidationError:
    question_id: str
    field: str
    rule: str
    severity: str  # critical, warning, info
    message: str


@dataclass
class ValidationReport:
    total_questions: int = 0
    valid_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    errors: list[ValidationError] = field(default_factory=list)
    by_game: dict[str, dict] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: str = ""


# --- Validator ---


class QuestionValidator:
    """Bilge Arena soru doğrulayıcı — Supabase REST API üzerinden çalışır."""

    PAGE_SIZE = 1000

    def __init__(self, supabase_url: str, supabase_token: str) -> None:
        self._url = supabase_url.rstrip("/")
        self._headers = {
            "apikey": supabase_token,
            "Authorization": f"Bearer {supabase_token}",
            "Content-Type": "application/json",
            "Prefer": "count=exact",
        }

    # --- Supabase fetch ---

    async def fetch_questions(
        self,
        *,
        game: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Fetch questions from Supabase with pagination. Returns (rows, total_count)."""
        url = f"{self._url}/rest/v1/questions"
        params: dict[str, str] = {
            "select": "id,external_id,game,category,subcategory,difficulty,level_tag,"
            "content,is_active,is_boss,times_answered,times_correct,source,exam_ref",
            "order": "created_at.asc",
            "limit": str(limit),
            "offset": str(offset),
        }
        if game:
            params["game"] = f"eq.{game}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers, params=params)
            resp.raise_for_status()
            total = int(resp.headers.get("content-range", "0/0").split("/")[-1] or 0)
            return resp.json(), total

    async def fetch_all_questions(self, *, game: str | None = None) -> list[dict]:
        """Fetch all questions with automatic pagination."""
        all_rows: list[dict] = []
        offset = 0
        while True:
            rows, total = await self.fetch_questions(game=game, limit=self.PAGE_SIZE, offset=offset)
            all_rows.extend(rows)
            offset += len(rows)
            if offset >= total or not rows:
                break
        return all_rows

    # --- Single question validation ---

    def validate_question(self, q: dict) -> list[ValidationError]:
        """Validate a single question dict. Returns list of errors (empty = valid)."""
        errors: list[ValidationError] = []
        qid = q.get("id", "unknown")

        def _add(field_: str, rule: str, severity: str, msg: str) -> None:
            errors.append(ValidationError(qid, field_, rule, severity, msg))

        # 1. game enum
        game = q.get("game")
        if game not in VALID_GAMES:
            _add("game", "invalid_game", "critical", f"Geçersiz game: {game!r}")
            return errors

        # 2. category-game uyumu
        category = q.get("category")
        if category and category not in VALID_CATEGORIES.get(game, []):
            _add("category", "category_mismatch", "warning", f"'{category}' kategorisi '{game}' için tanımlı değil")

        # 3. difficulty range
        diff = q.get("difficulty")
        if diff is not None and (not isinstance(diff, int) or diff < 1 or diff > 5):
            _add("difficulty", "invalid_difficulty", "warning", f"Zorluk 1-5 arası olmalı, bulundu: {diff}")

        # 4. source
        source = q.get("source")
        if source and source not in VALID_SOURCES:
            _add("source", "invalid_source", "info", f"Bilinmeyen source: {source!r}")

        # 5. content existence
        content = q.get("content")
        if not content or not isinstance(content, dict):
            _add("content", "missing_content", "critical", "content alanı boş veya dict değil")
            if q.get("is_active"):
                _add("is_active", "active_but_broken", "critical", "Soru AKTİF ama content bozuk!")
            return errors

        # Detect content type for format-specific validation
        content_type = content.get("type", "multiple_choice")

        # --- cloze_test: passage + questions[] alt dizisi ---
        if content_type == "cloze_test":
            return self._validate_cloze_test(q, content, errors, _add)

        # --- dialogue: lines[] + options + correct ---
        if content_type == "dialogue":
            return self._validate_dialogue(q, content, errors, _add)

        # --- Standard format: question/sentence + options + answer/correct ---
        return self._validate_standard(q, content, errors, _add)

    def _validate_options_and_answer(
        self,
        content: dict,
        errors: list[ValidationError],
        _add,
        *,
        field_prefix: str = "content",
    ) -> bool:
        """Validate options array and answer/correct field. Returns True if options are valid."""
        options = content.get("options")
        if not isinstance(options, list):
            _add(f"{field_prefix}.options", "options_not_list", "critical", f"options bir liste değil: {type(options).__name__}")
            return False

        opt_count = len(options)
        if opt_count < EXPECTED_OPTIONS_COUNT[0]:
            _add(
                f"{field_prefix}.options",
                "too_few_options",
                "warning",
                f"Seçenek sayısı az: {opt_count} (beklenen ≥{EXPECTED_OPTIONS_COUNT[0]})",
            )
        elif opt_count > EXPECTED_OPTIONS_COUNT[1]:
            _add(
                f"{field_prefix}.options",
                "too_many_options",
                "warning",
                f"Seçenek sayısı fazla: {opt_count} (beklenen ≤{EXPECTED_OPTIONS_COUNT[1]})",
            )

        # empty options
        for i, opt in enumerate(options):
            if not opt or (isinstance(opt, str) and not opt.strip()):
                _add(f"{field_prefix}.options", "empty_option", "warning", f"Seçenek [{i}] boş")

        # duplicate options (case-sensitive — yazım kuralları ve genetik sorularında
        # büyük/küçük harf farkı anlam taşır)
        str_options = [str(o).strip() for o in options if o]
        if len(str_options) != len(set(str_options)):
            seen: set[str] = set()
            dupes = [o for o in str_options if o in seen or seen.add(o)]  # type: ignore[func-returns-value]
            _add(f"{field_prefix}.options", "duplicate_options", "warning", f"Tekrar eden seçenek(ler): {dupes[:3]}")

        # answer/correct field (both names accepted)
        answer = content.get("answer") if content.get("answer") is not None else content.get("correct")
        if answer is None:
            _add(f"{field_prefix}.answer", "missing_answer", "critical", "Cevap (answer/correct) alanı yok")
        elif not isinstance(answer, int):
            _add(f"{field_prefix}.answer", "answer_not_int", "critical", f"Cevap integer değil: {type(answer).__name__} ({answer!r})")
        elif answer < 0:
            _add(f"{field_prefix}.answer", "negative_answer", "critical", f"Cevap negatif: {answer}")
        elif answer >= opt_count:
            _add(f"{field_prefix}.answer", "answer_out_of_bounds", "critical", f"Cevap index ({answer}) ≥ seçenek sayısı ({opt_count})")

        return True

    def _validate_standard(
        self,
        q: dict,
        content: dict,
        errors: list[ValidationError],
        _add,
    ) -> list[ValidationError]:
        """Validate standard multiple_choice format (question/sentence + options + answer/correct)."""
        # question text: question, sentence, or passage accepted
        question_text = content.get("question") or content.get("sentence") or ""
        if not question_text or not isinstance(question_text, str):
            _add("content.question", "missing_question_text", "critical", "Soru metni (question/sentence) yok")
        else:
            if len(question_text.strip()) < MIN_QUESTION_LENGTH:
                _add("content.question", "too_short", "info", f"Soru metni çok kısa ({len(question_text)} karakter)")
            if len(question_text) > MAX_QUESTION_LENGTH:
                _add("content.question", "too_long", "warning", f"Soru metni çok uzun ({len(question_text)} karakter)")

        valid_opts = self._validate_options_and_answer(content, errors, _add)
        if not valid_opts and q.get("is_active"):
            _add("is_active", "active_but_broken", "critical", "Soru AKTİF ama options bozuk!")
            return errors

        # active but broken
        if q.get("is_active") and any(e.severity == "critical" for e in errors):
            if not any(e.rule == "active_but_broken" for e in errors):
                _add("is_active", "active_but_broken", "critical", "Soru AKTİF ama kritik hatalar var!")

        # external_id
        ext_id = q.get("external_id")
        if ext_id and not isinstance(ext_id, str):
            _add("external_id", "invalid_external_id", "info", f"external_id string değil: {type(ext_id).__name__}")

        return errors

    def _validate_cloze_test(
        self,
        q: dict,
        content: dict,
        errors: list[ValidationError],
        _add,
    ) -> list[ValidationError]:
        """Validate cloze_test format: passage + questions[] sub-array."""
        # passage text
        passage = content.get("passage", "")
        if not passage or not isinstance(passage, str):
            _add("content.passage", "missing_question_text", "critical", "Cloze test passage metni yok")
        elif len(passage.strip()) < MIN_QUESTION_LENGTH:
            _add("content.passage", "too_short", "info", f"Passage çok kısa ({len(passage)} karakter)")

        # questions sub-array
        questions = content.get("questions")
        if not isinstance(questions, list) or not questions:
            _add("content.questions", "missing_cloze_questions", "critical", "Cloze test questions dizisi yok veya boş")
            if q.get("is_active"):
                _add("is_active", "active_but_broken", "critical", "Soru AKTİF ama cloze questions bozuk!")
            return errors

        # Validate each sub-question
        for i, sub_q in enumerate(questions):
            if not isinstance(sub_q, dict):
                _add(f"content.questions[{i}]", "invalid_sub_question", "critical", f"Alt soru [{i}] dict değil")
                continue
            self._validate_options_and_answer(
                sub_q,
                errors,
                _add,
                field_prefix=f"content.questions[{i}]",
            )

        # active but broken
        if q.get("is_active") and any(e.severity == "critical" for e in errors):
            if not any(e.rule == "active_but_broken" for e in errors):
                _add("is_active", "active_but_broken", "critical", "Soru AKTİF ama kritik hatalar var!")

        return errors

    def _validate_dialogue(
        self,
        q: dict,
        content: dict,
        errors: list[ValidationError],
        _add,
    ) -> list[ValidationError]:
        """Validate dialogue format: lines[] + options + correct."""
        # lines array
        lines = content.get("lines")
        if not isinstance(lines, list) or not lines:
            _add("content.lines", "missing_dialogue_lines", "critical", "Dialogue lines dizisi yok veya boş")
        else:
            has_blank = any(isinstance(ln, dict) and "----" in ln.get("line", "") for ln in lines)
            if not has_blank:
                _add("content.lines", "no_blank_in_dialogue", "warning", "Dialogue'da boşluk (----) bulunamadı")

        # options + correct — use shared validator
        self._validate_options_and_answer(content, errors, _add)

        # active but broken
        if q.get("is_active") and any(e.severity == "critical" for e in errors):
            if not any(e.rule == "active_but_broken" for e in errors):
                _add("is_active", "active_but_broken", "critical", "Soru AKTİF ama kritik hatalar var!")

        return errors

    # --- Batch validation ---

    async def run_full_validation(self, *, game: str | None = None) -> ValidationReport:
        """Run validation on all questions, optionally filtered by game."""
        start = time.monotonic()
        questions = await self.fetch_all_questions(game=game)

        report = ValidationReport(
            total_questions=len(questions),
            timestamp=datetime.now(UTC).isoformat(),
        )

        game_stats: dict[str, dict] = {}

        for q in questions:
            q_game = q.get("game", "unknown")
            if q_game not in game_stats:
                game_stats[q_game] = {"total": 0, "valid": 0, "errors": 0, "warnings": 0}
            game_stats[q_game]["total"] += 1

            errs = self.validate_question(q)
            if not errs:
                report.valid_count += 1
                game_stats[q_game]["valid"] += 1
            else:
                has_critical = False
                has_warning = False
                for e in errs:
                    report.errors.append(e)
                    report.by_rule[e.rule] = report.by_rule.get(e.rule, 0) + 1
                    if e.severity == "critical":
                        has_critical = True
                    elif e.severity == "warning":
                        has_warning = True
                    elif e.severity == "info":
                        report.info_count += 1

                if has_critical:
                    report.error_count += 1
                    game_stats[q_game]["errors"] += 1
                elif has_warning:
                    report.warning_count += 1
                    game_stats[q_game]["warnings"] += 1

        report.by_game = game_stats
        report.duration_ms = (time.monotonic() - start) * 1000
        return report

    async def get_summary(self) -> dict:
        """Quick summary: total per game from Supabase."""
        summary = {}
        for game in VALID_GAMES:
            _, total = await self.fetch_questions(game=game, limit=1, offset=0)
            summary[game] = total
        summary["total"] = sum(summary.values())
        return summary
