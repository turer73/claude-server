"""Digest sunum katmanı — text/HTML render + sinyal-kontrol (saf; gather'ın döndürdüğü
dict'i biçimlendirir, collector çağırmaz)."""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

from app.core.digest.sources import WINDOW_HOURS


def _trend_tokens(trend: list[dict[str, Any]]) -> list[str]:
    """Compact per-project change tokens, e.g. '↑bilge-arena +6', '⊘old-proj'."""
    toks: list[str] = []
    for c in trend:
        if c["kind"] == "dropped":
            toks.append(f"⊘{c['project']}")
        elif c["kind"] == "new":
            toks.append(f"+{c['project']}(yeni)")
        else:
            arrow = "↑" if c["delta"] > 0 else "↓"
            sign = f"+{c['delta']}" if c["delta"] > 0 else str(c["delta"])
            toks.append(f"{arrow}{c['project']} {sign}")
    return toks


def has_signal(d: dict[str, Any]) -> bool:
    """Decide whether to emit at all — 'NOTHING_NEW' if nothing actionable."""
    m = d["memory"]
    if m["new_bugs"] or m["unread_notes"]:
        return True
    if any(v for v in d["commits"].values()):
        return True
    sp = d["cron"].get("self_pentest")
    if sp and sp["findings"]:
        return True
    if d["system"]["service"] != "active":
        return True
    v = d.get("vps") or {}
    if v and (not v.get("online") or (v.get("cpu") or 0) >= 90 or (v.get("mem") or 0) >= 90 or (v.get("disk") or 0) >= 90):
        return True
    if (d.get("cron_jobs") or {}).get("bad"):
        return True
    lv = d.get("liveness") or {}
    if lv.get("dead") or lv.get("stale"):
        return True
    pr = d.get("pr_review") or {}
    if pr.get("signaled") or pr.get("fetch_fail"):
        return True
    ci = d.get("ci") or {}
    return bool(ci and ((ci.get("failed") or 0) > 0 or ci.get("stale") or ci.get("regressions")))


