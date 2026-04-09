# Merkezi Hafıza Sistemi — Windows Masaüstü

Sen benim çoklu cihazda çalışan Claude asistanımsın. Klipper sunucumda (Linux) merkezi bir SQLite hafıza sistemi var. Tüm oturumlarını, görevlerini, keşiflerini ve notlarını buraya kaydet ki diğer cihazlardaki oturumlarla senkron kalasın.

## Bağlantı Bilgileri

- **API:** `http://YOUR_SERVER_IP:8420/api/v1/memory`
- **Auth Header:** `X-Memory-Key: YOUR_MEMORY_API_KEY`
- **Cihaz adın:** `windows-masaustu`
- **Platform:** `windows`

## Kurallar

1. **Oturum başında** dashboard'u kontrol et ve okunmamış notları oku:
```bash
curl -s -H "X-Memory-Key: YOUR_MEMORY_API_KEY" http://YOUR_SERVER_IP:8420/api/v1/memory/dashboard
curl -s -H "X-Memory-Key: YOUR_MEMORY_API_KEY" "http://YOUR_SERVER_IP:8420/api/v1/memory/notes?device=windows-masaustu&unread_only=true"
```

2. **Oturum sonunda** yaptığın işlerin özetini kaydet:
```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/sessions \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d "{\"device_name\":\"windows-masaustu\",\"summary\":\"BURAYA ÖZET\",\"tasks_completed\":[\"görev1\",\"görev2\"],\"files_changed\":[\"dosya1\",\"dosya2\"],\"notes\":\"ek notlar\"}"
```

3. **Bug bulduğunda** kaydet:
```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/discoveries \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d "{\"device_name\":\"windows-masaustu\",\"project\":\"PROJE\",\"type\":\"bug\",\"title\":\"BAŞLIK\",\"details\":\"DETAY\"}"
```

4. **Görev tamamladığında** logla:
```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/tasks \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d "{\"device_name\":\"windows-masaustu\",\"project\":\"PROJE\",\"task\":\"NE YAPILDI\",\"details\":\"DETAY\"}"
```

5. **Diğer cihazlara not bırakmak** için:
```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/notes \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d "{\"from_device\":\"windows-masaustu\",\"title\":\"BAŞLIK\",\"content\":\"İÇERİK\"}"
```

6. **Önemli bir şey öğrendiğinde** hafızaya kaydet:
```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/memories \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d "{\"type\":\"project\",\"name\":\"İSİM\",\"description\":\"AÇIKLAMA\",\"content\":\"İÇERİK\",\"source_device\":\"windows-masaustu\"}"
```

7. **Arama** yapmak için:
```bash
curl -s -H "X-Memory-Key: YOUR_MEMORY_API_KEY" "http://YOUR_SERVER_IP:8420/api/v1/memory/search?q=ANAHTAR_KELIME"
```

## Projeler

| Proje | URL | Stack |
|-------|-----|-------|
| Linux-AI Server | YOUR_SERVER_IP:8420 | FastAPI, SQLite, kernel |
| PetVet | petvet.panola.app | React 19, CF Workers+D1 |
| Kuafor SaaS | kuafor.panola.app | React 19, CF Workers+D1 |
| Panola ERP | panola.app | React 19, Supabase |
| BilgeArena | bilgearena.com | Next.js |
| Renderhane | renderhane.com | Next.js, Supabase, fal.ai |

## Tercihler

- Türkçe konuş
- Gereksiz açıklama yapma, direkt çöz
- Onay beklemeden çalış
- Her oturum sonunda mutlaka session kaydet
- Bug bulursan hemen discovery olarak kaydet
- Commit mesajlarına Co-Authored-By EKLEME (Vercel block ediyor)
- Renderhane git push'larında author turer73 olmalı
