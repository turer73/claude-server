#!/usr/bin/env python3
"""SEO CTR-watch — PR#223 'arena yks' title-fix etkisini otomatik izle (kendini-emekliye-ayıran).

Plan #654: bilge-arena homepage <title> CTR-fix'inin (ALT-A, 'Arena YKS' bitişik) GSC etkisini
doğrula. Akış: PR#223 merge olana kadar NO-OP; merge sonrası +1/+2/+4 hafta checkpoint'lerinde
'arena yks' CTR/pos'u baseline'la karşılaştırır, bulguyu ortak-hafıza NOT'una yazar (SessionStart-
görünür). +4 hafta (week4) sonra final verdict + concluded → kalıcı NO-OP (kendini emekliye ayırır).
Telegram yok (seo-gsc deseni). State: data/seo-ctr-watch-state.json.

seo-gsc.py helper'larını (token+GSC) yeniden kullanır — ayrı auth yok.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
from datetime import UTC, date, datetime
from typing import Any

ROOT = "/opt/linux-ai-server"
STATE_FILE = os.environ.get("CTR_WATCH_STATE", os.path.join(ROOT, "data", "seo-ctr-watch-state.json"))
ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", f"{ROOT}/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")

PR_NUM = "223"
PR_REPO = "turer73/bilge-arena"
PROP = "sc-domain:bilgearena.com"
QUERY = "arena yks"
PAGE = "https://bilgearena.com/"
# Merge-ÖNCESİ baseline (2026-06-17, 28g): 'arena yks' → bilgearena.com/
BASE_CTR = 1.92
BASE_POS = 4.42
BASE_IMPR = 104
# (checkpoint adı, merge'den sonra min gün)
CHECKPOINTS = [("week1", 7), ("week2", 14), ("week4", 28)]
CTR_TARGET = 3.0  # surer-realist hedef %; üstü = başarılı

# seo-gsc helper'larını import et (token + _api + _post_json + _envget).
# Yol __file__'a göre (CI checkout'u /opt'ta DEĞİL — hardcoded ROOT import'u CI'da patlatır).
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("seogsc", os.path.join(_HERE, "seo-gsc.py"))
assert _spec is not None, "seo-gsc.py spec yüklenemedi"
assert _spec.loader is not None, "seo-gsc.py loader yok"
gsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gsc)


# ── Saf fonksiyonlar (mock'la test edilir) ──────────────────────────────────


def due_checkpoints(days_since_merge: int, reported: list[str]) -> list[str]:
    """Vadesi gelmiş ama henüz raporlanmamış checkpoint adları."""
    return [name for name, mind in CHECKPOINTS if days_since_merge >= mind and name not in reported]


def verdict(current_ctr: float, current_pos: float, is_final: bool) -> str:
    """Baseline'a göre CTR/pos hareketini yorumla."""
    b_ctr, b_pos = BASE_CTR, BASE_POS
    dctr = current_ctr - b_ctr
    pos_note = ""
    if current_pos < b_pos - 0.3:
        pos_note = f" + pozisyon iyileşti ({b_pos}→{current_pos:.2f})"
    elif current_pos > b_pos + 0.3:
        pos_note = f" + pozisyon geriledi ({b_pos}→{current_pos:.2f})"
    if current_ctr >= CTR_TARGET:
        return f"✅ BAŞARILI — CTR %{b_ctr}→%{current_ctr:.2f} (hedef %{CTR_TARGET} aşıldı){pos_note}"
    if dctr > 0.5:
        return f"🟢 İYİLEŞME — CTR %{b_ctr}→%{current_ctr:.2f} (+{dctr:.2f}p, yükseliyor){pos_note}"
    if dctr < -0.5:
        return f"🔴 GERİLEME — CTR %{b_ctr}→%{current_ctr:.2f} ({dctr:.2f}p){pos_note}"
    base = f"⚪ DEĞİŞİM YOK — CTR %{b_ctr}→%{current_ctr:.2f} (~aynı){pos_note}"
    if is_final:
        base += " → title-fix ETKİSİZ görünüyor; başlık revize/geri-al değerlendir."
    return base


# ── I/O ──────────────────────────────────────────────────────────────────────


def load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE) as fh:
            result: dict[str, Any] = json.load(fh)
            return result
    except (OSError, ValueError):
        return {"merged_at": None, "reported": [], "concluded": False}


def save_state(st: dict[str, Any]) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh, indent=2)


