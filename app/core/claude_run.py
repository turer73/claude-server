"""/api/v1/claude/run yanıtından GÜVENLİ sonuç çıkarma — tek kaynak.

Neden ayrı modül: 7 otomasyon script'i (auto-investigate, agent-health-report,
system-state, data-analyst, ad-advisor, content-editor, adsense-readiness) bu
yanıtı tüketiyor ve hepsi aynı hatayı yapıyordu — `result`'ı doğrudan okuyup
endpoint'in döndürdüğü `ok` bayrağını görmezden gelmek.

Kırılma senaryosu (canlı, 2026-07-24..31): abonelik haftalık limite takılınca
claude CLI şunu döner —

    {"type":"result","subtype":"success","is_error":true,"api_error_status":429,
     "result":"You've hit your weekly limit · resets ..."}

`subtype` "success" DİYE YALAN SÖYLER; güvenilir tek sinyal `is_error`. Üstelik
`result` BOŞ DEĞİL, dolayısıyla script'lerdeki mevcut "boş inceleme" koruması
sorunsuz geçiyor ve rate-limit hata metni gerçek teşhis/rapor diye DB'ye
yazılıyordu (17 zehirli kayıt).

Sözleşme — FAIL-CLOSED: `ok` yoksa veya falsy ise "" döner. `ok` alanı yalnız
_parse_claude_json başarı yolunda üretilir; endpoint'in diğer çıkışları
({"error": ...}) zaten `result` içermez, yani bu davranış hiçbir çalışan yolu
bozmaz — sadece daha önce sessizce geçen hata metinlerini eler.

Metin-tabanlı sentinel taraması BİLEREK YOK: "weekly limit" gibi ifadeler meşru
bir raporun içinde de geçebilir (mentions ≠ is-an-error). Yapısal `ok` sinyali
otoriter kaynaktır.
"""

from __future__ import annotations

from typing import Any


def claude_result(payload: Any) -> str:
    """Yanıttan kullanılabilir sonuç metnini döndür; hata/şüphe halinde "".

    Çağıranlar zaten `if not finding: ...` şeklinde boş-kontrolü yapıyor, bu
    yüzden "" dönmek onları mevcut güvenli yola sokar (ekstra dallanma gerekmez).
    """
    if not isinstance(payload, dict):
        return ""
    if not payload.get("ok"):
        return ""
    result = payload.get("result")
    if not isinstance(result, str):
        return ""
    return result.strip()
