"""PR#365 Codex bulgularinin duzeltmelerini kilitler (10/10 kodda dogrulandi).

Hepsinin ortak sinifi SESSIZ BASARISIZLIK: arac "tamam" der, gercek durum
degildir. Testler davranisi degil, SONUCU kilitler.

Scriptler `scripts/` altinda ve adlarinda tire var -> importlib ile yukleniyor
(mevcut tests/test_mail_infra_scripts.py ile ayni desen).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(filename: str, modname: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


cf_dns = _load("cf-dns.py", "cf_dns_codex_fixes")


# --- cf-dns: sayfalama -----------------------------------------------------
# 200'den fazla kayitli zone'da tek sayfa cekmek SESSIZCE kayip uretiyordu:
# cmd_upsert kaydi bulamayip "yok" sanip IKINCISINI olusturur -> SPF/DMARC
# duplicate'i, yani --match-prefix'in onlemek icin var oldugu hata.


def test_records_follows_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        1: {"success": True, "result": [{"id": f"a{i}"} for i in range(200)], "result_info": {"total_pages": 3}},
        2: {"success": True, "result": [{"id": f"b{i}"} for i in range(200)], "result_info": {"total_pages": 3}},
        3: {"success": True, "result": [{"id": "c0"}], "result_info": {"total_pages": 3}},
    }
    seen: list[int] = []

    def fake_call(token: str, path: str, method: str = "GET", body: object = None) -> dict:
        # `[?&]` SART: cikarilmis `page=` deseni "per_page=200" ile eslesiyor ve
        # sayfa numarasi yerine 200 okuyor.
        page = int(re.search(r"[?&]page=(\d+)", path).group(1))
        seen.append(page)
        return pages[page]

    monkeypatch.setattr(cf_dns, "call", fake_call)
    out = cf_dns.records("tok", "zid")

    assert seen == [1, 2, 3], f"tum sayfalar gezilmedi: {seen}"
    assert len(out) == 401, len(out)


def test_records_stops_without_total_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """total_pages yoksa bos sayfada dur — sonsuz donguye girme."""
    responses = [
        {"success": True, "result": [{"id": "a"}]},
        {"success": True, "result": []},
    ]

    def fake_call(token: str, path: str, method: str = "GET", body: object = None) -> dict:
        return responses.pop(0)

    monkeypatch.setattr(cf_dns, "call", fake_call)
    assert len(cf_dns.records("tok", "zid")) == 1


# --- cf-dns: token secimi --------------------------------------------------
# Okuma probe'u yalnizca OKUMA kanitlar. Read-yetkili/Edit-yetkisiz token
# secilip donuluyordu; .env'de duzenleyebilen BASKA token varken her yazma
# yetki hatasiyla dusuyordu.


def test_write_call_falls_back_to_next_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cf_dns, "zone_candidates", lambda zone: [("okur", "tok-ro", "z1"), ("yazar", "tok-rw", "z1")])
    calls: list[str] = []

    def fake_call(token: str, path: str, method: str = "GET", body: object = None) -> dict:
        calls.append(token)
        if token == "tok-ro":
            return {"success": False, "errors": [{"code": 9109, "message": "Unauthorized to access requested resource"}]}
        return {"success": True, "result": {"id": "r1"}}

    monkeypatch.setattr(cf_dns, "call", fake_call)
    res = cf_dns.write_call("panola.app", "/zones/{zid}/dns_records", "POST", {})

    assert res["success"] is True
    assert calls == ["tok-ro", "tok-rw"], calls


def test_write_call_does_not_retry_real_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yetki DISI hatada token degistirmek anlamsiz — ve hatayi gizler."""
    monkeypatch.setattr(cf_dns, "zone_candidates", lambda zone: [("a", "t1", "z"), ("b", "t2", "z")])
    calls: list[str] = []

    def fake_call(token: str, path: str, method: str = "GET", body: object = None) -> dict:
        calls.append(token)
        return {"success": False, "errors": [{"code": 81057, "message": "Record already exists."}]}

    monkeypatch.setattr(cf_dns, "call", fake_call)
    res = cf_dns.write_call("panola.app", "/zones/{zid}/dns_records", "POST", {})

    assert res["success"] is False
    assert calls == ["t1"], f"gercek hatada siradaki token denenmemeli: {calls}"


# --- mx-cutover: geri alma komutlari calistirilabilir olmali ---------------


