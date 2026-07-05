"""Spawn-isolation Faz-1 testleri (tasarım §2.2-2.5 + kabul-kriterleri 2-6).

Gerçek-yol: tmp-git-repo (sim-/opt) + _spawn-worktree-lib.sh bash-üzerinden source-edilip
fonksiyonlar koşulur; audit-script gerçek-koşum. Mock-maske yok (git/bash gerçek).

G1-repro: base'de lib yok → source-FAIL. Çekirdek-kanıt: E2E-collision-regresyonu —
spawn-commit'i sonrası ANA-MASTER HEAD DEĞİŞMEZ (bugünkü 6-kirliliğin tam-tersi).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "automation" / "_spawn-worktree-lib.sh"
AUDIT = REPO_ROOT / "automation" / "autonomous-spawn-audit.sh"

_MISSING = [t for t in ("bash", "git") if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(bool(_MISSING), reason=f"bash+git gerek: {_MISSING}")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"git {args}: {r.stderr[-300:]}"
    return r.stdout.strip()


def _make_sim_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sim-opt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("sim\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _lib_env(tmp_path: Path, repo: Path) -> dict[str, str]:
    fake_tg = tmp_path / "fake-telegram.sh"
    fake_tg.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    return {
        **os.environ,
        "SPAWN_REPO_ROOT": str(repo),
        "SPAWN_WT_BASE": str(tmp_path / "wt"),
        "LOG_FILE": str(tmp_path / "wt.log"),
        "TELEGRAM_ALERT": str(fake_tg),
        "SPAWN_ISOLATION": "1",
    }


def _run_lib(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = f'. "{LIB.as_posix()}"\n{script}'
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, env=env, timeout=120)


def test_commit_preserved_and_master_untouched(tmp_path):
    """KRİTER-2+5 (çekirdek, E2E-collision-regresyonu): spawn worktree'de commit atar →
    ref'te KORUNUR, worktree silinir, ANA-MASTER HEAD DEĞİŞMEZ + working-tree temiz."""
    repo = _make_sim_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    r = _run_lib(
        """
setup_spawn_worktree 42
[ -n "$WT_PATH" ] || { echo "SETUP-FAIL"; exit 1; }
echo "spawn-dosyasi" > "$WT_PATH/spawn-eseri.txt"
git -C "$WT_PATH" add spawn-eseri.txt
git -C "$WT_PATH" -c user.email=s@s -c user.name=spawn commit -q -m "spawn-isi (kirlilik-senaryosu)"
preserve_and_cleanup_worktree 42
echo "REF=$SPAWN_WORK_REF"
""",
        _lib_env(tmp_path, repo),
    )
    assert r.returncode == 0, r.stderr[-400:] + r.stdout[-200:]
    ref = next(line.split("=", 1)[1] for line in r.stdout.splitlines() if line.startswith("REF="))
    assert ref.startswith("refs/spawn-work/42-")
    # ana-master DEĞİŞMEDİ (6-kirliliğin tam-tersi) + working-tree temiz
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "status", "--porcelain") == ""
    # commit ref'te KORUNDU (work-loss yok) ve içeriği doğru
    preserved = _git(repo, "rev-parse", ref)
    assert preserved != base
    assert "spawn-eseri.txt" in _git(repo, "show", "--name-only", "--format=", preserved)
    # worktree kaldırıldı
    assert not list((tmp_path / "wt").glob("spawn-42-*"))


def test_no_commit_no_ref(tmp_path):
    """Commit'siz spawn → ref üretilmez, worktree sessizce kalkar, master değişmez."""
    repo = _make_sim_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    r = _run_lib(
        'setup_spawn_worktree 7\npreserve_and_cleanup_worktree 7\necho "REF=[$SPAWN_WORK_REF]"',
        _lib_env(tmp_path, repo),
    )
    assert r.returncode == 0, r.stderr[-300:]
    assert "REF=[]" in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == base


def test_stale_worktree_repair(tmp_path):
    """KRİTER-4 (P2-c): kesik-önceki-koşumun worktree'si kalmışsa yeni-setup REPAIR eder
    (fallback değil) — stale silinir, yeni-nonce'la kurulur."""
    repo = _make_sim_repo(tmp_path)
    env = _lib_env(tmp_path, repo)
    # 1. koşum: setup yapıp cleanup YAPMADAN çık (process-kill simülasyonu)
    r1 = _run_lib('setup_spawn_worktree 9\necho "WT1=$WT_PATH"', env)
    assert r1.returncode == 0
    stale = next(line.split("=", 1)[1] for line in r1.stdout.splitlines() if line.startswith("WT1="))
    assert Path(stale).is_dir()
    # 2. koşum: aynı-note → stale-repair + yeni-worktree
    r2 = _run_lib('setup_spawn_worktree 9\necho "WT2=$WT_PATH"\npreserve_and_cleanup_worktree 9', env)
    assert r2.returncode == 0, r2.stderr[-300:]
    wt2 = next(line.split("=", 1)[1] for line in r2.stdout.splitlines() if line.startswith("WT2="))
    assert wt2 != ""
    assert wt2 != stale  # yeni-nonce
    assert not Path(stale).exists()  # stale temizlendi


