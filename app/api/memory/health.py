"""Health + maintenance + spawn-failure DLQ router handler'ları (memory paketi).

Gövdeler birebir taşındı (Faz 3).
"""

from fastapi import HTTPException, Query

from app.api.memory import SpawnFailureRetryResponse, _send_telegram, get_db, router


@router.get("/health")
async def memory_health():
    """Sistem sağlık raporu — stale data, never-read, duplicates"""
    db = get_db()
    try:
        total = db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
        active_total = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active'").fetchone()[0]
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        # Sağlık metriği YALNIZ aktif kayıtlara dayanır. Obsolete/closed kayıtların
        # okunmamış olması beklenir ve aksiyonluk değildir — ham never_read/total
        # oranı bu yüzden yanıltıcı (~%86) çıkıyordu. Gerçek temizlik sinyali = aktif okunmamış.
        active_never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0").fetchone()[0]
        stale_60 = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE status='active' AND created_at < datetime('now', '-60 days')"
        ).fetchone()[0]
        # Gerçek temizlik sinyali = auto-cleanup'ın FİİLEN arşivlediği küme:
        # aktif + okunmamış + bug-DEĞİL + 60g'den eski. never_read_pct açık bug'ları
        # ve taze kayıtları da sayar (yanıltıcı kırmızı) — recommendation buna değil
        # actionable_stale'e bağlı.
        actionable_stale = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0 "
            "AND type NOT IN ('bug') AND created_at < datetime('now', '-60 days')"
        ).fetchone()[0]
        most_read = [
            dict(r)
            for r in db.execute("SELECT id, project, type, title, read_count FROM discoveries ORDER BY read_count DESC LIMIT 5").fetchall()
        ]
        never_read_pct = round(active_never_read / active_total * 100, 1) if active_total > 0 else 0

        return {
            "total_discoveries": total,
            "active_discoveries": active_total,
            "never_read": never_read,
            "active_never_read": active_never_read,
            "never_read_pct": never_read_pct,
            "stale_60_days": stale_60,
            "actionable_stale": actionable_stale,
            "most_read": most_read,
            "recommendation": (
                "Sistem sağlıklı — arşivlenecek eski okunmamış kayıt yok"
                if actionable_stale == 0
                else f"{actionable_stale} eski okunmamış kayıt arşivlenebilir — auto-cleanup çalıştır"
            ),
        }
    finally:
        db.close()


@router.post("/maintenance/archive-stale")
async def archive_stale(days: int = 90):
    """Eski, hiç okunmamış kayıtları obsolete yap"""
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE discoveries SET status='obsolete' "
            "WHERE status='active' AND read_count=0 AND type NOT IN ('bug') "
            "AND created_at < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        db.commit()
        return {"archived": cur.rowcount}
    finally:
        db.close()


@router.post("/maintenance/auto-cleanup")
async def auto_cleanup(days: int = 60, dry_run: bool = False):
    """Kapsamlı bakım — stale arşivle + FTS temizlik + rapor"""
    db = get_db()
    try:
        stale_count = 0
        if not dry_run:
            cur = db.execute(
                "UPDATE discoveries SET status='obsolete' WHERE status='active' AND read_count=0 "
                "AND type NOT IN ('bug') AND created_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            )
            stale_count = cur.rowcount
        else:
            stale_count = db.execute(
                "SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0 "
                "AND type NOT IN ('bug') AND created_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            ).fetchone()[0]

        if not dry_run:
            db.execute("DELETE FROM discoveries_fts WHERE rowid NOT IN (SELECT id FROM discoveries)")
            db.commit()

        total = db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
        active_total = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active'").fetchone()[0]
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        active_never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0").fetchone()[0]
        active_bugs = db.execute("SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active'").fetchone()[0]

        report = {
            "action": "dry_run" if dry_run else "cleanup",
            "stale_archived": stale_count,
            "total_discoveries": total,
            "active_discoveries": active_total,
            "never_read": never_read,
            "active_never_read": active_never_read,
            "active_bugs": active_bugs,
            # Aktif-kapsamlı oran (yanıltıcı ham %86 yerine gerçek temizlik sinyali)
            "never_read_pct": round(active_never_read / max(active_total, 1) * 100, 1),
        }

        if not dry_run:
            await _send_telegram(
                f"<b>\U0001f9f9 Klipper Bakım Raporu</b>\n"
                f"Arşivlenen: {stale_count} kayıt\n"
                f"Kalan: {total} discovery, {active_bugs} aktif bug\n"
                f"Okunmamış: {never_read} (%{report['never_read_pct']})"
            )

        return report
    finally:
        db.close()


@router.get("/maintenance/detect-conflicts")
async def detect_conflicts():
    db = get_db()
    try:
        stale_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, title, status FROM discoveries "
                "WHERE type='bug' AND status='active' AND title LIKE '%COZULDU%' ORDER BY project"
            ).fetchall()
        ]

        dups = [
            dict(r)
            for r in db.execute(
                "SELECT project, type, title, COUNT(*) as cnt, GROUP_CONCAT(id) as ids "
                "FROM discoveries GROUP BY project, type, title HAVING cnt > 1 ORDER BY cnt DESC LIMIT 20"
            ).fetchall()
        ]

        return {
            "stale_bugs_cozuldu": stale_bugs,
            "duplicate_discoveries": dups,
            "total_stale": len(stale_bugs),
            "total_dups": len(dups),
        }
    finally:
        db.close()


# NOTE: Secrets endpoints moved to app/api/admin.py — they use JWT auth
# (require_auth) for dashboard compatibility, separate from the X-Memory-Key
# auth this router uses.
#
# NOTE: Task Queue endpoints (GET/POST /queue, PUT /queue/{id}/claim, /result)
# removed 2026-05-25 along with task_queue table — 1 ay kullanilmadi, smoke
# test'ten oteye gecmedi. Aktif iş günlüğü tasks_log (/tasks endpoint'leri).


# ============ DLQ: Spawn Failures (P0.2) ============


@router.get("/spawn-failures")
async def list_spawn_failures(
    status: str | None = Query(None, regex="^(pending_retry|poison|archived|orphaned)$"),
    limit: int = Query(50, ge=1, le=200),
):
    """Autonomous Claude spawn fail DLQ listesi. Filter: status."""
    db = get_db()
    try:
        q = "SELECT * FROM spawn_failures WHERE 1=1"
        params: list = []
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY first_failed_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in db.execute(q, params).fetchall()]
        return {"count": len(rows), "rows": rows}
    finally:
        db.close()


@router.post("/spawn-failures/{failure_id}/retry")
async def retry_spawn_failure(failure_id: int):
    """
    Manuel retry: DLQ row'unu pending_retry'a geri al (attempt_num=0 reset, fresh start).
    Bir sonraki cron tick'inde (~15dk) hemen cekilir.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, note_id, status FROM spawn_failures WHERE id=?",
            (failure_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "spawn_failure not found")
        if row["status"] == "archived":
            return SpawnFailureRetryResponse(
                id=row["id"],
                note_id=row["note_id"],
                status="archived",
                message="Already archived (success). No-op.",
            ).model_dump()
        db.execute(
            "UPDATE spawn_failures SET status='pending_retry', last_retry_at=NULL, attempt_num=0 WHERE id=?",
            (failure_id,),
        )
        db.commit()
        return SpawnFailureRetryResponse(
            id=row["id"],
            note_id=row["note_id"],
            status="pending_retry",
            message="Reset for retry. Next cron tick (~15min) will pick up.",
        ).model_dump()
    finally:
        db.close()
