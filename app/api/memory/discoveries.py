"""Discovery + project-summary router handler'ları (memory paketi).

Gövdeler birebir taşındı (Faz 3).
"""

import asyncio

from fastapi import HTTPException

from app.api.memory import DiscoveryCreate, DiscoveryUpdate, _fire_event, _sync_fts, _track_read, get_db, router
from app.api.memory import signal_quality as sq
from app.core.privacy import redact


@router.get("/discoveries")
async def list_discoveries(
    project: str | None = None,
    type: str | None = None,
    status: str | None = None,
    as_of: str | None = None,
    limit: int = 30,
):
    """Discovery listele. as_of=<ISO ts> verilirse bi-temporal 'o an aktif' sorgu:
    valid_at <= as_of AND (invalid_at IS NULL OR invalid_at > as_of) = 'X tarihinde aktif sinyaller'."""
    db = get_db()
    try:
        sq.ensure_signal_columns(db)
        query = (
            "SELECT id, session_id, device_name, project, type, title, status, "
            "rationale, read_count, date(created_at) as date FROM discoveries WHERE 1=1"
        )
        params = []
        if project:
            query += " AND project=?"
            params.append(project)
        if type:
            query += " AND type=?"
            params.append(type)
        if status:
            query += " AND status=?"
            params.append(status)
        if as_of:
            query += " AND valid_at <= ? AND (invalid_at IS NULL OR invalid_at > ?)"
            params.extend([as_of, as_of])
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()


@router.get("/discoveries/{discovery_id}")
async def get_discovery(discovery_id: int):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM discoveries WHERE id=?", (discovery_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Discovery not found")
        _track_read(db, "discoveries", discovery_id)
        try:  # decay-recency: son-erişim bump (yoksa recency sessizce 'yaş'a döner — klipper notu)
            db.execute("UPDATE discoveries SET last_accessed=datetime('now') WHERE id=?", (discovery_id,))
            db.commit()
        except Exception:
            pass
        return dict(row)
    finally:
        db.close()