def _rollback_line(backup_line: str) -> str:
    """mx-cutover.sh'teki awk'i AYNEN script'ten cikarip kosar (kopyalamaz)."""
    src = (SCRIPTS / "mx-cutover.sh").read_text()
    m = re.search(r"awk -v CF=\"\$CF\" -v ZONE=\"\$ZONE\" \\\n\s*('.*?')\s*\\\n", src, re.DOTALL)
    assert m, "awk programi script'te bulunamadi"
    prog = m.group(1)[1:-1]
    return subprocess.run(
        ["awk", "-v", "CF=/opt/cf-dns.py", "-v", "ZONE=panola.app", prog],
        input=backup_line,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def test_rollback_command_is_executable() -> None:
    """ENVIRON[] bos donuyordu ve $5 'prio=1' idi -> uretilen satir calismazdi."""
    line = _rollback_line("abc123  MX     panola.app     aspmx.l.google.com prio=1\n")

    assert "/opt/cf-dns.py upsert panola.app MX panola.app aspmx.l.google.com --priority 1" in line, line
    assert "prio=" not in line, f"prio= oneki temizlenmemis: {line}"
    assert not line.startswith("upsert"), f"CF yolu bos kalmis: {line}"


# --- Dogrudan cagrilan operator scriptleri calistirilabilir olmali ---------


@pytest.mark.parametrize(
    "name",
    [
        "mx-cutover.sh",
        "stalwart-create-mailboxes.sh",
        "stalwart-dkim-dns.sh",
        "stalwart-dkim-test.sh",
        "stalwart-imap-check.py",
        "stalwart-setup-acme.sh",
        "stalwart-p0-harden.sh",
    ],
)
def test_operator_scripts_are_executable(name: str) -> None:
    """Kullanim ornekleri bunlari dogrudan cagiriyor; 100644 ise 'Permission denied'."""
    mode = subprocess.run(
        ["git", "ls-files", "-s", f"scripts/{name}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.split()
    assert mode, f"scripts/{name} git indeksinde yok"
    assert mode[0] == "100755", f"{name} modu {mode[0]} (100755 olmali)"


# --- Sabit /tmp yolu (symlink saldirisi) -----------------------------------


def test_create_mailboxes_uses_private_tmpdir() -> None:
    """root ile kosan script ONGORULEBILIR /tmp adi kullanmamali."""
    src = (SCRIPTS / "stalwart-create-mailboxes.sh").read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    assert "mktemp -d" in code, "ozel gecici dizin acilmiyor"
    assert "/tmp/mbresp" not in code, "sabit /tmp/mbresp hala calisan kodda"
    assert "/tmp/imapchk" not in code, "sabit /tmp/imapchk hala calisan kodda"


# --- Sir komut satirina girmemeli ------------------------------------------


def test_acme_sends_token_over_stdin_not_argv() -> None:
    """Token vps-run.sh govdesine gomulurse curl/ssh argv'sinde ifsa olur."""
    src = (SCRIPTS / "stalwart-setup-acme.sh").read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    assert "cat > /tmp/.cf_tok" in code, "token uzak dosyaya yazilmiyor"
    # Token artik PRE dosyasina (vps-run.sh govdesi) YAZILMAMALI.
    pre_block = code.split('} > "$PRE"')[0].split("PRE=$(mktemp")[-1]
    assert "TOKEN" not in pre_block, "token hala vps-run.sh govdesine gomuluyor"
    assert "printf " in code, "stdin kanali kurulmamis: printf yok"
    assert "ssh " in code, "stdin kanali kurulmamis: dogrudan ssh cagrisi yok"


# --- p0-harden: kismi sertlestirmede sifir donme ---------------------------


def test_p0_harden_exits_nonzero_on_partial() -> None:
    src = (SCRIPTS / "stalwart-p0-harden.sh").read_text()
    assert re.search(r'echo "SONUC: KISMI[^"]*"\nexit 1', src), "KISMI sonrasi exit 1 yok"
    assert re.search(r'echo "SONUC: TAMAM"\n\s*exit 0', src), "TAMAM sonrasi exit 0 yok"


def test_p0_harden_inspects_patch_body() -> None:
    """HTTP 200 + govdede hata = BASARISIZ; govde atilirsa sessiz basarisizlik."""
    code = "\n".join(ln for ln in (SCRIPTS / "stalwart-p0-harden.sh").read_text().splitlines() if not ln.lstrip().startswith("#"))
    assert "-o /dev/null" not in code, "PATCH govdesi hala atiliyor"
    assert "BODY=" in code, "govde degiskene alinmiyor"
    assert "notFound" in code, "govde hata kontrolu yok"
    # Tek bir yerde degil, HER IKI PATCH yolunda (ilk hesap + kalan hesaplar).
    assert code.count("BODY=") >= 2, f"govde kontrolu yalnizca {code.count('BODY=')} PATCH yolunda"


def test_p0_harden_reads_creds_before_archiving() -> None:
    """--force yolunda arsivleme, kimlik dogrulamadan SONRA olmali."""
    src = (SCRIPTS / "stalwart-p0-harden.sh").read_text()
    saved = src.index("SAVED_ADM=$(grep")
    archive = src.index('mv "$CRED" "$CRED.$STAMP.eski"')
    auth_ok = src.index('echo "  baglanti tamam"')
    assert saved < auth_ok < archive, "sira yanlis: once oku, dogrula, SONRA arsivle"
