#!/usr/bin/env python3
"""Intent-Liveness Audit — öz-introspeksiyon / ölü-refleks denetçisi (LIVESYS-INTRO).

liveness.py process/veri-canlılığını ölçer; bu denetçi DEKLARASYON-vs-GERÇEK boşluğunu
arar: bir script "cron'da çalışırım" der ama crontab'da yok mu? crontab'da var ama dosya
yok mu? failover hedefi decommissioned-IP mi? "DISABLED" yorumu var ama satır aktif mi?

TAMAMEN SALT-OKUNUR: hiçbir şey silmez/uncomment etmez/restart etmez. Bulguları yalnız
emit-event.sh (type=intent-liveness) + critical→discovery(bug) ile yüzeye çıkarır.

4 kontrol:
  K1 header-schedule ↔ crontab : script header'ı cron/systemd beyan ediyor ama scheduled mı?
  K2 reverse                   : crontab'taki script dosyası gerçekten var mı?
  K3 failover-hedef-canlılık   : decommissioned-IP/dead-infra hedefi var mı?
  K4 flag-staleness            : "DISABLED/stale" yorumu var ama satır aktif mi?

FP-koruma: 'systemd timer' beyanı → 'needs-host-verification' (orphan DEME); social-* →
'needs-vps-verification' (zamanlama VPS'te olabilir, gecmis VPS-discovery-eksigi dersi).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.environ.get("LIVESYS_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTOMATION = os.path.join(ROOT, "automation")
CRONTAB_FILE = os.path.join(AUTOMATION, "crontab")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")

# K3: decommissioned / dead-infra işaretleri (hedef bunları içeriyorsa ölü-refleks).
# DİKKAT: yalnız GERÇEKTEN ölü hedefler. 194.163.134.239 BURADA DEĞİL — o CANLI production
# VPS (Contabo/Dokploy, .env N8N_WEBHOOK_URL aktif); Coolify VPS'ten kaldırıldı (2026-04-07)
# ama VPS yaşıyor → IP'yi dead sayma (surer F3: canlı-IP false-positive kaynağıydı).
DEAD_INFRA = ["coolify.panola.app"]
# systemd-timer ile çalışan (cron'da OLMAMASI normal) — orphan deme, host-verify iste.
SYSTEMD_HINT = re.compile(r"systemd\s*timer", re.I)
# header cron beyanı: 'Cron: <5-alan>' veya 'cron <5-alan>'
_CRON_DECL = re.compile(r"cron[:\s]+([\d*/,\-]+(?:\s+[\d*/,\-]+){4})", re.I)
_SH_REF = re.compile(r"/?([A-Za-z0-9_.\-]+\.sh)\b")


def header_lines(path: str, n: int = 8) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join([next(fh, "") for _ in range(n)])
    except OSError:
        return ""


def declared_schedule(header: str) -> str | None:
    """Header'da zamanlama beyanı türü: 'cron' | 'systemd' | None."""
    if SYSTEMD_HINT.search(header):
        return "systemd"
    if _CRON_DECL.search(header):
        return "cron"
    return None


def crontab_basenames(crontab_text: str) -> set[str]:
    """Crontab'taki (yorum-olmayan) satırlarda geçen tüm .sh basename'leri."""
    names: set[str] = set()
    for line in crontab_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for m in _SH_REF.finditer(s):
            names.add(m.group(1))
    return names


def crontab_script_paths(crontab_text: str) -> list[str]:
    """Crontab'taki HEDEF script tam-yolları (K2). Hem cron-wrap'li hem DOĞRUDAN entry'ler
    (Codex P2): wrap'li satırda wrapper-sonrası hedefi, doğrudan satırda ilk mutlak .sh/.py."""
    paths: list[str] = []
    for line in crontab_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # cron-wrap'li ise wrapper'ın KENDİSİNİ değil, sardığı hedefi al
        scope = s.split("klipper-cron-wrap.sh", 1)[1] if "klipper-cron-wrap.sh" in s else s
        m = re.search(r"(/[A-Za-z0-9_./\-]+\.(?:sh|py))", scope)
        if m:
            paths.append(m.group(1))
    return paths


