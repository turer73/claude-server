"""Ajan-runtime dashboard endpoint testleri — last-run/iş/bulgu/model/başarı kartları."""

from app.api.agents import (
    _AGENT_MANIFEST,
    _CRON_SCRIPTS,
    _codereview_card,
    _cron_card,
    _devops_card,
    _research_card,
    _sev_from_details,
)


def test_manifest_covers_decision_agents():
    keys = {a["key"] for a in _AGENT_MANIFEST}
    # SEO/ads/data-analiz/research/memory ajanları manifeste dahil mi
    for k in ("research", "ad-advisor", "data-analyst", "seo-gsc", "memory-triage", "autonomous-daily-summary"):
        assert k in keys, f"{k} manifeste eksik"
    # cron ajanları allowlist'li script'e sahip (manuel-tetikleme güvenliği) — triggerable=False
    # olarak işaretlenenler İSTİSNA (Codex #328-P2: require_admin-korumalı/uzun-süren script'ler
    # generic require_write trigger'ından kasıtlı dışlanır, bkz ci-fix-runall/self-pentest).
    for a in _AGENT_MANIFEST:
        if a["type"] == "cron" and a.get("triggerable", True):
            assert a["key"] in _CRON_SCRIPTS
    for a in _AGENT_MANIFEST:
        if a.get("triggerable") is False:
            assert a["key"] not in _CRON_SCRIPTS, f"{a['key']} triggerable=False ama _CRON_SCRIPTS'te — sunucu-taraflı gate delinmiş"


def test_cron_card_no_log_no_events():
    spec = {"key": "x", "name": "X", "role": "r", "schedule": "günlük", "models": ["m"], "log": None, "evsrc": None}
    card = _cron_card(spec)
    assert card["type"] == "cron"
    assert card["last_run"] is None  # log+event yok → dürüst None (uydurma yok)
    assert card["running"] is False
    assert card["triggerable"] is True


def test_cron_card_success_rate_from_outcomes(tmp_path, monkeypatch):
    """cron-kart success_rate + son-koşu cron_outcomes'tan gelir (hardcoded None DEĞİL — 'süs' algısı fix)."""
    import sqlite3

    from app.api import agents

    db = tmp_path / "srv.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    for r in ("pass", "pass", "fail", "pass"):
        con.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('data-analyst',?,datetime('now'))", (r,))
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(db))
    spec = {"key": "data-analyst", "name": "Veri", "role": "r", "schedule": "haftalık", "models": ["m"], "log": None, "evsrc": None}
    card = agents._cron_card(spec)
    assert card["success_rate"]["value"] == 0.75  # 3/4 pass — GERÇEK oran (henüz-veri-yok DEĞİL)
    assert card["success_rate"]["n"] == 4
    assert card["last_run"] is not None  # cron_outcomes'tan son-koşu
    assert card["running"] is True


def test_cron_success_uses_job_override_when_key_mismatches_outcomes(tmp_path, monkeypatch):
    # code-review-bulgu: memory-synthesize/intent-liveness-audit/autonomous-daily-summary'nin
    # manifest-key'i cron_outcomes.job ile UYUŞMUYOR (job='memory-synth' vb.) — spec['job']
    # override'i olmadan success_rate hep None kalıyordu.
    import sqlite3

    from app.api import agents

    db = tmp_path / "srv.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    # Deterministik timestamp'ler (Codex #328-P2 r6: latest_ok artık rows[0]'a bakıyor — tied
    # datetime('now') sıralamayı belirsizleştirip flaky yapabilirdi).
    for r, ts in (("fail", "2026-06-01 00:00:00"), ("pass", "2026-06-08 00:00:00"), ("pass", "2026-06-15 00:00:00")):
        con.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('memory-synth',?,?)", (r, ts))
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(db))

    spec = next(a for a in _AGENT_MANIFEST if a["key"] == "memory-synthesize")
    assert spec["job"] == "memory-synth"  # manifest-key != gercek job (bilinen ayrisma)
    last, rate, latest_ok = agents._cron_success(spec)
    assert rate is not None
    assert rate["n"] == 3
    assert latest_ok is True  # en-son (2026-06-15) satır pass
    assert last is not None


