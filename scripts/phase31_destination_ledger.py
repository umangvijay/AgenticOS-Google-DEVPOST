#!/usr/bin/env python3
"""Phase 31 §31.1 — the operator's independent destination ledger.

A tiny HTTP service, deliberately OUTSIDE the product, that records
every write it receives and reports the durable ledger on GET. The
scenario runner (and every Phase 31 drill that asserts an external
effect) reads THIS ledger — never the product's own tables — so "one
destination row per confirmed effect" is observed independently.

Endpoints:
    POST /records      {"key": ..., "value": ..., "nonce": ...}
                       → 201 {"record_id", "seq"}
    GET  /records?key=...   → {"records": [...]}   (full durable list)
    POST /faults/drop-response {"until_seq": N}
                       → the next N responses are dropped AFTER the
                         write is durably committed (lost-ack drill)
    POST /faults/clear      → clear fault injections
    GET  /health            → {"ok": true, "records": N}

Run:  .venv/bin/python scripts/phase31_destination_ledger.py --port 8799
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import threading
import time
from pathlib import Path

STATE_PATH = Path(os.environ.get(
    "PHASE31_LEDGER_STATE",
    "/tmp/phase31-destination-ledger.json"))

_lock = threading.Lock()
_records: list[dict] = []
_drop_next = 0


def _load() -> None:
    global _records
    if STATE_PATH.exists():
        try:
            _records = json.loads(STATE_PATH.read_text()).get("records", [])
        except ValueError:
            _records = []


def _save() -> None:
    STATE_PATH.write_text(json.dumps({"records": _records}, indent=2))


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, body: dict, drop: bool = False) -> None:
        if drop:
            # The write IS durably committed; the client observes the
            # lost-ack window (connection closes with no response).
            self.wfile.close()
            return
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            with _lock:
                self._json(200, {"ok": True, "records": len(_records),
                                 "dropped_responses": _drop_next})
        elif self.path.startswith("/records"):
            key = self.path.split("key=")[-1] if "key=" in self.path else None
            with _lock:
                items = [r for r in _records
                         if key is None or r.get("key") == key]
                self._json(200, {"records": items})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        global _drop_next
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/records":
            with _lock:
                rec = {"record_id": f"dest-{len(_records) + 1}",
                       "seq": len(_records) + 1,
                       "committed_at": time.time(),
                       **body}
                _records.append(rec)
                _save()
                drop = _drop_next > 0
                if drop:
                    _drop_next -= 1
            # The write IS durably committed before the (possibly
            # dropped) response — that is the lost-ack window §31.4/§31.9
            # drill exercises.
            self._json(201, {"record_id": rec["record_id"],
                             "seq": rec["seq"]}, drop=drop)
        elif self.path == "/faults/drop-response":
            with _lock:
                _drop_next = int(body.get("count", 1))
            self._json(200, {"drop_next": _drop_next})
        elif self.path == "/faults/clear":
            with _lock:
                _drop_next = 0
            self._json(200, {"drop_next": 0})
        elif self.path == "/reset":
            with _lock:
                _records.clear()
                _drop_next = 0
                _save()
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    _load()
    import http.server
    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), H)
    print(f"phase31 destination ledger on :{args.port} "
          f"({len(_records)} durable records)")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
