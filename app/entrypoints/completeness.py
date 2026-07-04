"""G4 completeness-guard — registry'nin kendi-kör-noktasını kapatan META-gate (tasarım §2b).

Registry "tüm teslim-yüzeylerini listeliyor mu" sorusu, çözdüğü bug-sınıfının aynısıdır
(#1222: 4-hook-fix note-poller'ı kaçırdı). Bu modül repo'yu içerik-imzasıyla tarar ve
keşfedilen her yüzeyin ENTRYPOINTS ∪ EXEMPT içinde olmasını zorlar — yeni teslim-yüzeyi
kayıtsız eklenirse CI-FAIL (registry sessizce bayatlayamaz).

İmza (gerçek-producer'lardan çıkarıldı, #100383 keşfi): teslim-yüzeyi = notes-verisine
ERİŞEN ve OKUNMAMIŞLIK kavramı taşıyan dosya. Not-ATAN yüzeyler (create_note POST) unread
sorgulamaz → imza-dışı kalır (FP-freni). Dürüst-sınır (tasarım §5): imzanın kendisi
eksik-olabilir (meta-meta) — imza-satırları aşağıda tek-yerde, değişiklikleri review-görünür.

İlk-koşum kanıtı (2026-07-04): guard daha PR'lanmadan 2 GERÇEK kayıtsız-yüzey buldu
(dashboard.py + autonomous-daily-summary.sh unread-sayaçları — üstelik ikisi de held-körü
ve legacy read=0, #647-sınıfı doğruluk-bug'ı) + 1 imza-FP yakalandı (unreadable → \\b-sınır).
"""

from __future__ import annotations

import re
from pathlib import Path

# Taranan ağaç: runtime-yüzeylerin yaşadığı dizinler. tests/ hariç (test-fixture'ları
# imza taşır ama yüzey değildir); docs/ hariç.
SCAN_DIRS = ("app", "scripts", "automation")
SCAN_SUFFIXES = (".py", ".sh")

# İmza-1: notes-verisine erişim (SQL-tablo / REST-yol / pragma-şema).
_NOTES_ACCESS = re.compile(r"FROM\s+notes\b|memory/notes|pragma_table_info\(['\"]notes['\"]\)", re.IGNORECASE)
# İmza-2: okunmamışlık/teslim kavramı (not-ATAN yüzeylerde bulunmaz — ayırıcı).
# \b-sınırlı: 'unreadable' gibi alt-string FP'leri elendi (ilk-koşum: autonomous-health-check).
_DELIVERY = re.compile(r"\bunread\b|read\s*=\s*0|\bread_by\b|_unread_pred", re.IGNORECASE)


def discover_delivery_surfaces(repo_root: Path) -> set[str]:
    """Repo-göreli locator-seti: notes-erişimi + teslim-kavramı taşıyan dosyalar."""
    found: set[str] = set()
    for d in SCAN_DIRS:
        base = repo_root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.suffix not in SCAN_SUFFIXES or not p.is_file():
                continue
            rel = p.relative_to(repo_root).as_posix()
            if "/tests/" in f"/{rel}" or "__pycache__" in rel:
                continue
            if rel.startswith("app/entrypoints/"):
                continue  # imza-tanım dosyaları (self-FP); registry kendisi yüzey değil
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _NOTES_ACCESS.search(text) and _DELIVERY.search(text):
                found.add(rel)
    return found


def unregistered_surfaces(repo_root: Path) -> set[str]:
    """Keşif − (ENTRYPOINTS ∪ EXEMPT) = kayıtsız-kaçak yüzeyler. Boş-küme = guard-yeşil."""
    from app.entrypoints.registry import ENTRYPOINTS, EXEMPT

    registered = {ep.locator for ep in ENTRYPOINTS} | set(EXEMPT)
    return discover_delivery_surfaces(repo_root) - registered
