"""scripts/intent-liveness-audit.py — öz-introspeksiyon denetçisi (LIVESYS-INTRO).

audit() saf fonksiyon (emit'siz): deklarasyon↔gerçek boşluğunu sınar. Sentetik fixture:
orphan/dead-infra/missing/stale → FLAG; live/systemd/social → doğru sınıf, FP yok.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ila", ROOT / "scripts" / "intent-liveness-audit.py")
ila = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ila)


def _script(d: Path, name: str, header: str, body: str = "") -> None:
    (d / name).write_text(f"#!/bin/bash\n{header}\n{body}\n")


def _subjects(findings):
    return [s for _, s, _ in findings]


def test_orphan_cron_declared_not_scheduled(tmp_path):
    _script(tmp_path, "ghost.sh", "# Cron: 0 5 * * * (her gün)")
    findings = ila.audit(str(tmp_path), crontab_text="")  # boş crontab → scheduled değil
    assert any(s == "intent:ghost.sh" for s in _subjects(findings))
    assert any("orphan" in d for _, s, d in findings if s == "intent:ghost.sh")


def test_no_fp_when_scheduled(tmp_path):
    _script(tmp_path, "live.sh", "# Cron: 0 7 * * *")
    cron = "0 7 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh live /opt/linux-ai-server/automation/live.sh\n"
    findings = ila.audit(str(tmp_path), crontab_text=cron)
    assert not any(s == "intent:live.sh" for s in _subjects(findings))


def test_systemd_is_host_verify_not_orphan(tmp_path):
    _script(tmp_path, "backup-monitor.sh", "# Systemd timer ile günde 2x çalışır")
    findings = ila.audit(str(tmp_path), crontab_text="")
    det = [d for _, s, d in findings if s == "intent:backup-monitor.sh"]
    assert det
    assert "host-verification" in det[0]
    # 'orphan adayı' sınıfına DÜŞMEMELİ — host-verify ayrı sınıf
    assert "ölü-refleks adayı" not in det[0]


def test_social_is_vps_verify(tmp_path):
    _script(tmp_path, "social-weekly-generate.sh", "# Cron: 0 10 * * 0")
    findings = ila.audit(str(tmp_path), crontab_text="")
    det = [d for _, s, d in findings if s == "intent:social-weekly-generate.sh"]
    assert det
    assert "vps-verification" in det[0].lower()


def test_dead_infra_is_critical(tmp_path):
    # coolify.panola.app GERÇEK-ölü (Coolify decommission); 194.163.134.239 ARTIK YOK (canlı-VPS, surer F3)
    _script(tmp_path, "old-failover.sh", "# Klipper cron: */3 * * * *", body='URL="https://coolify.panola.app/health"')
    findings = ila.audit(str(tmp_path), crontab_text="")
    sev = [s for s, subj, _ in findings if subj == "intent:old-failover.sh" and s == "critical"]
    assert sev  # decommissioned hedef → critical


def test_live_vps_ip_not_flagged_dead(tmp_path):
    # surer F3 regresyon: CANLI production VPS IP'sini hedefleyen script dead-infra DEĞİL
    _script(tmp_path, "vps-backup.sh", "# Cron: 0 3 * * *", body='HOST="194.163.134.239"')
    findings = ila.audit(str(tmp_path), crontab_text="")
    dead = [d for s, subj, d in findings if subj == "intent:vps-backup.sh" and "dead-infra" in d]
    assert not dead  # canlı-VPS IP'si ölü-flag'lenmemeli


def test_missing_file_in_crontab(tmp_path):
    # crontab'da scheduled ama dosya yok → K2 critical
    cron = "0 4 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh gone /opt/linux-ai-server/automation/gone.sh\n"
    findings = ila.audit(str(tmp_path), crontab_text=cron)
    assert any(s == "critical" and "gone.sh" in subj for s, subj, _ in findings)


def test_stale_disabled_flag(tmp_path):
    cron = "# DISABLED: eski not\n*/20 * * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh x /opt/linux-ai-server/automation/x.sh\n"
    findings = ila.audit(str(tmp_path), crontab_text=cron)
    assert any("stale-flag" in d for _, _, d in findings)


def test_clean_repo_no_findings(tmp_path):
    # 3 canlı script + hepsi crontab'da → 0 bulgu (FP yok)
    cron_lines = []
    for n in ("a", "b", "c"):
        _script(tmp_path, f"{n}.sh", "# Cron: 0 1 * * *")
        cron_lines.append(f"0 1 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh {n} /opt/linux-ai-server/automation/{n}.sh")
    findings = ila.audit(str(tmp_path), crontab_text="\n".join(cron_lines))
    assert findings == []


def test_k2_catches_direct_unwrapped_missing(tmp_path):
    # Codex P2: cron-wrap'siz DOĞRUDAN entry, dosya yok → critical
    cron = "0 3 * * * /opt/linux-ai-server/automation/direct-gone.sh\n"
    findings = ila.audit(str(tmp_path), crontab_text=cron)
    assert any(s == "critical" and "direct-gone.sh" in subj for s, subj, _ in findings)


def test_k2_catches_live_crontab_drift(tmp_path):
    # Codex P2: repo-crontab'da YOK ama canlı-crontab'da silinmiş script → critical (host-drift)
    live = "0 2 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh drift /opt/linux-ai-server/automation/drift-gone.sh\n"
    findings = ila.audit(str(tmp_path), crontab_text="", live_crontab=live)
    assert any(s == "critical" and "drift-gone.sh" in subj for s, subj, _ in findings)
