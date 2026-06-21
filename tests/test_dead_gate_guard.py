"""T1 static-lint (CI gate) + dead-gate heuristic birim-testleri.

Ana test: production kod (app/ + automation/) icinde `os.environ.get` ile okunan
boolean feature-gate OLMAMALI -> read_env_var kullanilmali. Aksi halde gate serviste
sessizce no-op olur (systemd `.env`'i process-env'e gecirmiyor; #174 sinifi).

Geri kalan testler heuristic'in FP/FN'ini kilitler (gelecekte gevsetilmesin diye).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.dead_gate import (
    audit_runtime_dead_gates,
    is_gate_name,
    scan_source_for_dead_gates,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_dead_gate_in_production_code() -> None:
    """app/ + automation/ icinde sifir dead-gate ihlali (CI gate)."""
    roots = [_REPO_ROOT / "app", _REPO_ROOT / "automation"]
    violations = scan_source_for_dead_gates(roots)
    assert violations == [], "Dead-gate ihlal(ler)i bulundu — os.environ.get yerine read_env_var kullan:\n" + "\n".join(
        f"  {v.file}:{v.line}  {v.name}\n      {v.snippet}" for v in violations
    )


@pytest.mark.parametrize(
    "name",
    ["CI_SIGNAL_DEDUP_ENABLED", "SIGNAL_SEMANTIC_DEDUP", "CODE_REVIEW_ENABLED", "FOO_GATE", "X_FLAG", "Y_ON", "Z_DISABLED"],
)
def test_is_gate_name_positive(name: str) -> None:
    assert is_gate_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["RAG_METRICS_DB", "DB_PATH", "OLLAMA_URL", "GITHUB_TOKEN", "CLAUDE_TG_CWD", "DEFAULT_API_KEY", "LOG_DIR", "DATA_FILE"],
)
def test_is_gate_name_excludes_path_secret(name: str) -> None:
    assert is_gate_name(name) is False


def _write_py(tmp_path: Path, body: str) -> None:
    (tmp_path / "snippet.py").write_text("import os\n\n" + body, encoding="utf-8")


def test_scan_flags_os_environ_get_bool_gate(tmp_path: Path) -> None:
    _write_py(tmp_path, 'def f():\n    return os.environ.get("X_ENABLED", "1") != "0"\n')
    violations = scan_source_for_dead_gates([tmp_path])
    assert len(violations) == 1
    assert violations[0].name == "X_ENABLED"


def test_scan_flags_getenv_and_subscript(tmp_path: Path) -> None:
    _write_py(
        tmp_path,
        'def f():\n    a = os.getenv("A_GATE") == "1"\n    b = os.environ["B_FLAG"] == "true"\n    return a or b\n',
    )
    names = {v.name for v in scan_source_for_dead_gates([tmp_path])}
    assert names == {"A_GATE", "B_FLAG"}


def test_scan_ignores_read_env_var(tmp_path: Path) -> None:
    (tmp_path / "safe.py").write_text(
        'from app.core.config import read_env_var\n\ndef f():\n    return (read_env_var("X_ENABLED") or "1") != "0"\n',
        encoding="utf-8",
    )
    assert scan_source_for_dead_gates([tmp_path]) == []


def test_scan_excludes_path_and_secret_suffix(tmp_path: Path) -> None:
    """Path/secret-isimli os.environ.get FP YARATMAZ (gate-sonek yok / path-sonek var)."""
    _write_py(
        tmp_path,
        "def f():\n"
        '    a = os.environ.get("RAG_METRICS_DB", "/x")\n'
        '    b = os.environ.get("DB_PATH")\n'
        '    c = os.environ.get("GITHUB_TOKEN")\n'
        '    d = os.environ.get("OLLAMA_URL")\n'
        "    return a, b, c, d\n",
    )
    assert scan_source_for_dead_gates([tmp_path]) == []


def test_scan_flags_six_common_fn_patterns(tmp_path: Path) -> None:
    """Klipper #100091: karsilastirma-sarti OLMADAN 6 yaygin gate-formu da yakalanir.

    Eski Compare-only scanner bunlari kaciriyordu (yuksek-FN). idiom(4) bu codebase'in
    kendi gate-deseni (CODE_REVIEW_ENABLED tipi) — read_env_var ile guvenli, ama
    os.environ.get ile yazilirsa olu olur, yakalanmali.
    """
    _write_py(
        tmp_path,
        "def f1():\n"
        '    if os.environ.get("A_ENABLED"):\n'  # truthy (en yaygin)
        "        return 1\n"
        "def f2():\n"
        '    if not os.environ.get("B_GATE"):\n'
        "        return 1\n"
        "def f3():\n"
        '    return os.environ.get("C_FLAG") in ("1", "true")\n'
        "def f4():\n"
        '    return os.environ.get("D_ENABLED", "1").strip().lower() not in ("0", "false")\n'
        "def f5():\n"
        '    return os.environ.get("E_ON", "on") == "on"\n'
        "def f6():\n"
        '    return bool(os.environ.get("F_ENABLED"))\n',
    )
    names = sorted(v.name for v in scan_source_for_dead_gates([tmp_path]))
    assert names == ["A_ENABLED", "B_GATE", "C_FLAG", "D_ENABLED", "E_ON", "F_ENABLED"]


def test_scan_axis2_bool_literal_usage_despite_secret_suffix(tmp_path: Path) -> None:
    """AXIS-2 (klipper #100095 / Codex #175): bool-literal ile karsilastirilan env, isim
    _KEY/_HOST gibi sonek-disli olsa bile gate'tir (SSH_STRICT_HOST_KEY). is_gate_name'in
    _KEY-dislamasi bunu AXIS-1'de kacirir; AXIS-2 (kullanim-bazli) yakalar."""
    _write_py(
        tmp_path,
        "def f():\n"
        '    strict = os.environ.get("SSH_STRICT_HOST_KEY", "").strip().lower() in ("1", "true", "yes")\n'
        '    lvl = os.environ.get("LOG_VERBOSE") == "1"\n'
        "    return strict, lvl\n",
    )
    names = sorted(v.name for v in scan_source_for_dead_gates([tmp_path]))
    assert names == ["LOG_VERBOSE", "SSH_STRICT_HOST_KEY"]


def test_scan_axis2_fp_safe_secret_presence_and_nonbool(tmp_path: Path) -> None:
    """AXIS-2 FP-safe: secret presence-check (truthy, bool-literal yok) ve non-bool
    karsilastirma (== "sk-xyz") flaglenmez. secret/path asla "1"/"true" ile karsilastirilmaz."""
    _write_py(
        tmp_path,
        "def f():\n"
        '    if os.environ.get("GITHUB_TOKEN"):\n'
        "        pass\n"
        '    mode = os.environ.get("API_KEY") == "sk-xyz"\n'
        '    db = os.environ.get("DB_PATH", "/x")\n'
        "    return mode, db\n",
    )
    assert scan_source_for_dead_gates([tmp_path]) == []


def test_audit_runtime_detects_dead_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text(
        'import os\n\ndef f():\n    return os.environ.get("X_ENABLED", "1") != "0"\n',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("X_ENABLED=0\n", encoding="utf-8")
    monkeypatch.delenv("X_ENABLED", raising=False)  # process-env'de YOK -> olu
    dead = audit_runtime_dead_gates(str(env_file), [src])
    assert len(dead) == 1
    assert dead[0].name == "X_ENABLED"
    assert "m.py" in dead[0].reader


def test_audit_runtime_clean_when_gate_in_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text(
        'import os\n\ndef f():\n    return os.environ.get("X_ENABLED", "1") != "0"\n',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("X_ENABLED=0\n", encoding="utf-8")
    monkeypatch.setenv("X_ENABLED", "0")  # process-env'de VAR -> olu degil
    assert audit_runtime_dead_gates(str(env_file), [src]) == []


def test_audit_runtime_clean_when_no_env_gate(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text(
        'import os\n\ndef f():\n    return os.environ.get("X_ENABLED", "1") != "0"\n',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_PATH=/x\n", encoding="utf-8")  # SOME_PATH gate-reader degil -> intersection bos
    assert audit_runtime_dead_gates(str(env_file), [src]) == []


def test_audit_runtime_detects_axis2_key_suffix_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Klipper #100103 / Codex L227 regresyon-kaniti: _KEY-sonekli AXIS-2 bool-gate
    (.env'de var + process-env'de yok + os.environ.get+bool-compare reader) runtime
    audit'te YAKALANMALI. Eski is_gate_name env-on-suzgeci bunu 0 donduruyordu (asimetri:
    statik AXIS-2 yakaliyor ama runtime backstop kaciriyordu)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text(
        'import os\n\ndef f():\n    return os.environ.get("SSH_STRICT_HOST_KEY", "").strip().lower() in ("1", "true")\n',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("SSH_STRICT_HOST_KEY=1\n", encoding="utf-8")
    monkeypatch.delenv("SSH_STRICT_HOST_KEY", raising=False)  # process-env'de YOK -> olu
    dead = audit_runtime_dead_gates(str(env_file), [src])
    assert len(dead) == 1
    assert dead[0].name == "SSH_STRICT_HOST_KEY"


def test_scan_excludes_heavy_dirs(tmp_path: Path) -> None:
    """klipper #100114 / 88C-runaway: scan_source venv/site-packages/__pycache__ vb.
    ATLAR (rglob bunlari yuruyup 25dk core-pin yapiyordu). Bu dizinlerdeki .py taranmaz."""
    (tmp_path / "app.py").write_text(
        'import os\n\ndef f():\n    return os.environ.get("X_ENABLED") != "0"\n',
        encoding="utf-8",
    )
    heavy = tmp_path / ".venv" / "lib" / "site-packages"
    heavy.mkdir(parents=True)
    (heavy / "evil.py").write_text(
        'import os\n\ndef g():\n    return os.environ.get("Y_ENABLED") != "0"\n',
        encoding="utf-8",
    )
    names = sorted(v.name for v in scan_source_for_dead_gates([tmp_path]))
    assert names == ["X_ENABLED"]  # Y_ENABLED (.venv/site-packages) ATLANDI
