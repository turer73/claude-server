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


def test_scan_ignores_path_suffix_and_non_bool_compare(tmp_path: Path) -> None:
    _write_py(
        tmp_path,
        "def f():\n"
        '    p = os.environ.get("RAG_METRICS_DB", "/x")\n'  # path-sonek -> haric
        '    q = os.environ.get("X_ENABLED", "v") == "verbose"\n'  # bool-literal degil
        "    return p, q\n",
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
    env_file.write_text("SOME_PATH=/x\n", encoding="utf-8")  # gate-key yok -> kaynak-tarama bile yok
    assert audit_runtime_dead_gates(str(env_file), [src]) == []
