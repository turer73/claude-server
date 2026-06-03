"""WebSocket authentication helper (güvenlik fix: auth'suz WS RCE/sızıntı).

WS endpoint'leri REST gibi Depends() kullanamaz -> handshake'te elle doğrula.
Kısa-ömürlü JWT query-param ile gelir (`?token=<JWT>`); dashboard localStorage'daki
JWT'yi geçirir. Doğrulama BAŞARISIZ -> accept ETME, 1008 ile kapat. (Kalıcı internal-
key ?api_key= ile URL'e KONMAZ — log-replay riski, Codex #27.)
"""

from __future__ import annotations

from jose import JWTError

from app.auth.jwt_handler import decode_token
from app.core.config import get_settings
from app.exceptions import AuthenticationError

# RFC6455 1008 = policy violation (auth red).
WS_POLICY_VIOLATION = 1008


async def authenticate_ws(websocket, require_admin: bool = False) -> dict | None:
    """WS bağlantısını doğrula. Başarılı -> claims dict; başarısız -> socket'i
    1008 ile kapatır + None döner (çağıran hemen return etmeli, accept ETMEDEN).

    Kabul edilen kimlik: `?token=<JWT>` (kısa-ömürlü). require_admin=True ise
    permissions=='admin' şart (terminal = REST shell ile aynı).
    """
    settings = get_settings()
    # GÜVENLIK (Codex #27): SADECE kısa-ömürlü JWT ?token= kabul. Kalıcı internal-key'i
    # ?api_key= ile URL'e KOYMA — access-log'a yazılırsa SÜRESİZ replay (kalıcı key);
    # JWT ttl=1h ile sınırlı. Residual JWT-in-URL (1h, internal LAN/TS) = accepted-risk;
    # follow-up: WS-subprotocol/header auth + access-log query-param redaction.
    token = websocket.query_params.get("token")

    claims: dict | None = None
    if token:
        try:
            claims = decode_token(token, settings.jwt_secret)
        except (AuthenticationError, JWTError):
            # decode_token jose hatalarini AuthenticationError'a sarar (JWTError DEGIL);
            # ikisini de yakala -> malformed/expired token temiz 1008-reddi (500/trace degil).
            claims = None

    if claims is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return None
    if require_admin and claims.get("permissions") != "admin":
        await websocket.close(code=WS_POLICY_VIOLATION)
        return None
    return claims
