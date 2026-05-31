"""Panola Social — Weekly Content Planner.
Generates weekly content calendar using AI + strategy config.
"""

import json
import random
from datetime import datetime, timedelta

from src.config import parse_json_response
from src.engine import get_client, load_products, load_settings, generate_content
from src.config import api_call_with_retry
from src.db import (
    create_weekly_plan,
    get_weekly_plan_by_week,
    delete_weekly_plan_for_week,
)


def compute_week_start(week_start=None):
    """Upcoming Monday (or today if Monday) as YYYY-MM-DD. Single source of truth
    shared by the planner and the rotation product selector."""
    if week_start:
        return week_start
    today = datetime.now()
    days_ahead = 7 - today.weekday()
    if days_ahead == 7:
        days_ahead = 0
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def get_rotation_product(target_week_start):
    """Deterministic weekly product rotation keyed on the TARGET week's ISO week
    number. PRODUCTS order = config/products.yml insertion order. Deterministic so
    a re-run for the same week resolves to the same product (idempotency-friendly)."""
    from datetime import date as _date
    products = list(load_products().keys())
    iso_week = _date.fromisoformat(target_week_start).isocalendar()[1]
    return products[iso_week % len(products)]


def generate_weekly_plan(product_key, week_start=None, force=False):
    """Generate a weekly content plan for a product.

    Idempotent: if a plan already exists for (week_start, product) it is returned
    as-is (skipped=True) without re-calling the LLM, unless force=True replaces it.
    """
    products = load_products()
    settings = load_settings()
    product = products[product_key]
    schedule = settings["posting"]["schedule"]

    if not week_start:
        today = datetime.now()
        days_ahead = 7 - today.weekday()
        if days_ahead == 7:
            days_ahead = 0
        week_start = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    existing = get_weekly_plan_by_week(week_start, product_key)
    if existing and not force:
        return {
            "plan_id": existing["id"],
            "week_start": week_start,
            "product": product_key,
            "days": existing["plan_data"],
            "total_posts": len(existing["plan_data"]),
            "skipped": True,
        }
    if existing and force:
        delete_weekly_plan_for_week(week_start, product_key)

    pillars = product["pillars"]

    client = get_client()

    # Get limitations from product_knowledge DB
    from src.product_knowledge import get_knowledge, get_quality_rules
    pk_data = get_knowledge(product_key)
    limitations = pk_data.get("limitations", {})
    limits_text = chr(10).join(f"  - {k}: {v}" for k, v in limitations.items()) if limitations else "Yok"
    
    # Get hard quality rules
    q_rules = get_quality_rules(product_key)
    hard_rules = [r["rule"] for r in q_rules if r.get("severity") == "hard"]
    rules_text = chr(10).join(f"  - {r}" for r in hard_rules) if hard_rules else ""

    prompt = f"""Sen {product['name']} icin sosyal medya strateji uzmanisin. Turkiye pazari icin 1 haftalik Instagram icerik takvimi olustur.

URUN: {product['name']} — {product['tagline']}
ACIKLAMA: {product['description']}
HEDEF KITLE: {product['target_audience']}
OZELLIKLER: {', '.join(product['features'])}
SORUNLAR: {', '.join(product.get('pain_points', []))}
URL: {product['url']}

SINIRLAR (bu konularda icerik URETME):
{limits_text}

YASAK KONULAR:
{rules_text}

HAFTALIK PROGRAM (sabit):
"""

    day_names = ["pazartesi", "sali", "carsamba", "persembe", "cuma", "cumartesi"]
    for day in day_names:
        if day in schedule and schedule[day]:
            s = schedule[day]
            prompt += f"- {day.title()}: Saat {s['time']}, Tip: {s['type']}, Sutun: {s['pillar']}\n"

    prompt += "\nICERIK SUTUNLARI:\n"
    for name, info in pillars.items():
        prompt += f"- {name} (%{info['ratio']}): {info['desc']}\n"

    prompt += """
HER GUN ICIN OLUSTUR:
- Spesifik konu (genel degil, somut)
- Kisa aciklama (2-3 cumle, ne anlatilacak)
- Gorsel tipi onerisi

KURALLAR:
- Art arda ayni konu/tip olmasin
- Haftanin basindan sonuna dogal bir akis olsun
- Konular urunun farkli yonlerini gostersin
- Turkiye pazarina uygun, guncel konular

JSON array olarak don:
[
  {
    "day": "pazartesi",
    "time": "09:00",
    "content_type": "educational_carousel",
    "pillar": "egitici",
    "topic": "Spesifik konu",
    "description": "Kisa aciklama",
    "visual_type": "Gorsel onerisi"
  }
]"""

    response = api_call_with_retry(client,
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    plan_data = parse_json_response(text)

    if not isinstance(plan_data, list):
        return {"error": "Plan parse failed", "raw": text}

    plan_id = create_weekly_plan(week_start, product_key, plan_data)

    return {
        "plan_id": plan_id,
        "week_start": week_start,
        "product": product_key,
        "days": plan_data,
        "total_posts": len(plan_data),
    }


def generate_week_content(product_key, week_start=None, force=False):
    """Generate plan + all content for the week. Continues on individual failures."""
    print(f"📅 Haftalik plan olusturuluyor: {product_key}...")
    plan = generate_weekly_plan(product_key, week_start, force=force)

    if "error" in plan:
        print(f"❌ Plan hatasi: {plan['error']}")
        return plan

    # Idempotency: an existing plan means content was already generated for this
    # (week_start, product). Skip regeneration entirely (no LLM, no DB writes).
    if plan.get("skipped"):
        print(f"⏭️  Plan zaten var ({plan['week_start']} {product_key}) — uretim atlandi.")
        return {"plan": plan, "contents": [], "success_count": 0,
                "fail_count": 0, "skipped": True}

    print(f"✅ Plan hazir — {plan['total_posts']} post")

    results = []
    failed = 0
    for day in plan["days"]:
        print(f"  📝 {day['day']}: {day['content_type']} — {day.get('topic', '')[:50]}...")
        try:
            content = generate_content(
                product_key=product_key,
                content_type=day["content_type"],
                topic=day.get("topic", ""),
                pillar=day.get("pillar"),
            )
            results.append({**day, "content": content})
        except Exception as e:
            failed += 1
            print(f"  ❌ {day['day']} HATA: {e}")
            results.append({**day, "content": {"error": str(e)}})

    ok = len(results) - failed
    print(f"\n✅ Haftalik icerik: {ok}/{len(results)} basarili{f', {failed} hata' if failed else ''}")
    return {"plan": plan, "contents": results, "success_count": ok, "fail_count": failed}
