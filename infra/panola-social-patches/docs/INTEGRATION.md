# Adapter Entegrasyon — panola-social Faz 2

**Hedef:** /opt/panola-social/adapter/ olarak deploy + engine.py wiring + DB migration.

---

## Dosya Yerlesimi (VPS)

```
/opt/panola-social/
├── adapter/
│   ├── __init__.py
│   ├── base.py
│   ├── telegram.py        (FULL)
│   └── whatsapp.py        (SKELETON, dormant)
├── sql/migrations/
│   └── 001_channel_configs.sql   (yeni)
└── tests/
    └── test_telegram.py
```

---

## Migration

```bash
cd /opt/panola-social
sqlite3 data/social.db < adapter_sql/001_channel_configs.sql

# Dogrula
sqlite3 data/social.db "SELECT * FROM channel_configs;"
```

---

## engine.py Wiring

Mevcut `generate_and_publish()` veya benzeri fonksiyona ek (Instagram zaten var, sadece append):

```python
# engine.py basina:
from adapter import get_enabled_adapters, PostContent

# generate_and_publish() icinde — Instagram post sonrasi:
def _publish_to_channels(product: str, post_id: int, post_data: dict) -> list[dict]:
    """
    Tum aktif kanallara yayinla. Hatalari yutar, devam eder.
    Sonuc channel_publishes tablosuna kaydet.
    """
    import sqlite3
    db_path = os.environ.get("PANOLA_SOCIAL_DB", "/opt/panola-social/data/social.db")
    results = []

    content = PostContent(
        text=post_data.get("caption", post_data.get("text", "")),
        image_urls=post_data.get("image_urls"),
        link_url=post_data.get("cta_url"),
        hashtags=post_data.get("hashtags"),
    )

    adapters = get_enabled_adapters(product)
    for ad in adapters:
        try:
            res = ad.publish(product, content)
        except Exception as e:
            logger.exception("adapter %s publish exception", ad.name)
            res = PublishResult(channel=ad.name, success=False, error=str(e))

        results.append({"channel": ad.name, "success": res.success, "error": res.error})

        # DB kaydet
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO channel_publishes(post_id, product, channel, success, "
                    "external_id, external_url, error, raw_response) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (post_id, product, res.channel, 1 if res.success else 0,
                     res.external_id, res.external_url, res.error,
                     json.dumps(res.raw_response) if res.raw_response else None),
                )
        except Exception:
            logger.exception("channel_publishes insert fail")

    return results
```

---

## Env Vars (VPS systemd EnvironmentFile)

`/etc/default/panola-social` veya servis env'i:

```bash
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_PARSE_MODE=HTML            # opsiyonel
PANOLA_SOCIAL_DB=/opt/panola-social/data/social.db

# WhatsApp DORMANT - ileri implementation sonrasi:
# WHATSAPP_TOKEN=
# WHATSAPP_PHONE_NUMBER_ID=
# WHATSAPP_API_VERSION=v21.0
```

---

## Telegram Bot Setup

Surer veya VPS, herhangi bir terminalde:

1. Telegram'da [@BotFather](https://t.me/BotFather) -> `/newbot` -> bot adi + username
2. Token al, `/etc/default/panola-social` icine ekle
3. Channel olustur (Public veya Private):
   - **Public:** username belirle (örn: `@panola_kuafor`), config_json'a `{"chat_id": "@panola_kuafor"}` yaz
   - **Private:** bot'u admin yap, https://api.telegram.org/bot<TOKEN>/getUpdates ile chat_id numeric al
4. Bot'u channel'a admin yap, "post messages" yetkisi ver
5. DB'de enabled=1 yap:
   ```sql
   UPDATE channel_configs
   SET enabled=1, config_json='{"chat_id":"@panola_kuafor"}'
   WHERE product='kuafor' AND channel='telegram';
   ```

---

## Smoke Test (VPS)

```bash
cd /opt/panola-social
source venv/bin/activate
pip install requests pytest  # gerekiyorsa

# Test calistir
pytest tests/test_telegram.py -v

# Live test (asagidaki Python tek satir)
python -c "
from adapter import get_enabled_adapters, PostContent
adapters = get_enabled_adapters('kuafor')
print('Enabled adapters:', [a.name for a in adapters])
for a in adapters:
    print(a.name, a.health_check())
    if a.name == 'telegram':
        r = a.publish('kuafor', PostContent(text='Telegram adapter smoke test - panola-social Faz 2'))
        print('publish:', r)
"
```

---

## Health Endpoint Ekleme (Faz 1 #99586 webhook_server.py)

Mevcut `/api/health` JSON'una channel block:

```python
@app.get("/api/health")
def health():
    base = {...}  # mevcut renderhane_balance, ig_token_ok, vb.
    try:
        from adapter import ADAPTER_REGISTRY
        base["channels"] = {
            name: ad.health_check() for name, ad in ADAPTER_REGISTRY.items()
        }
    except Exception as e:
        base["channels_error"] = str(e)
    return base
```

---

## Dogrulama Listesi

- [ ] adapter/ dizini deploy edildi
- [ ] sql/migrations/001 uygulandi (sqlite3 dump grep channel_configs)
- [ ] TELEGRAM_BOT_TOKEN env set
- [ ] Bot @BotFather'dan olusturuldu
- [ ] Test channel @panola_test admin'i bot
- [ ] channel_configs UPDATE: chat_id + enabled=1
- [ ] pytest tests/test_telegram.py PASS
- [ ] Live smoke: kuafor channel'a test mesaj
- [ ] /api/health channels block donuyor
- [ ] engine.py wiring: 1 post -> Instagram + Telegram parallel
- [ ] channel_publishes tablosunda kayit
- [ ] WhatsApp dormant olarak duruyor (health status="skeleton")

---

## Olasi Sapma Riskleri

1. **Python version uyumu:** VPS Python 3.x — telegram.py from __future__ + Protocol kullanir, 3.8+ gerekli
2. **requests dependency:** muhtemelen zaten kurulu (panola-social Anthropic SDK kullaniyor); yoksa pip install
3. **DB lock:** SQLite WAL mode degilse paralel publish lock sebep olabilir; calismayan durumda PRAGMA journal_mode=WAL
4. **Telegram rate limit:** Bot 30 msg/sec, channel 20 msg/min — mevcut Instagram frekansinda sorun yok
5. **Markdown escape:** parse_mode=MarkdownV2 kullanilirsa _ * [ ] gibi karakterler escape gerekli; default HTML daha guvenli