def test_manifest_job_override_agents_match_known_mismatches():
    mismatches = {
        "memory-synthesize": "memory-synth",
        "intent-liveness-audit": "intent-liveness",
        "autonomous-daily-summary": "autonomous-summary",
    }
    by_key = {a["key"]: a for a in _AGENT_MANIFEST}
    for key, job in mismatches.items():
        assert by_key[key].get("job") == job, f"{key}: job-override eksik/yanlış"


def test_discoveries_for_filters_project_type_and_title(tmp_path, monkeypatch):
    import sqlite3

    from app.api import agents

    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "created_at TEXT, status TEXT DEFAULT 'active')"
    )
    rows = [
        ("linux-ai-server", "learning", "Haftalık veri analizi (data-analyst) — 2026-W28"),
        ("linux-ai-server", "learning", "Teknik-SEO denetimi (seo-audit)"),  # farklı-ajan, eslesmemeli
        ("code-review", "bug", "scripts/data-analyst.py:1 SQL Injection (data-analyst)"),  # yanlış-project
        ("linux-ai-server", "bug", "AUTO-alert: cron:data-analyst"),  # yanlış-type (types=learning istendi)
    ]
    for project, typ, title in rows:
        con.execute("INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,datetime('now'))", (project, typ, title))
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))

    findings = agents._discoveries_for("%(data-analyst)%")
    assert len(findings) == 1
    assert "data-analyst" in findings[0]["title"]
    assert findings[0]["kind"] == "discovery"


def test_discoveries_for_multi_type(tmp_path, monkeypatch):
    import sqlite3

    from app.api import agents

    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "created_at TEXT, status TEXT DEFAULT 'active')"
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,datetime('now'))",
        ("linux-ai-server", "learning", "GSC fırsatı: sc-domain:x.com"),
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,datetime('now'))",
        ("linux-ai-server", "bug", "GSC: sc-domain:y.com"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))

    findings = agents._discoveries_for("GSC%", types=("learning", "bug"))
    assert len(findings) == 2


def test_discoveries_for_bad_db_no_crash(monkeypatch):
    from app.api import agents

    monkeypatch.setattr(agents, "MEMORY_DB", "/nonexistent/testing.sqlite")
    assert agents._discoveries_for("%x%") == []


def test_discoveries_for_list_patterns_or_together(tmp_path, monkeypatch):
    # Birden çok title_like pattern'i OR'lanmalı (tek çağrıda birden fazla başlık-deseni eşleştir).
    import sqlite3

    from app.api import agents

    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "created_at TEXT, status TEXT DEFAULT 'active')"
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,datetime('now'))",
        ("linux-ai-server", "bug", "GUVENLIK: sunucu-API auth-bypass (2 endpoint)"),
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,datetime('now'))",
        ("linux-ai-server", "bug", "ALARM: disk kritik eşik"),
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,datetime('now'))",
        ("linux-ai-server", "bug", "hiçbiriyle eşleşmeyen başlık"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))

    findings = agents._discoveries_for(["GUVENLIK:%", "ALARM:%"], types=("bug",))
    assert len(findings) == 2
    titles = {f["title"] for f in findings}
    assert "GUVENLIK: sunucu-API auth-bypass (2 endpoint)" in titles
    assert "ALARM: disk kritik eşik" in titles


