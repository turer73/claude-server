"""G4 entry-point registry — cross-cutting invariant'ların ∀-parametrize tabanı.

Neden (docs/g4-entry-point-registry-design.md): G1 repro-gate "test bir-şey yakalıyor"u
garantiler ama "DOĞRU/TÜM yolları test ediyor"u DEĞİL. held-note delivery bug'ı (#1222)
tam böyle yaşadı: filtre ~8 yüzeye yayılıydı, 4-hook-fix note-poller SPAWN-yüzeyini
kaçırdı → tek-yüzey-testi yeşilken HOLD etkisizdi. Registry giriş-noktalarını deklaratif
sayar; invariant-testleri buradan parametrize olur (kaçan-yüzey = üretilmemiş-test = FAIL).

Saf-stdlib, import-hafif (Faz-3c DEFAULT_DB dersi: hook'lar her-turn import edebilir).
Pilot concern: note_delivery (tek-concern, over-mekanizasyon-freni — tasarım §4).
Kayıt-listesi 2026-07-04 koddan doğrulandı (#100383 spec-verify: signal_quality +
notify-cron notes-teslimi YOK → kayıt-dışı; MCP'de pilot-kayıt yok).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Pilot invariant-etiketi. Genişleme (auth, destructive_guard) evidence-ile (tasarım §4).
NOTE_DELIVERY = "note_delivery"


class Category(StrEnum):
    API = "api"
    HOOK = "hook"
    CRON = "cron"
    MCP = "mcp"
    WS = "ws"
    # Tasarım-enum'una ek (#100383): agent-feed/claude-memory manuel-CLI teslim-yüzeyleri.
    CLI = "cli"


@dataclass(frozen=True)
class EntryPoint:
    """Tek giriş-noktası kaydı.

    locator: repo-göreli dosya-yolu — completeness-guard keşif-diff'i bununla eşleşir
    ve test_locators_exist çürümüş-kaydı (taşınan/silinen dosya) CI'da yakalar.
    concerns: bu yüzeyin uyması gereken cross-cutting invariant-etiketleri.
    """

    id: str
    category: Category
    locator: str
    concerns: tuple[str, ...] = ()
    note: str = ""


# ── note_delivery yüzeyleri (koddan doğrulanmış 9 kayıt, #100383) ──────────────
# Ortak held-dışlama: app/api/memory/__init__.py::_unread_pred (API-yüzeyler onu kullanır);
# shell-yüzeyler kendi sorgularında filtreler. Invariant: status='held' not HİÇBİR yüzeyden
# teslim/spawn edilmez (#1222).
ENTRYPOINTS: tuple[EntryPoint, ...] = (
    EntryPoint(
        id="api:notes-list",
        category=Category.API,
        locator="app/api/memory/notes.py",
        concerns=(NOTE_DELIVERY,),
        note="GET /notes + unread-akışları; merkezi _unread_pred kullanır",
    ),
    EntryPoint(
        id="api:onboard",
        category=Category.API,
        locator="app/api/memory/onboard.py",
        concerns=(NOTE_DELIVERY,),
        note="session-context onboard çıktısındaki okunmamış-not bloğu",
    ),
    EntryPoint(
        id="cron:digest",
        category=Category.CRON,
        locator="app/core/digest/sources.py",
        concerns=(NOTE_DELIVERY,),
        note="memory_delta.unread_notes — digest raporu teslim-sayılır (rapor≠HOLD-çekirdeği ama tutarlılık)",
    ),
    EntryPoint(
        id="hook:stop-check-inbox",
        category=Category.HOOK,
        locator="scripts/hooks/stop-check-inbox.py",
        concerns=(NOTE_DELIVERY,),
    ),
    EntryPoint(
        id="hook:session-start",
        category=Category.HOOK,
        locator="scripts/hooks/session-start.sh",
        concerns=(NOTE_DELIVERY,),
    ),
    EntryPoint(
        id="hook:user-prompt-messages",
        category=Category.HOOK,
        locator="scripts/hooks/user-prompt-messages.sh",
        concerns=(NOTE_DELIVERY,),
    ),
    EntryPoint(
        id="cron:note-poller",
        category=Category.CRON,
        locator="automation/note-poller.sh",
        concerns=(NOTE_DELIVERY,),
        note="OTONOM-SPAWN tetikleyicisi — #1222'de kaçan yüzey; en-kritik kayıt",
    ),
    EntryPoint(
        id="cli:agent-feed",
        category=Category.CLI,
        locator="scripts/agent-feed.sh",
        concerns=(NOTE_DELIVERY,),
    ),
    EntryPoint(
        id="cli:claude-memory",
        category=Category.CLI,
        locator="scripts/claude-memory.sh",
        concerns=(NOTE_DELIVERY,),
    ),
    # Completeness-guard İLK-KOŞUM keşifleri (2026-07-04): tasarım-listesinde ve ilk
    # 9-kayıtta YOKTU — guard değerini PR'lanmadan kanıtladı (#1222-sınıfı kaçak-yüzey).
    EntryPoint(
        id="api:dashboard",
        category=Category.API,
        locator="app/api/memory/dashboard.py",
        concerns=(NOTE_DELIVERY,),
        note="unread_notes SAYACI (içerik değil) — held-körü + legacy read=0 (per-device değil): doğruluk-bug'ı, ayrı-fix",
    ),
    EntryPoint(
        id="cron:autonomous-daily-summary",
        category=Category.CRON,
        locator="automation/autonomous-daily-summary.sh",
        concerns=(NOTE_DELIVERY,),
        note="DEFERRED_NOTES sayacı — held-körü + legacy read=0: doğruluk-bug'ı, ayrı-fix",
    ),
)

# Keşfedilen ama kayıt-dışı bırakılan yüzeyler: locator -> gerekçe.
# Completeness-guard (PR-B) keşif-sonucunu ENTRYPOINTS ∪ EXEMPT ile diff'ler;
# ikisinde de olmayan yüzey = CI-FAIL. Gerekçesiz-exempt YASAK (boş-string reddedilir).
EXEMPT: dict[str, str] = {
    "app/api/memory/signal_quality.py": "notes-teslimi yok (grep-doğrulandı #100383) — sinyal-metrik endpoint'i",
    "automation/notify-cron.sh": "notes-erişimi yok — alerts-tabanlı bildirim",
    "app/mcp/tools/handlers.py": "handle_memory_context notes teslim ETMİYOR (memories/discoveries) — MCP pilot-dışı",
    "app/core/action_review.py": "held ÜRETEN taraf (create_note status='held') — teslim-yüzeyi değil",
    "app/core/consciousness.py": "self-model için unread SAYACI okur (note teslim etmez) — bilinç katmanı",
    "automation/agent-watchdog.sh": "'held'=flock-lock bağlamı — notes ile ilgisiz",
    "app/api/memory/__init__.py": (
        "imza-FP (PR#302 default-deny): DEVICE_KEY_ROUTE_ALLOWLIST'teki "
        "'/api/v1/memory/notes' path-string-literal'i NOTES_ACCESS'i tetikliyor, "
        "_unread_pred TANIMI (çağrısı değil, notes.py çağırır) DELIVERY'yi — dosya "
        "kendisi teslim etmiyor, gerçek yüzey zaten notes.py (kayıtlı)"
    ),
}


def by_concern(concern: str) -> tuple[EntryPoint, ...]:
    """Concern-etiketini taşıyan kayıtlar — ∀-parametrize testlerin kaynağı."""
    return tuple(ep for ep in ENTRYPOINTS if concern in ep.concerns)


def by_category(category: Category) -> tuple[EntryPoint, ...]:
    return tuple(ep for ep in ENTRYPOINTS if ep.category is category)


def get(ep_id: str) -> EntryPoint:
    for ep in ENTRYPOINTS:
        if ep.id == ep_id:
            return ep
    raise KeyError(f"entry-point kayıtlı değil: {ep_id!r}")
