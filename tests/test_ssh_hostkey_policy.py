"""Regresyon guard: tüm shell-ssh çağrıları host-key doğrulamasını AÇIK tutmalı.

StrictHostKeyChecking=no host-key doğrulamasını kapatır → VPS-MITM açığı. Politika
'accept-new' (TOFU: ilk-bağlantıyı kabul et ama key-DEĞİŞİMİNİ reddet = MITM koruması).
Bu test, birinin yanlışlıkla '=no'ya geri dönmesini yakalar (codecov-exec değil,
kaynak-tarama regresyon-guard'ı; ilgili satırlar canlı-VPS gerektirdiği için unit-test edilmez).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SSH_FILES = [
    "app/mcp/tools.py",
    "app/core/ci_runner.py",
    "app/core/devops_agent.py",
    "app/api/social.py",
    "app/api/vps.py",
    "app/api/claude_code.py",
]


@pytest.mark.parametrize("rel", SSH_FILES)
def test_no_strict_hostkey_disabled(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=no" not in src, (
        f"{rel}: 'StrictHostKeyChecking=no' host-key doğrulamasını kapatır (MITM). 'accept-new' kullan."
    )


def test_accept_new_present_somewhere():
    # En az bir ssh-yolu accept-new kullanıyor olmalı (politika gerçekten uygulanmış).
    found = any("StrictHostKeyChecking=accept-new" in (ROOT / rel).read_text(encoding="utf-8") for rel in SSH_FILES)
    assert found, "Hiçbir dosyada StrictHostKeyChecking=accept-new yok — politika uygulanmamış?"