def pr_merge_date() -> tuple[str | None, str]:
    """(mergedAt-ISO veya None, err). gh ile PR#223 durumu."""
    try:
        out = subprocess.run(
            ["gh", "pr", "view", PR_NUM, "--repo", PR_REPO, "--json", "state,mergedAt"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if out.returncode != 0:
            return None, f"gh rc={out.returncode}: {out.stderr.strip()[:100]}"
        d = json.loads(out.stdout or "{}")
        if d.get("state") == "MERGED" and d.get("mergedAt"):
            return d["mergedAt"], ""
        return None, ""
    except Exception as e:
        return None, f"gh exception: {str(e)[:100]}"


def pull_current() -> tuple[float, float, int] | None:
    """GSC 'arena yks' → bilgearena.com/ : (ctr%, pos, impr). seo-gsc helper'ları."""
    token, err = gsc._acquire_token()
    if err:
        raise RuntimeError(f"GSC auth: {err}")
    from datetime import timedelta

    enc = urllib.parse.quote(PROP, safe="")
    end = datetime.now(UTC).date()
    start = end - timedelta(days=28)
    r = gsc._api(
        token,
        f"sites/{enc}/searchAnalytics/query",
        {
            "startDate": str(start),
            "endDate": str(end),
            "dimensions": ["query", "page"],
            "dimensionFilterGroups": [{"filters": [{"dimension": "query", "operator": "equals", "expression": QUERY}]}],
            "rowLimit": 10,
        },
    )
    for row in r.get("rows", []):
        _q, pg = row.get("keys", ["", ""])
        if pg == PAGE:
            return round(row.get("ctr", 0) * 100, 2), round(row.get("position", 0), 2), int(row.get("impressions", 0))
    return None


def write_note(checkpoint: str, body: str) -> str:
    """Checkpoint sonucu → ortak-hafıza NOT'u (SessionStart-görünür). Telegram yok."""
    mkey = gsc._envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    try:
        gsc._post_json(
            f"{API_BASE}/api/v1/memory/notes",
            {"from_device": "klipper", "title": f"SEO-CTR-watch: 'arena yks' {checkpoint}", "content": body},
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    st = load_state()
    if st.get("concluded"):
        print("OUTCOME: pass | deney sonuçlandı (week4), izleme emekli — no-op")
        return 0

    merged_at, err = pr_merge_date()
    if err:
        print(f"OUTCOME: partial | gh kontrol hatası: {err}")
        return 0
    if not merged_at and not st.get("merged_at"):
        print("OUTCOME: pass | PR#223 henüz merge olmadı — izleme beklemede")
        return 0

    reported = st.setdefault("reported", [])
    # Merge ilk kez tespit
    if merged_at and not st.get("merged_at"):
        st["merged_at"] = merged_at
        if "merge" not in reported:
            werr = write_note(
                "merge-başladı",
                (
                    f"PR#223 merge oldu ({merged_at[:10]}). 'arena yks' CTR-fix izlemesi başladı.\n"
                    f"Baseline (merge öncesi): CTR %{BASE_CTR}, pos {BASE_POS}, "
                    f"{BASE_IMPR} gösterim. +1/+2/+4 hafta karşılaştırılacak (plan #654)."
                ),
            )
            if not werr:
                reported.append("merge")

    md = datetime.fromisoformat(st["merged_at"].replace("Z", "+00:00")).date()
    days = (date.today() - md).days
    due = due_checkpoints(days, reported)

    raised, errs = 0, []
    if due:
        try:
            cur = pull_current()
        except Exception as e:
            print(f"OUTCOME: partial | GSC çekilemedi: {str(e)[:100]}")
            save_state(st)
            return 0
        ctr = pos = 0.0
        impr = 0
        if cur is None:
            body = f"'arena yks' → {PAGE} için GSC satırı yok (gösterim düştü?). Baseline %{BASE_CTR}."
        else:
            ctr, pos, impr = cur
        for name in due:
            is_final = name == "week4"
            if cur is not None:
                v = verdict(ctr, pos, is_final)
                body = (
                    f"{name} (merge+{days}g): {v}\n"
                    f"Şu an: CTR %{ctr}, pos {pos}, {impr} gösterim | Baseline: CTR %{BASE_CTR}, pos {BASE_POS}.\n"
                    f"Plan #654. {'DENEY SONUÇLANDI.' if is_final else 'Devam izleniyor.'}"
                )
            werr = write_note(name, body)
            if werr:
                errs.append(werr)
            else:
                reported.append(name)
                raised += 1
            if is_final:
                st["concluded"] = True

    save_state(st)
    if errs:
        print(f"OUTCOME: partial | merge+{days}g, {raised} checkpoint, MEMORY-FAIL: {errs[0]}")
    else:
        print(f"OUTCOME: pass | merge+{days}g, {raised} yeni checkpoint raporlandı (no-op ise 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
