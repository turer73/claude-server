# Klipper-Memory Extraction — Phase 1 Plan

**Tarih:** 2026-05-28
**Durum:** Phase 1 (arastirma + plan) yaziliyor; Phase 2 (extraction) sonra.
**Sahibi:** klipper
**Bag:** [`app/api/memory.py`](../app/api/memory.py) (1343 satir, 20+ endpoint, 7 tablo)
        + [`docs/letta-multi-device-rfc.md`](letta-multi-device-rfc.md) referans tasarim.

---

## 1. Hedef

Klipper'da production'da calisan **multi-device memory layer**'i Letta-bagimsiz,
standalone bir OSS paket olarak cikarmak. Goose pattern'i tekrarlaniyor
([[goose-extension-phase3-done]]) — denenmis, basarili.

Kullanim hikayesi (paketi kuran biri):

> "FastAPI sunucumu yaziyorum, agent'larim icin tiered memory + multi-device
> sync istiyorum. `pip install agent-memory-server`, env iki var, mount,
> bitti."

Letta-compat **scope disi** (V1). RFC'yi upstream'e atmadigimiza karar verildi
([[letta-rfc commit 893a8be]]); Letta'nin tool family'leriyle 1:1 eşleşme yok.

---

## 2. Arastirma Bulgulari (Phase 1)

### 2.1 Mevcut envanter (klipper'da)

