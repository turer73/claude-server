"""
WhatsApp Business Cloud API adapter — panola-social Faz 2 (SKELETON / DORMANT)

DURUM: ALT YAPI HAZIR, AKTIF DEGIL. is_configured() False doner, publish() NotImplementedError.

Aktiflestirmek icin:
1. Meta Developer hesap (3d-labx@3d-labx.com) -> WhatsApp Business App
2. Test phone number al, marketing template Meta'ya submit (2-3 gun bekleme)
3. Template approval sonrasi WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID env ekle
4. _IMPLEMENTED = True yap, publish/send_template_message metotlarini doldur
5. Tests yaz, smoke + integration

Konfigurasyon (gelecek):
  WHATSAPP_TOKEN=...
  WHATSAPP_PHONE_NUMBER_ID=...
  WHATSAPP_API_VERSION=v21.0          # opsiyonel default

DB (social.db channel_configs gelecek):
  product='kuafor', channel='whatsapp', enabled=1,
  config_json='{"approved_templates": ["randevu_hatirlatma","haftalik_ipucu"]}'

DB (social.db whatsapp_contacts):
  CREATE TABLE whatsapp_contacts (
    id INTEGER PRIMARY KEY,
    product TEXT NOT NULL,
    phone TEXT NOT NULL,          -- E.164 format +905551112233
    opt_in INTEGER DEFAULT 1,
    opt_out_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(product, phone)
  );

Maliyet (Meta WhatsApp Business 2026):
  - 1000 service conversation/ay UCRETSIZ
  - Marketing conversation TR: ~$0.005-0.025 per conv
  - Template approval 2-3 gun (marketing template)
  - 24 saat session window kurali (musteri baslatmazsa template gerekli)

KVKK/GDPR:
  - Opt-in zorunlu (musteri acik onay vermeli)
  - Opt-out mekanizmasi sart (STOP keyword + manuel)
  - Veri 90 gun saklama tavsiyesi
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .base import ChannelAdapter, PostContent, PublishResult, register_adapter

logger = logging.getLogger(__name__)


_IMPLEMENTED = False  # Activation flag. Implementation tamamlaninca True yap.


class WhatsAppAdapter:
    """
    WhatsApp Business Cloud API adapter — SKELETON.

    is_configured() False oldugu surece engine bunu skip eder.
    Implementation hazir oldugunda _IMPLEMENTED = True yap + metotlari doldur.
    """

    name = "whatsapp"

    def __init__(self) -> None:
        self._token = os.environ.get("WHATSAPP_TOKEN", "").strip()
        self._phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        self._api_version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
        # enabled = ALT YAPI olarak HER ZAMAN False (dormant)
        # _IMPLEMENTED ve env tamamlanana kadar
        self.enabled = _IMPLEMENTED and bool(self._token) and bool(self._phone_id)

    # ---- ChannelAdapter protocol ----

    def is_configured(self) -> bool:
        # Skeleton: her zaman False. engine bunu skip eder.
        return self.enabled

    def publish(self, product: str, content: PostContent) -> PublishResult:
        """
        IMPLEMENTATION TODO:
        1. channel_configs.config_json.approved_templates kontrol et
        2. whatsapp_contacts'tan opt_in=1 numaralari cek
        3. Her numaraya template message gonder (Meta Cloud API):
           POST https://graph.facebook.com/{api_version}/{phone_id}/messages
           Bearer {token}
           body: {messaging_product:"whatsapp", to, type:"template", template:{name, language, components}}
        4. 24 saat session window check (musteri son mesaj > 24 saat ise template zorunlu)
        5. Rate limit: 1000/ay free tier takibi
        6. Hata kategorize: 401=token, 400=template, 429=rate limit, 500+=meta
        """
        return PublishResult(
            channel=self.name,
            success=False,
            error="whatsapp_adapter_not_implemented (_IMPLEMENTED=False, skeleton mode)",
        )

    def health_check(self) -> dict[str, Any]:
        return {
            "status": "skeleton",
            "implemented": _IMPLEMENTED,
            "has_token": bool(self._token),
            "has_phone_id": bool(self._phone_id),
            "note": "Adapter dormant. Meta Developer hesap + template approval bekliyor.",
        }

    # ---- TODO: implementation stubs ----

    def send_template_message(
        self,
        to_phone: str,
        template_name: str,
        language: str = "tr",
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        IMPLEMENTATION TODO. Tek numaraya marketing template gonderme.

        Args:
            to_phone: E.164 format (+905551112233)
            template_name: Meta'da onaylanmis template name
            language: 'tr' veya 'en_US'
            components: header/body/button parametreleri

        Returns:
            {message_id, status, ...} veya {error, code}
        """
        raise NotImplementedError("send_template_message: skeleton, implementation bekleniyor")

    def broadcast(
        self,
        product: str,
        template_name: str,
        components: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        IMPLEMENTATION TODO. Bir urun icin opt-in numaralarina broadcast.

        Args:
            product: 'kuafor','petvet',...
            template_name: Meta'da onaylanmis template
            components: dinamik degerler (musteri adi, randevu zamani)

        Returns:
            Numara basina sonuc listesi
        """
        raise NotImplementedError("broadcast: skeleton, implementation bekleniyor")

    def add_contact(self, product: str, phone: str) -> bool:
        """
        IMPLEMENTATION TODO. opt-in onaylanan musteriyi DB'ye ekle.
        Existing opt_out_at varsa NULL'a cevir (rejoin).
        """
        raise NotImplementedError("add_contact: skeleton")

    def opt_out(self, phone: str) -> bool:
        """
        IMPLEMENTATION TODO. STOP keyword'le veya manuel opt-out.
        opt_in=0, opt_out_at=now() update.
        """
        raise NotImplementedError("opt_out: skeleton")


# Modul yuklenirken kayit. enabled=False oldugundan get_enabled_adapters() doner.
register_adapter(WhatsAppAdapter())
