"""G4 registry-iskelet testleri (PR-A). Completeness-guard PR-B'de, ∀-invariant PR-C'de.

Buradaki kilitler registry'nin KENDİ tutarlılığı: id-benzersiz, locator-dosyaları gerçekten
var (taşınan/silinen dosya = çürük-kayıt → CI-FAIL), concern-sorgusu doğru, exempt-gerekçeli.
"""

from __future__ import annotations

from pathlib import Path

from app.entrypoints.registry import ENTRYPOINTS, EXEMPT, NOTE_DELIVERY, Category, by_category, by_concern, get

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ids_unique():
    ids = [ep.id for ep in ENTRYPOINTS]
    assert len(ids) == len(set(ids)), f"çift id: {[i for i in ids if ids.count(i) > 1]}"


def test_locators_exist():
    """Çürük-kayıt gate'i: locator repo'da yoksa registry bayatlamış demektir (taşıma/silme)."""
    missing = [ep.id for ep in ENTRYPOINTS if not (REPO_ROOT / ep.locator).is_file()]
    assert not missing, f"locator-dosyası yok: {missing}"


def test_exempt_locators_exist_and_justified():
    """EXEMPT de çürüyebilir: dosya silindiyse kayıt temizlenmeli; gerekçesiz-exempt yasak."""
    missing = [loc for loc in EXEMPT if not (REPO_ROOT / loc).is_file()]
    assert not missing, f"exempt-dosyası yok: {missing}"
    empty = [loc for loc, why in EXEMPT.items() if not why.strip()]
    assert not empty, f"gerekçesiz exempt: {empty}"


def test_note_delivery_concern_has_all_nine_surfaces():
    """Pilot-kapsam kilidi (#100383 koddan-doğrulanmış liste): 9 yüzey, spawn-yüzeyi dahil.
    Yüzey silinirse/etiketi düşerse bu test konuşur — sessiz-daralma olmaz."""
    ids = {ep.id for ep in by_concern(NOTE_DELIVERY)}
    assert ids == {
        "api:notes-list",
        "api:onboard",
        "cron:digest",
        "hook:stop-check-inbox",
        "hook:session-start",
        "hook:user-prompt-messages",
        "cron:note-poller",
        "cli:agent-feed",
        "cli:claude-memory",
    }
    assert "cron:note-poller" in ids  # #1222'de kaçan SPAWN-yüzeyi — asla düşmemeli


def test_by_category_and_get():
    hooks = by_category(Category.HOOK)
    assert {ep.id for ep in hooks} == {"hook:stop-check-inbox", "hook:session-start", "hook:user-prompt-messages"}
    assert get("cron:note-poller").locator == "automation/note-poller.sh"
    try:
        get("api:yok-boyle-kayit")
        raise AssertionError("KeyError bekleniyordu")
    except KeyError:
        pass


def test_registry_import_is_stdlib_light():
    """Faz-3c dersi: hook'lar her-turn import edebilir — registry app-bağımlılığı çekmemeli.
    (aiosqlite/fastapi gibi ağır-modüller registry-importuyla gelmemeli.)"""
    import subprocess
    import sys

    code = (
        "import sys; import app.entrypoints.registry; "
        "heavy = [m for m in ('aiosqlite', 'fastapi', 'requests') if m in sys.modules]; "
        "print(','.join(heavy) or 'CLEAN')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert r.returncode == 0, r.stderr[-300:]
    assert r.stdout.strip() == "CLEAN", f"registry-importu ağır-modül çekiyor: {r.stdout.strip()}"
