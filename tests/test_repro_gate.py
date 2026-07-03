"""G1 repro-gate.sh parse-guard'ları (git-overlay yolu ilk-gerçek-kullanımda entegrasyon-test).

Bu test'in kendisi G1'in dogfood'u: repro-gate script'inin parse-mantığını doğrular.
"""
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "repro-gate.sh"


def _run(pr_body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PR_BODY": pr_body, "BASE_SHA": "", "HEAD_SHA": "", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_missing_repro_line_fails():
    r = _run("## Özet\nbir şey yaptım\n")
    assert r.returncode == 1
    assert "Repro-Test" in (r.stdout + r.stderr)


def test_na_skips():
    r = _run("## Özet\nfoo\nRepro-Test: N/A — docs-only\n")
    assert r.returncode == 0
    assert "atlandı" in r.stdout


def test_na_slash_variant_skips():
    r = _run("Repro-Test: n/a saf-refactor\n")
    assert r.returncode == 0


def test_nontest_path_rejected():
    # tests/ altında olmayan repro -> reddedilir (BASE/HEAD boş olsa da path-guard önce çalışır)
    r = _run("Repro-Test: app/foo.py::test_x\n")
    assert r.returncode == 1
    assert "tests/" in (r.stdout + r.stderr)
