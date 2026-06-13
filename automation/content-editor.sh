#!/bin/bash
# İçerik Editörü — scripts/content-editor.py sarmalayıcısı (multi-uzman: editör).
# ON-DEMAND: SEO blog makalesi üretir → dosya-blog'lu site (renderhane) için PR açar,
# diğerleri için taslağı ortak-hafızaya yazar. Auto-publish YOK (PR + insan review).
#
# Kullanım:
#   automation/content-editor.sh renderhane "AI ile ürün fotoğrafı çekimi"
#   automation/content-editor.sh renderhane --suggest      # konu önerileri (üretmez)
#
# klipper-cron-wrap.sh ile çağrılırsa OUTCOME marker'ı cron_outcomes'a düşer.
# Bilinçli olarak cron'a BAĞLANMADI: içerik üretimi deliberate olmalı (oto-spam değil);
# istenirse haftalık tek-konu cron'u sonra eklenir.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/content-editor.py "$@"
