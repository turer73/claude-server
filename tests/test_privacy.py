"""Tests for app.core.privacy.redact — secret strip."""

from app.core.privacy import redact


def test_none_returns_none():
    out, labels = redact(None)
    assert out is None
    assert labels == []


def test_empty_string():
    out, labels = redact("")
    assert out == ""
    assert labels == []


def test_no_secret_unchanged():
    text = "Bu sade bir aciklama, hicbir secret yok."
    out, labels = redact(text)
    assert out == text
    assert labels == []


def test_github_pat_classic_redacted():
    token = "ghp_" + "a" * 36
    out, labels = redact(f"key={token} sonra")
    assert token not in out
    assert "[REDACTED:github_pat_classic]" in out
    assert "github_pat_classic" in labels


def test_github_pat_fine_redacted():
    token = "github_pat_" + "X" * 82
    out, labels = redact(token)
    assert token not in out
    assert "github_pat_fine" in labels


def test_anthropic_key_redacted():
    token = "sk-ant-" + "a" * 95
    out, labels = redact(f"X-API-Key: {token}")
    assert token not in out
    assert "anthropic" in labels


def test_openai_key_redacted_not_clashing_with_anthropic():
    """sk-* match ediyor ama sk-ant-* zaten ustte yakaland icin OpenAI sadece
    48-char [A-Za-z0-9] match'i alir."""
    oai = "sk-" + "A" * 48
    ant = "sk-ant-" + "b" * 90
    out, labels = redact(f"openai={oai}\nanthropic={ant}")
    assert oai not in out
    assert ant not in out
    assert "openai" in labels
    assert "anthropic" in labels


def test_google_api_redacted():
    token = "AIza" + "x" * 35
    out, labels = redact(token)
    assert "[REDACTED:google_api]" in out
    assert "google_api" in labels


def test_aws_access_key_redacted():
    token = "AKIA" + "Q" * 16
    out, labels = redact(f"AWS_ACCESS_KEY_ID={token}")
    assert token not in out
    assert "aws_access_key" in labels


def test_slack_token_redacted():
    token = "xoxb-" + "1" * 20
    out, labels = redact(token)
    assert token not in out
    assert "slack_token" in labels


def test_bearer_token_redacted():
    out, labels = redact("Authorization: Bearer abc123def456ghi789jkl0_token")
    assert "abc123" not in out
    assert "bearer_token" in labels


def test_multiple_secrets_in_one_text():
    text = f"gh={'ghp_' + 'a'*36} aws={'AKIA' + 'Q'*16}"
    out, labels = redact(text)
    assert "[REDACTED:github_pat_classic]" in out
    assert "[REDACTED:aws_access_key]" in out
    assert set(labels) == {"github_pat_classic", "aws_access_key"}


def test_legitimate_documentation_token_also_redacted_false_positive_accepted():
    """Documentation snippet'leri de redact eder — false negative'den (gercek
    leak) daha az pahali. Bu kabul edilen davranis."""
    doc_example = "ghp_" + "a" * 36
    out, _ = redact(f"Example PAT format: {doc_example}")
    assert doc_example not in out


def test_short_strings_not_falsely_redacted():
    """Kisa benzeri pattern'ler match etmemeli (kelime sinirlari + min length)."""
    out, labels = redact("kalip ghp_kisa AIza_short AKIA_kucuk sk-tiny")
    assert labels == []
