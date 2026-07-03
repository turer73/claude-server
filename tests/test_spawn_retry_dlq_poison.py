"""#1234 repro — dlq_poison_alert python-bloğu: MEMORY_API_KEY yoksa crash ETMEMELİ.

G1 repro-gate testi: automation/autonomous-spawn-retry.sh içindeki python-heredoc'u
(gerçek-üretim kodu, kopya değil) çıkarıp key'siz .env ile koşturur.
- Fix ÖNCESİ (merge-base): `[...][0]` boş-listede IndexError → rc!=0 → bu test FAIL (bug-kanıtı).
- Fix SONRASI: fail-loud mesaj + SystemExit(0) → rc==0 + 'ATLANDI' çıktısı.
Key-VAR senaryosu bilerek YOK: blok hardcoded http://127.0.0.1:8420'ye POST atar —
lokal canlı server'a gerçek-yazma yan-etkisi (test-izolasyon ihlali) olur.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "automation" / "autonomous-spawn-retry.sh"


def _extract_poison_python_block() -> str:
    """dlq_poison_alert'teki tek `python3 <<'PY'` heredoc gövdesi (satırlar arası)."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if "python3 <<'PY'" in ln]
    assert len(starts) == 1, f"beklenen 1 PY-heredoc, bulunan {len(starts)} (script yapısı değişti — testi güncelle)"
    body: list[str] = []
    for ln in lines[starts[0] + 1 :]:
        if ln.strip() == "PY":
            return "\n".join(body)
        body.append(ln)
    raise AssertionError("PY heredoc terminatörü bulunamadı")


def test_dlq_poison_block_missing_key_graceful_skip(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("OTHER_VAR=x\n", encoding="utf-8")  # MEMORY_API_KEY YOK
    block = _extract_poison_python_block()
    r = subprocess.run(
        [sys.executable, "-c", block],
        env={
            "HOOK_ENV_FILE": str(env_file),
            "NOTE_ID_VAR": "99999",
            "TITLE_VAR": "t",
            "FROM_VAR": "test",
            "ATTEMPTS_VAR": "3",
            "RC_VAR": "1",
            "ERR_VAR": "err",
            "DATE_VAR": "20260101-0000",
            "SYSTEMROOT": "C:\\Windows",  # Windows lokal: socket/ssl importu için gerekli, Linux'ta zararsız
            "PYTHONIOENCODING": "utf-8",  # Windows lokal: mesajdaki em-dash cp1254'te decode-crash yapar
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert r.returncode == 0, f"key-yok crash etti (fix-öncesi IndexError davranışı): stderr={r.stderr[-400:]}"
    assert "ATLANDI" in r.stdout  # fail-loud log (sessiz-değil)
