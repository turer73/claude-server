"""G6 enforcement-ladder değerlendirme çekirdeği (saf-stdlib, test-edilebilir).

Tasarım: docs/g6-enforcement-ladder-design.md §3-4. RECOMMEND-ONLY — branch-protection'ı
DEĞİŞTİRMEZ, yalnız promote/demote/hold ÖNERİSİ üretir (insan-aktüasyon Turgut'ta, §4).

Production-filtre (spec-verify #100406, klipper #100402): repro-gate/g4-invariant yalnız
pull_request'te koşar → saf-branch-filtre tüm-veriyi eler. "production" = dev-iterasyon
TEKİLLEŞTİRME: her (gate_id, pr_number) için SON run (aynı-PR'ın 5-push'u 1-sayılır);
pr_number NULL olanlar run_id-başına tekil. Aynı-bug'ın tekrar-sayımı precision'ı şişirmez.

Öneri motoru DETERMİNİSTİK (aynı-veri → aynı-öneri), eşikler env-override'lı (kalibrasyon §7).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

# Eşikler — tasarım §3 varsayılanları (ilk-değerler, G6-çalıştıkça kalibre §7).
MIN_FIRINGS = 20
PROMOTE_THRESHOLD = 0.95
DEMOTE_THRESHOLD = 0.70
MIN_GT = 0.5  # human_classified_fraction alt-sınırı (fail-safe: hepsi-unknown → öneri-yok)


@dataclass(frozen=True)
class GateStats:
    gate_id: str
    firing: int  # production-dedup sonrası benzersiz-run sayısı
    tc_human: int
    fp_human: int
    unclassified: int

    @property
    def human_classified(self) -> int:
        return self.tc_human + self.fp_human

    @property
    def precision(self) -> float | None:
        return self.tc_human / self.human_classified if self.human_classified else None

    @property
    def human_fraction(self) -> float:
        return self.human_classified / self.firing if self.firing else 0.0


def production_stats(conn: sqlite3.Connection, days: int = 30) -> dict[str, GateStats]:
    """Production-dedup'lı gate-istatistikleri. Her (gate_id, pr_number) → SON run (window-fn).
    NULL pr_number: run_id-başına tekil (COALESCE ile ayrı-grup). Sadece verdict='fail'
    kayıtları FP-sınıflaması taşır (pass/skip terfi-precision'ına girmez — fail-yakalamaların
    doğruluğunu ölçüyoruz)."""
    # gate_telemetry (G2) yoksa boş — G6 tek-başına çağrılırsa 'no such table' yerine no-op.
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gate_telemetry'").fetchone():
        return {}
    rows = conn.execute(
        """
        WITH dedup AS (
            SELECT gate_id, verdict, fp_class, fp_source,
                   ROW_NUMBER() OVER (
                       PARTITION BY gate_id, COALESCE('pr' || pr_number, 'run' || run_id)
                       ORDER BY run_id DESC
                   ) AS rn
              FROM gate_telemetry
             WHERE ts >= datetime('now', ?)
        )
        SELECT gate_id,
               SUM(verdict='fail')                                        AS firing,
               SUM(fp_class='true_catch'     AND fp_source='human')       AS tc_human,
               SUM(fp_class='false_positive' AND fp_source='human')       AS fp_human,
               SUM(verdict='fail' AND fp_class='unknown')                 AS unclassified
          FROM dedup
         WHERE rn = 1
         GROUP BY gate_id
        """,
        (f"-{int(days)} days",),
    ).fetchall()
    out: dict[str, GateStats] = {}
    for gate_id, firing, tc, fp, unc in rows:
        out[gate_id] = GateStats(gate_id, firing or 0, tc or 0, fp or 0, unc or 0)
    return out


@dataclass(frozen=True)
class Recommendation:
    gate_id: str
    current_rung: str
    action: str  # 'promote' | 'demote' | 'hold'
    reason: str


def evaluate(rung: str, s: GateStats) -> Recommendation:
    """Tek gate için deterministik öneri (tasarım §3). Basamak + istatistik → aksiyon."""
    base = f"firing={s.firing} precision={s.precision} human_frac={round(s.human_fraction, 2)}"

    if rung == "non_required":
        # Terfi: 4 koşul da (§3). Sıra: veri-yeterliliği önce (thin-data'da precision-anlamsız).
        if s.firing < MIN_FIRINGS:
            return Recommendation(s.gate_id, rung, "hold", f"yetersiz-firing (<{MIN_FIRINGS}); {base}")
        if s.human_fraction < MIN_GT:
            return Recommendation(s.gate_id, rung, "hold", f"yetersiz-ground-truth (<{MIN_GT}); fp-mark gerek; {base}")
        if s.precision is not None and s.precision >= PROMOTE_THRESHOLD:
            return Recommendation(s.gate_id, rung, "promote", f"terfi-uygun (≥{PROMOTE_THRESHOLD}); {base}")
        return Recommendation(s.gate_id, rung, "hold", f"precision-yetersiz (<{PROMOTE_THRESHOLD}); {base}")

    if rung == "required":
        # Düşürme: Goodhart-drift (§3). Precision hesaplanabiliyor VE eşik-altı.
        if s.precision is not None and s.precision < DEMOTE_THRESHOLD:
            return Recommendation(s.gate_id, rung, "demote", f"drift (<{DEMOTE_THRESHOLD}); {base}")
        return Recommendation(s.gate_id, rung, "hold", f"required-stabil; {base}")

    # shadow/demoted/off: otomatik-öneri yok (insan-yönetir; §2).
    return Recommendation(s.gate_id, rung, "hold", f"{rung}: otomatik-öneri-dışı; {base}")


def run_eval(conn: sqlite3.Connection, days: int = 30) -> list[Recommendation]:
    """Tüm kayıtlı gate'leri değerlendir + gate_ladder.last_eval/history güncelle.
    Basamak-DEĞİŞTİRMEZ (recommend-only); yalnız öneriyi history'e ekler (denetim-izi)."""
    stats = production_stats(conn, days)
    ladder = conn.execute("SELECT gate_id, rung, history_json FROM gate_ladder ORDER BY gate_id").fetchall()
    recs: list[Recommendation] = []
    for gate_id, rung, history_json in ladder:
        s = stats.get(gate_id, GateStats(gate_id, 0, 0, 0, 0))
        rec = evaluate(rung, s)
        recs.append(rec)
        history = json.loads(history_json or "[]")
        history.append({"action": rec.action, "reason": rec.reason})
        conn.execute(
            "UPDATE gate_ladder SET last_eval=datetime('now'), history_json=? WHERE gate_id=?",
            (json.dumps(history[-50:], ensure_ascii=False), gate_id),  # son-50 tut (sınırsız-büyüme önle)
        )
    conn.commit()
    return recs


def format_report(recs: list[Recommendation], unclassified: dict[str, int]) -> str:
    """İnsan+note-okur özet. unclassified_rate GÖRÜNÜR (mark-borcu, tasarım §7)."""
    lines = ["# G6 enforcement-ladder değerlendirmesi (recommend-only — aktüasyon Turgut'ta)"]
    for r in recs:
        flag = {"promote": "⬆ TERFİ-ÖNERİSİ", "demote": "⬇ DÜŞÜR-ÖNERİSİ", "hold": "· hold"}[r.action]
        unc = unclassified.get(r.gate_id, 0)
        lines.append(f"{flag}  {r.gate_id} [{r.current_rung}] — {r.reason}" + (f" | işaretlenmemiş-fail={unc}" if unc else ""))
    actionable = [r for r in recs if r.action != "hold"]
    lines.append("")
    lines.append(f"# {len(actionable)} aktüasyon-önerisi (Turgut onayıyla gate-promote.sh/gate-demote.sh)")
    return "\n".join(lines)
