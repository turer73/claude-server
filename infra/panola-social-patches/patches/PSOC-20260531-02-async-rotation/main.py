#!/usr/bin/env python3
"""Panola Social — CLI Entry Point v2.

Usage:
    python main.py generate --product petvet --type educational_carousel --topic "Kedi asilari"
    python main.py plan-week --product petvet
    python main.py generate-week --product petvet
    python main.py publish --id 5
    python main.py publish-scheduled
    python main.py approve --id 3
    python main.py schedule --id 3 --at "2026-04-01T09:00:00"
    python main.py list --status draft --product petvet
    python main.py calendar
    python main.py collect-metrics
    python main.py report
    python main.py stats
    python main.py init-db
    python main.py token-check
    python main.py token-refresh
    python main.py token-auto
"""

import argparse
import json
import sys


def cmd_generate(args):
    from src.engine import generate_content
    result = generate_content(
        product_key=args.product,
        content_type=args.type,
        topic=args.topic or "",
        pillar=args.pillar,
        extra_context=args.context or "",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_plan_week(args):
    from src.planner import generate_weekly_plan
    result = generate_weekly_plan(args.product, args.week_start)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_generate_week(args):
    from src.planner import generate_week_content
    job_id = getattr(args, "job_id", None)
    try:
        result = generate_week_content(
            args.product, args.week_start, force=getattr(args, "force", False)
        )
    except Exception as e:
        if job_id:
            from src.db import finish_generation_job
            finish_generation_job(job_id, "failed", error=str(e))
        raise
    if job_id:
        from src.db import finish_generation_job
        plan_id = (result.get("plan") or {}).get("plan_id")
        if "error" in result:
            finish_generation_job(job_id, "failed", weekly_plan_id=plan_id,
                                  error=str(result.get("error")))
        else:
            finish_generation_job(job_id, "done", weekly_plan_id=plan_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_publish(args):
    from src.publisher import publish_content
    result = publish_content(args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_publish_scheduled(args):
    from src.scheduler import publish_scheduled
    result = publish_scheduled()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_approve(args):
    from src.scheduler import approve_content
    result = approve_content(args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_schedule(args):
    from src.scheduler import schedule_content
    result = schedule_content(args.id, args.at)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_list(args):
    from src.db import list_contents
    contents = list_contents(status=args.status, product=args.product, limit=args.limit)
    for c in contents:
        status_icon = {"draft": "📝", "approved": "✅", "scheduled": "📅", "published": "🚀", "failed": "❌"}.get(c["status"], "?")
        print(f"  {status_icon} #{c['id']} [{c['content_type']}] {c.get('title', 'N/A')[:50]} — {c['status']}")
    print(f"\nToplam: {len(contents)} icerik")


def cmd_calendar(args):
    from src.scheduler import get_calendar
    cal = get_calendar(product=args.product)
    print(json.dumps(cal, ensure_ascii=False, indent=2, default=str))


def cmd_collect_metrics(args):
    from src.analyzer import collect_metrics
    result = collect_metrics()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_report(args):
    from src.analyzer import generate_weekly_report
    report = generate_weekly_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def cmd_stats(args):
    from src.db import get_content_stats
    stats = get_content_stats()
    if not stats:
        print("Henuz icerik yok.")
        return
    print("\n📊 Icerik Istatistikleri:")
    for status, count in stats.items():
        icon = {"draft": "📝", "approved": "✅", "scheduled": "📅", "published": "🚀", "failed": "❌"}.get(status, "?")
        print(f"  {icon} {status}: {count}")
    print(f"  Toplam: {sum(stats.values())}")


def cmd_init_db(args):
    from src.db import init_db
    init_db()




def cmd_generate_image(args):
    from src.db import get_content, update_content_media
    from src.image_gen import generate_images
    import json as j

    content = get_content(args.id)
    if not content:
        print(j.dumps({"error": f"Content {args.id} not found"}))
        return

    raw = j.loads(content["raw_response"]) if content["raw_response"] else {}
    paths = generate_images(raw, content["content_type"], content["product"])
    if paths:
        update_content_media(args.id, paths)
    print(j.dumps({"content_id": args.id, "images": paths, "count": len(paths)}, indent=2))

def cmd_token_check(args):
    from src.token_manager import check_token
    result = check_token()
    if result["valid"]:
        days = result["days_left"]
        print("  Token gecerli -- " + str(days) + " gun kaldi")
    else:
        err = result.get("error", "?")
        print("  Token gecersiz: " + str(err))


def cmd_token_refresh(args):
    from src.token_manager import refresh_token
    import json
    result = refresh_token()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_token_auto(args):
    from src.token_manager import auto_refresh_if_needed
    import json
    result = auto_refresh_if_needed()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_token_auto(args):
    from src.token_manager import auto_refresh_if_needed
    import json
    result = auto_refresh_if_needed()
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_rh_balance(args):
    from src.renderhane_client import get_balance
    balance = get_balance()
    print(f"  Renderhane kredi: {balance}")


def cmd_rh_generate(args):
    from src.renderhane_client import generate_text_image, generate_scene, remove_background
    import json

    if args.tool == "text-to-image":
        path = generate_text_image(args.prompt, tier=args.tier)
    elif args.tool == "scene":
        if not args.image_url:
            print("ERROR: --image-url gerekli")
            return
        path = generate_scene(args.image_url, args.prompt)
    elif args.tool == "bg-remove":
        if not args.image_url:
            print("ERROR: --image-url gerekli")
            return
        path = remove_background(args.image_url)
    else:
        print(f"Desteklenmeyen arac: {args.tool}")
        return

    print(json.dumps({"tool": args.tool, "output": path}, indent=2))


def cmd_rh_status(args):
    from src.renderhane_client import get_job_status
    import json
    result = get_job_status(args.job_id)
    print(json.dumps(result, indent=2))


def cmd_smart_approve(args):
    """Quality gate + approve + schedule."""
    import json
    from datetime import datetime, timedelta
    from src.quality_gate import gate_and_approve
    from src.db import get_db

    gate_result = gate_and_approve()

    with get_db() as db:
        approved = db.execute(
            "SELECT id FROM contents WHERE status=\"approved\" ORDER BY id"
        ).fetchall()

        if not approved:
            print(json.dumps({**gate_result, "scheduled": 0}, ensure_ascii=False, indent=2))
            return

        base = datetime.now()
        days_ahead = (7 - base.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        monday = base + timedelta(days=days_ahead)

        scheduled = 0
        for i, row in enumerate(approved):
            day_offset = i % 6
            schedule_dt = (monday + timedelta(days=day_offset)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            db.execute(
                "UPDATE contents SET status=\"scheduled\", scheduled_at=? WHERE id=?",
                (schedule_dt.isoformat(), row[0])
            )
            scheduled += 1
        db.commit()

    result = {
        **gate_result,
        "scheduled": scheduled,
        "schedule_start": monday.strftime("%Y-%m-%d"),
        "schedule_end": (monday + timedelta(days=5)).strftime("%Y-%m-%d"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

def main():
    parser = argparse.ArgumentParser(
        description="Panola Social — Professional Content Engine v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Komutlar")

    # generate
    p = sub.add_parser("generate", help="Tek icerik olustur")
    p.add_argument("--product", "-p", default="petvet", help="Urun (petvet, kuafor, panola_erp)")
    p.add_argument("--type", "-t", default="single_image_tip", help="Icerik tipi")
    p.add_argument("--topic", help="Konu")
    p.add_argument("--pillar", help="Icerik sutunu")
    p.add_argument("--context", help="Ek baglam")
    p.set_defaults(func=cmd_generate)

    # plan-week
    p = sub.add_parser("plan-week", help="Haftalik plan olustur")
    p.add_argument("--product", "-p", default="petvet")
    p.add_argument("--week-start", help="Hafta baslangici (YYYY-MM-DD)")
    p.set_defaults(func=cmd_plan_week)

    # generate-week
    p = sub.add_parser("generate-week", help="Haftalik plan + tum icerikler")
    p.add_argument("--product", "-p", default="petvet")
    p.add_argument("--week-start", help="Hafta baslangici (YYYY-MM-DD)")
    p.add_argument("--job-id", help="generation_jobs takip ID (async)")
    p.add_argument("--force", action="store_true", help="Mevcut plani replace et")
    p.set_defaults(func=cmd_generate_week)

    # publish
    p = sub.add_parser("publish", help="Icerigi yayinla")
    p.add_argument("--id", type=int, required=True, help="Icerik ID")
    p.set_defaults(func=cmd_publish)

    # publish-scheduled
    p = sub.add_parser("publish-scheduled", help="Zamanlanmis icerikleri yayinla")
    p.set_defaults(func=cmd_publish_scheduled)

    # approve
    p = sub.add_parser("approve", help="Icerigi onayla")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(func=cmd_approve)

    # schedule
    p = sub.add_parser("schedule", help="Icerik zamanla")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--at", required=True, help="Zamanlama (ISO: 2026-04-01T09:00:00)")
    p.set_defaults(func=cmd_schedule)

    # list
    p = sub.add_parser("list", help="Icerik listele")
    p.add_argument("--status", "-s", help="Filtre: draft, approved, scheduled, published, failed")
    p.add_argument("--product", "-p", help="Urun filtresi")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    # calendar
    p = sub.add_parser("calendar", help="Icerik takvimi")
    p.add_argument("--product", "-p")
    p.set_defaults(func=cmd_calendar)

    # collect-metrics
    p = sub.add_parser("collect-metrics", help="Instagram metriklerini topla")
    p.set_defaults(func=cmd_collect_metrics)

    # report
    p = sub.add_parser("report", help="Haftalik performans raporu")
    p.set_defaults(func=cmd_report)

    # stats
    p = sub.add_parser("stats", help="Icerik istatistikleri")
    p.set_defaults(func=cmd_stats)

    # init-db
    p = sub.add_parser("init-db", help="Veritabanini baslat")
    p.set_defaults(func=cmd_init_db)



    # generate-image
    p = sub.add_parser("generate-image", help="Mevcut icerik icin gorsel uret")
    p.add_argument("--id", type=int, required=True, help="Icerik ID")
    p.set_defaults(func=cmd_generate_image)

    # token-check
    p = sub.add_parser('token-check', help='Instagram token durumu')
    p.set_defaults(func=cmd_token_check)

    # token-refresh
    p = sub.add_parser('token-refresh', help='Instagram token yenile')
    p.set_defaults(func=cmd_token_refresh)

    # token-auto
    p = sub.add_parser('token-auto', help='Token kontrol + gerekirse yenile')
    p.set_defaults(func=cmd_token_auto)

    # Renderhane job status
    p = sub.add_parser("job-status", help="Renderhane job durumu sorgula")
    p.add_argument("--job-id", required=True, help="Job ID")
    p.set_defaults(func=cmd_rh_status)

    # Renderhane generate
    p = sub.add_parser("rh-generate", help="Renderhane ile gorsel uret")
    p.add_argument("--tool", required=True, help="Tool: text-to-image, scene, bg-remove")
    p.add_argument("--prompt", default="", help="Prompt")
    p.add_argument("--image-url", default=None, help="Kaynak gorsel URL")
    p.add_argument("--tier", default="fast", help="Tier: fast, standard, premium")
    p.set_defaults(func=cmd_rh_generate)


    # smart-approve
    p = sub.add_parser("smart-approve", help="Kalite kapisi + onay + zamanlama")
    p.set_defaults(func=cmd_smart_approve)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()


# --- Renderhane API commands ---