def audit(automation_dir: str, crontab_text: str, live_crontab: str = "") -> list[tuple[str, str, str]]:
    """(severity, subject, detail) bulguları. SALT-OKUNUR — yalnız okur+sınıflandırır."""
    findings: list[tuple[str, str, str]] = []
    scheduled = crontab_basenames(crontab_text) | crontab_basenames(live_crontab)

    try:
        scripts = sorted(f for f in os.listdir(automation_dir) if f.endswith(".sh"))
    except OSError:
        return [("critical", "intent-liveness", f"automation dizini okunamadı: {automation_dir}")]

    for fname in scripts:
        path = os.path.join(automation_dir, fname)
        hdr = header_lines(path)
        decl = declared_schedule(hdr)

        # K1: header zamanlama beyan ediyor ama crontab'da yok
        if decl == "cron" and fname not in scheduled:
            if fname.startswith("social-"):
                findings.append(
                    (
                        "warn",
                        f"intent:{fname}",
                        "header cron beyan ediyor + klipper-crontab'da YOK → VPS-cross-check gerek "
                        "(zamanlama VPS'te olabilir; tek-başına 'ölü' deme — needs-vps-verification)",
                    )
                )
            else:
                findings.append(
                    (
                        "warn",
                        f"intent:{fname}",
                        "header 'cron' beyan ediyor ama crontab'da YOK → ölü-refleks adayı (orphan); RETIRE veya re-schedule kararı gerek",
                    )
                )
        elif decl == "systemd" and fname not in scheduled:
            findings.append(
                (
                    "warn",
                    f"intent:{fname}",
                    "header 'systemd timer' beyan ediyor → 'systemctl list-timers' ile HOST-verify "
                    "gerek (orphan DEĞİL; needs-host-verification)",
                )
            )

        # K3: hedef decommissioned/dead-infra
        try:
            with open(path, encoding="utf-8", errors="replace") as bfh:
                body = bfh.read()
        except OSError:
            body = ""
        for dead in DEAD_INFRA:
            if dead in body:
                findings.append(
                    ("critical", f"intent:{fname}", f"hedef decommissioned/dead-infra ({dead}) → ölü-refleks; RETIRE veya hedef-güncelle")
                )
                break

    # K2: scheduled script dosyası var mı — HEM repo crontab HEM canlı crontab (Codex P2:
    # host-drift'te canlı entry silinmiş script'e işaret edebilir, repo'da olmasa bile yakala).
    seen_paths: set[str] = set()
    for src in (crontab_text, live_crontab):
        for p in crontab_script_paths(src):
            if p in seen_paths:
                continue
            seen_paths.add(p)
            rel = p.replace("/opt/linux-ai-server/", "")
            cands = [p, os.path.join(automation_dir, os.path.basename(p)), os.path.join(ROOT, rel)]
            if not any(os.path.isfile(c) for c in cands):
                findings.append(
                    ("critical", f"intent:{os.path.basename(p)}", f"crontab'da scheduled ama dosya YOK: {p} → kırık cron-entry")
                )

    # K4: 'DISABLED'/stale yorum var ama bir sonraki satır aktif (uncommented)
    lines = crontab_text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"\bDISABLED\b", line, re.I) and line.strip().startswith("#"):
            # sonraki yorum-olmayan satır aktif mi
            for nxt in lines[i + 1 : i + 4]:
                t = nxt.strip()
                if t and not t.startswith("#"):
                    findings.append(("warn", "intent:crontab", f"'DISABLED' yorumu var ama altındaki satır AKTİF (stale-flag): {t[:70]}"))
                    break
    return findings


def _emit(severity: str, subject: str, detail: str) -> None:
    """emit-event.sh ile yüzeye çıkar (best-effort, fail-safe). SALT side-channel."""
    if os.environ.get("INTENT_AUDIT_NO_EMIT") == "1":
        return  # test/dry: event yüzeye çıkarma
    emit = os.path.join(ROOT, "scripts", "emit-event.sh")
    if not os.path.isfile(emit):
        return
    try:
        subprocess.run(
            [emit, "intent-liveness", subject, severity, f"intent-liveness: {detail[:60]}", detail[:300]],
            timeout=10,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> int:
    live = ""
    try:  # host-feed: canlı crontab (read-only)
        live = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open(CRONTAB_FILE, encoding="utf-8") as fh:
            crontab_text = fh.read()
    except OSError as e:
        print(f"OUTCOME: fail | crontab okunamadı: {e}")
        return 0

    findings = audit(AUTOMATION, crontab_text, live)
    crit = sum(1 for s, _, _ in findings if s == "critical")
    for sev, subj, det in findings:
        _emit(sev, subj, det)
        print(f"[{sev.upper()}] {subj}: {det}")

    if not findings:
        print("OUTCOME: pass | intent-liveness: deklarasyon↔gerçek tutarlı, ölü-refleks yok")
    elif crit:
        print(f"OUTCOME: partial | intent-liveness: {len(findings)} bulgu ({crit} critical) → ortak-hafıza/event")
    else:
        print(f"OUTCOME: partial | intent-liveness: {len(findings)} bulgu (warn) → event")
    return 0


if __name__ == "__main__":
    sys.exit(main())
