"""Regresyon guard: HİÇBİR shell-ssh çağrısı host-key doğrulamasını kapatmamalı.

`StrictHostKeyChecking=no` host-key doğrulamasını kapatır → VPS-MITM açığı. Politika
`accept-new` (TOFU: ilk-bağlantıyı kabul et ama key-DEĞİŞİMİNİ reddet = MITM koruması).

Codex P2 (#135): sabit-allowlist yerine REPO-GENELİ dinamik tarama — gelecekte eklenen
cron/infra SSH çağrıları da kapsanır (automation/, infra/ vb. unutulmasın).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Literal'i parça-parça kur → bu test dosyası kendini eşleştirmesin.
FORBIDDEN = "StrictHostKeyChecking" + "=" + "no"
SAFE = "StrictHostKeyChecking" + "=" + "accept-new"
SCAN_DIRS = ["app", "automation", "scripts", "infra"]
THIS_FILE = Path(__file__).name


def _ssh_source_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for ext in ("*.py", "*.sh"):
            files.extend(p for p in base.rglob(ext) if ".bak" not in p.name and p.name != THIS_FILE)
    return files


def test_no_strict_hostkey_disabled_repo_wide():
    offenders = [str(p.relative_to(ROOT)) for p in _ssh_source_files() if FORBIDDEN in p.read_text(encoding="utf-8", errors="ignore")]
    assert not offenders, f"host-key doğrulaması kapalı (MITM) — 'accept-new' kullan: {offenders}"


def test_accept_new_present_somewhere():
    found = any(SAFE in p.read_text(encoding="utf-8", errors="ignore") for p in _ssh_source_files())
    assert found, "Hiçbir SSH çağrısında accept-new yok — politika uygulanmamış?"


def test_scan_finds_files():
    # Tarama gerçekten dosya buluyor (boş-liste yanlış-yeşil olmasın)
    assert _ssh_source_files(), "SSH kaynak taraması boş — glob bozuk?"
