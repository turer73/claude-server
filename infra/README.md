# infra/

Klipper sunucusunda calisan docker-compose stack'leri. `/opt/{n8n,monitoring,tools,qdrant}` symlink'lerle bu dizinleri gosterir.

## Stack'ler

| Dizin | Compose path | Container'lar | Erisim |
|---|---|---|---|
| `n8n/` | `/opt/n8n/` symlink | n8n | Tailscale + localhost (5678) |
| `monitoring/` | `/opt/monitoring/` symlink | prometheus, grafana, node-exporter, cadvisor | grafana Tailscale+localhost (3030); digerleri localhost-only |
| `tools/` | `/opt/tools/` symlink | dozzle, uptime-kuma, stirling-pdf | Tailscale + localhost |
| `qdrant/` | `/opt/qdrant/` symlink | qdrant | localhost-only (6333/6334) |
| `legacy/` | — | (calismayan) | `/opt/linux-ai-server/docker-compose*.yml` eski kopyalari, referans amacli |

## Kullanim

```bash
cd /opt/n8n && docker compose up -d            # veya
docker compose -f /opt/n8n/docker-compose.yml up -d
```

Symlink oldugu icin `/opt/<stack>/` ve `/opt/linux-ai-server/infra/<stack>/` ayni dosyaya isaret eder. Repo'da degisiklik = canli stack'te degisiklik (recreate gerekir).

## Audit notlari (2026-05-13)

- Tum container portlari `0.0.0.0`'dan `127.0.0.1` veya `100.84.251.49`'a (Tailscale) tasindi
- UFW host servisleri (SSH, linux-ai-server, ollama) icin LAN+Tailscale'e acik, internet'e kapali
- Docker iptables UFW'yi bypass eder; container kisitlamasi compose port binding ile yapilir (bu dizinler)
