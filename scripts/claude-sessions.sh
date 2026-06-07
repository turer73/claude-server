#!/bin/bash
# Co-session CLI — canli Claude oturumlarini gor + oturumlar-arasi mesajlasma.
# Detay/mimari: scripts/hooks/cosession.py
set -u
PY=/opt/linux-ai-server/scripts/hooks/cosession.py

case "${1:-live}" in
  live|ls|list)
    python3 "$PY" list
    ;;
  msg|send)
    shift
    # msg [--urgent] [--to pts/N|all] "metin..."
    python3 "$PY" send "$@"
    ;;
  prune)
    python3 "$PY" prune && echo "olu oturumlar temizlendi"
    ;;
  *)
    cat <<'EOF'
Co-session CLI — birden fazla canli Claude oturumu koordinasyonu

  claude-sessions.sh live                       Canli oturumlar + bekleyen mesajlar
  claude-sessions.sh msg "metin"                Tum diger oturumlara PASIF mesaj
  claude-sessions.sh msg --to pts/1 "metin"     Belirli oturuma (tty) mesaj
  claude-sessions.sh msg --urgent "metin"       ACIL — alici Stop'ta otonom isler
  claude-sessions.sh prune                       Olu PID'leri temizle

DURUST SINIR: bosta bekleyen oturuma aninda push YOK. Teslim alicinin kendi
aktivite noktasinda olur (prompt gonderme / arac cagrisi / turn-bitisi).
EOF
    ;;
esac
