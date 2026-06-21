"""Dead-gate guard — `.env`'de tanimli ama `os.environ.get` ile okunan boolean
feature-gate'leri yakalar.

Kok-neden: systemd unit `.env`'i `EnvironmentFile=` ile process-env'e gecirmiyor
(bkz `read_env_var`, app/core/config.py). Bu yuzden bir gate `os.environ.get` ile
okununca, operator `.env`'e `X=0` yazsa bile `os.environ` bos doner -> default'a
duser -> dokumanli kill-switch SERVISTE SESSIZCE no-op olur. Lokal testler
`monkeypatch.setenv` ile gecer (os.environ dolu) -> "lokal-gecer / serviste-olu"
imzasi. Bu #174 sinifi (SIGNAL_SEMANTIC_DEDUP).

Iki tuketici (savunma-derinligi):
- tests/test_dead_gate_guard.py — T1 static-lint (CI gate): kaynak-tarama, sifir-ihlal.
- app/main.py lifespan — boot-config-log: runtime, T1'i kacirani yakalar (gate `.env`'de
  var + process-env'de yok + os.environ.get-reader var -> aktif-olu).

Heuristic (klipper Q2): allowlist DEGIL -> gelecekteki gate'i oto-yakalar, kendini
genisletir. gate-sonek ICERIR + path/secret-sonek ile BITMEZ.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Boolean feature-gate sonekleri (ICERIR). CI_SIGNAL_DEDUP_ENABLED, X_GATE, ...
GATE_SUFFIXES: tuple[str, ...] = ("_ENABLED", "_DEDUP", "_GATE", "_FLAG", "_ON", "_DISABLED")

# Path/secret/baglanti env'leri (BITMEZ) — bunlar mesru os.environ.get kullanir
# (RAG_METRICS_DB, DB_PATH, OLLAMA_URL, GITHUB_TOKEN, ...). FP'yi onler.
PATH_SUFFIXES: tuple[str, ...] = (
    "_PATH",
    "_URL",
    "_DB",
    "_KEY",
    "_TOKEN",
    "_DIR",
    "_FILE",
    "_HOST",
    "_PORT",
    "_SECRET",
)

# Boolean-kullanim isareti: env-okuma bu literallerden biriyle karsilastiriliyorsa
# (!= "0" / == "1" / == "true" ...) bu bir gate'tir (path/sayisal-deger degil).
BOOL_LITERALS: frozenset[str] = frozenset({"0", "1", "true", "false", "True", "False", "yes", "no"})


def is_gate_name(name: str) -> bool:
    """Boolean feature-gate ismi mi? Path/secret env'lerini dislar.

    >>> is_gate_name("CI_SIGNAL_DEDUP_ENABLED")
    True
    >>> is_gate_name("RAG_METRICS_DB")  # path-sonek -> haric
    False
    """
    if any(name.endswith(p) for p in PATH_SUFFIXES):
        return False
    return any(s in name for s in GATE_SUFFIXES)


@dataclass(frozen=True)
class Violation:
    """T1 ihlali: os.environ.get ile okunan boolean gate (read_env_var kullanmali)."""

    file: str
    line: int
    name: str
    snippet: str


@dataclass(frozen=True)
class DeadGate:
    """Runtime aktif-olu gate: `.env`'de var, process-env'de yok, os.environ.get-reader var."""

    name: str
    reader: str  # "dosya:satir"


def _is_os_environ(node: ast.AST) -> bool:
    """node `os.environ` mi? (Attribute(value=Name('os'), attr='environ'))."""
    return isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"


def _env_read_name(node: ast.AST) -> str | None:
    """node bir env-okuma ise okunan string-literal ismi dondur, degilse None.

    Yakalanan formlar: os.environ.get("X"...) · os.getenv("X"...) · os.environ["X"].
    read_env_var(...) KASITLI yakalanmaz (guvenli yol).
    """
    if isinstance(node, ast.Call) and node.args:
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            func = node.func
            if isinstance(func, ast.Attribute):
                # os.environ.get("X")
                if func.attr == "get" and _is_os_environ(func.value):
                    return arg0.value
                # os.getenv("X")
                if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
                    return arg0.value
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return sl.value
    return None


def _is_bool_literal(node: ast.AST) -> bool:
    """node "0"/"1"/"true"/... gibi boolean-literal Constant mi? (int 0/1 dahil)."""
    return isinstance(node, ast.Constant) and str(node.value) in BOOL_LITERALS


class _Scanner(ast.NodeVisitor):
    """Compare node'larinda gate-env-okuma + boolean-literal cifti arar."""

    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []

    def visit_Compare(self, node: ast.Compare) -> None:
        operands: list[ast.expr] = [node.left, *node.comparators]
        gate_name: str | None = None
        for op in operands:
            name = _env_read_name(op)
            if name and is_gate_name(name):
                gate_name = name
                break
        if gate_name and any(_is_bool_literal(op) for op in operands):
            self.found.append((node.lineno, gate_name))
        self.generic_visit(node)


def _scan_text(src: str) -> list[tuple[int, str]]:
    """Kaynak metni tara; (satir, gate-ismi) listesi dondur. Parse hatasi -> []."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    scanner = _Scanner()
    scanner.visit(tree)
    return scanner.found


def scan_source_for_dead_gates(roots: Iterable[str | Path]) -> list[Violation]:
    """roots altindaki tum .py'leri tara; os.environ.get boolean-gate ihlallerini dondur."""
    violations: list[Violation] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for py in sorted(root_path.rglob("*.py")):
            try:
                src = py.read_text(encoding="utf-8")
            except OSError:
                continue
            hits = _scan_text(src)
            if not hits:
                continue
            lines = src.splitlines()
            for lineno, name in hits:
                snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
                violations.append(Violation(str(py), lineno, name, snippet[:160]))
    return violations


def _gate_keys_in_env_file(env_file: str) -> dict[str, str]:
    """`.env`'deki gate-isimli KEY=VALUE ciftlerini dondur (yorum/bos satir atlanir)."""
    out: dict[str, str] = {}
    if not os.path.exists(env_file):
        return out
    try:
        with open(env_file) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if is_gate_name(key):
                    out[key] = val.strip()
    except OSError:
        return {}
    return out


def audit_runtime_dead_gates(env_file: str, source_roots: Iterable[str | Path]) -> list[DeadGate]:
    """Boot-time aktif-olu gate tespiti (T1'i kacirani yakalar — savunma-derinligi).

    Olu kosulu (UCU birden): gate `.env`'de tanimli + process-env'de YOK +
    kodda os.environ.get-reader'i var. Yaygin durum (`.env`'de gate-key yok) ->
    kaynak-tarama HIC calismaz (sifir-maliyet startup).
    """
    env_gates = _gate_keys_in_env_file(env_file)
    suspects = [k for k in env_gates if k not in os.environ]
    if not suspects:
        return []
    readers: dict[str, str] = {}
    for v in scan_source_for_dead_gates(source_roots):
        readers.setdefault(v.name, f"{v.file}:{v.line}")
    return [DeadGate(name=k, reader=readers[k]) for k in suspects if k in readers]