def test_discoveries_for_excludes_resolved_status(tmp_path, monkeypatch):
    # Codex #328-P2 r4-P2: status filtresi YOKTU — bir pentest-bulgusu mevcut pentest API'siyle
    # (app/api/security.py) resolved/completed işaretlense bile Ajanlar sekmesinde sonsuza dek
    # 'aktif' P2 olarak görünmeye devam ederdi.
    import sqlite3

    from app.api import agents

    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "created_at TEXT, status TEXT DEFAULT 'active')"
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at,status) VALUES (?,?,?,datetime('now'),?)",
        ("linux-ai-server", "bug", "GUVENLIK: sunucu-API auth-bypass (2 endpoint)", "completed"),
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at,status) VALUES (?,?,?,datetime('now'),?)",
        ("linux-ai-server", "bug", "GUVENLIK: hâlâ açık bir sorun", "active"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))

    findings = agents._discoveries_for("GUVENLIK:%", types=("bug",))
    assert len(findings) == 1
    assert "hâlâ açık" in findings[0]["title"]


def test_cron_card_self_pentest_shows_summary_not_vulnerability_details(tmp_path, monkeypatch):
    # Codex #328-P2 r1→r5: self-pentest için disc_like/whitelist-scope sırayla denendi, KÖK-SORUN
    # kaldı — vulnerability-detayları (TLS/header/auth-bypass) dedicated pentest API'de
    # verify_pentest_key arkasında, /agents/runtime yalnız require_auth ister. Fix: disc_like
    # TAMAMEN kaldırıldı — kart artık discoveries'e HİÇ bakmıyor, yalnız cron:<job> wrapper-
    # özetini (sayı, detay-YOK) gösteriyor.
    import sqlite3

    from app.api import agents

    mem_db = tmp_path / "mem.db"
    con = sqlite3.connect(mem_db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "created_at TEXT, status TEXT DEFAULT 'active')"
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,datetime('now'))",
        ("petvet.panola.app", "bug", "self-pentest: eksik security header"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(mem_db))

    srv_db = tmp_path / "srv.db"
    con2 = sqlite3.connect(srv_db)
    con2.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, title TEXT, severity TEXT, timestamp TEXT, detail TEXT)")
    con2.execute(
        "INSERT INTO events (source,title,severity,timestamp) "
        "VALUES ('cron:self-pentest','self-pentest: 3/3 domain tarandı, 1 bulgu','warn',datetime('now'))"
    )
    con2.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    # Codex #328-P2 r6: latest_ok=False olmalı ki cron:<job> fallback'i tetiklensin (aksi halde
    # events'te satır olsa bile en-son cron_outcomes sonucu bilinmiyorsa fallback gösterilmez).
    con2.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('self-pentest','partial',datetime('now'))")
    con2.commit()
    con2.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(srv_db))

    spec = next(a for a in _AGENT_MANIFEST if a["key"] == "self-pentest")
    assert "disc_like" not in spec  # vulnerability-detayına doğrudan erişim YOK
    card = agents._cron_card(spec)
    assert len(card["findings"]) == 1
    assert card["findings"][0]["kind"] == "event"  # cron-özeti, discovery DEĞİL
    assert "eksik security header" not in card["findings"][0]["title"]  # detay sızmadı
    assert card["triggerable"] is False


def test_cron_card_pending_table_source(tmp_path, monkeypatch):
    # Codex #328-P2: self-improvement'ın normal-başarı yolu self_improvement_pending'e yazar,
    # event sadece DB-yazma-hatasında fallback — pending_table birincil kaynak olmalı.
    import sqlite3

    from app.api import agents

    srv_db = tmp_path / "srv.db"
    con = sqlite3.connect(srv_db)
    con.execute("CREATE TABLE self_improvement_pending (id INTEGER PRIMARY KEY, title TEXT, priority TEXT, status TEXT, created_at TEXT)")
    con.execute(
        "INSERT INTO self_improvement_pending (title,priority,status,created_at) VALUES (?,?,?,datetime('now'))",
        ("Kod değişikliği önerisi: X modülü", "high", "pending"),
    )
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, title TEXT, severity TEXT, timestamp TEXT, detail TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(srv_db))

    spec = next(a for a in _AGENT_MANIFEST if a["key"] == "self-improvement")
    assert spec["pending_table"] == "self_improvement_pending"
    card = agents._cron_card(spec)
    assert len(card["findings"]) == 1
    assert card["findings"][0]["severity"] == "P1"
    assert card["findings"][0]["kind"] == "pending"


