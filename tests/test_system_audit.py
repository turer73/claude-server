"""Sistem-denetim endpoint'i testleri (Turgut 2026-07-18: 'tüm ajanlar ve sistemler izlensin').

Tasarım-ilkeleri test edilir: read-model agregasyon, son-görülme+durum birlikte,
cömert-eşik (uzak-cihazda kırmızı yok), hata-yollarının GÖRÜNÜR olması (sessiz-pass yok).
"""

import sqlite3

from app.api import agents
from app.api.agents import (
    _consciousness_card,
    _cron_jobs_sweep,
    _devices_activity,
    _iso_utc,
    _systemd_snapshot,
)


def test_iso_utc_normalizes_sqlite_space_format():
    # SQLite datetime('now') boşluk-ayıraç/tz'siz üretir — JS Date() bunu tarayıcı-lokaline göre
    # yorumlayıp 3-saat kaydırabilir; UTC-işaretli ISO'ya çevrilmeli.
    assert _iso_utc("2026-07-18 19:00:00") == "2026-07-18T19:00:00+00:00"
    assert _iso_utc("2026-07-18T19:00:00+03:00") == "2026-07-18T19:00:00+03:00"  # tz'li dokunma
    assert _iso_utc(None) is None
    assert _iso_utc("çöp-veri") == "çöp-veri"  # parse-fail → olduğu-gibi (veri-kaybı yok)


def test_cron_jobs_sweep_aggregates_per_job(tmp_path, monkeypatch):
    db = tmp_path / "srv.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    rows = [
        ("digest", "pass", "2026-07-18 08:00:00"),
        ("digest", "fail", "2026-07-17 08:00:00"),
        ("digest", "pass", "2026-07-16 08:00:00"),
        ("memory-synth", "partial", "2026-07-13 06:47:02"),
    ]
    for job, result, ts in rows:
        con.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES (?,?,?)", (job, result, ts))
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(db))

    jobs = {j["job"]: j for j in _cron_jobs_sweep()}
    assert set(jobs) == {"digest", "memory-synth"}
    # Manifest'ten BAĞIMSIZ sweep — wrapper'dan geçen her job otomatik kapsanır
    d = jobs["digest"]
    assert d["last_result"] == "pass"  # en-yeni satır
    assert d["last_run"].startswith("2026-07-18T08:00:00")  # ISO-normalize edilmiş
    assert d["ok_rate"] == round(2 / 3, 3)
    # 'partial' jenerik sweepte orana KATILMAZ (job'a göre başarı ya da kısmi-fail olabilir) —
    # ama son-sonuç ham gösterilir (panel sarı nokta basar, gizlenmez)
    m = jobs["memory-synth"]
    assert m["last_result"] == "partial"
    assert m["ok_rate"] == 0.0


def test_cron_jobs_sweep_missing_db_returns_empty(monkeypatch):
    monkeypatch.setattr(agents, "server_db_path", lambda: "/nonexistent/x.db")
    assert _cron_jobs_sweep() == []


def test_devices_activity_merges_freshest_and_includes_unregistered(tmp_path, monkeypatch):
    # disc#1351 dersi: 'surer' devices-tablosunda kayıtsızken bile iz bırakıyordu — kayıt-eksikliği
    # paneli KÖR bırakmamalı, cihaz 'kayitsiz' etiketiyle yine listelenmeli.
    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, platform TEXT, last_seen TEXT)")
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, device_name TEXT, created_at TEXT)")
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, from_device TEXT, created_at TEXT)")
    con.execute("CREATE TABLE tasks_log (id INTEGER PRIMARY KEY, device_name TEXT, created_at TEXT)")
    con.execute("INSERT INTO devices (name,platform,last_seen) VALUES ('klipper','linux','2026-01-01 00:00:00')")
    con.execute("INSERT INTO sessions (device_name,created_at) VALUES ('klipper',datetime('now','-2 hours'))")
    con.execute("INSERT INTO notes (from_device,created_at) VALUES ('klipper',datetime('now','-1 hours'))")
    # kayıtsız cihaz: yalnız notes'ta iz var
    con.execute("INSERT INTO notes (from_device,created_at) VALUES ('surer',datetime('now','-30 hours'))")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))

    devs = {d["name"]: d for d in _devices_activity()}
    assert set(devs) == {"klipper", "surer"}
    k = devs["klipper"]
    assert k["registered"] is True
    assert k["status"] == "aktif"  # en-taze iz (1sa önceki not) < 24sa
    assert k["last_activity"] is not None
    s = devs["surer"]
    assert s["registered"] is False  # devices'ta yok ama iz var → yine listede
    assert s["status"] == "sessiz"  # 24-72sa: SARI/normal-sınıf — kırmızı YOK (cömert-eşik)


def test_devices_activity_no_trace_is_long_silent(tmp_path, monkeypatch):
    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, platform TEXT, last_seen TEXT)")
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, device_name TEXT, created_at TEXT)")
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, from_device TEXT, created_at TEXT)")
    con.execute("CREATE TABLE tasks_log (id INTEGER PRIMARY KEY, device_name TEXT, created_at TEXT)")
    con.execute("INSERT INTO devices (name,platform,last_seen) VALUES ('android-telefon','android',NULL)")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))

    devs = _devices_activity()
    assert len(devs) == 1
    assert devs[0]["status"] == "uzun-sessiz"  # iz yok → gri (kırmızı DEĞİL)
    assert devs[0]["last_activity"] is None


