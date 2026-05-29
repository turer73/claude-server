"""
panola-social Faz 2 multi-channel adapter paketi.

Adapter'lar modul yuklenirken kendilerini ADAPTER_REGISTRY'ye kaydeder.
engine.py tarafindan get_enabled_adapters(product) ile cagrilir.

Mevcut adapters:
- telegram (FULL implementation)
- whatsapp (SKELETON, dormant - _IMPLEMENTED=False)

Yeni adapter eklemek icin:
1. base.ChannelAdapter Protocol'u implement et
2. register_adapter(YourAdapter()) ile kaydet
3. Bu __init__.py'da import et (modul yuklenmesi icin)
"""

from .base import (
    ADAPTER_REGISTRY,
    ChannelAdapter,
    PostContent,
    PublishResult,
    get_enabled_adapters,
    register_adapter,
)

# Adapter'lari yukle (her biri register_adapter cagiriyor)
from . import telegram  # noqa: F401
from . import whatsapp  # noqa: F401

__all__ = [
    "ADAPTER_REGISTRY",
    "ChannelAdapter",
    "PostContent",
    "PublishResult",
    "get_enabled_adapters",
    "register_adapter",
]
