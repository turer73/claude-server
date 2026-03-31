#!/bin/bash
# WiFi Watchdog — IP veya gateway erişimi kaybolursa WiFi yeniden bağlanır
# Systemd timer ile her 2 dakikada çalışır

INTERFACE="wlp3s0"
GATEWAY="192.168.1.1"
CONNECTION="Turer_plus_5G"
LOG_TAG="wifi-watchdog"

# Gateway'e ping at (2 deneme, 3sn timeout)
if ping -c 2 -W 3 -I "$INTERFACE" "$GATEWAY" &>/dev/null; then
    exit 0
fi

# İlk başarısızlıkta 5sn bekle, tekrar dene (geçici kesintileri atla)
sleep 5
if ping -c 2 -W 3 -I "$INTERFACE" "$GATEWAY" &>/dev/null; then
    exit 0
fi

# Gateway'e ulaşılamıyor — yeniden bağlan
logger -t "$LOG_TAG" "Gateway $GATEWAY unreachable on $INTERFACE. Reconnecting WiFi..."

# Önce DHCP yenilemeyi dene
nmcli device reapply "$INTERFACE" 2>/dev/null
sleep 5

if ping -c 2 -W 3 -I "$INTERFACE" "$GATEWAY" &>/dev/null; then
    logger -t "$LOG_TAG" "DHCP reapply fixed the connection."
    exit 0
fi

# DHCP yetmedi — bağlantıyı tamamen yeniden kur
nmcli connection down "$CONNECTION" 2>/dev/null
sleep 2
nmcli connection up "$CONNECTION" 2>/dev/null
sleep 5

if ping -c 2 -W 3 -I "$INTERFACE" "$GATEWAY" &>/dev/null; then
    logger -t "$LOG_TAG" "WiFi reconnected successfully."
else
    logger -t "$LOG_TAG" "WiFi reconnect FAILED. Manual intervention needed."
fi