class _FakeConsciousness:
    """Worker-lock nedeniyle bu worker'da running=False görünen stream."""

    @property
    def status(self):
        return {
            "running": False,
            "started_at": None,
            "thought_count": 42,
            "interval": 15,
            "last_thought": {"focus": "sistem"},
            "emotion": "calm",
        }


def test_consciousness_card_effective_running_from_thoughts(tmp_path, monkeypatch):
    # ÇİFT-WORKER görünürlük tuzağı: stream tek worker'da koşar; istek öbür worker'a düşerse
    # status.running=False → dashboard yanlış 'Durdu' basardı. thoughts tablosu worker-bağımsız
    # gerçek: taze düşünce (≤20dk) varsa FİİLEN canlı.
    db = tmp_path / "srv.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, timestamp TEXT, focus TEXT, emotion TEXT, content TEXT)")
    con.execute("INSERT INTO thoughts (timestamp) VALUES (datetime('now','-5 minutes'))")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(db))

    card = _consciousness_card(_FakeConsciousness())
    assert card["key"] == "consciousness"
    assert card["type"] == "continuous"
    assert card["running"] is True  # status=False AMA taze düşünce var → fiilen canlı
    assert card["last_run"] is not None
    assert card["stats"]["Düşünce (24sa)"] == 1
    assert card["triggerable"] is False


def test_consciousness_card_stale_thoughts_honest_stopped(tmp_path, monkeypatch):
    db = tmp_path / "srv.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, timestamp TEXT, focus TEXT, emotion TEXT, content TEXT)")
    con.execute("INSERT INTO thoughts (timestamp) VALUES (datetime('now','-3 hours'))")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(db))

    card = _consciousness_card(_FakeConsciousness())
    assert card["running"] is False  # bayat düşünce → dürüst 'Durdu' (uydurma-canlılık yok)


class _FakeCompleted:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_systemd_snapshot_maps_states(monkeypatch):
    states = "active\nactive\nactive\ninactive\nfailed\n"
    monkeypatch.setattr(agents.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout=states))
    snap = _systemd_snapshot()
    assert len(snap) == len(agents._AUDIT_UNITS)
    by_unit = {s["unit"]: s["state"] for s in snap}
    assert by_unit["linux-ai-server.service"] == "active"
    assert by_unit["klipper-note-poller.service"] == "inactive"
    assert by_unit["klipper-telegram-poller.service"] == "failed"


def test_systemd_snapshot_error_is_visible_not_silent(monkeypatch):
    # fail-safe-maskeler dersi: subprocess patlarsa 'error' GÖRÜNÜR olmalı, sessiz-pass değil.
    def _boom(*a, **k):
        raise OSError("systemctl yok")

    monkeypatch.setattr(agents.subprocess, "run", _boom)
    snap = _systemd_snapshot()
    assert all(s["state"] == "error" for s in snap)
    assert all("systemctl yok" in s.get("detail", "") for s in snap)


def test_systems_snapshot_error_paths_honest(tmp_path, monkeypatch):
    # Tüm dış-kontroller kapalıyken: 5 bileşen de listede, hepsi ok=False + detail dolu —
    # hiçbir bileşen sessizce listeden düşmez.
    monkeypatch.setattr(agents, "_http_check", lambda url, timeout=2.0: (False, "conn refused"))

    def _fail_run(*a, **k):
        raise OSError("cmd yok")

    monkeypatch.setattr(agents.subprocess, "run", _fail_run)
    monkeypatch.setattr(agents, "server_db_path", lambda: "/nonexistent/x.db")

    systems = {s["key"]: s for s in agents._systems_snapshot()}
    assert set(systems) == {"ollama", "qdrant", "docker", "vps", "eski-klipper"}
    assert all(not s["ok"] for s in systems.values())
    assert all(s["detail"] for s in systems.values())


def test_systems_snapshot_vps_from_last_probe_row(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_http_check", lambda url, timeout=2.0: (False, "x"))

    def _fail_run(*a, **k):
        raise OSError("cmd yok")

    monkeypatch.setattr(agents.subprocess, "run", _fail_run)
    db = tmp_path / "srv.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE vps_metrics_history (id INTEGER PRIMARY KEY, timestamp TEXT, online INTEGER, "
        "cpu_usage REAL, memory_usage REAL, disk_usage REAL, containers_total INTEGER, containers_up INTEGER)"
    )
    con.execute("INSERT INTO vps_metrics_history (timestamp,online,containers_total,containers_up) VALUES ('2026-07-18 19:00:00',1,21,21)")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(db))

    systems = {s["key"]: s for s in agents._systems_snapshot()}
    assert systems["vps"]["ok"] is True
    assert "21/21" in systems["vps"]["detail"]


def test_system_audit_route_registered_before_catch_all():
    # Regresyon-koruması: FastAPI kayıt-sırası önceliği — /system-audit catch-all /{name}'den
    # SONRA kaydedilirse /{name} onu ajan-adı sanıp yutar (endpoint hiç çalışmaz).
    paths = [getattr(r, "path", "") for r in agents.router.routes]
    audit_idx = paths.index("/api/v1/agents/system-audit")
    catch_idx = paths.index("/api/v1/agents/{name}")
    assert audit_idx < catch_idx, "system-audit route'u catch-all'dan ÖNCE kayıtlı olmalı"
