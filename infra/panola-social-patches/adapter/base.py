"""
Channel Adapter Protocol — panola-social Faz 2

Tum kanal adapter'lari bu protokole uymali. Yeni kanal eklemek icin:
1. Bu Protocol'u implement et
2. ADAPTER_REGISTRY'ye kaydet
3. engine.py.generate_and_publish() icinde channel iter ile cagir
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable
from datetime import datetime


@dataclass
class PublishResult:
    """Tek bir kanal/post sonucu."""
    channel: str                  # 'telegram','whatsapp','gbp','linkedin','tiktok'
    success: bool
    external_id: Optional[str] = None      # platform-native ID (telegram message_id, vb.)
    external_url: Optional[str] = None     # public URL varsa
    error: Optional[str] = None
    posted_at: Optional[datetime] = None
    raw_response: Optional[dict[str, Any]] = None


@dataclass
class PostContent:
    """Engine'in adapter'a verdigi paketlenmis icerik."""
    text: str                              # ana metin (TR, ASCII-safe TR karakterleri)
    image_urls: list[str] | None = None    # R2/Renderhane output URL'leri
    video_url: Optional[str] = None        # tek video varsa
    link_url: Optional[str] = None         # CTA / kaynak link
    hashtags: list[str] | None = None      # ['kuafor', 'salon']
    metadata: dict[str, Any] | None = None # platform-specific override (ornek: telegram parse_mode)


@runtime_checkable
class ChannelAdapter(Protocol):
    """Tum kanal adapter'lari bu protokolu implement etmeli."""

    name: str
    """Kanal adi, lowercase: 'telegram','whatsapp', ..."""

    enabled: bool
    """Adapter aktif mi? Konfigurasyondan okunur."""

    def is_configured(self) -> bool:
        """Tum gerekli env/db config var mi? False ise adapter skip."""
        ...

    def publish(self, product: str, content: PostContent) -> PublishResult:
        """
        Tek post yayini. Sync (mevcut hybrid_gen.py paterni).
        product: 'kuafor','petvet','panola_erp'
        Hata durumunda success=False + error, exception RAISE ETME (engine continue eder).
        """
        ...

    def health_check(self) -> dict[str, Any]:
        """
        Adapter durumu — Uptime Kuma / dashboard icin.
        Return: {'status':'ok|degraded|fail', 'last_post':timestamp, 'rate_limit':...}
        """
        ...


# Registry — engine.py bunu okuyup tum adapter'lari cagirir
ADAPTER_REGISTRY: dict[str, ChannelAdapter] = {}


def register_adapter(adapter: ChannelAdapter) -> None:
    """Adapter'i registry'ye ekle. main.py'da modul yuklenirken cagrilir."""
    ADAPTER_REGISTRY[adapter.name] = adapter


def get_enabled_adapters(product: str) -> list[ChannelAdapter]:
    """
    Bir urun icin aktif adapter'lari getir.
    DB'deki channel_configs tablosundan product+channel+enabled okur.
    Adapter is_configured() False ise dahil etme.
    """
    import sqlite3, os
    db_path = os.environ.get("PANOLA_SOCIAL_DB", "/opt/panola-social/data/social.db")
    out: list[ChannelAdapter] = []
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "SELECT channel FROM channel_configs WHERE product=? AND enabled=1",
                (product,),
            )
            for (ch,) in cur.fetchall():
                ad = ADAPTER_REGISTRY.get(ch)
                if ad and ad.enabled and ad.is_configured():
                    out.append(ad)
    except Exception:
        # DB yok veya tablo yok -> sessiz sifir donus, engine devam eder
        pass
    return out