| Bilesen | Konum | Boyut | OSS'a giriyor mu? |
|---|---|---|---|
| HTTP router | `app/api/memory.py` | 1343 satir | **Evet (trim'lenmis)** |
| Schema | live SQLite (`data/claude_memory.db`) | 8 tablo | Evet, generic'lenmis |
| CLI helper | `scripts/claude-memory.sh` | ~200 satir | Hayir (V1) — adopter kendi yazar |
| Memory dosya katmani | `~/.claude/projects/.../memory/MEMORY.md` | tiered MD | Hayir — bu Claude Code spesifik |
| SessionStart hook | `scripts/hooks/session-start.sh` | shell | Hayir — adopter pattern olarak ornek alir |
| Onboarding endpoints | memory.py icinde TR prompt | ~150 satir | **Hayir** — klipper'a tightly coupled |

### 2.2 Sanitization audit (klipper-spesifik)

```text
DB_PATH                = "/opt/linux-ai-server/data/claude_memory.db"   # hardcoded
MEMORY_API_KEY         = read_env_var("MEMORY_API_KEY")                 # env var ismi
source_device default  = 'klipper'                                      # 1 user expectation
device_name default    = 'klipper'                                      # sessions/tasks
onboarding endpoints   = TR-language prompts mentioning "klipper" 12x
```

Hepsi config-injectable hale getirilebilir. Onboarding endpoint'leri
**OSS pakette olmayacak** — Klipper'a tightly coupled, generic anlami yok.

### 2.3 Letta tool family ile karsilastirma (V1 kapsam disi)

| Letta tool | Bizim API | Uyum |
|---|---|---|
| `core_memory_replace` | `PUT /memories/{id}` | semantically close, args farkli |
| `archival_memory_insert` | `POST /memories` (type=archival) | tier alan yok bizimkinde |
| `archival_memory_search` | `GET /search?q=` | FTS5 var bizde |

V1'de Letta-compatibility yapmiyoruz; V2'de adapter shim onerilebilir
(`letta-adapter` ayri paket).

---

## 3. Mimari Karar

### 3.1 Iki entrypoint, tek paket

```
agent-memory-server/
├── src/agent_memory/
│   ├── server.py        # FastAPI app, mount-able
│   ├── schema.sql       # SQLite DDL
│   ├── auth.py          # X-Memory-Key middleware
│   └── routes/
│       ├── memories.py
│       ├── devices.py
│       └── sessions.py
├── README.md
├── BACKEND_CONTRACT.md   # API surface as a contract (Goose ornegi gibi)
├── LICENSE (Apache-2.0)
└── pyproject.toml
```

Adopter:
```python
from fastapi import FastAPI
from agent_memory import create_router

app = FastAPI()
app.include_router(create_router(db_path="./memory.db", api_key=os.environ["MEM_KEY"]))
```

**Goose paketinden fark:** Goose MCP client'ti (HTTP cagiran). Bu paket
hem **backend** (FastAPI router) hem **DB schema** sunuyor. MCP client
katmani V2'de eklenebilir (`agent-memory-mcp` ayri paket).

### 3.2 MVP scope (V1)

| Endpoint | Method | V1'de? | Neden |
|---|---|---|---|
| `/memories` (list, create, get, update, soft-delete) | CRUD | **Evet** | Core deger |
| `/memories/{id}/read` (mark read, increments counter) | PUT | **Evet** | Recall mekani |
| `/devices` (list, register, ping) | CRUD | **Evet** | Multi-device farkimiz |
| `/device-projects` (attach, list per device) | C+L | **Evet** | RFC'deki Option B |
| `/sessions` (list per device, create) | C+L | **Evet** | Session log |
| `/search?q=` (FTS5 across memories+sessions) | GET | **Evet** | Pratik |
| `/discoveries` (bug/fix tracking) | CRUD | Hayir | Klipper-spesifik konvansiyon |
| `/notes` (agent-to-agent messages) | CRUD | Hayir | Klipper-spesifik akis |
| `/dashboard` (HTML rendered status) | GET | Hayir | UI; adopter kendi yapsin |
| `/onboarding/*` (klipper-spesifik TR prompts) | GET | Hayir | Sanitization risk |
| `/secrets` (admin) | * | Hayir | Auth ayri (JWT), scope disi |

Net: **6 endpoint set, 4 tablo** (memories, devices, device_projects,
sessions). Bu kabul edilebilir bir V1.

### 3.3 Bagimliliklar

- `fastapi>=0.115`, `pydantic>=2`, `aiosqlite` (Klipper'da sync sqlite3 var,
  paket icin async daha iyi)
- Optional: `sqlalchemy>=2` — V1'de raw SQL yeterli (Letta'nin yaptigi gibi
  ORM'ye gecmek scope creep)

### 3.4 Test stratejisi

- `pytest` + `pytest-asyncio` (Goose paketindeki gibi)
- httpx `TestClient` ile in-memory SQLite
- Hedef: 25-40 test, %85+ coverage

---

## 4. Acik kararlar (Phase 2 oncesi user gerek)

1. **Paket adi.** Aday'lar: `agent-memory-server`, `multi-device-memory`,
   `klipper-memory`, `polymem`. PyPI availability ayri kontrol gerekli.
2. **Repo adi.** Tek-isim politika (PyPI name = repo name) Goose'da uygulandi.
3. **Maintainer handle.** Goose'da `KlipperOS` kullandik. Devam mi?
4. **PostgreSQL backend.** V1 SQLite-only; PG'yi V2'ye ertelersek
   bu README'de net belirtilmeli.
5. **Search backend.** FTS5 (SQLite built-in) kullanacagiz. Embedding-based
   search (Letta'nin sundugu) **V1 disi**.

---

## 5. Sugarcoat etmedigim 4 risk

1. **1343-satir tek dosya** — modulerize ederken davranissal regresyon
   riski yuksek. Klipper'da production'da kosuyor; coverage %100 degil.
   Extraction sirasinda Klipper'i KIRMAMAK icin Goose unification pattern'ini
   uygulayacagim: generic paket once, Klipper sonra ayni paketten import
   (adapter route'lar gibi). Ama bu **memory.py'in tamamen yeniden yazimi**
   anlamina gelir — Goose security.py'den daha buyuk iz.

2. **Letta-compat'tan vazgectik ama "Letta-inspired" pozisyonlamasi
   tartismali.** Paketin README'sinde Letta'dan bahsetmek mi
   (tiered memory ilhami), bahsetmemek mi (kendi seyimiz)? Reusable
   markedingten kacinmazsak Letta'nin AI policy'sine bir nevi
   uyumsuzluk hissedilebilir (yes, doc'umuz onlardan ilham aliyor).
   Karar: README'de "MemGPT paper'a kredi" + "Letta-compat scope disi"
   net yazimi.

3. **Multi-device feature'i "ozellige sahip alani secen kullanici"
   icin avantaj** — ama tek kullaniciyla calisan adopter icin
   `source_device` field'i overhead. Default'a `'default'` koyup
   adopter farkinda olmadan single-tenant kullanabilir. Ama bu
   bizim ayird edici ozelligimiz, on plana cikarilmali.

4. **Schema migration sistemi yok.** Klipper'da migration `data/migrations/`
   altinda manuel SQL dosyalari. OSS paket icin **alembic** ya da en
   azindan `bootstrap_schema(db_path)` function'i gerek. Bu V1'de
   eklenmeli, sonra eklenirse adopter veri kaybi yasayabilir.

---

## 6. Phase 2 yol haritasi (taslak)

Goose pattern'i:

1. **Slice 1 — Generic paket iskeleti** (4-6h)
   - `extensions/agent-memory/` (monorepo icinde) yarat
   - schema.sql + memories router (CRUD) + auth middleware
   - 12-15 test
2. **Slice 2 — Devices + sessions** (3-4h)
   - devices, device_projects, sessions router
   - 10-12 test
3. **Slice 3 — Search + bootstrap** (2-3h)
   - FTS5 setup, `/search` endpoint
   - `bootstrap_schema()` migration helper
   - 5-8 test
4. **Klipper adaptation** (Goose unification pattern'i)
   - `app/api/memory.py`'i shim'le: generic paket'ten router import et,
     klipper-spesifik onboarding/discoveries/notes'u ek router olarak ekle
   - Mevcut endpoint'ler bozulmasin (regression test'leri yesil)

Phase 3 (publish): standalone repo + PyPI, Goose adimlarinin aynisi.

---

## 7. Tahmini effort

- Phase 1 (bu plan): ~2h (yapildi)
- Phase 2 Slice 1-3: 9-13h
- Phase 2 Klipper adaptation: 3-5h
- Phase 3 (publish): 1-2h (Goose'da denedik, prosedur belli)

**Toplam: 15-22 saat** — Goose'tan **belirgin** olarak daha buyuk
(Goose toplam ~10h idi). User'a karar verilmesi gereken nokta:
**bu kapsam dogru mu, daha kucuk MVP'i tercih eder misin?**
