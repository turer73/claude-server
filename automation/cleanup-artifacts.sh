#!/usr/bin/env bash
# Yaş-temelli artifact temizliği — timestamped/biriken log + test artifact'leri.
#
# logrotate bunlara UYMAZ: her dosya benzersiz-isimli (per-spawn / per-run timestamp),
# bir kez yazılır → rotation deseni yok. Doğru araç = find -mtime ile yaş-temelli sil.
#
# Kapsam (hepsi sınırsız-büyüyen artifact biriktiricisi):
#   data/hook-logs   — autonomous spawn per-event logları
#   logs/e2e         — e2e test artifact'leri (en büyük birikici)
#   logs/self-pentest, logs/nuclei — güvenlik tarama çıktıları
#
# RETAIN_DAYS'ten yeni dosyalar KORUNUR (son aktivite kaybolmaz). İdempotent.
set -euo pipefail

ROOT=/opt/linux-ai-server
RETAIN_DAYS="${ARTIFACT_RETAIN_DAYS:-30}"

deleted_total=0
for d in data/hook-logs logs/e2e logs/self-pentest logs/nuclei; do
    dir="$ROOT/$d"
    [ -d "$dir" ] || continue
    n=$(find "$dir" -type f -mtime "+${RETAIN_DAYS}" 2>/dev/null | wc -l)
    if [ "$n" -gt 0 ]; then
        find "$dir" -type f -mtime "+${RETAIN_DAYS}" -delete 2>/dev/null || true
        deleted_total=$((deleted_total + n))
    fi
    # Boşalan timestamped subdir'leri buda (üst dizini SİLME: -mindepth 1)
    find "$dir" -mindepth 1 -type d -empty -delete 2>/dev/null || true
done

# OUTCOME marker — klipper-cron-wrap.sh log + alert deseni
echo "OUTCOME: artifact-cleanup OK — ${deleted_total} dosya silindi (retain=${RETAIN_DAYS}d)"
