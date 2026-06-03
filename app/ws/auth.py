"""WebSocket authentication helper (güvenlik fix: auth'suz WS RCE/sızıntı).

WS endpoint'leri REST gibi Depends() kullanamaz -> handshake'te elle doğrula.
Token query-param ile gelir (`?token=<JWT>` veya `?api_key=<internal>`); dashboard
localStorage'daki JWT'yi geçirir. Doğrulama BAŞARISIZ -> accept ETME, 1008 ile kapat.
"""

from __future__ import annotations

from jose import JWTError

from app.auth.jwt_handler import decode_token
from app.core.config import get_settings

# RFC6455 1008 = policy violation (auth red).
WS_POLICY_VIOLATION = 1008


async def authenticate_ws(websocket, require_admin: bool = False) -> dict | None:
    """WS bağlantısını doğrula. Başarılı -> claims dict; başarısız -> socket'i
    1008 ile kapatır + None döner (çağıran hemen return etmeli, accept ETMEDEN).

    Kabul edilen kimlik: `?api_key=<internal_api_key>` (admin) VEYA `?token=<JWT>`.
    require_admin=True ise permissions=='admin' şart (terminal = REST shell ile aynı).
    """
    settings = get_settings()
    token = websocket.query_params.get("token")
    api_key = websocket.query_params.get("api_key")

    claims: dict | None = None
    internal = getattr(settings, "internal_api_key", "") or ""
    # internal_api_key BOŞ ise eşleşmeye izin verme (fail-closed; "" == "" tuzağı).
    if api_key and internal and api_key == internal:
        claims = {"sub": "internal", "permissions": "admin"}
    elif token:
        try:
            claims = decode_token(token, settings.jwt_secret)
        except JWTError:
            claims = None

    if claims is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return None
    if require_admin and claims.get("permissions") != "admin":
        await websocket.close(code=WS_POLICY_VIOLATION)
        return None
    return claims