def test_cron_card_falls_back_to_cron_wrapper_event_when_primary_empty(tmp_path, monkeypatch):
    # Codex #328-P2: disc_like/evsrc/pending_table boşsa (ör. Memory-API-down → discovery hiç
    # yazılmadı) klipper-cron-wrap.sh'nin HER-ZAMAN yazdığı cron:<job> event'i fallback olmalı,
    # "Bulgu yok" sessizliği yerine hata-detayı görünmeli.
    import sqlite3

    from app.api import agents

    srv_db = tmp_path / "srv.db"
    con = sqlite3.connect(srv_db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, title TEXT, severity TEXT, timestamp TEXT, detail TEXT)")
    con.execute(
        "INSERT INTO events (source,title,severity,timestamp) "
        "VALUES ('cron:pattern-recognition','cron pattern-recognition fail','critical',datetime('now'))"
    )
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    # Codex #328-P2 r6: latest_ok=False olmalı ki fallback tetiklensin.
    con.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('pattern-recognition','fail',datetime('now'))")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(srv_db))
    monkeypatch.setattr(agents, "MEMORY_DB", "/nonexistent/testing.sqlite")  # discoveries hep boş donsun

    spec = next(a for a in _AGENT_MANIFEST if a["key"] == "pattern-recognition")
    card = agents._cron_card(spec)
    assert len(card["findings"]) == 1
    assert card["findings"][0]["kind"] == "event"
    assert "fail" in card["findings"][0]["title"]


def test_cron_card_prioritizes_newer_cron_fail_over_stale_discovery(tmp_path, monkeypatch):
    # Codex #328-P2 r3: r1'in fallback'i yalnız findings TAMAMEN BOŞKEN devreye giriyordu — eski
    # bir discovery varken (ör. haftalar-önce yazılmış) son-run'ın TAZE fail'i stale-bulgunun
    # arkasında gizli kalırdı. cron:<job> event'i findings[0]'dan YENİYSE öne alınmalı.
    import sqlite3

    from app.api import agents

    mem_db = tmp_path / "mem.db"
    con = sqlite3.connect(mem_db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "created_at TEXT, status TEXT DEFAULT 'active')"
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,?)",
        ("linux-ai-server", "learning", "Tekrar Eden Pattern'ler — eski", "2026-06-01 00:00:00"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(mem_db))

    srv_db = tmp_path / "srv.db"
    con2 = sqlite3.connect(srv_db)
    con2.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, title TEXT, severity TEXT, timestamp TEXT, detail TEXT)")
    con2.execute(
        "INSERT INTO events (source,title,severity,timestamp) "
        "VALUES ('cron:pattern-recognition','cron pattern-recognition fail','critical','2026-07-15 00:45:05')"
    )
    con2.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    # Codex #328-P2 r6: latest_ok=False olmalı ki fallback tetiklensin.
    con2.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('pattern-recognition','fail','2026-07-15 00:45:05')")
    con2.commit()
    con2.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(srv_db))

    spec = next(a for a in _AGENT_MANIFEST if a["key"] == "pattern-recognition")
    card = agents._cron_card(spec)
    assert card["findings"][0]["kind"] == "event"  # taze fail öne alındı, eski discovery ARKADA
    assert "fail" in card["findings"][0]["title"]
    assert len(card["findings"]) == 2  # eski discovery de hâlâ listede, sadece sırası değişti


