"""Mail altyapisi araclarindaki iki sessiz-hata sinifini kilitler.

Ikisi de 2026-08-10 kurulumunda fiilen ISIRDI ve ikisi de sessizdi — arac
"basarili" raporlayip yanlis sonuc uretti. Testler o davranislari sabitler.

Scriptler `scripts/` altinda ve adlarinda tire var, yani normal import ile
yuklenemezler; importlib ile dosyadan yukleniyorlar.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(filename: str, modname: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


cf_dns = _load("cf-dns.py", "cf_dns_under_test")
imap_check = _load("stalwart-imap-check.py", "stalwart_imap_check_under_test")


# --- cf-dns: hangi kaydin uzerine yazilacagi -------------------------------
# SPF/DMARC gibi TEKIL TXT kayitlarinda ikinci bir kayit eklemek additive
# degildir; SPF'te permerror uretir. Bu yuzden prefix eslesmesi var olan kaydi
# YERINDE bulmali, yeni olusturmaya dusmemeli.

SPF_OLD = {"id": "1", "type": "TXT", "name": "panola.app", "content": "v=spf1 include:_spf.google.com ~all"}
VERIFY = {"id": "2", "type": "TXT", "name": "panola.app", "content": "google-site-verification=abc"}
MX_GOOGLE = {"id": "3", "type": "MX", "name": "panola.app", "content": "aspmx.l.google.com"}
MX_SELF = {"id": "4", "type": "MX", "name": "panola.app", "content": "mail.panola.app"}


def test_match_prefix_edits_existing_spf_instead_of_adding_second() -> None:
    hit = cf_dns.select_existing([SPF_OLD, VERIFY], "TXT", "panola.app", "v=spf1 ip4:1.2.3.4 ~all", "v=spf1")
    assert [r["id"] for r in hit] == ["1"]


def test_match_prefix_ignores_unrelated_txt_at_same_name() -> None:
    hit = cf_dns.select_existing([VERIFY], "TXT", "panola.app", "v=spf1 ~all", "v=spf1")
    assert hit == []


def test_match_prefix_tolerates_quoted_content() -> None:
    quoted = dict(SPF_OLD, content='"v=spf1 include:_spf.google.com ~all"')
    hit = cf_dns.select_existing([quoted], "TXT", "panola.app", "v=spf1 ~all", "v=spf1")
    assert [r["id"] for r in hit] == ["1"]


def test_mx_without_prefix_matches_exact_content_only() -> None:
    # Bir zone mesru olarak birden cok MX tutar; icerik esitligi olmadan
    # her upsert bir kardesi ezerdi.
    hit = cf_dns.select_existing([MX_GOOGLE, MX_SELF], "MX", "panola.app", "mail.panola.app", None)
    assert [r["id"] for r in hit] == ["4"]


def test_multiple_prefix_matches_are_returned_so_caller_can_refuse() -> None:
    dupe = dict(SPF_OLD, id="9")
    hit = cf_dns.select_existing([SPF_OLD, dupe], "TXT", "panola.app", "v=spf1 ~all", "v=spf1")
    assert len(hit) == 2


@pytest.mark.parametrize("name", ["@", "", "panola.app"])
def test_fqdn_treats_apex_forms_as_the_zone(name: str) -> None:
    assert cf_dns.fqdn("panola.app", name) == "panola.app"


def test_fqdn_appends_zone_once() -> None:
    assert cf_dns.fqdn("panola.app", "mail") == "mail.panola.app"
    assert cf_dns.fqdn("panola.app", "mail.panola.app") == "mail.panola.app"


# --- imap-check: "reddedildi" ile "ulasilamadi" ayrimi ---------------------
# Stalwart duz 143'te LOGIN'i kapatir ve bunu yanlis parolayla AYNI istisna
# tipiyle bildirir. Ayrim yapilmazsa CALISAN bir parola "yanlis" raporlanir.


@pytest.mark.parametrize(
    "message",
    [
        "LOGIN is disabled on the clear-text port.",
        "b'LOGIN is disabled on the clear-text port.'",
        "AUTHENTICATE is DISABLED here",
        "cleartext authentication not permitted",
    ],
)
def test_transport_refusal_is_not_read_as_wrong_password(message: str) -> None:
    assert imap_check.is_transport_refusal(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "[AUTHENTICATIONFAILED] Authentication failed",
        "b'[AUTHENTICATIONFAILED] Authentication failed'",
        "Invalid credentials",
    ],
)
def test_real_auth_failure_is_reported_as_rejection(message: str) -> None:
    assert imap_check.is_transport_refusal(message) is False