def test_fallback_critical_emit(tmp_path):
    """KRİTER-6 (§2.5): worktree-altyapı-hatası → shared-fallback (WT_PATH boş) AMA
    CRITICAL-log (sessiz-fallback YOK)."""
    repo = _make_sim_repo(tmp_path)
    env = _lib_env(tmp_path, repo)
    (tmp_path / "wt").write_text("dosya — dizin-degil", encoding="utf-8")  # mkdir-FAIL tetikler
    r = _run_lib('setup_spawn_worktree 5\necho "WT=[$WT_PATH]"', env)
    assert r.returncode == 0, r.stderr[-300:]
    assert "WT=[]" in r.stdout  # fallback-shared
    log = (tmp_path / "wt.log").read_text(encoding="utf-8")
    assert "CRITICAL" in log
    assert "FALLBACK-SHARED" in log


def test_isolation_kill_switch(tmp_path):
    """SPAWN_ISOLATION=0 acil-rollback: worktree kurulmaz, log'lu (bilinçli-shared)."""
    repo = _make_sim_repo(tmp_path)
    env = {**_lib_env(tmp_path, repo), "SPAWN_ISOLATION": "0"}
    r = _run_lib('setup_spawn_worktree 3\necho "WT=[$WT_PATH]"', env)
    assert r.returncode == 0
    assert "WT=[]" in r.stdout
    assert "KAPALI" in (tmp_path / "wt.log").read_text(encoding="utf-8")


def test_audit_scans_spawn_ref(tmp_path):
    """KRİTER-3 (P2-b): izole-commit /opt-HEAD'i ilerletmese de audit spawn-ref'i tarar —
    sensitive-file (.env) commit'i SUSPICIOUS yakalanır."""
    repo = _make_sim_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    env = _lib_env(tmp_path, repo)
    r = _run_lib(
        """
setup_spawn_worktree 42
printf 'MEMORY_API_KEY=sahte' > "$WT_PATH/.env.autonomous"
git -C "$WT_PATH" add -f .env.autonomous
git -C "$WT_PATH" -c user.email=s@s -c user.name=spawn commit -q -m "sensitive-dokunma"
preserve_and_cleanup_worktree 42
echo "REF=$SPAWN_WORK_REF"
""",
        env,
    )
    assert r.returncode == 0, r.stderr[-300:]
    ref = next(line.split("=", 1)[1] for line in r.stdout.splitlines() if line.startswith("REF="))
    # spawn-head-file = base (autonomous-claude.sh'ın yazdığı format)
    head_dir = tmp_path / "hook-state"
    head_dir.mkdir()
    (head_dir / "spawn-head-42.txt").write_text(base + "\n", encoding="utf-8")
    audit_log = tmp_path / "audit.log"
    ra = subprocess.run(
        ["bash", str(AUDIT), "42", ref],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "AUDIT_REPO": str(repo), "SPAWN_HEAD_DIR": str(head_dir), "AUDIT_LOG": str(audit_log)},
    )
    assert ra.returncode == 0, ra.stderr[-300:]
    log = audit_log.read_text(encoding="utf-8")
    assert "SUSPICIOUS" in log, f"audit sensitive-commit'i yakalamadı: {log[-400:]}"


# ── Faz-2 (P2-a): write-guard hook + per-spawn settings ─────────────────────────

GUARD = REPO_ROOT / "automation" / "spawn-write-guard.sh"
_NEED_JQ = bool(_MISSING) or shutil.which("jq") is None


def _run_guard(tool_input: dict, wt: str) -> subprocess.CompletedProcess[str]:
    import json as _json

    return subprocess.run(
        ["bash", str(GUARD)],
        input=_json.dumps({"tool_name": "Write", "tool_input": tool_input}),
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "SPAWN_WT_PATH": wt},
    )


@pytest.mark.skipif(_NEED_JQ, reason="bash/git/jq gerek")
def test_guard_denies_outside_worktree(tmp_path):
    """KRİTER-1 (P2-a çekirdek): worktree-DIŞI absolute-yazma DENY (exit 2 + mesaj)."""
    wt = tmp_path / "wt-x"
    wt.mkdir()
    r = _run_guard({"file_path": "/opt/linux-ai-server/app/foo.py"}, str(wt))
    assert r.returncode == 2
    assert "DENY" in r.stderr
    assert "worktree" in r.stderr