def render_text(d: dict[str, Any]) -> str:
    L: list[str] = []
    today = dt.date.today().isoformat()
    L.append(f"═══ Digest — {today} ═══")
    L.append("")
    m = d["memory"]
    L.append(f"Açık bug ({len(m['open_bugs'])}):")
    for b in m["open_bugs"]:
        L.append(f"  [{b['project']:<22}] #{b['id']:<4} {b['title'][:70]}")
    L.append("")
    L.append(f"Son {WINDOW_HOURS}h:")
    L.append(f"  + {len(m['new_bugs'])} yeni bug, {len(m['unread_notes'])} okunmamış not")
    for b in m["new_bugs"][:5]:
        L.append(f"    yeni: [{b['project']}] #{b['id']} {b['title'][:60]}")
    L.append("")
    L.append("Commit aktivitesi:")
    any_commits = False
    for proj, commits in sorted(d["commits"].items()):
        if not commits:
            continue
        any_commits = True
        L.append(f"  {proj} ({len(commits)})")
        for c in commits[:5]:
            L.append(f"    {c['sha']} {c['msg']}")
    if not any_commits:
        L.append("  (none)")
    L.append("")
    sp = d["cron"].get("self_pentest")
    if sp:
        age_note = "bugün" if sp["age_days"] == 0 else f"{sp['age_days']}g önce"
        L.append(f"Self-pentest son: {sp['date']} ({age_note}), {len(sp['findings'])} bulgulu domain")
        for f in sp["findings"]:
            sub_parts = []
            for k in ("content", "headers", "tls", "cookies", "bundles"):
                if f[k]:
                    sub_parts.append(f"{k}={f[k]}")
            L.append(f"  ⚠ {f['domain']}: {' '.join(sub_parts)}")
    L.append("")
    cj = d.get("cron_jobs") or {}
    if cj.get("jobs"):
        bad = cj.get("bad") or []
        if bad:
            L.append(f"Cron işleri ({len(bad)} sorunlu / {len(cj['jobs'])} izlenen):")
            for j in bad:
                L.append(f"  ⚠ {j['job']}: {j['result']} (rc={j['rc']}, {j['source']}) {(j.get('detail') or '')[:60]}")
        else:
            L.append(f"Cron işleri: ✓ {len(cj['jobs'])} iş izlendi, hepsi pass")
        L.append("")
    pr = d.get("pr_review") or {}
    pr_list = pr.get("prs") or []
    if pr_list or pr.get("fetch_fail"):
        L.append(f"Açık PR'lar — review-triyaj ({len(pr_list)}):")
        for p in pr_list:
            cx = " codex:?" if p.get("codex") is None else (f" codex:{p['codex']}" if p["codex"] else "")
            L.append(f"  • {p['repo']}#{p['num']} [CI:{p['ci']}{cx}] {p['title']}")
        if pr.get("fetch_fail"):
            L.append("  ⚠ fetch-fail: bir+ repo taranamadı (eksik olabilir)")
        L.append("")
    lv = d.get("liveness") or {}
    bad_lv = (lv.get("dead") or []) + (lv.get("stale") or [])
    if bad_lv:
        L.append(f"Liveness ({len(lv.get('dead') or [])} ölü / {len(lv.get('stale') or [])} stale):")
        for r in bad_lv:
            L.append(f"  {'☠' if r['status'] == 'dead' else '⚠'} {r['source']} [{r['klass']}]: {r['detail'][:55]}")
        L.append("")
    s = d["system"]
    svc_glyph = "✓" if s["service"] == "active" else "✗"
    L.append(
        f"Sistem: {svc_glyph} linux-ai-server {s['service']}  |  "
        f"disk {s['disk_used_pct']} (free {s['disk_avail']})  |  "
        f"ram {s['mem_used_mb']}/{s['mem_total_mb']} MB"
    )
    v = d.get("vps") or {}
    if v:
        if v.get("online"):
            L.append(
                f"VPS: ✓ cpu {v['cpu']:.0f}%  |  ram {v['mem']:.0f}%  |  "
                f"disk {v['disk']:.0f}%  |  {v['containers_up']}/{v['containers_total']} container"
            )
        else:
            L.append("VPS: ✗ erişilemiyor")
    ci = d.get("ci") or {}
    if ci:
        age = "?" if ci["age_days"] is None else f"{ci['age_days']}g önce"
        stale = " ⚠ BAYAT" if ci.get("stale") else ""
        L.append(f"CI: son run {ci['started_at'][:10]} ({age}{stale})  |  {ci['passed']}/{ci['total']} geçti, {ci['failed']} fail")
        for fp in ci.get("failing_projects", []):
            L.append(f"  ✗ {fp['project']}: {fp['passed']}/{fp['total']}")
        toks = _trend_tokens(ci.get("trend", []))
        if toks:
            L.append("  trend (vs önceki run): " + ", ".join(toks))
    return "\n".join(L)


