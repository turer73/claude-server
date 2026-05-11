# Cok-Ajan Gelistirme Ortami

## Genel Yapi

Iki makine, iki ajan, bir hafiza sistemi:

| Makine    | Model | Rol              | Konum                        |
|-----------|-------|------------------|------------------------------|
| Klipper   | Opus  | Kontrolcu/Beyin  | 100.113.153.62 (Linux)       |
| Windows   | Sonnet| Uretici/Eller    | LAPTOP / DESKTOP (Windows)   |

Haberlesme kanali: `http://100.113.153.62:8420/api/v1/memory/notes`  
Auth: `X-Memory-Key: $env:KLIPPER_MEMORY_KEY` (Windows User env var)

---

## Dosyalar

```
F:\projelerim\scripts\
  oturum-baslat.ps1         -- Masaustu kisayolunun actigi terminal
  klipper-isbirligi.ps1     -- Tum kc/kclaude/opus-kontrol fonksiyonlari
  prompt-opus-kontrolcu.md  -- Opus'un sistem promptu (planlama + review kurallari)
  prompt-sonnet-uretici.md  -- Sonnet'in sistem promptu (uygulama kurallari)
  sistem-tanimi.md          -- Bu dosya
```

---

## Is Akisi

```
1. Kullanici Windows terminalinde:
      kclaude "bilge-arena'da X ekle" -opus

2. Klipper Opus:
   - Kodu okur, analiz eder
   - Gorev paketini JSON olarak olusturur
   - memory/notes'a "Gorev Paketi: BA-20260511-01" baslikli not atar

3. Windows Sonnet (manuel ya da gorev-bekle ile):
   - Paketi okur
   - Uygular: kod yazar, test calistirir, commit atar
   - sonuc-gonder -gorevId "BA-20260511-01" -otomatikKontrol

4. Klipper Opus (otomatik tetiklenir):
   - git diff okur
   - testleri dogrular
   - Karar verir: onaylandi | revizyon_gerekli | red
   - memory/notes'a "Kontrol Karari: BA-20260511-01" atar
   - Onaylandiysa tasks_log'a kaydeder
```

---

## Gorev Paketi Formati (Opus -> Sonnet)

```json
{
  "tip": "gorev_paketi",
  "gorev_id": "PROJE-YYYYMMDD-NN",
  "gonderen": "klipper-opus",
  "alici": "surer-sonnet",
  "proje": "bilge-arena",
  "cwd": "/opt/linux-ai-server",
  "hedef": "tek cumlede ne yapilacak",
  "adimlar": ["1. ...", "2. ..."],
  "kisitlar": ["commit mesajina Co-Authored-By ekleme"],
  "basari_kriteri": "pytest geciyor, X endpoint calisiyor",
  "oncelik": "yuksek|normal|dusuk"
}
```

## Kontrol Karari Formati (Opus)

```json
{
  "tip": "kontrol_karari",
  "gorev_id": "BA-20260511-01",
  "gonderen": "klipper-opus",
  "karar": "onaylandi|revizyon_gerekli|red",
  "incelenen": ["git diff OK", "pytest 47 passed"],
  "sorunlar": [],
  "revizyon_talimat": "revizyon_gerekli ise ne yapilmali",
  "notlar": "kisa ozet"
}
```

---

## Windows Terminal Komutlari

### Yerel
| Komut                          | Ne yapar                            |
|-------------------------------|--------------------------------------|
| `klipper`                     | SSH ile Klipper'a baglan             |
| `proje <ad>`                  | F:\projelerim\<ad> dizinine gec      |
| `mem <kelime>`                | Merkezi hafizada ara                 |
| `bug-logla <proje> <baslik>`  | Bug kaydet                           |
| `oturum-kaydet <ozet>`        | Oturumu hafizaya yaz                 |
| `vps <komut>`                 | VPS'te komut calistir                |

### Klipper Isbirligi
| Komut                          | Ne yapar                            |
|-------------------------------|--------------------------------------|
| `kc <komut>`                  | Klipper'da shell komutu              |
| `kdurum`                      | CPU/RAM/disk durumu                  |
| `kservis [restart\|log]`      | Servis durumu/yeniden baslatma       |
| `klog [-canli]`               | Son loglar                           |
| `ktest [filtre]`              | pytest calistir                      |
| `kgit <komut>`                | Klipper'da git komutu                |
| `kdosya-al <yol>`             | Klipper'dan dosya oku                |
| `kizle`                       | Canli metrik izle                    |

### Cok-Ajan
| Komut                                    | Ne yapar                          |
|-----------------------------------------|-----------------------------------|
| `kclaude <prompt>`                      | Klipper'da Sonnet gorevi baslat   |
| `kclaude <prompt> -opus`               | Klipper'da Opus/Kontrolcu baslat  |
| `kclaude-izle <id>`                    | Gorev ciktisini takip et          |
| `gorev-bekle`                           | Opus'tan gorev paketi dinle       |
| `sonuc-gonder -gorevId X -durum Y`     | Sonucu Opus'a raporla             |
| `sonuc-gonder ... -otomatikKontrol`    | Rapor + aninda Opus kontrolu      |
| `opus-kontrol -gorevId X`              | Opus'a manuel kontrol gorevi ver  |

---

## Hafiza Sistemi