@router.post("/discoveries")
async def create_discovery(data: DiscoveryCreate):
    """Duplicate korumalı discovery oluştur — bi-temporal + semantik-dedup + importance.

    Koruma katmanları (sırayla, HEPSİ fail-safe — yazma yolu Ollama/Qdrant'a bağımlı değil):
    - Privacy: details secret/token redact (app.core.privacy).
    - 5dk exact-window: ayni project+type+title+details son 5dk → skip.
    - Semantik-dedup (signal_quality): embed→Qdrant→qwen2.5 op-kararı
      (ADD/UPDATE/NOOP/SUPERSEDE). Ollama/Qdrant down/garbage → ADD'e düşer, exact-title devralır.
    - Exact-title fallback: ayni project+type+title aktif kayıt → güncelle.
    - importance (1-10, fail→5) + valid_at (bi-temporal) + Qdrant upsert (fail-safe).
    """
    details_clean, redacted_labels = redact(data.details)

    db = get_db()
    try:
        sq.ensure_signal_columns(db)  # idempotent bi-temporal/decay migration

        # 5-dakika exact-match dedup window
        recent_dup = db.execute(
            "SELECT id FROM discoveries WHERE project=? AND type=? AND title=? "
            "AND COALESCE(details,'')=? "
            "AND created_at > datetime('now','-5 minutes')",
            (data.project, data.type, data.title, details_clean or ""),
        ).fetchone()
        if recent_dup:
            return {"id": recent_dup[0], "status": "duplicate_skipped_5min", "secrets_redacted": redacted_labels}

        # Semantik-dedup (fail-safe → ADD). NOOP/UPDATE erken-döner; SUPERSEDE eskiyi geçersizler.
        decision = sq.semantic_dedup(project=data.project, title=data.title, details=details_clean)
        op = decision.get("operation", "ADD")
        vec = decision.get("vector")
        if op == "NOOP" and decision.get("target_id"):
            return {"id": decision["target_id"], "status": "duplicate_skipped_semantic", "secrets_redacted": redacted_labels}
        if op == "UPDATE" and decision.get("target_id"):
            tid = decision["target_id"]
            if details_clean or data.rationale:
                db.execute(
                    "UPDATE discoveries SET details=COALESCE(?, details), device_name=?, rationale=COALESCE(?, rationale) WHERE id=?",
                    (details_clean, data.device_name, data.rationale, tid),
                )
                db.commit()
            return {"id": tid, "status": "merged_semantic", "secrets_redacted": redacted_labels}
        superseded_id = None
        if op == "SUPERSEDE" and decision.get("target_id"):
            superseded_id = decision["target_id"]
            db.execute(
                "UPDATE discoveries SET status='superseded', invalid_at=datetime('now') WHERE id=? AND status='active'",
                (superseded_id,),
            )
            db.commit()

        # Exact-title fallback (semantik ADD/degrade yolu — ayni-baslik aktif kayit → guncelle).
        # SUPERSEDE'de bilerek YENİ row istiyoruz, fallback'i atla.
        if not superseded_id:
            existing = db.execute(
                "SELECT id FROM discoveries WHERE project=? AND type=? AND title=? AND status='active'",
                (data.project, data.type, data.title),
            ).fetchone()
            if existing:
                if details_clean or data.rationale:
                    db.execute(
                        "UPDATE discoveries SET details=COALESCE(?, details), device_name=?, rationale=COALESCE(?, rationale) WHERE id=?",
                        (details_clean, data.device_name, data.rationale, existing[0]),
                    )
                    db.commit()
                return {"id": existing[0], "status": "already_exists", "secrets_redacted": redacted_labels}

        # importance (fail-safe → 5) + INSERT (valid_at=now bi-temporal, supersedes_id linkage)
        importance = sq.score_importance(data.title, details_clean)
        cur = db.execute(
            "INSERT INTO discoveries "
            "(session_id, device_name, project, type, title, details, status, rationale, valid_at, importance, supersedes_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                data.session_id,
                data.device_name,
                data.project,
                data.type,
                data.title,
                details_clean,
                data.status or "active",
                data.rationale,
                importance,
                superseded_id,
            ),
        )
        db.commit()
        _sync_fts(db, cur.lastrowid, data.title, details_clean)
        db.commit()
        new_id = cur.lastrowid

        # Qdrant upsert (fail-safe; vec yoksa atlanir — semantik-dedup ileride exact-title'a düşer)
        sq.ensure_collection()
        sq.upsert_discovery(new_id, vec, {"project": data.project, "type": data.type, "title": data.title, "status": "active"})

        event_type = f"{data.type}_created" if data.type in ("bug", "fix") else "discovery_created"
        asyncio.create_task(
            _fire_event(
                event_type,
                {"id": new_id, "project": data.project, "type": data.type, "title": data.title, "device": data.device_name},
            )
        )

        result = {"id": new_id, "status": "created", "importance": importance, "secrets_redacted": redacted_labels}
        if superseded_id:
            result["supersedes_id"] = superseded_id
        if decision.get("degraded"):
            result["signal_degraded"] = decision["degraded"]  # görünür: dedup/embed atlandıysa BİL
        return result
    finally:
        db.close()