def render_html(d: dict[str, Any]) -> str:
    """Telegram parse_mode=HTML — only <b>, <i>, <code>, <pre> are safe (no <br>)."""
    today = dt.date.today().isoformat()
    m = d["memory"]
    esc = html.escape  # serbest-metin (bug/commit/cron/liveness) parse_mode=HTML'i bozmasın
    parts: list[str] = []
    parts.append(f"<b>Digest — {today}</b>")
    parts.append("")
    parts.append(f"<b>Açık bug ({len(m['open_bugs'])})</b>")
    for b in m["open_bugs"][:10]:
        parts.append(f"  [<code>{esc(b['project'])}</code>] #{b['id']} {esc(b['title'][:70])}")
    if len(m["open_bugs"]) > 10:
        parts.append(f"  … (+{len(m['open_bugs']) - 10})")
    parts.append("")
    parts.append(f"<b>Son {WINDOW_HOURS}h:</b> +{len(m['new_bugs'])} yeni bug / {len(m['unread_notes'])} okunmamış not")
    parts.append("")
    parts.append("<b>Commit:</b>")
    any_commits = False
    for proj, commits in sorted(d["commits"].items()):
        if not commits:
            continue
        any_commits = True
        parts.append(f"  <i>{esc(proj)}</i> ({len(commits)})")
        for c in commits[:3]:
            parts.append(f"    <code>{c['sha']}</code> {esc(c['msg'])}")
    if not any_commits:
        parts.append("  (none)")
    parts.append("")
    sp = d["cron"].get("self_pentest")
    if sp and sp["findings"]:
        parts.append(f"<b>Pentest ({sp['date']}):</b> {len(sp['findings'])} bulgulu")
        for f in sp["findings"]:
            sub = ", ".join(f"{k}={f[k]}" for k in ("content", "headers", "tls", "cookies", "bundles") if f[k])
            parts.append(f"  ⚠ <code>{esc(f['domain'])}</code> {esc(sub)}")
    cj = d.get("cron_jobs") or {}
    if cj.get("bad"):
        parts.append(f"<b>Cron ({len(cj['bad'])} sorunlu / {len(cj['jobs'])}):</b>")
        for j in cj["bad"]:
            parts.append(f"  ⚠ <code>{esc(j['job'])}</code> {esc(j['result'])} (rc={j['rc']}) {esc((j.get('detail') or '')[:50])}")
    elif cj.get("jobs"):
        parts.append(f"<b>Cron:</b> ✓ {len(cj['jobs'])} iş pass")
    pr = d.get("pr_review") or {}
    pr_list = pr.get("prs") or []
    if pr_list or pr.get("fetch_fail"):
        parts.append(f"<b>Açık PR — review ({len(pr_list)}):</b>")
        for p in pr_list:
            cx = " codex:?" if p.get("codex") is None else (f" codex:{p['codex']}" if p["codex"] else "")
            # PR title HTML-escape: <>& parse_mode=HTML dijesti bozabilir (Codex-P2).
            parts.append(f"  • <code>{p['repo']}#{p['num']}</code> [CI:{p['ci']}{cx}] {esc(p['title'])}")
        if pr.get("fetch_fail"):
            parts.append("  ⚠ fetch-fail: bir+ repo taranamadı")
    lv = d.get("liveness") or {}
    bad_lv = (lv.get("dead") or []) + (lv.get("stale") or [])
    if bad_lv:
        parts.append(f"<b>Liveness ({len(lv.get('dead') or [])} ölü / {len(lv.get('stale') or [])} stale):</b>")
        for r in bad_lv:
            parts.append(f"  {'☠' if r['status'] == 'dead' else '⚠'} <code>{esc(r['source'])}</code> {esc(r['detail'][:50])}")
    s = d["system"]
    parts.append(f"<b>Sistem:</b> {s['service']} | disk {s['disk_used_pct']} | ram {s['mem_used_mb']}/{s['mem_total_mb']}MB")
    v = d.get("vps") or {}
    if v:
        if v.get("online"):
            parts.append(
                f"<b>VPS:</b> cpu {v['cpu']:.0f}% | ram {v['mem']:.0f}% | "
                f"disk {v['disk']:.0f}% | {v['containers_up']}/{v['containers_total']} container"
            )
        else:
            parts.append("<b>VPS:</b> ✗ erişilemiyor")
    ci = d.get("ci") or {}
    if ci:
        age = "?" if ci["age_days"] is None else f"{ci['age_days']}g önce"
        stale = " ⚠ BAYAT" if ci.get("stale") else ""
        fp = ci.get("failing_projects", [])
        fp_note = (" — fail: " + ", ".join(esc(p["project"]) for p in fp)) if fp else ""
        parts.append(
            f"<b>CI:</b> {ci['started_at'][:10]} ({age}{stale}) | {ci['passed']}/{ci['total']} geçti, {ci['failed']} fail{fp_note}"
        )
        toks = _trend_tokens(ci.get("trend", []))
        if toks:
            parts.append("  <i>trend:</i> " + ", ".join(toks))
    return "\n".join(parts)