def test_cron_card_hides_stale_fail_event_after_later_pass(tmp_path, monkeypatch):
    # Codex #328-P2 r6: klipper-cron-wrap.sh yalnız RESULT!=pass'te events satırı yazar — bir job
    # haftalar-önce fail edip SONRA pass'lamaya başlasa bile events'teki tek satır hâlâ o eski
    # fail'e ait olur (yeni pass'lar hiç event yazmaz). cron_outcomes'un GERÇEK en-son sonucu pass
    # ise (latest_ok=True) stale event fallback'i GÖSTERİLMEMELİ.
    import sqlite3

    from app.api import agents

    srv_db = tmp_path / "srv.db"
    con = sqlite3.connect(srv_db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, title TEXT, severity TEXT, timestamp TEXT, detail TEXT)")
    con.execute(
        "INSERT INTO events (source,title,severity,timestamp) "
        "VALUES ('cron:pattern-recognition','cron pattern-recognition fail','critical','2026-06-01 00:00:00')"
    )
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    for r, ts in (("fail", "2026-06-01 00:00:00"), ("pass", "2026-06-08 00:00:00"), ("pass", "2026-06-15 00:00:00")):
        con.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('pattern-recognition',?,?)", (r, ts))
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(srv_db))
    monkeypatch.setattr(agents, "MEMORY_DB", "/nonexistent/testing.sqlite")

    spec = next(a for a in _AGENT_MANIFEST if a["key"] == "pattern-recognition")
    card = agents._cron_card(spec)
    assert card["findings"] == []  # stale fail-event GİZLENDİ, job artık sağlıklı
    assert card["success_rate"]["value"] == round(2 / 3, 3)  # _cron_success 3-ondalık yuvarlıyor