@router.put("/discoveries/{discovery_id}")
async def update_discovery(discovery_id: int, data: DiscoveryUpdate):
    """Discovery güncelle — status lifecycle (active → completed/obsolete/superseded)"""
    db = get_db()
    try:
        sq.ensure_signal_columns(db)  # invalid_at yazımı için kolon garantisi
        fields, params = [], []
        if data.title:
            fields.append("title=?")
            params.append(data.title)
        if data.details:
            fields.append("details=?")
            params.append(data.details)
        if data.status:
            fields.append("status=?")
            params.append(data.status)
            if data.status == "completed":
                fields.append("resolved=1")
            if data.status in ("completed", "obsolete", "superseded"):
                fields.append("invalid_at=datetime('now')")  # bi-temporal: gerçek-dünya geçersizleşme
        if data.rationale:
            fields.append("rationale=?")
            params.append(data.rationale)
        if not fields:
            raise HTTPException(400, "No fields to update")
        params.append(discovery_id)
        db.execute(f"UPDATE discoveries SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
        return {"status": "updated"}
    finally:
        db.close()


@router.put("/discoveries/{discovery_id}/resolve")
async def resolve_discovery(discovery_id: int):
    db = get_db()
    try:
        sq.ensure_signal_columns(db)  # invalid_at yazımı için kolon garantisi
        db.execute(
            "UPDATE discoveries SET resolved=1, status='completed', invalid_at=datetime('now') WHERE id=?",
            (discovery_id,),
        )
        db.commit()
        return {"status": "resolved"}
    finally:
        db.close()


@router.get("/discoveries/by-type/{dtype}")
async def list_discoveries_by_type(dtype: str, project: str | None = None, status: str | None = "active"):
    db = get_db()
    try:
        query = (
            "SELECT id, project, type, title, details, status, read_count, "
            "device_name, date(created_at) as date FROM discoveries WHERE type=?"
        )
        params = [dtype]
        if project:
            query += " AND project=?"
            params.append(project)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY project, created_at DESC"
        rows = db.execute(query, params).fetchall()
        # Toplu read tracking + decay-recency bump
        for r in rows:
            _track_read(db, "discoveries", r["id"])
        try:
            db.executemany("UPDATE discoveries SET last_accessed=datetime('now') WHERE id=?", [(r["id"],) for r in rows])
            db.commit()
        except Exception:
            pass
        return [dict(r) for r in rows]
    finally:
        db.close()


# ============ Projects ============


@router.get("/projects")
async def list_projects_summary():
    """Proje bazlı özet — health skoru ile"""
    db = get_db()
    try:
        projects = {}
        for row in db.execute("""
            SELECT project, type, status, COUNT(*) as cnt
            FROM discoveries GROUP BY project, type, status ORDER BY project
        """).fetchall():
            p = row[0]
            if p not in projects:
                projects[p] = {
                    "name": p,
                    "open_bugs": 0,
                    "fixes": 0,
                    "architecture": 0,
                    "active_plans": 0,
                    "completed_plans": 0,
                    "workarounds": 0,
                    "tasks": 0,
                }
            t, s, c = row[1], row[2], row[3]
            if t == "bug" and s == "active":
                projects[p]["open_bugs"] = c
            elif t == "fix":
                projects[p]["fixes"] += c
            elif t == "architecture":
                projects[p]["architecture"] += c
            elif t == "plan" and s == "active":
                projects[p]["active_plans"] = c
            elif t == "plan" and s == "completed":
                projects[p]["completed_plans"] = c
            elif t == "workaround":
                projects[p]["workarounds"] += c

        for row in db.execute("SELECT project, COUNT(*) FROM tasks_log GROUP BY project").fetchall():
            if row[0] in projects:
                projects[row[0]]["tasks"] = row[1]
            else:
                projects[row[0]] = {
                    "name": row[0],
                    "open_bugs": 0,
                    "fixes": 0,
                    "architecture": 0,
                    "active_plans": 0,
                    "completed_plans": 0,
                    "workarounds": 0,
                    "tasks": row[1],
                }

        # Health skoru: mimari var, plan var, bug az = sağlıklı
        for p in projects.values():
            score = 0
            if p["architecture"] > 0:
                score += 30
            if p["active_plans"] > 0 or p["completed_plans"] > 0:
                score += 20
            if p["tasks"] > 0:
                score += 20
            if p["fixes"] > 0:
                score += 15
            if p["open_bugs"] == 0:
                score += 15
            elif p["open_bugs"] <= 2:
                score += 5
            p["health"] = min(score, 100)

        return sorted(projects.values(), key=lambda x: x["health"], reverse=True)
    finally:
        db.close()


@router.get("/projects/{project_name}")
async def get_project_detail(project_name: str):
    """Proje detayı — discoveries, tasks, sessions, health"""
    db = get_db()
    try:
        discoveries = [
            dict(r)
            for r in db.execute(
                "SELECT id, type, title, details, status, read_count, device_name, date(created_at) as date "
                "FROM discoveries WHERE project=? ORDER BY type, created_at DESC",
                (project_name,),
            ).fetchall()
        ]

        tasks = [
            dict(r)
            for r in db.execute(
                "SELECT id, task, status, device_name, details, date(created_at) as date "
                "FROM tasks_log WHERE project=? ORDER BY created_at DESC LIMIT 30",
                (project_name,),
            ).fetchall()
        ]

        sessions = [
            dict(r)
            for r in db.execute(
                "SELECT id, session_num, date, device_name, platform, substr(summary,1,120) as summary "
                "FROM sessions WHERE summary LIKE ? ORDER BY id DESC LIMIT 10",
                (f"%{project_name}%",),
            ).fetchall()
        ]

        devices = [
            dict(r)
            for r in db.execute(
                "SELECT device_name, local_path, datetime(last_activity) as last_activity FROM device_projects WHERE project=?",
                (project_name,),
            ).fetchall()
        ]

        type_counts = {}
        for d in discoveries:
            key = f"{d['type']}_{d['status']}" if d["status"] != "active" else d["type"]
            type_counts[key] = type_counts.get(key, 0) + 1

        return {
            "project": project_name,
            "stats": type_counts,
            "total_discoveries": len(discoveries),
            "total_tasks": len(tasks),
            "discoveries": discoveries,
            "tasks": tasks,
            "sessions": sessions,
            "devices": devices,
        }
    finally:
        db.close()
