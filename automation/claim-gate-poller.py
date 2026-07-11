#!/usr/bin/env python3
"""Claim-gate poller (koordinasyon-güvenlik paketi 3/3) — DB-CLAIM'i PR'a ZORLAYAN köprü.

Neden bot: GitHub Actions runner'ı Tailscale'e erişemez → gate memory-DB'nin yanında
(klipper'da) koşmalı. Açık PR'ların head-branch'ini active_claims ile eşler, GitHub
commit-status yazar; branch-protection bu status'u required yapınca zorlama tamamlanır.

Modlar (enforcement-ladder, soft-gate dersi — DEFAULT-OFF):
- CLAIM_GATE_ENFORCE=0 (default, advisory): claim VARSA success-status yazar (görünürlük);
  claim YOKSA status YAZMAZ, yalnız loglar — yokken success basmak yanlış-güven, failure
  basmak merge'i fiilen bloklar (required olmasa da kırmızı korkutur). Telemetri biriksin.
- CLAIM_GATE_ENFORCE=1: claim yoksa failure-status (branch-protection required ile blok).
  Acil-kaçış: PR'a 'claim-override' etiketi → success + AUDIT log (watchdog-FP dersi:
  fail-closed ama kaçış-kapılı).

Ortam: GH_TOKEN (veya GITHUB_TOKEN; .env fallback), CLAIM_GATE_REPOS="turer73/claude-server,..."
Cron: klipper-cron-wrap.sh claim-gate (5dk) — OUTCOME marker'lı. Salt-okunur DB erişimi.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys as _sys
import urllib.request
from pathlib import Path as _Path
from typing import Any

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo-root (app.db.data_layer)

from app.db.data_layer import MEMORY_DB, get_conn

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
GH_API = "https://api.github.com"
STATUS_CONTEXT = "claim-gate"
OVERRIDE_LABEL = "claim-override"
STALE_CLEAR_STATE = "pending"
STALE_CLEAR_DESC = "advisory: claim released — sonraki poll'da yeniden-degerlendirilecek"


def _envget(key: str) -> str:
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def check_claim(db: Any, repo_full: str, branch: str) -> dict[str, Any] | None:
    """Aktif + süresi-dolmamış claim ara. repo hem tam-ad ('turer73/claude-server') hem
    kısa-ad ('claude-server') formunda eşlenir (ajanlar kısa-ad yazıyor).
    active_claims tablosu lazy-kurulur (app/api/memory/claims.py::_ensure_claims) — ilk
    /claims isteğinden önce hiç yok. Taze deploy'da bu SELECT 'no such table' patlardı
    (Codex#304 bulgu-3); tablo-yok = claim-yok (boş-küme), crash değil."""
    short = repo_full.split("/")[-1]
    try:
        row = db.execute(
            "SELECT device, task_key, expires_at FROM active_claims "
            "WHERE active=1 AND expires_at >= datetime('now') AND branch=? AND repo IN (?, ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (branch, repo_full, short),
        ).fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return None
        raise
    return dict(row) if row else None


def decide(claim: dict[str, Any] | None, enforce: bool, has_override: bool) -> tuple[str | None, str]:
    """(state, description) — state=None: status YAZMA (advisory-modda claim-yok).
    Karar-tablosu saf-fonksiyon: test edilebilir, GH'siz."""
    if claim:
        return "success", f"claim: {claim['device']} ({claim['task_key']}, bitiş {claim['expires_at']})"
    if has_override:
        return "success", "claim-override etiketi (AUDIT: insan-onaylı kaçış-kapısı)"
    if enforce:
        return "failure", "aktif claim yok — önce POST /api/v1/memory/claims (CLAIM protokolü)"
    return None, "advisory: claim yok (status yazılmadı, log-only)"


def _gh(token: str, url: str, payload: dict[str, Any] | None = None) -> tuple[Any, str]:
    """(body, link-header) döner — link boş-string olabilir (son-sayfa/POST)."""
    req = urllib.request.Request(  # noqa: S310 (sabit https GH API)
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        method="POST" if payload else "GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}"), resp.headers.get("Link", "")


def _next_page_url(link_header: str) -> str | None:
    """RFC-5988 Link-header'dan rel="next" URL'i çıkar (Codex#304 bulgu-2: 50-PR limiti)."""
    for part in link_header.split(","):
        seg = part.strip()
        if not seg or 'rel="next"' not in seg:
            continue
        start = seg.find("<")
        end = seg.find(">")
        if start != -1 and end != -1:
            return seg[start + 1 : end]
    return None


def _all_open_prs(token: str, repo: str) -> list[dict[str, Any]]:
    """Tüm açık PR'ları sayfalayarak topla — tek-sayfa (50) limiti >50-PR'lu repoda
    kalan PR'ları hiç değerlendirmiyordu (Codex#304 bulgu-2)."""
    prs: list[dict[str, Any]] = []
    url: str | None = f"{GH_API}/repos/{repo}/pulls?state=open&per_page=50"
    while url:
        body, link = _gh(token, url)
        prs.extend(body)
        url = _next_page_url(link)
    return prs


def _latest_status(token: str, repo: str, sha: str) -> dict[str, str] | None:
    """STATUS_CONTEXT için en-son-postalanan durum (state+description), hiç yoksa None."""
    body, _ = _gh(token, f"{GH_API}/repos/{repo}/commits/{sha}/status")
    for s in body.get("statuses", []):
        if s.get("context") == STATUS_CONTEXT:
            return {"state": s["state"], "description": s.get("description", "")}
    return None


def _should_post(new_state: str | None, new_desc: str, latest: dict[str, str] | None) -> tuple[str | None, str] | None:
    """Postalanacak (state, desc) döner, postalanmayacaksa None. Saf-fonksiyon (GH'siz test edilir).

    - new_state=None (advisory, claim-yok) + önceki 'success' hâlâ duruyor -> bayat-success'i
      pending ile TEMİZLE (Codex#304 bulgu-1: claim kalkınca eski yeşil GH'de asılı kalıyordu).
    - new_state=None + önceki success DEĞİL (hiç-yok/zaten-pending) -> gerçekten atla (advisory
      niyeti: claim-yokken gürültü üretme).
    - new_state dolu + önceki AYNI (state+desc) -> atla (Codex#304 bulgu-4: 1000-status/SHA
      limiti — her 5dk aynı-durumu yeniden-postalamak günler içinde limiti doldurur).
    - aksi hâlde postala.
    """
    if new_state is None:
        if latest and latest["state"] == "success":
            return STALE_CLEAR_STATE, STALE_CLEAR_DESC
        return None
    if latest and latest["state"] == new_state and latest["description"] == new_desc[:130]:
        return None
    return new_state, new_desc


def process_repo(db: Any, token: str, repo: str, enforce: bool) -> tuple[int, int]:
    """(islenen-PR, status-POST-hatasi) döndürür."""
    prs = _all_open_prs(token, repo)
    posted_fail = 0
    for pr in prs:
        branch = pr["head"]["ref"]
        sha = pr["head"]["sha"]
        labels = {lbl["name"] for lbl in pr.get("labels", [])}
        claim = check_claim(db, repo, branch)
        state, desc = decide(claim, enforce, OVERRIDE_LABEL in labels)
        try:
            latest = _latest_status(token, repo, sha)
            to_post = _should_post(state, desc, latest)
        except Exception as e:  # latest-status okunamazsa eski davranışa düş (postala)
            to_post = (state, desc) if state is not None else None
            print(f"  latest-status okunamadı ({str(e)[:80]}), fallback: postala-varsa")
        unchanged = " [postalanmadi:degismedi]" if state and not to_post else ""
        print(f"PR#{pr['number']} {repo}@{branch}: {state or 'SKIP'}{unchanged} — {desc}")
        if to_post is None:
            continue
        post_state, post_desc = to_post
        payload = {"state": post_state, "context": STATUS_CONTEXT, "description": post_desc[:130]}
        try:
            _gh(token, f"{GH_API}/repos/{repo}/statuses/{sha}", payload)
        except Exception as e:  # tek-PR hatası turu öldürmesin
            posted_fail += 1
            print(f"  status-POST hatası: {str(e)[:100]}")
    return len(prs), posted_fail


def main() -> int:
    token = _envget("GH_TOKEN") or _envget("GITHUB_TOKEN")
    repos = [r.strip() for r in _envget("CLAIM_GATE_REPOS").split(",") if r.strip()]
    enforce = _envget("CLAIM_GATE_ENFORCE").strip() == "1"
    if not token or not repos:
        print("OUTCOME: fail | GH_TOKEN/CLAIM_GATE_REPOS eksik")
        return 0
    db = get_conn(MEMORY_DB, readonly=True)
    total = fails = 0
    try:
        for repo in repos:
            try:
                n, f = process_repo(db, token, repo, enforce)
                total += n
                fails += f
            except Exception as e:
                fails += 1
                print(f"{repo}: repo-hatası {str(e)[:100]}")
    finally:
        db.close()
    mode = "enforce" if enforce else "advisory"
    if fails:
        print(f"OUTCOME: partial | claim-gate({mode}): {total} PR, {fails} hata")
    else:
        print(f"OUTCOME: pass | claim-gate({mode}): {total} PR tarandı")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