- **Endpoint:** `http://100.113.153.62:8420/api/v1/memory`
- **Auth header:** `X-Memory-Key: $env:KLIPPER_MEMORY_KEY`
- **Cihaz adi (Windows):** `surer`
- **Cihaz adi (Klipper):** `klipper`
- **Tablolar:** memories, sessions, tasks_log, discoveries, notes

Gorev koordinasyonu `notes` tablosu uzerinden yurutuluyor:
- Gorev Paketi: `title = "Gorev Paketi: <id>"`
- Gorev Sonucu: `title = "Gorev Sonucu: <id>"`
- Kontrol Karari: `title = "Kontrol Karari: <id>"`

---

## Cok-Ajan Sistemi: klipper-opus Rolu

Bu Linux makinesi (klipper, 100.113.153.62) **kontrolcu/koordinator ajan** rolundedir.
Uygulayici ajan: surer (Sonnet, Windows). Aktif altyapi:
- `SessionStart` hook: oturum basinda unread notlari + acik bug'lari + son test sonuclarini yukler
- `UserPromptSubmit` hook: yeni note geldiginde otomatik mesaj basina inject eder (live arrival)
- `/api/v1/memory/notes` async kanal, `handoff.py send/wait/check` helper

### Is Akisi
1. **Plan + spec**: Hedefi ana hatlariyla yazip Sonnet icin `gorev_paketi` JSON'una donustur. `gorev_id` formati `PROJE-YYYYMMDD-NN` (sortable, [[feedback_gorev_id_convention.md]]).
2. **Send + paralel**: `handoff.py send` ile notu birak. `wait` blocking BEKLEME — Sonnet uzun iste calisirken (>5dk) baska is yap:
   - Ayni projede non-overlapping baska kol (file_scope cakismayan)
   - Baska projede plan/QC/memory triage
   - Acik bug review
   - UserPromptSubmit hook done notu geldiginde mesaj basina inject eder → o anki paralel isi sagla, QC moduna gec
3. **QC pipeline** (done notu sonrasi):
   - `git fetch && git pull origin main`
   - Pre-submit gate: `tsc --noEmit && vitest run && next build` (proje tipine gore — [[feedback_ci_skip_local_test_only.md]])
   - Live smoke test: canli endpoint + DB durumu ([[feedback_smoke_test_against_live.md]])
   - CI run watch: `gh run watch <id> --exit-status`
4. **Karar**: `Kontrol Karari: <gorev_id>` notu ile `onaylandi | revizyon_gerekli | red`.
   - Onaylandi → `tasks_log` kayit + memory state guncelle, sapmalar varsa discovery olarak `feedback_*.md` / `reference_*.md` ekle
   - Klipper QC direkt fix pattern: ufak hatalar (1-2 satir migration/config tashih) icin Sonnet'e round-trip yerine Klipper kendisi fix + push, kontrol_karari'na "after-fix" notu ekle (ornek: Faz 2.6 `4c55eec` IMMUTABLE migration fix)

### Kurallar
- **Headless yasak**: Klipper'da `claude -p` calistirma — API ucretine doner, Max plan kapsami disi ([[feedback_opus_sonnet_division.md]])
- **Interactive Opus**: Bu oturum (`claude --model opus`) Max plan'a sayar
- **Fire-and-forget refleksi**: Sonnet'i bekleyen Klipper = atil Klipper. Hook altyapisi notification getiriyor, ben blocking olmayi birakmaliyim
- **Rol sinir**: Klipper koordinator + QC + entegre, uretici degil. Sonnet'in kapsami icindeki kod yazimini ustlenme ([[feedback_role_split_klipper_sonnet.md]])

---

## Cok-Ajan Sistemi: surer-sonnet Rolu

Bu Windows makinesi (surer) **uygulayici ajan** rolundedir.
Kontrolcu ajan: klipper (Opus, 100.113.153.62).
Haberlesme kanali: `POST /api/v1/memory/notes` (X-Memory-Key header).

### Is Akisi
1. Oturum basinda `inbox` calistir — "Gorev Paketi:*" baslikli notlari kontrol et
2. Gorevi al: `content` alanindaki JSON `gorev_paketi` tipinde olmali
3. Uygula: `cwd` ye gec, `adimlar` i sirali yap, `basari_kriteri` ni dogrula
4. Rapor at — gorev biter bitmez `Gorev Sonucu: <gorev_id>` baslikli not:
   - `commit` hash + test sayisi (`vitest`/`pytest`) + `tsc` + `build` durumu
   - Spec disi kararlari "sapma" basligi altinda dokumante et (Next.js API quirk, JSDOM bypass, test infra fix vb.)
   - SSH direct commit pattern destekleniyor (klipper repo'ya dogrudan push, Faz 1.3.c+ pattern)

### Kurallar
- **Spec-uyumu**: `kisitlar` listesindeki kurallari ihlal etme (Co-Author yasagi Vercel-deploy projelerde, scoped `git add` — [[feedback_spec_git_add_selective.md]])
- **Pre-submit gate**: tsc + vitest + build uclusunu local'de calistir (local pass != CI pass — [[feedback_ci_skip_local_test_only.md]])
- **Migration kurallari**: Partial index predicate'inde `now()`/`current_*` YASAK ([[feedback_migration_immutable_predicate.md]])
- **gorev_id**: Klipper'in atadigi formati koru (`PROJE-YYYYMMDD-NN`), donus notuna ayni id ile bagla
