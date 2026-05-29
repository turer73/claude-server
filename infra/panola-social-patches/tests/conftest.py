"""
pytest conftest — panola-social Faz 2 adapter testleri.

sys.path'e staging dizinini ekler; test_telegram.py'nin hardcoded
/opt/panola-social path'i yoksa fallback olarak burasi kullanilir.
"""

import sys
from pathlib import Path

# infra/panola-social-patches/ dizinini path'e ekle
# boylece 'adapter' paketi import edilebilir olur
_staging_root = Path(__file__).parent.parent.resolve()
if str(_staging_root) not in sys.path:
    sys.path.insert(0, str(_staging_root))
