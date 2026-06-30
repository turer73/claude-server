"""DevOpsAgent shared models, constants, playbooks, and VPS probe helpers.

Split out of devops_agent.py so the behavior mixins can import them without a
cycle (mixins import this; the facade imports the mixins)."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Any

# INTERV: yalnız GERİ-ALINABİLİR komutlar rollback'e uygun (DAR set). cpu-governor değişimi
# tersine çevrilebilir (önceki governor'a dön); prune/delete/truncate/restart GERİ-ALINAMAZ
# (escalate-only — surer kuralı). Governor adı güvenlik için katı doğrulanır.
_GOVERNOR_RE = re.compile(r"scaling_governor|cpufreq-set\s+-g")
_VALID_GOVERNOR = re.compile(r"^[a-z]+$")  # ondemand/schedutil/performance/powersave...

# GÜVENLİK: otonom remediation'da servis/konteyner adı f-string ile TAM-SHELL
# komutuna giriyor (ShellExecutor=create_subprocess_shell; whitelist yalnız ilk
# komuta bakar, zincir geçer). Adlar config'ten gelir (monitor_critical_*) ama
# config-drift geçmişi var (canlı server.yml repo'yu ezer) → savunma-derinliği:
# katı ad-doğrulama + shlex.quote. systemd unit + docker name güvenli karakter
# kümesi (@ . _ : - izinli; boşluk/meta YASAK).
_VALID_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]*$")

# #567 FP fix: sustained-window gating. cpu/mem/disk geçici-zirve eğilimli (zamanlanmış
# ağır iş — test-runner/e2e gece bakım penceresinde %98 anlık zirve yapıyor ama sürekli
# değil). Critical SADECE son N örnek de eşik-üstüyse (sürdürülen-yük). temperature hariç
# (fiziksel — tek yüksek okuma gerçek, thermal yanıt anlık olmalı).
_SUSTAINED_N = int(os.environ.get("ALERT_SUSTAINED_SAMPLES", "3"))  # 3×30s = 90s sürdürülen
_SUSTAINED_SOURCES = frozenset({"cpu", "memory", "disk"})


@dataclass
class Alert:
    id: str
    severity: str  # warning, critical
    source: str  # cpu, memory, disk, temperature, service, docker
    message: str
    value: float
    threshold: float
    timestamp: str
    resolved: bool = False
    resolved_at: str | None = None
    remediation: str | None = None


@dataclass
class RemediationRecord:
    timestamp: str
    alert_source: str
    action: str
    command: str
    result: str
    success: bool


# ── Playbooks ──────────────────────────────────────────────

PLAYBOOKS: dict[str, list[dict[str, str]]] = {
    "cpu_critical": [
        {"desc": "Log top CPU consumers", "cmd": "ps aux --sort=-%cpu | head -6"},
    ],
    # GUVENLIK (FAZ5 playbook-safening): YIKICI/geri-alinamaz adimlar cikarildi.
    # `--volumes` (adli/named volume veri-silme) ve otonom-backup-silme YASAK —
    # auto-mode'da false-positive critical'de veri kaybi riski. Sadece guvenli
    # reclaim: image/container/network prune (volume HARIC), cache/eski-tmp temizle,
    # dev log truncate. Backup-rotation = daily-backup.sh'in isi, remediation'in DEGIL.
    "memory_critical": [
        {"desc": "Docker prune (volume HARIC)", "cmd": "docker system prune -f 2>/dev/null || true"},
        {"desc": "Clear pip cache", "cmd": "pip cache purge 2>/dev/null || true"},
        {"desc": "Clear old tmp files", "cmd": "find /tmp -type f -mtime +1 -delete 2>/dev/null || true"},
    ],
    "disk_critical": [
        {"desc": "Docker prune (volume HARIC)", "cmd": "docker system prune -f 2>/dev/null || true"},
        {"desc": "Truncate dev logs", "cmd": "find /var/log -name '*.log' -size +50M -exec truncate -s 10M {} \\; 2>/dev/null || true"},
    ],
    "temperature_critical": [
        {
            "desc": "Set CPU governor to powersave",
            "cmd": "cpufreq-set -g powersave 2>/dev/null || echo 'powersave' | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true",
        },
    ],
    "service_down": [
        {"desc": "Restart service", "cmd": "systemctl restart {service}"},
    ],
    "docker_down": [
        {"desc": "Restart container", "cmd": "docker restart {container}"},
    ],
}

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_SSH = f"ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 {VPS_HOST}"

# Fixed, internal probe run on the VPS to collect host metrics + container state.
# Single sample of /proc/stat deltas (no tool dependency beyond awk/free/df/docker).
VPS_PROBE_SCRIPT = """\
read -r _ a1 b1 c1 d1 _ < /proc/stat
sleep 1
read -r _ a2 b2 c2 d2 _ < /proc/stat
awk -v db=$(( (a2+b2+c2)-(a1+b1+c1) )) -v dt=$(( (a2+b2+c2+d2)-(a1+b1+c1+d1) )) 'BEGIN{printf "CPU=%.1f\\n",(dt>0)?db*100/dt:0}'
free | awk '/^Mem:/{printf "MEM=%.1f\\n",$3/$2*100}'
df / | awk 'NR==2{gsub(/%/,"",$5);print "DISK="$5}'
echo "CTOTAL=$(docker ps -aq 2>/dev/null | wc -l)"
echo "CUP=$(docker ps -q 2>/dev/null | wc -l)"
echo "NAMES=$(docker ps --format '{{.Names}}' 2>/dev/null | tr '\\n' ',')"
"""
# base64 so it travels as a single quote-safe token through `ssh host '...'`
VPS_PROBE_B64 = base64.b64encode(VPS_PROBE_SCRIPT.encode()).decode()


def parse_vps_probe(stdout: str) -> dict[str, Any]:
    """Parse KEY=VALUE lines emitted by VPS_PROBE_SCRIPT into a typed dict."""
    kv: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    def _f(key: str) -> float | None:
        try:
            return float(kv[key])
        except (KeyError, ValueError):
            return None

    def _i(key: str) -> int | None:
        try:
            return int(kv[key])
        except (KeyError, ValueError):
            return None

    return {
        "cpu": _f("CPU"),
        "mem": _f("MEM"),
        "disk": _f("DISK"),
        "containers_total": _i("CTOTAL"),
        "containers_up": _i("CUP"),
        "names": [n for n in kv.get("NAMES", "").split(",") if n],
    }
