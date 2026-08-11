#!/usr/bin/env python3
"""Cloudflare DNS helper — one entry point for every zone we own.

Tokens live in .env under several names (historical: one per project), and
which token can edit which zone is not obvious from the name. Rather than
hardcoding that mapping, each call probes the tokens until one can read the
target zone, then reuses it. Keeps callers from pasting bearer tokens into
shell commands.

    cf-dns.py zones
    cf-dns.py list <zone>
    cf-dns.py upsert <zone> <type> <name> <content> [--priority N] [--ttl N] [--proxied]
    cf-dns.py delete <zone> <record-id>

`upsert` matches on (type, name) — and for MX also on content, since a zone
legitimately holds several MX records. It prints what it changed; nothing is
removed unless you call `delete`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, cast

API = "https://api.cloudflare.com/client/v4"
ENV_FILE = os.environ.get("ENV_FILE", "/opt/linux-ai-server/.env")

Record = dict[str, Any]


def read_tokens() -> list[tuple[str, str]]:
    """Every CLOUDFLARE_*TOKEN in .env, first occurrence wins per name."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("CLOUDFLARE_") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if "TOKEN" not in name or name in seen:
                    continue
                seen.add(name)
                out.append((name, value.strip().strip('"').strip("'")))
    except OSError as exc:
        sys.exit(f"error: cannot read {ENV_FILE}: {exc}")
    return out


def call(token: str, path: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{API}{path}"
    # The bearer token is attached below, so the request must not be able to
    # leave the Cloudflare API over an attacker-chosen scheme or host. `path`
    # carries zone ids and record names; assert the composed URL rather than
    # trusting that it stayed well-formed.
    if not url.startswith("https://api.cloudflare.com/"):
        sys.exit(f"error: refusing to send credentials to {url!r}")

    req = urllib.request.Request(  # noqa: S310 — https://api.cloudflare.com asserted above
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return cast("dict[str, Any]", json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        # Cloudflare returns its error detail in the body on 4xx; that is far
        # more useful than the status alone, so prefer it when parseable.
        try:
            return cast("dict[str, Any]", json.loads(exc.read()))
        except Exception:
            return {"success": False, "errors": [{"message": f"HTTP {exc.code}"}]}
    except OSError as exc:
        return {"success": False, "errors": [{"message": str(exc)}]}


def resolve_zone(zone: str) -> tuple[str, str]:
    """Find (token, zone_id) for a zone name — the first token that can READ
    its records. Listing a zone is not enough: several tokens can see a zone
    they have no DNS permission on, and that only fails later, mid-change."""
    for name, token in read_tokens():
        res = call(token, f"/zones?name={zone}")
        if not res.get("success") or not res.get("result"):
            continue
        zid = res["result"][0]["id"]
        probe = call(token, f"/zones/{zid}/dns_records?per_page=1")
        if probe.get("success"):
            return token, zid
        print(f"  ({name} sees {zone} but cannot read DNS — skipping)", file=sys.stderr)
    sys.exit(f"error: no token in {ENV_FILE} can edit DNS for {zone}")


def records(token: str, zid: str) -> list[Record]:
    res = call(token, f"/zones/{zid}/dns_records?per_page=200")
    if not res.get("success"):
        sys.exit(f"error: {res.get('errors')}")
    return cast("list[Record]", res["result"])


def fqdn(zone: str, name: str) -> str:
    if name in ("@", "", zone):
        return zone
    return name if name.endswith(zone) else f"{name}.{zone}"


def select_existing(
    all_records: list[Record],
    rtype: str,
    name: str,
    content: str,
    match_prefix: str | None,
) -> list[Record]:
    """Which record an upsert should overwrite — or [] to create a new one.

    Getting this wrong is silent and costly, so the two cases are kept apart:

    * `match_prefix` — singleton TXT records (SPF, DMARC). A zone may hold only
      one of each; publishing a second does not add to the first, it invalidates
      both (SPF permerror). So the record is matched by its leading marker and
      edited in place. More than one match means the zone is already broken —
      refuse rather than pick one arbitrarily.
    * MX/TXT without a prefix — a zone legitimately holds many of these under
      one name. Match on exact content so an upsert cannot clobber a sibling.
    """
    hits = [r for r in all_records if r["type"] == rtype and r["name"] == name]
    if match_prefix is not None:
        return [r for r in hits if r["content"].strip('"').startswith(match_prefix)]
    if rtype in ("MX", "TXT"):
        return [r for r in hits if r["content"] == content]
    return hits


def cmd_zones(_: argparse.Namespace) -> None:
    for name, token in read_tokens():
        res = call(token, "/zones?per_page=50")
        if not res.get("success"):
            print(f"{name}: unusable ({res.get('errors')})")
            continue
        for z in res["result"]:
            probe = call(token, f"/zones/{z['id']}/dns_records?per_page=1")
            perm = "dns-rw" if probe.get("success") else "zone-only"
            print(f"{z['name']:24} {perm:9} via {name}")


def cmd_list(args: argparse.Namespace) -> None:
    token, zid = resolve_zone(args.zone)
    for r in sorted(records(token, zid), key=lambda x: (x["type"], x["name"])):
        prio = f" prio={r['priority']}" if r.get("priority") is not None else ""
        print(f"{r['id']}  {r['type']:6} {r['name']:40} {r['content'][:70]}{prio}")


def cmd_upsert(args: argparse.Namespace) -> None:
    token, zid = resolve_zone(args.zone)
    name = fqdn(args.zone, args.name)
    payload: dict[str, Any] = {
        "type": args.type,
        "name": name,
        "content": args.content,
        "ttl": args.ttl,
    }
    if args.type in ("A", "AAAA", "CNAME"):
        payload["proxied"] = args.proxied
    if args.priority is not None:
        payload["priority"] = args.priority

    existing = select_existing(records(token, zid), args.type, name, args.content, args.match_prefix)
    if args.match_prefix and len(existing) > 1:
        sys.exit(f"error: {len(existing)} records match prefix {args.match_prefix!r} on {name}; resolve by hand before upserting")

    if existing:
        rid = existing[0]["id"]
        res = call(token, f"/zones/{zid}/dns_records/{rid}", "PUT", payload)
        action = "updated"
    else:
        res = call(token, f"/zones/{zid}/dns_records", "POST", payload)
        action = "created"

    if not res.get("success"):
        sys.exit(f"error: {res.get('errors')}")
    print(f"{action}: {args.type} {name} -> {args.content}")


def cmd_delete(args: argparse.Namespace) -> None:
    token, zid = resolve_zone(args.zone)
    res = call(token, f"/zones/{zid}/dns_records/{args.record_id}", "DELETE")
    if not res.get("success"):
        sys.exit(f"error: {res.get('errors')}")
    print(f"deleted: {args.record_id}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("zones").set_defaults(func=cmd_zones)

    pl = sub.add_parser("list")
    pl.add_argument("zone")
    pl.set_defaults(func=cmd_list)

    pu = sub.add_parser("upsert")
    pu.add_argument("zone")
    pu.add_argument("type")
    pu.add_argument("name")
    pu.add_argument("content")
    pu.add_argument("--priority", type=int)
    pu.add_argument("--ttl", type=int, default=300)
    pu.add_argument("--proxied", action="store_true")
    pu.add_argument("--match-prefix", help="edit the existing record whose content starts with this (for singleton TXT: SPF, DMARC)")
    pu.set_defaults(func=cmd_upsert)

    pd = sub.add_parser("delete")
    pd.add_argument("zone")
    pd.add_argument("record_id")
    pd.set_defaults(func=cmd_delete)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
