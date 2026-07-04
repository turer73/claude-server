"""G2 gate-telemetry testleri (PR-G2a): migration + collector GERÇEK script-yolundan.

Mock-maske yok: collector bash-script'i fake-gh PATH-shim'iyle uçtan-uca koşar
(gh-CLI sahte, sqlite3/jq gerçek). Windows-lokalde sqlite3/jq/bash eksikse SKIP —
CI (ubuntu) otoriter ([[feedback_grep_ere_pipe_portability_ci_authoritative]] deseni).

G1-repro: base'de script'ler yok → testler FAIL (yeni-özellik; anlamlı-mekanik kanıt
test_collector_end_to_end'in kendisi: verdict-mapping + skip_na + watermark + idempotensi).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_MISSING = [t for t in ("bash", "sqlite3", "jq") if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(bool(_MISSING), reason=f"CI-otoriter; lokalde eksik araç: {_MISSING}")


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **env}
    return subprocess.run(cmd, capture_output=True, text=True, env=full_env, timeout=120)


def _make_fake_gh(tmp_path: Path, runs: list[dict], jobs_by_run: dict[int, list[dict]], na_job_ids: set[int]) -> Path:
    """PATH-önüne konan sahte-gh: gerçek gh-CLI çıktı-şekillerini basar (#100388 spec-verify)."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
    for rid, jobs in jobs_by_run.items():
        (fixtures / f"jobs_{rid}.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    na_ids = " ".join(str(j) for j in na_job_ids) or "NONE"
    gh = bindir / "gh"
    gh.write_text(
        f"""#!/bin/bash
# fake-gh — test-shim. Argümanlara göre fixture basar.
FIX="{fixtures.as_posix()}"
case "$1 $2" in
  "run list")
    cat "$FIX/runs.json" ;;
  "run view")
    if [ "$3" = "--repo" ] && [ "$5" = "--job" ]; then
      JOB_ID="$6"
      for na in {na_ids}; do
        [ "$JOB_ID" = "$na" ] && {{ echo "repro-gate atlandı (fix-dışı PR)"; exit 0; }}
      done
      echo "Repro-Test = tests/x.py::t | base'de FAIL bekleniyor"; exit 0
    fi
    cat "$FIX/jobs_$3.json" ;;
  "pr list")
    echo "77" ;;
  *)
    echo "fake-gh: bilinmeyen: $*" >&2; exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return bindir


def _env(tmp_path: Path, bindir: Path) -> dict[str, str]:
    return {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "COVERAGE_DB": str(tmp_path / "coverage.db"),
        "STATE_FILE": str(tmp_path / "watermark.txt"),
        "GATE_TELEMETRY_LOG": str(tmp_path / "collect.log"),
        "REPO": "test/repo",
        "GH_LIMIT": "40",
    }


def test_migration_idempotent(tmp_path):
    env = {"COVERAGE_DB": str(tmp_path / "coverage.db")}
    for _ in range(2):  # 2. koşum da temiz (IF NOT EXISTS)
        r = _run(["bash", str(REPO_ROOT / "scripts" / "migrate-gate-telemetry.sh")], env)
        assert r.returncode == 0, r.stderr[-300:]
    con = sqlite3.connect(tmp_path / "coverage.db")
    cols = {row[1] for row in con.execute("PRAGMA table_info(gate_telemetry)").fetchall()}
    con.close()
    assert {"gate_id", "run_id", "verdict", "fp_class", "fp_source"} <= cols


def test_collector_end_to_end(tmp_path):
    """Gerçek-yol: fail→fail, success+atlandı-log→skip_na, success→pass; in-progress
    İŞLENMEZ ve watermark'ı İLERLETMEZ; 2. koşum idempotent (satır-sayısı sabit)."""
    runs = [
        {"databaseId": 101, "conclusion": "failure", "headBranch": "b1", "headSha": "sha1", "createdAt": "2026-07-04T01:00:00Z"},
        {"databaseId": 102, "conclusion": "success", "headBranch": "b2", "headSha": "sha2", "createdAt": "2026-07-04T02:00:00Z"},
        {"databaseId": 103, "conclusion": "success", "headBranch": "b3", "headSha": "sha3", "createdAt": "2026-07-04T03:00:00Z"},
        {"databaseId": 104, "conclusion": "", "headBranch": "b4", "headSha": "sha4", "createdAt": "2026-07-04T04:00:00Z"},
    ]
    jobs = {
        101: [{"name": "repro-gate", "conclusion": "failure", "databaseId": 9101}],
        102: [{"name": "repro-gate", "conclusion": "success", "databaseId": 9102}],  # log: atlandı → skip_na
        103: [{"name": "repro-gate", "conclusion": "success", "databaseId": 9103}],  # log: normal → pass
    }
    bindir = _make_fake_gh(tmp_path, runs, jobs, na_job_ids={9102})
    env = _env(tmp_path, bindir)

    r = _run(["bash", str(REPO_ROOT / "automation" / "gate-telemetry-collect.sh")], env)
    assert r.returncode == 0, (r.stderr[-400:], (tmp_path / "collect.log").read_text(encoding="utf-8")[-400:])

    con = sqlite3.connect(tmp_path / "coverage.db")
    rows = con.execute("SELECT run_id, verdict, pr_number, fp_class FROM gate_telemetry ORDER BY run_id").fetchall()
    con.close()
    assert rows == [(101, "fail", 77, "unknown"), (102, "skip_na", 77, "unknown"), (103, "pass", 77, "unknown")]
    # in-progress 104 işlenmedi; watermark son-tamamlanan'da (103) — sonraki tick 104'ü toplar
    assert (tmp_path / "watermark.txt").read_text(encoding="utf-8") == "103"

    # idempotensi: aynı fixture'la 2. koşum — satır-sayısı sabit (upsert, çift-insert yok)
    r2 = _run(["bash", str(REPO_ROOT / "automation" / "gate-telemetry-collect.sh")], env)
    assert r2.returncode == 0
    con = sqlite3.connect(tmp_path / "coverage.db")
    assert con.execute("SELECT COUNT(*) FROM gate_telemetry").fetchone()[0] == 3
    con.close()


def test_collector_watermark_skips_processed(tmp_path):
    """Watermark-altı run'lar yeniden-fetch edilmez (jobs_101 fixture'ı silinse bile hata yok)."""
    runs = [{"databaseId": 101, "conclusion": "failure", "headBranch": "b", "headSha": "s", "createdAt": "2026-07-04T01:00:00Z"}]
    jobs = {101: [{"name": "repro-gate", "conclusion": "failure", "databaseId": 9101}]}
    bindir = _make_fake_gh(tmp_path, runs, jobs, na_job_ids=set())
    env = _env(tmp_path, bindir)
    assert _run(["bash", str(REPO_ROOT / "automation" / "gate-telemetry-collect.sh")], env).returncode == 0
    (tmp_path / "fixtures" / "jobs_101.json").unlink()  # watermark-altı → dokunulmamalı
    r = _run(["bash", str(REPO_ROOT / "automation" / "gate-telemetry-collect.sh")], env)
    assert r.returncode == 0, r.stderr[-300:]
