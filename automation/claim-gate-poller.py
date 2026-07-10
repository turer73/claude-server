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
    kısa-ad ('claude-server') formunda eşlenir (ajanlar kısa-ad yazıyor)."""
    short = repo_full.split("/")[-1]
    row = db.execute(
        "SELECT device, task_key, expires_at FROM active_claims "
        "WHERE active=1 AND expires_at >= datetime('now') AND branch=? AND repo IN (?, ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (branch, repo_full, short),
    ).fetchone()
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


def _gh(token: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(  # noqa: S310 (sabit https GH API)
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        method="POST" if payload else "GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def process_repo(db: Any, token: str, repo: str, enforce: bool) -> tuple[int, int]:
    """(islenen-PR, status-POST-hatasi) döndürür."""
    prs = _gh(token, f"{GH_API}/repos/{repo}/pulls?state=open&per_page=50")
    posted_fail = 0
    for pr in prs:
        branch = pr["head"]["ref"]
        sha = pr["head"]["sha"]
        labels = {lbl["name"] for lbl in pr.get("labels", [])}
        claim = check_claim(db, repo, branch)
        state, desc = decide(claim, enforce, OVERRIDE_LABEL in labels)
        line = f"PR#{pr['number']} {repo}@{branch}: {state or 'SKIP'} — {desc}"
        print(line)
        if state is None:
            continue
        try:
            _gh(token, f"{GH_API}/repos/{repo}/statuses/{sha}", {"state": state, "context": STATUS_CONTEXT, "description": desc[:130]})
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