@pytest.mark.skipif(_NEED_JQ, reason="bash/git/jq gerek")
def test_guard_allows_inside_worktree_and_tmp(tmp_path):
    wt = tmp_path / "wt-x"
    wt.mkdir()
    assert _run_guard({"file_path": str(wt / "app" / "foo.py")}, str(wt)).returncode == 0
    assert _run_guard({"file_path": "gorece/dosya.py"}, str(wt)).returncode == 0  # relative → wt-içi
    assert _run_guard({"file_path": "/tmp/calisma.txt"}, str(wt)).returncode == 0  # tmp-paritesi


@pytest.mark.skipif(_NEED_JQ, reason="bash/git/jq gerek")
def test_guard_denies_traversal_and_failclosed(tmp_path):
    """'..'-traversal normalize-edilip DENY; parse-edilemeyen-input FAIL-CLOSED DENY."""
    wt = tmp_path / "wt-x"
    wt.mkdir()
    # DİKKAT (2×CI-dersi): pytest-tmp CI'da /tmp-altında — wt-göreli '..'-zinciri kaç-seviye
    # olursa-olsun /tmp-istisnasında kalabiliyor → traversal'ı /tmp'den BAĞIMSIZ kurgula:
    # '..'-içeren mutlak-yol, normalize → /opt/... → DENY (realpath-çözümü yine test-edilir).
    r = _run_guard({"file_path": "/opt/linux-ai-server/../linux-ai-server/kacak.txt"}, str(wt))
    assert r.returncode == 2, f"rc={r.returncode} stderr={r.stderr[-200:]}"
    # parse-fail (file_path yok) → deny
    r2 = _run_guard({"baska_alan": 1}, str(wt))
    assert r2.returncode == 2
    # SPAWN_WT_PATH tanımsız → deny
    import json as _json

    r3 = subprocess.run(
        ["bash", str(GUARD)],
        input=_json.dumps({"tool_input": {"file_path": "/tmp/x"}}),
        capture_output=True,
        text=True,
        timeout=30,
        env={k: v for k, v in os.environ.items() if k != "SPAWN_WT_PATH"},
    )
    assert r3.returncode == 2


@pytest.mark.skipif(_NEED_JQ, reason="bash/git/jq gerek")
def test_per_spawn_settings_generated(tmp_path):
    """Faz-2 settings-üretimi: /opt-Edit/Write-allow'ları worktree'ye map'lenir + hook-enjekte;
    base-dosya DEĞİŞMEZ."""
    import json as _json

    repo = _make_sim_repo(tmp_path)
    base_settings = REPO_ROOT / "automation" / "autonomous-claude-settings.json"
    base_before = base_settings.read_text(encoding="utf-8")
    env = {**_lib_env(tmp_path, repo), "WRITE_GUARD": str(GUARD)}
    r = _run_lib(
        f'setup_spawn_worktree 42\nmake_spawn_settings "{base_settings.as_posix()}"\necho "SET=$SPAWN_SETTINGS"\ncat "$SPAWN_SETTINGS"',
        env,
    )
    lib_log = tmp_path / "wt.log"
    logtail = lib_log.read_text(encoding="utf-8")[-600:] if lib_log.exists() else "(log yok)"
    assert r.returncode == 0, f"stderr={r.stderr[-300:]} LOG={logtail}"
    out_path = next(line.split("=", 1)[1] for line in r.stdout.splitlines() if line.startswith("SET="))
    # Codex-re3 P1-CRUX: settings WORKTREE-DIŞINDA (pool-parent) — spawn yazamaz (tampering-önleme).
    wt_base = str(tmp_path / "wt")
    assert out_path.startswith(wt_base + "/.spawn-settings-"), f"settings worktree-dışı-değil: '{out_path}'"
    assert "/spawn-42-" not in out_path, f"settings worktree-İÇİNDE (tampering-riski): '{out_path}' — LOG={logtail}"
    body = r.stdout.split("\n", r.stdout.splitlines().index("SET=" + out_path) + 1)[-1]
    cfg = _json.loads(body[body.index("{") :])
    allows = cfg["permissions"]["allow"]
    assert not any(a == "Edit(//opt/linux-ai-server/**)" or a == "Write(//opt/linux-ai-server/**)" for a in allows), (
        "/opt Edit/Write-allow hâlâ duruyor — daraltma başarısız"
    )
    assert any(a.startswith("Edit(//") and "spawn-42-" in a for a in allows)
    # Codex-re3 P1-Read: Read /opt KORUNUR (salt-oku) + worktree Read EKLENİR.
    assert "Read(//opt/linux-ai-server/**)" in allows, "Read /opt kayboldu"
    assert any(a.startswith("Read(//") and "spawn-42-" in a for a in allows), "worktree Read-izni eklenmedi"
    hooks = cfg["hooks"]["PreToolUse"]
    assert any("spawn-write-guard" in h["hooks"][0]["command"] for h in hooks)
    assert any("SPAWN_WT_PATH=" in h["hooks"][0]["command"] for h in hooks)
    assert base_settings.read_text(encoding="utf-8") == base_before  # base-dosya dokunulmadı
