#!/usr/bin/env python3
"""Klipper note poller core — surer notlarini isle.

Kullanim: python3 _klipper_poller_core.py MEM_KEY API_BASE LAST_ID STATE_PATH LOG_PATH
stdin: raw JSON from GET /notes
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(path: str, msg: str) -> None:
    try:
        with open(path, "a") as f:
            f.write(f"{_ts()} {msg}\n")
    except OSError:
        pass


def _api(method: str, path: str, base: str, key: str, body=None):
    url = base + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={"X-Memory-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310
            return json.loads(r.read())
    except Exception:
        return {}


def _classify(title: str, content: str) -> str:
    t = (title + " " + content).lower()
    if '"gorev_sonucu"' in content or "gorev_sonucu" in content:
        return "RESULT"
    if any(k in t for k in ["basarisiz", "failed", "hata:", "error:"]):
        return "FAILURE"
    if any(k in t for k in ["tamamlandi", "completed", "done", "basarili"]):
        return "RESULT"
    if any(k in t for k in ["ack", "tesekk", "tamam", "alindi"]):
        return "ACK"
    return "INFO"


def main():
    if len(sys.argv) < 6:
        print("usage: script MEM_KEY API_BASE LAST_ID STATE_PATH LOG_PATH")
        sys.exit(1)

    mem_key, api_base, last_id_str, state_path, log_path = sys.argv[1:6]
    last_id = int(last_id_str)

    raw = sys.stdin.read().strip()
    if not raw:
        _log(log_path, "WARN: stdin bos")
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _log(log_path, f"WARN: JSON parse hata: {e}")
        return

    notes = data if isinstance(data, list) else (data.get("value") or data.get("notes") or data.get("data") or [])

    # Surer'dan gelen, okunmamis, yeni notlar
    relevant = [n for n in notes if n.get("from_device") == "surer" and not n.get("read") and int(n.get("id", 0)) > last_id]
    relevant.sort(key=lambda n: n.get("id", 0))

    if not relevant:
        return

    _log(log_path, f"INFO: {len(relevant)} yeni surer notu")
    max_id = last_id

    for n in relevant:
        nid = int(n.get("id", 0))
        title = str(n.get("title", ""))
        content = str(n.get("content", ""))
        ntype = _classify(title, content)

        if nid > max_id:
            max_id = nid

        # Gorev sonucu: memory tasks log
        if ntype in ("RESULT", "FAILURE"):
            status = "completed" if ntype == "RESULT" else "failed"
            proje = "genel"
            try:
                c = json.loads(content)
                proje = c.get("proje", c.get("gorev_id", "genel"))
            except Exception:
                pass
            _api(
                "POST",
                "/tasks",
                api_base,
                mem_key,
                {
                    "device_name": "klipper",
                    "project": proje,
                    "task": f"Surer sonucu: {title[:80]}",
                    "details": content[:400],
                    "status": status,
                },
            )
            _log(log_path, f"INFO: [{nid}] {ntype} -> tasks log (proje={proje})")

        else:
            _log(log_path, f"INFO: [{nid}] {ntype}: {title[:60]}")

        # Mark read
        _api("PUT", f"/notes/{nid}/read", api_base, mem_key)

    # State guncelle
    if max_id > last_id:
        try:
            with open(state_path, "w") as f:
                json.dump({"last_seen_id": max_id}, f)
            _log(log_path, f"INFO: state {last_id} -> {max_id}")
        except OSError as e:
            _log(log_path, f"WARN: state yazma hata: {e}")


if __name__ == "__main__":
    main()
