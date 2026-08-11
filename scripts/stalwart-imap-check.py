#!/usr/bin/env python3
"""Stalwart'ta bir hesabin parolasini IMAP ile dogrular.

    stalwart-imap-check.py <hesap> <parola> [host]

Cikis kodlari — cagiran taraf "reddedildi" ile "baglanamadi"yi AYIRT edebilsin
diye ayri tutuluyor. Ikisini tek bir "basarisiz"a indirgemek, kimlik dogrulama
sonucu hakkinda yanlis sonuca goturur:

    0  giris basarili
    1  kimlik reddedildi (parola yanlis / hesap yok)
    2  tasima hatasi (baglanti yok, TLS yok, port kapali) — parola hakkinda
       hicbir sey soylemez

Once 143+STARTTLS, olmazsa 993 implicit TLS denenir. Duz 143 KULLANILMAZ:
Stalwart clear-text portta LOGIN'i kapatir ve bu, yanlis parolayla ayni
istisnayi ureterek sahte "reddedildi" verir.

Sertifika dogrulamasi bilerek kapali — ACME baglanana kadar sunucuda
self-signed sertifika var; burada dogrulanan sey parola, sertifika degil.
"""

from __future__ import annotations

import imaplib
import ssl
import sys


def is_transport_refusal(message: str) -> bool:
    """Sunucu girisi PAROLADAN bagimsiz bir sebeple mi reddetti?

    Kritik ayrim: Stalwart duz 143'te LOGIN'i tamamen kapatir ve bunu yanlis
    parolayla AYNI istisna tipiyle (imaplib.IMAP4.error) bildirir. Ikisini
    ayirmazsan calisan bir parola "reddedildi" diye raporlanir — bu tam olarak
    2026-08-10'da bir hesabin parolasini bilinmeyen bir degere dusuren hataydi.
    """
    low = message.lower()
    return "disabled" in low or "clear-text" in low or "cleartext" in low


def check(account: str, password: str, host: str) -> tuple[int, str]:
    # Sertifika dogrulamasi bilerek kapali: bu kontrol sunucuya 127.0.0.1
    # uzerinden baglanir, sertifika ise mail.panola.app icin duzenlenmistir —
    # hostname dogrulamasi tanim geregi gecemez. Burada dogrulanan sey PAROLA,
    # sertifika degil; sertifika ayrica dis testlerle kontrol ediliyor.
    # Bu araci uzak bir hosta karsi kullanacaksan bu satiri degistir.
    ctx = ssl._create_unverified_context()  # noqa: S323
    transport_errors: list[str] = []

    for mode in ("starttls", "implicit"):
        try:
            if mode == "starttls":
                conn = imaplib.IMAP4(host, 143, timeout=15)
                conn.starttls(ctx)
            else:
                conn = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx, timeout=15)
        except Exception as exc:
            transport_errors.append(f"{mode}: {exc}")
            continue

        try:
            conn.login(account, password)
        except imaplib.IMAP4.error as exc:
            # Baglanti kuruldu, sunucu girisi reddetti -> parola hakkinda
            # gercek bir cevap. Tek istisna: sunucu bu portta LOGIN'i hic
            # kabul etmiyorsa bu da IMAP4.error olarak gelir, onu ayikla.
            msg = str(exc)
            try:
                conn.logout()
            except Exception:
                pass
            if is_transport_refusal(msg):
                transport_errors.append(f"{mode}: {msg}")
                continue
            return 1, f"reddedildi ({mode}): {msg}"
        except Exception as exc:
            transport_errors.append(f"{mode}: {exc}")
            continue

        try:
            conn.logout()
        except Exception:
            pass
        return 0, f"giris basarili ({mode})"

    return 2, "tasima hatasi -> " + " | ".join(transport_errors)


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: stalwart-imap-check.py <hesap> <parola> [host]")
    host = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
    code, detail = check(sys.argv[1], sys.argv[2], host)
    print(detail)
    sys.exit(code)


if __name__ == "__main__":
    main()
