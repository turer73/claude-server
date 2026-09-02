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
    cols = [r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()]
    con.close()
    assert active == 3  # hiçbiri arşivlenmedi
    assert "merged_into" not in cols  # Codex P2: DRY_RUN ŞEMAYI da MUTATE ETMEZ


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


def test_min_cluster_skips_small_on_apply(tmp_path, monkeypatch):
    # MEMSYN_MIN_CLUSTER=3 → yalnız ≥3-üye kümeler APPLY; 2-üye küme atlanır (surer staged-apply)
    rows = [
        (1, "project", "a", "d", "x"),
        (2, "project", "b", "d", "x"),
        (3, "project", "c", "d", "x"),  # 3-üye küme (vektör A)
        (4, "project", "d", "d", "y"),
        (5, "project", "e", "d", "y"),  # 2-üye küme (vektör B)
    ]
    db = _mkdb(tmp_path, rows)
    monkeypatch.setattr(memsyn, "DB_PATH", str(db))
    monkeypatch.setattr(memsyn, "APPLY", True)
    monkeypatch.setattr(memsyn, "MIN_CLUSTER", 3)
    monkeypatch.setattr(memsyn, "embed", lambda texts: [[1.0, 0.0]] * 3 + [[0.0, 1.0]] * 2)
    res = memsyn.synthesize()
    assert res["clusters"] == 2
    assert res["archived"] == 2  # yalnız 3-üye kümeden 2 arşiv
    assert res["skipped_small"] == 1  # 2-üye küme atlandı
    con = sqlite3.connect(db)
    active45 = con.execute("SELECT COUNT(*) FROM memories WHERE id IN (4,5) AND active=1").fetchone()[0]
    con.close()
    assert active45 == 2  # 2-üye küme dokunulmadı


# --- memsyn-bak: tutarli kopya + retention (2026-09-02) ---
#
# Iki ayri kusur vardi: (1) kopya canli WAL DB'den `shutil.copy2` ile ham
# aliniyordu, yani `-wal`'siz TUTARSIZ olabiliyordu; (2) HIC temizlik yoktu —
# 12 dosya / 7,4 GB birikmisti ve her gece yedege giriyordu.


def _make_wal_db_with_uncheckpointed_rows(path):
    """Verisi WAL'de duran (henuz checkpoint edilmemis) canli DB.

    Baglanti ACIK birakilir: kapanista checkpoint tetiklenmesin. Ham dosya
    kopyasi bu satirlari GORMEZ, SQLite backup API gorur.
    """
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(25)])
    con.commit()
    return con  # acik birakiliyor


def test_backup_db_captures_wal_content_unlike_raw_copy(tmp_path, monkeypatch):
    """Kopya WAL'deki satirlari da icermeli — ham dosya kopyasi icermezdi."""
    import shutil

    db = tmp_path / "claude_memory.db"
    con = _make_wal_db_with_uncheckpointed_rows(db)
    try:
        assert (tmp_path / "claude_memory.db-wal").stat().st_size > 0, "WAL bos, test anlamsiz"

        # Kontrol: ESKI davranis (ham kopya) satirlari KACIRIR.
        raw = tmp_path / "raw-copy.db"
        shutil.copy2(db, raw)
        raw_con = sqlite3.connect(f"file:{raw}?mode=ro", uri=True)
        try:
            raw_rows = raw_con.execute("SELECT count(*) FROM t").fetchone()[0]
        except sqlite3.DatabaseError:
            raw_rows = -1  # tablo bile gorunmuyor
        finally:
            raw_con.close()
        assert raw_rows != 25, "ham kopya WAL'i gormus — test ayirt edici degil"

        # YENI davranis: SQLite backup API tam icerigi alir.
        monkeypatch.setattr(memsyn, "DB_PATH", str(db))
        dst = memsyn._backup_db()

        chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            assert chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert chk.execute("SELECT count(*) FROM t").fetchone()[0] == 25
        finally:
            chk.close()
    finally:
        con.close()


def _touch_baks(tmp_path, stamps):
    db = tmp_path / "claude_memory.db"
    db.write_bytes(b"SQLite format 3\x00")
    for s in stamps:
        (tmp_path / f"claude_memory.db.memsyn-bak.{s}").write_text("eski kopya")
    return db


def test_prune_keeps_only_newest_n(tmp_path, monkeypatch):
    """En yeni N kopya kalir, eskiler budanir."""
    db = _touch_baks(tmp_path, [100, 200, 300, 400, 500])
    monkeypatch.setattr(memsyn, "DB_PATH", str(db))
    monkeypatch.setattr(memsyn, "MEMSYN_KEEP", 2)

    assert memsyn._prune_backups() == 3

    kalan = sorted(p.name for p in tmp_path.glob("*.memsyn-bak.*"))
    assert kalan == [
        "claude_memory.db.memsyn-bak.400",
        "claude_memory.db.memsyn-bak.500",
    ], kalan


def test_prune_never_touches_foreign_files(tmp_path, monkeypatch):
    """Desen DAR: yabanci dosyalar ve canli DB asla silinmez."""
    db = _touch_baks(tmp_path, [100, 200, 300])
    (tmp_path / "claude_memory.db.memsyn-bak.ELDE-TUTULACAK").write_text("sayisal degil")
    (tmp_path / "server.db.memsyn-bak.100").write_text("baska DB")
    (tmp_path / "claude_memory.db-wal").write_text("sidecar")
    monkeypatch.setattr(memsyn, "DB_PATH", str(db))
    monkeypatch.setattr(memsyn, "MEMSYN_KEEP", 1)

    memsyn._prune_backups()

    assert db.exists(), "canli DB silindi!"
    assert (tmp_path / "claude_memory.db.memsyn-bak.ELDE-TUTULACAK").exists()
    assert (tmp_path / "server.db.memsyn-bak.100").exists()
    assert (tmp_path / "claude_memory.db-wal").exists()
    assert (tmp_path / "claude_memory.db.memsyn-bak.300").exists()  # en yeni korunur
