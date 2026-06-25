"""extensions/gh-runner — güvenli Docker-ephemeral runner scriptleri.

Container'a girmeden (fake curl/docker PATH-gölgeli) loop davranışını doğrular:
mint-success → docker-run REG_TOKEN ile; mint-fail → backoff (API-spam yok); entrypoint
REG_TOKEN'sız fail-fast. Gerçek GitHub/Docker'a gitmez.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GHR = ROOT / "extensions" / "gh-runner"
LOOP = GHR / "run-ephemeral-loop.sh"
ENTRY = GHR / "entrypoint.sh"


def _fake_bin(bindir: Path, name: str, body: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    f = bindir / name
    f.write_text("#!/bin/bash\n" + body)
    f.chmod(0o755)


def _run_loop(tmp_path: Path, extra_env: dict) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "KOKEN_RUNNER_PAT_FILE": str(tmp_path / "pat"),
        "KOKEN_RUNNER_LOG": str(tmp_path / "run.log"),
        "KOKEN_RUNNER_IMAGE": "test-img",
        "KOKEN_RUNNER_REPO": "owner/repo",
        "KOKEN_RUNNER_BACKOFF_BASE": "0",  # test hızı — backoff sleep'i sıfırla
        **extra_env,
    }
    (tmp_path / "pat").write_text("fake-pat-123")
    return subprocess.run(["bash", str(LOOP)], env=env, capture_output=True, text=True, timeout=15)


def test_entrypoint_requires_reg_token(tmp_path):
    # REG_TOKEN yoksa config.sh'a ULAŞMADAN fail-fast (set -u / :?).
    r = subprocess.run(
        ["bash", str(ENTRY)],
        env={"PATH": "/usr/bin:/bin", "REPO_URL": "https://github.com/owner/repo"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode != 0
    assert "REG_TOKEN" in r.stderr


def test_loop_mint_success_runs_ephemeral_container(tmp_path):
    # curl registration-token döndürür (gerçek jq ayıklar) → docker run REG_TOKEN ile çağrılır.
    _fake_bin(tmp_path / "bin", "curl", "printf '%s' '{\"token\":\"REG123\"}'\n")
    # fake docker: argümanlarını dosyaya yaz (REG_TOKEN geçti mi doğrula)
    _fake_bin(tmp_path / "bin", "docker", f'echo "$@" > "{tmp_path}/docker.args"\nexit 0\n')
    r = _run_loop(tmp_path, {"KOKEN_RUNNER_MAX_CYCLES": "1"})
    assert r.returncode == 0
    args = (tmp_path / "docker.args").read_text()
    assert "run" in args
    assert "--rm" in args
    assert "REG_TOKEN=REG123" in args  # kısa-ömürlü token container'a geçti
    assert "--pull never" in args  # yerel image (registry-pull yok)


def test_loop_mint_failure_backs_off_no_spam(tmp_path):
    # curl başarısız (token boş) → docker ÇAĞRILMAZ, backoff log'lanır, MAX_CYCLES'te durur.
    _fake_bin(tmp_path / "bin", "curl", "exit 1\n")
    _fake_bin(tmp_path / "bin", "docker", f'echo called > "{tmp_path}/docker.called"\n')
    r = _run_loop(tmp_path, {"KOKEN_RUNNER_MAX_CYCLES": "2"})
    assert r.returncode == 0
    assert not (tmp_path / "docker.called").exists()  # mint-fail → container başlatılmadı
    log = (tmp_path / "run.log").read_text()
    assert log.count("backoff") >= 2  # her fail-cycle backoff (busy-spam değil)


def test_loop_pat_missing_is_failure_not_crash(tmp_path):
    # PAT dosyası yoksa mint fail → backoff (crash değil).
    _fake_bin(tmp_path / "bin", "curl", "exit 1\n")
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "KOKEN_RUNNER_PAT_FILE": str(tmp_path / "nope"),
        "KOKEN_RUNNER_LOG": str(tmp_path / "run.log"),
        "KOKEN_RUNNER_BACKOFF_BASE": "0",
        "KOKEN_RUNNER_MAX_CYCLES": "1",
    }
    r = subprocess.run(["bash", str(LOOP)], env=env, capture_output=True, text=True, timeout=15)
    assert r.returncode == 0  # crash yok
    assert "backoff" in (tmp_path / "run.log").read_text()