def test_events_for_appends_detail_to_title(tmp_path, monkeypatch):
    # Codex #328-P2 r6: klipper-cron-wrap.sh title'ı jenerik basar ("cron <job> <result>"), asıl
    # bilgi (rc + OUTCOME-satırı) detail'de — bu kayboluyor, _events_for yalnız title okuyordu.
    import sqlite3

    from app.api import agents

    srv_db = tmp_path / "srv.db"
    con = sqlite3.connect(srv_db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, title TEXT, severity TEXT, timestamp TEXT, detail TEXT)")
    con.execute(
        "INSERT INTO events (source,title,severity,timestamp,detail) VALUES (?,?,?,datetime('now'),?)",
        ("cron:self-pentest", "cron self-pentest partial", "warning", "rc=0 self-pentest: 3/3 domain, 2 bulgu"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(srv_db))

    findings = agents._events_for("cron:self-pentest")
    assert len(findings) == 1
    assert findings[0]["title"] == "cron self-pentest partial: rc=0 self-pentest: 3/3 domain, 2 bulgu"


def test_cron_card_uses_discoveries_when_disc_like_set(tmp_path, monkeypatch):
    # code-review-bulgu: ad-advisor/data-analyst/seo-audit/seo-gsc'nin GERÇEK ciktisi
    # server.db.events'te DEGIL, discoveries'te -- _events_for onlari hep 'Bulgu yok'
    # gosteriyordu. disc_like set'liyken _cron_card discoveries'i kullanmali, events'i DEGIL.
    import sqlite3

    from app.api import agents

    # Codex #328-P2 r3: _cron_card cron:<job> event'ini findings[0]'dan yeniyse öne alabilir; r6:
    # bu yalnız latest_ok=False iken olur (burada cron_outcomes'a hiç satır eklenmiyor → latest_ok
    # None → fallback hiç tetiklenmez, discovery tek-başına kalır). Timestamp'ler yine de sabit
    # (wall-clock-timing'e bağlı flaky-test riskinden genel-olarak kaçınmak için).
    mem_db = tmp_path / "mem.db"
    con = sqlite3.connect(mem_db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "created_at TEXT, status TEXT DEFAULT 'active')"
    )
    con.execute(
        "INSERT INTO discoveries (project,type,title,created_at) VALUES (?,?,?,?)",
        ("linux-ai-server", "learning", "Reklam fırsatları (ad-advisor)", "2026-07-15 12:00:00"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(mem_db))

    srv_db = tmp_path / "srv.db"
    con = sqlite3.connect(srv_db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, title TEXT, severity TEXT, timestamp TEXT, detail TEXT)")
    con.execute(
        "INSERT INTO events (source,title,severity,timestamp) VALUES ('cron:ad-advisor','irrelevant-alert','warn','2026-07-15 06:00:00')"
    )
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(srv_db))

    spec = next(a for a in _AGENT_MANIFEST if a["key"] == "ad-advisor")
    card = agents._cron_card(spec)
    assert len(card["findings"]) == 1
    assert "ad-advisor" in card["findings"][0]["title"]
    assert card["findings"][0]["kind"] == "discovery"


def test_research_card_ondemand_not_triggerable():
    spec = {"key": "research", "name": "Araştırma", "role": "r", "schedule": "istek-üzerine", "models": ["qwen", "claude CLI"]}
    card = _research_card(
        spec, {"findings": [{"time": "t", "title": "[araştırma] FastAPI", "severity": "", "status": "active", "kind": "research"}], "n": 1}
    )
    assert card["type"] == "ondemand"
    assert card["triggerable"] is False  # topic gerekir → API'den
    assert "FastAPI" in card["current_task"]


def test_sev_from_details():
    assert _sev_from_details("[P1] injection") == "P1"
    assert _sev_from_details("[P2] x") == "P2"
    assert _sev_from_details("açıklama, sev yok") == ""


class _FakeRemediation:
    def __init__(self, source, action, success):
        self.timestamp = "2026-06-20T10:00:00"
        self.alert_source = source
        self.action = action
        self.success = success


class _FakeDevOps:
    _diag_model = "qwen2.5:3b"

    def __init__(self, log):
        self._remediation_log = log

    @property
    def status(self):
        return {"running": True, "last_check": "2026-06-20T10:05:00", "check_count": 42, "active_alerts": 1, "interval_seconds": 30}


def test_devops_card_success_rate_and_findings():
    log = [_FakeRemediation("service:x", "restart", True), _FakeRemediation("docker:y", "restart", False)]
    card = _devops_card(_FakeDevOps(log))
    assert card["key"] == "devops"
    assert card["running"] is True
    assert card["models"] == ["qwen2.5:3b (teşhis)"]
    assert card["success_rate"] == {"label": "Remediation başarısı", "value": 0.5, "n": 2}
    assert card["current_task"].startswith("Remediation: 1")  # aktif uyarı var
    assert len(card["findings"]) == 2
    assert card["findings"][0]["severity"] in ("P1", "P3")


def test_devops_card_no_remediation_no_rate():
    card = _devops_card(_FakeDevOps([]))
    assert card["success_rate"] is None
    assert card["current_task"].startswith("Remediation: 1")  # aktif uyarı (status'ta 1)


class _FakeCRA:
    def status(self):
        return {
            "enabled": True,
            "model": "claude-haiku-4-5-20251001",  # tarama route (LLM_ROUTE_CODE_REVIEW)
            "verify_model": "claude-sonnet-4-6",  # kontrol/sentez route (LLM_ROUTE_VERIFY)
            "synthesis_model": "claude-sonnet-4-6",
            "interval_s": 300,
            "ticks": 7,
            "total_findings": 9,
            "last_run": "2026-06-20T09:00:00",
        }


def test_codereview_card_signal_rate_14d_window():
    # Sinyal-oranı = GERÇEK (completed) ÷ TRİYAJ (completed+obsolete), SON-14-GÜN penceresinden.
    # Yaşam-boyu kümülatif oran qwen/FP-seli havuzunu (306 kayıt) paydada taşıyıp %16 gösteriyordu
    # (gerçek pipeline %88'ken); tüm-zaman artık stats'ta ayrı satır. 30g DEĞİL 14g: klipper verify
    # (PR#301) — 30g bugünün tarihinde kötü-havuzu hâlâ kapsıyordu, 14g bunu hemen dışlıyor.
    crdb = {
        "counts": {"active": 9, "completed": 15, "obsolete": 85},  # tüm-zaman: %15
        "counts_14d": {"completed": 8, "obsolete": 2},  # son-14g: %80 — ana metrik BU
        "findings": [
            {"time": "2026-06-20T09:00", "title": "app/api/dev.py:48 injection", "severity": "P1", "status": "active", "kind": "bug"}
        ],
    }
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["key"] == "code-review"
    assert card["success_rate"] == {"label": "Sinyal (gerçek÷triaj, 14g)", "value": 0.8, "n": 10}
    assert card["stats"]["Sinyal tüm-zaman"] == "15% (100)"
    assert card["current_task"] == "Son inceleme: app/api/dev.py:48"
    assert "claude-haiku-4-5-20251001 (tarama)" in card["models"]
    assert "claude-sonnet-4-6 (kontrol/sentez)" in card["models"]


def test_codereview_card_14d_empty_falls_back_lifetime():
    # 14g'de hiç triyaj yoksa (sessiz ay) tüm-zamana düş — ama etiket dürüst kalsın
    crdb = {"counts": {"completed": 2, "obsolete": 6}, "counts_14d": {}, "findings": []}
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["success_rate"] == {"label": "Sinyal (gerçek÷triaj, tüm-zaman)", "value": 0.25, "n": 8}
    assert "Sinyal tüm-zaman" not in card["stats"]  # zaten ana metrik tüm-zaman — çift gösterme


def test_codereview_card_missing_counts_14d_backcompat():
    # counts_14d anahtarı yoksa (eski üretici) counts'a düşer — kart çökmez
    crdb = {"counts": {"completed": 2, "obsolete": 6}, "findings": []}
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["success_rate"]["value"] == 0.25
    assert card["success_rate"]["n"] == 8


def test_codereview_card_empty():
    card = _codereview_card(_FakeCRA(), {"counts": {}, "counts_14d": {}, "findings": []})
    assert card["success_rate"] is None
    assert card["current_task"] == "Kuyruk/sweep bekliyor"


def test_codereview_db_14d_window(tmp_path, monkeypatch):
    # _codereview_db SQL'i: 14g penceresi yalnız yeni kayıtları saymalı (created_at TEXT 'YYYY-MM-DD HH:MM:SS')
    import sqlite3

    from app.api import agents

    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, status TEXT, title TEXT, details TEXT, created_at TEXT)"
    )
    rows = [
        # eski dönem (>14g): 1 completed + 3 obsolete
        ("completed", "-40 days"),
        ("obsolete", "-40 days"),
        ("obsolete", "-20 days"),
        ("obsolete", "-15 days"),
        # yeni dönem (<14g): 4 completed + 1 obsolete
        ("completed", "-5 days"),
        ("completed", "-4 days"),
        ("completed", "-2 days"),
        ("completed", "-1 days"),
        ("obsolete", "-3 days"),
    ]
    for status, delta in rows:
        con.execute(
            "INSERT INTO discoveries (project,type,status,title,details,created_at) "
            "VALUES ('code-review','bug',?,'t','[P2] x',datetime('now',?))",
            (status, delta),
        )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))
    crdb = agents._codereview_db()
    assert crdb["counts"] == {"completed": 5, "obsolete": 4}
    assert crdb["counts_14d"] == {"completed": 4, "obsolete": 1}
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["success_rate"]["value"] == 0.8  # 4/5 — 14g penceresi
    assert card["stats"]["Sinyal tüm-zaman"] == "56% (9)"  # 5/9 kümülatif, ayrı satırda
