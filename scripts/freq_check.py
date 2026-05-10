#!/usr/bin/env python3
"""Vocabulary frequency check for CEFR-banded items.

Cross-checks each item's target word against wordfreq Zipf scale to verify
it actually falls in the expected CEFR band — independent of what the LLM
claims. Catches "NGSL 2000-3000" hallucinations.

wordfreq corpus: COCA + Wikipedia + OpenSubtitles + Twitter blend (CC-BY,
Robyn Speer). NOT identical to COCA exclusively, but well-correlated for
content-word frequency. Use as a sanity check, not a courtroom-grade rank.

Zipf-to-CEFR mapping (research-calibrated):
  A1  : zipf >= 6.0   (top ~1K, very high frequency)
  A2  : 5.5 <= zipf < 6.0  (top 1K-3K)
  B1  : 4.5 <= zipf < 5.5  (top 3K-30K)
  B2  : 3.5 <= zipf < 4.5  (top 30K-300K)
  C1+ : zipf < 3.5    (academic / low-frequency)

Tolerance: ±0.5 zipf gray zone is acceptable. Beyond that = mismatch.

Usage:
  freq_check.py items.jsonl --expected-cefr b1
  freq_check.py items.jsonl --expected-cefr b1 --output report.jsonl
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from wordfreq import zipf_frequency
except ImportError:
    sys.exit("wordfreq not installed. Run: pip install wordfreq")

CEFR_ZIPF_RANGE = {
    "a1": (6.0, 9.0),
    "a2": (5.5, 6.0),
    "b1": (4.5, 5.5),
    "b2": (3.5, 4.5),
    "c1": (2.5, 3.5),
    "c2": (0.0, 2.5),
}


def cefr_for_zipf(z: float) -> str:
    if z >= 6.0:
        return "a1"
    if z >= 5.5:
        return "a2"
    if z >= 4.5:
        return "b1"
    if z >= 3.5:
        return "b2"
    if z >= 2.5:
        return "c1"
    return "c2"


def normalize_target(text: str) -> list[str]:
    """Extract target word(s) from option text. Handles phrasal verbs ('adjust to')."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z'\s-]", "", text)
    return [w for w in text.split() if w and w not in {"to", "the", "a", "an"}]


def assess(item: dict, expected: str, tolerance: float) -> dict:
    if item.get("skill") != "vocabulary":
        return {"i": None, "skip": True, "reason": "not vocabulary"}
    options = item.get("options", [])
    ci = item.get("correct_index", 0)
    if not options or ci >= len(options):
        return {"skip": True, "reason": "no correct option"}
    target_text = options[ci]
    words = normalize_target(target_text)
    if not words:
        return {"skip": True, "reason": "empty target"}
    # For multi-word phrases, score the rarest content word (the "carrier")
    word_zipfs = [(w, zipf_frequency(w, "en")) for w in words]
    head_word, head_z = min(word_zipfs, key=lambda x: x[1])
    actual_band = cefr_for_zipf(head_z)
    expected_lo, expected_hi = CEFR_ZIPF_RANGE[expected.lower()]
    in_band = expected_lo - tolerance <= head_z <= expected_hi + tolerance
    return {
        "target": target_text,
        "scored_word": head_word,
        "all_words_zipf": [(w, round(z, 2)) for w, z in word_zipfs],
        "zipf": round(head_z, 2),
        "actual_cefr_band": actual_band,
        "expected_cefr": expected.lower(),
        "in_band": in_band,
        "deviation": round(head_z - (expected_lo + expected_hi) / 2, 2),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("items_jsonl")
    p.add_argument("--expected-cefr", required=True, choices=list(CEFR_ZIPF_RANGE.keys()))
    p.add_argument("--tolerance", type=float, default=0.5, help="zipf tolerance gray zone (default 0.5)")
    p.add_argument("--output", help="write per-item JSONL report")
    p.add_argument("--summary-only", action="store_true")
    args = p.parse_args()

    items = [json.loads(l) for l in Path(args.items_jsonl).read_text().splitlines() if l.strip()]
    rows = []
    for idx, it in enumerate(items, 1):
        r = assess(it, args.expected_cefr, args.tolerance)
        if r.get("skip"):
            continue
        r["i"] = idx
        rows.append(r)

    in_band_count = sum(1 for r in rows if r["in_band"])
    above = [r for r in rows if r["zipf"] > CEFR_ZIPF_RANGE[args.expected_cefr][1] + args.tolerance]
    below = [r for r in rows if r["zipf"] < CEFR_ZIPF_RANGE[args.expected_cefr][0] - args.tolerance]

    print(f"=== freq_check report ===")
    print(f"items_evaluated={len(rows)}  expected={args.expected_cefr}  tolerance=±{args.tolerance} zipf")
    print(f"in_band={in_band_count}/{len(rows)}  ({100*in_band_count//max(1,len(rows))}%)")
    print(f"too_easy_for_cefr (above band): {len(above)}")
    for r in above:
        print(f"  #{r['i']:2}  zipf={r['zipf']:.2f}  actual={r['actual_cefr_band']}  target={r['target']!r}")
    print(f"too_hard_for_cefr (below band): {len(below)}")
    for r in below:
        print(f"  #{r['i']:2}  zipf={r['zipf']:.2f}  actual={r['actual_cefr_band']}  target={r['target']!r}")

    if not args.summary_only:
        print()
        print(f"=== full per-item ===")
        for r in rows:
            mark = "✓" if r["in_band"] else "✗"
            print(f"  {mark} #{r['i']:2}  zipf={r['zipf']:.2f}  band={r['actual_cefr_band']}  dev={r['deviation']:+.2f}  {r['target']!r}")

    if args.output:
        Path(args.output).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        print(f"\nwrote per-item JSONL: {args.output}")


if __name__ == "__main__":
    main()
