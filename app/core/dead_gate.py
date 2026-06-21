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

# Boolean-literal degerler: bir env bunlarla karsilastirilirsa (== "1" / != "0" /
# in ("1","true",...)) BOOLEAN-GATE'tir — isim-soneki ne olursa olsun. secret/path
# asla "1"/"true" ile karsilastirilmaz -> FP-safe. Gate-sonek-siz / _KEY-disli gate'leri
# yakalar (SSH_STRICT_HOST_KEY, klipper #100095 / Codex #175 ssh_client:58).
BOOL_LITERALS: frozenset[str] = frozenset({"0", "1", "true", "false", "yes", "no", "on", "off"})


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


def _is_boolish(node: ast.expr) -> bool:
    """Boolean-literal Constant ("1"/"true"/"on"...) VEYA bool-literal koleksiyonu
    (in ("1", "true") gibi Tuple/List/Set). Case-insensitive."""
    if isinstance(node, ast.Constant):
        return str(node.value).lower() in BOOL_LITERALS
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(isinstance(e, ast.Constant) and str(e.value).lower() in BOOL_LITERALS for e in node.elts)
    return False


def _env_read_in_subtree(node: ast.AST) -> str | None:
    """Subtree'deki ilk os.environ.get/getenv/environ[] okumasinin ismini bul.
    `get("X", "").strip().lower()` gibi sarmalanmis env-okumalari icin (AXIS 2)."""
    for child in ast.walk(node):
        name = _env_read_name(child)
        if name is not None:
            return name
    return None


class _Scanner(ast.NodeVisitor):
    """os.environ.get boolean-gate okumalarini IKI eksende yakalar (read_env_var kullanilmali).

    AXIS 1 (isim-bazli, klipper #100091): gate-isimli (is_gate_name) HER os.environ.get/
      getenv/environ[] okumasi -> kullanim-formuna BAKILMAZ. 6 yaygin formu kapsar:
      truthy `if get("X_ENABLED")`, `not`, `in ("1","true")`, `.strip().lower() not in (...)`
      [codebase idiom], `== "on"`, `bool(...)`. read_env_var kasitli yakalanmaz.
    AXIS 2 (kullanim-bazli, klipper #100095 / Codex #175): bir env BOOL-LITERAL ile
      karsilastiriliyorsa (== "1" / != "0" / in ("1","true","yes")) -> isim-soneki
      FARKETMEZ. _KEY/_HOST gibi sonek-disli ama gate-olan env'leri yakalar
      (SSH_STRICT_HOST_KEY). FP-safe: secret/path asla "1"/"true" ile karsilastirilmaz.
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []

    def _add(self, lineno: int, name: str) -> None:
        if (lineno, name) not in self.found:
            self.found.append((lineno, name))

    def _check_name(self, node: ast.expr) -> None:
        name = _env_read_name(node)
        if name and is_gate_name(name):
            self._add(node.lineno, name)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_name(node)  # AXIS 1
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._check_name(node)  # AXIS 1
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # AXIS 2: operandlardan biri bool-literal ise, diger operand-subtree'deki env-okumasi
        # gate'tir. is_gate_name'ler AXIS 1'de alindi -> burada yalniz sonek-disli olanlar.
        operands: list[ast.expr] = [node.left, *node.comparators]
        if any(_is_boolish(op) for op in operands):
            for op in operands:
                if _is_boolish(op):
                    continue
                name = _env_read_in_subtree(op)
                if name and not is_gate_name(name):
                    self._add(node.lineno, name)
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


def _env_file_keys(env_file: str) -> set[str]:
    """`.env`'deki TUM KEY'leri dondur (gate-filtresi YOK; yorum/bos satir atlanir).

    is_gate_name burada UYGULANMAZ — gate-karari scanner'a (AXIS-1 isim + AXIS-2
    bool-usage) birakilir. Aksi halde _KEY-sonekli AXIS-2 gate'leri (SSH_STRICT_HOST_KEY)
    env-tarafinda elenir, runtime backstop onlari kacirir (klipper #100103 / Codex L227).
    """
    keys: set[str] = set()
    if not os.path.exists(env_file):
        return keys
    try:
        with open(env_file) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                keys.add(line.split("=", 1)[0].strip())
    except OSError:
        return set()
    return keys


def audit_runtime_dead_gates(env_file: str, source_roots: Iterable[str | Path]) -> list[DeadGate]:
    """Boot-time aktif-olu gate tespiti (T1'i kacirani yakalar — savunma-derinligi).

    Olu kosulu (UCU birden): key `.env`'de tanimli + process-env'de YOK + kodda
    gate-reader'i var (scanner AXIS-1 isim VEYA AXIS-2 bool-usage). Gate-karari
    SCANNER'a birakilir (env-tarafinda is_gate_name ON-SUZGECI YOK -> AXIS-2
    _KEY-sonekli gate'ler de yakalanir; klipper #100103). Yaygin durum (`.env`'de
    candidate-key yok / hepsi process-env'de) -> kaynak-tarama HIC calismaz.
    """
    # candidate = .env'de tanimli ama process-env'de YOK (systemd .env'i gecirmiyorsa hepsi).
    candidates = {k for k in _env_file_keys(env_file) if k not in os.environ}
    if not candidates:
        return []
    # olu = candidate ∩ scanner-reader (AXIS-1+AXIS-2 gate-kararini zaten kodluyor).
    dead: list[DeadGate] = []
    seen: set[str] = set()
    for v in scan_source_for_dead_gates(source_roots):
        if v.name in candidates and v.name not in seen:
            seen.add(v.name)
            dead.append(DeadGate(name=v.name, reader=f"{v.file}:{v.line}"))
    return dead
