"""pull-vps-backup.sh — Postgres yedegi yoksa "pass" DEME.

Arka plan (2026-08-15): script canli Postgres veri dizinlerini (plausible_db-data,
dokploy-postgres) tar'liyordu. Bu iki acidan yanlisti:
  1) Calisan bir PG'nin data dizinini pg_start_backup/WAL-arsivi olmadan tar
     etmek TUTARLI bir yedek degildir — restore'da bozuk cikabilir.
  2) tar, dosya okunurken degistigi icin ARALIKLI exit 1 veriyordu.
Dogru kaynak zaten vardi: VPS'in kendi backup.sh'i pg_dump/pg_dumpall uretiyor.
Script artik o volume'leri atlayip mantiksal dump'lari cekiyor.

Bu degisiklik yeni bir SESSIZ-ARIZA riski yaratir: PG volume'leri artik
tar'lanmadigi icin, mantiksal dump da inmezse elde HIC Postgres yedegi kalmaz —
ama eski sozlesmede bu yalnizca "partial" olurdu ve gunluk gurultuye gomulurdu.
Sozlesme bu yuzden sertlestirildi: SQL_OK=0 -> FAIL.

Testler `ssh`'i stub'layarak kosar; ag/VPS gerekmez.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "automation" / "pull-vps-backup.sh"


def _stub_ssh(bin_dir: Path, payload: str = "") -> None:
    """Her ssh cagrisina sabit cevap veren stub (varsayilan: bos)."""
    ssh = bin_dir / "ssh"
    ssh.write_text(f"#!/bin/bash\nprintf '%s' {payload!r}\nexit 0\n")
    ssh.chmod(0o755)


def _run(tmp_path: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "VPS_HOST": "root@test-invalid",
            "VPS_BACKUP_TARGET": str(tmp_path / "out"),
            # Guard KAPATILMIYOR — yalnizca gercekten mount'lu bir yol veriliyor.
            "VPS_BACKUP_MOUNT": "/",
            # Telegram/Kuma disari cikmasin.
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "KUMA_BACKUP_PUSH_URL": "",
            # Relay canli server.db'ye yazmasin.
            "DB_PATH": str(tmp_path / "nonexistent.db"),
        },
    )


@pytest.mark.skipif(shutil.which("mountpoint") is None, reason="mountpoint yok")
def test_no_sql_dump_is_fail_not_partial(tmp_path: Path) -> None:
    """Hic mantiksal dump inmezse OUTCOME fail olmali — partial yeterli degil.

    PG volume tar'i bilerek atlandigi icin, dump da yoksa Postgres yedegi YOKTUR.
    Bunu "partial" saymak, gercek bir veri-kaybi riskini gunluk gurultuye gomer.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_ssh(bin_dir)  # her sey bos -> volume yok, ch yok, sql yok

    result = _run(tmp_path, bin_dir)

    assert "OUTCOME: fail" in result.stdout, f"stdout={result.stdout!r}"
    assert "POSTGRES YEDEGI YOK" in result.stdout, result.stdout
    assert "OUTCOME: partial" not in result.stdout, result.stdout


def test_live_postgres_volumes_are_excluded_from_tar_list() -> None:
    """PG_VOLUME_SKIP gercekten iki PG volume'unu eliyor, digerlerini elemiyor.

    Regex script'ten OKUNUYOR (kopyalanmiyor) — deger degisirse test onu goru.
    """
    src = SCRIPT.read_text()
    m = re.search(r"^PG_VOLUME_SKIP='([^']+)'", src, re.MULTILINE)
    assert m, "PG_VOLUME_SKIP tanimi bulunamadi"
    skip = re.compile(m.group(1))

    # VPS'te 2026-08-15'te gercekten kesfedilen volume adlari.
    discovered = [
        "dokploy",
        "dokploy-postgres",
        "dokploy-redis",
        "grafana-data",
        "plausible_db-data",
        "zxvny99vl7108ybk5c2saipg_n8n-data",
    ]
    skipped = [v for v in discovered if skip.search(v)]
    kept = [v for v in discovered if not skip.search(v)]

    assert set(skipped) == {"dokploy-postgres", "plausible_db-data"}, skipped
    # dokploy (traefik konfig) ve dokploy-redis PG DEGIL — elenmemeli.
    assert set(kept) == {
        "dokploy",
        "dokploy-redis",
        "grafana-data",
        "zxvny99vl7108ybk5c2saipg_n8n-data",
    }, kept
