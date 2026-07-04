"""G4 completeness-guard testleri (PR-B). Tasarım §2b: registry sessizce bayatlayamaz.

G1-kanıtı (fix-remove→test-fail): registry'deki guard-keşifli 2 kayıt (api:dashboard,
cron:autonomous-daily-summary) geri alınırsa test_repo_has_no_unregistered_surfaces
FAIL eder — lokal doğrulandı (kayıtlar tam bu FAIL'i geçirmek için eklendi).
"""

from __future__ import annotations

from pathlib import Path

from app.entrypoints.completeness import _DELIVERY, _NOTES_ACCESS, discover_delivery_surfaces, unregistered_surfaces

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repo_has_no_unregistered_surfaces():
    """ASIL GATE: keşfedilen her teslim-yüzeyi ENTRYPOINTS ∪ EXEMPT içinde.
    Yeni yüzey kayıtsız eklenirse burada patlar (#1222'nin mekanik-önleyicisi)."""
    leaks = unregistered_surfaces(REPO_ROOT)
    assert not leaks, f"kayıtsız teslim-yüzeyi (registry'ye ekle veya gerekçeli-EXEMPT yap): {sorted(leaks)}"


def test_registered_surfaces_still_carry_signature():
    """Ters-yön çürüme-uyarısı: kayıtlı-yüzey imzayı kaybettiyse (teslim-kodu kaldırıldı)
    kayıt bayat demektir — registry'den düşülmeli. (EXEMPT'e değil: yüzey-değilse kayıt-fazla.)"""
    from app.entrypoints.registry import ENTRYPOINTS

    discovered = discover_delivery_surfaces(REPO_ROOT)
    stale = [ep.id for ep in ENTRYPOINTS if ep.locator not in discovered]
    assert not stale, f"kayıtlı ama imza-taşımayan (bayat-kayıt, registry'den düş): {stale}"


def test_guard_catches_new_unregistered_surface(tmp_path):
    """MEKANİK-repro: kayıtsız teslim-imzalı dosya eklenirse guard yakalamalı.
    (Sahte-ağaç — gerçek-repo'ya dosya eklemeden guard'ın yakalama-yolu kanıtlanır.)"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "yeni-teslim.sh").write_text('NOTES=$(sqlite3 db "SELECT title FROM notes WHERE read=0")\n', encoding="utf-8")
    assert unregistered_surfaces(tmp_path) == {"scripts/yeni-teslim.sh"}


def test_signature_boundaries():
    """İmza FP/FN sınırları (ilk-koşum dersleri):
    - 'unreadable' alt-string'i DELIVERY sayılmaz (autonomous-health-check FP'siydi)
    - yalnız not-ATAN kod (POST, unread'siz) teslim-imzası taşımaz
    - gerçek teslim-deseni iki imzayı da taşır"""
    assert not _DELIVERY.search('print("unreadable")')
    post_only = 'curl -X POST "$URL/api/v1/memory/notes" -d @body.json'
    assert _NOTES_ACCESS.search(post_only)
    assert not _DELIVERY.search(post_only)
    delivery = 'SELECT title FROM notes WHERE read=0 AND (read_by IS NULL OR read_by NOT LIKE "%|dev|%")'
    assert _NOTES_ACCESS.search(delivery)
    assert _DELIVERY.search(delivery)
