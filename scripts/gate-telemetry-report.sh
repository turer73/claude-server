#!/bin/bash
# gate-telemetry-report.sh — G2 precision-özeti: gate-başı firing/verdict/FP-oranı (son-30gün).
# Tasarım: docs/g2-gate-telemetry-design.md §2d. G6-ladder bu çıktıyı okur:
# düşük-FP + yeterli-firing (>=20) → REQUIRED-terfi önerisi; yüksek-FP → gözden-geçir.
#
# precision = true_catch / (true_catch + false_positive) — yalnız HUMAN-sınıflı kayıtlar
# (heuristik-aday sayılmaz; unknown paydaya girmez → az-veri-üstünden-hüküm verilmez).

set -euo pipefail

DB="${COVERAGE_DB:-/opt/linux-ai-server/data/coverage.db}"
DAYS="${REPORT_DAYS:-30}"
# #1249: DAYS SQL'e interpolate ediliyor — sayısal-guard (gate-fp-mark run_id-deseniyle tutarlı).
case "$DAYS" in
    ''|*[!0-9]*) echo "HATA: REPORT_DAYS sayısal olmalı (verilen: $DAYS)" >&2; exit 1 ;;
esac

sqlite3 -header -column "$DB" "
SELECT gate_id,
       COUNT(*)                                                    AS firing,
       SUM(verdict='pass')                                         AS pass,
       SUM(verdict='fail')                                         AS fail,
       SUM(verdict='skip_na')                                      AS skip_na,
       SUM(fp_class='true_catch'     AND fp_source='human')        AS tc_human,
       SUM(fp_class='false_positive' AND fp_source='human')        AS fp_human,
       SUM(fp_class='unknown')                                     AS unclassified,
       CASE WHEN SUM(fp_class IN ('true_catch','false_positive') AND fp_source='human') > 0
            THEN ROUND(1.0 * SUM(fp_class='true_catch' AND fp_source='human')
                 / SUM(fp_class IN ('true_catch','false_positive') AND fp_source='human'), 3)
            ELSE NULL END                                          AS precision
  FROM gate_telemetry
 WHERE ts >= datetime('now', '-' || $DAYS || ' days')
 GROUP BY gate_id
 ORDER BY gate_id;"

echo ""
echo "# fail-firing'ler (sınıflandırma-adayları; gate-fp-mark.sh ile human-işaretle):"
sqlite3 -header -column "$DB" "
SELECT gate_id, run_id, pr_number, branch, ts, fp_class, COALESCE(fp_source,'-') AS src
  FROM gate_telemetry
 WHERE verdict='fail' AND ts >= datetime('now', '-' || $DAYS || ' days')
 ORDER BY ts DESC LIMIT 20;"
