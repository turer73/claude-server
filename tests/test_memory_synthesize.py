"""scripts/memory-synthesize.py — hafıza sentezi (LIVESYS-MEMSYN).

Saf çekirdek (cosine/cluster/canonical) + DB davranışı: DRY_RUN yazma-YOK, APPLY
arşivler ama SİLMEZ (NO-DELETE), schema idempotent. embed() monkeypatch'lenir (Ollama'sız).
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("memsyn", ROOT / "scripts" / "memory-synthesize.py")
memsyn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memsyn)


def test_cosine_identical_orthogonal():
    assert memsyn.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert memsyn.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert memsyn.cosine([1.0], [1.0, 2.0]) == 0.0  # boyut uyuşmazlığı → 0


def test_cluster_groups_similar_excludes_singletons():
    ids = [10, 11, 12]
    vecs = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]  # 10&11 yakın, 12 uzak
    clusters = memsyn.cluster(ids, vecs, threshold=0.86)
    assert clusters == [[10, 11]]  # yalnız boyut≥2; tekil 12 dışlandı


def test_pick_canonical_longest_then_readcount():
    members = [
        {"id": 1, "content": "kısa", "read_count": 9},
        {"id": 2, "content": "çok daha uzun içerik buraya", "read_count": 0},
    ]
    assert memsyn.pick_canonical(members) == 2  # en uzun içerik kazanır


def _mkdb(tmp_path, rows) -> Path:
    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, type TEXT, name TEXT, description TEXT, "
        "content TEXT, created_at TEXT, updated_at TEXT, active INTEGER DEFAULT 1, read_count INTEGER DEFAULT 0)"
    )
    for r in rows:
        con.execute(
            "INSERT INTO memories (id, type, name, description, content, active) VALUES (?,?,?,?,?,1)",
            r,
        )
    con.commit()
    con.close()
    return db


_ROWS = [
    (1, "project", "a", "d", "içerik bir uzun uzun"),
    (2, "project", "b", "d", "içerik iki"),
    (3, "project", "c", "d", "tamamen farklı konu"),
]


def test_ensure_schema_adds_merged_into(tmp_path):
    db = _mkdb(tmp_path, _ROWS)
    con = sqlite3.connect(db)
    memsyn._ensure_schema(con)
    cols = [r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()]
    con.close()
    assert "merged_into" in cols


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    db = _mkdb(tmp_path, _ROWS)
    monkeypatch.setattr(memsyn, "DB_PATH", str(db))
    monkeypatch.setattr(memsyn, "APPLY", False)
    # 1&2 aynı vektör (kümelenir), 3 farklı
    monkeypatch.setattr(memsyn, "embed", lambda texts: [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    res = memsyn.synthesize()
    assert res["clusters"] == 1
    assert res["archived"] == 0  # DRY_RUN: yazma yok
    con = sqlite3.connect(db)
    active = con.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0]
    con.close()
    assert active == 3  # hiçbiri arşivlenmedi


def test_apply_archives_but_no_delete(tmp_path, monkeypatch):
    db = _mkdb(tmp_path, _ROWS)
    monkeypatch.setattr(memsyn, "DB_PATH", str(db))
    monkeypatch.setattr(memsyn, "APPLY", True)
    monkeypatch.setattr(memsyn, "embed", lambda texts: [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    res = memsyn.synthesize()
    assert res["clusters"] == 1
    assert res["archived"] == 1  # kümede 2 üye → 1 canonical + 1 arşiv
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    archived = con.execute("SELECT COUNT(*) FROM memories WHERE active=0 AND merged_into IS NOT NULL").fetchone()[0]
    con.close()
    assert total == 3  # NO-DELETE: satır sayısı değişmedi
    assert archived == 1  # soft-archive + merged_into izi


def test_apply_is_idempotent_second_run_noop(tmp_path, monkeypatch):
    db = _mkdb(tmp_path, _ROWS)
    monkeypatch.setattr(memsyn, "DB_PATH", str(db))
    monkeypatch.setattr(memsyn, "APPLY", True)
    # içerik-bazlı: 'tamamen' (#3) ayrı vektör, diğerleri (1&2) aynı → deterministik her koşuda
    monkeypatch.setattr(memsyn, "embed", lambda texts: [[0.0, 1.0] if "tamamen" in t else [1.0, 0.0] for t in texts])
    res1 = memsyn.synthesize()
    assert res1["archived"] == 1  # 1&2 kümelendi
    # ikinci koşu: arşivlenen (active=0+merged_into) dışlanır → kalan canonical+#3 farklı → yeni arşiv yok
    res2 = memsyn.synthesize()
    assert res2["archived"] == 0
