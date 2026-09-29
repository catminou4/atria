"""Read-only local ops page on localhost:8686.

Shows runs in flight, minted keys (count + masked list), per-stage
success/failure breakdown, challenge first-pass rate, and a failure-reason
histogram. GET only; reads StateStore + KeyStore straight from disk.
"""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .keystore import KeyStore
from .state import StateStore

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="{refresh}">
<title>atria-keys ops</title>
<style>
body{{font-family:-apple-system,monospace;background:#0d1117;color:#c9d1d9;margin:2em}}
h1{{font-size:1.2em}} h2{{font-size:1em;color:#8b949e;margin-top:2em}}
table{{border-collapse:collapse}} td,th{{border:1px solid #30363d;padding:4px 10px;text-align:left}}
.ok{{color:#3fb950}} .bad{{color:#f85149}} .run{{color:#d29922}}
</style></head><body>
<h1>atria-keys — ops</h1>
{body}
</body></html>"""


def _rows_html(rows: list[list[str]], header: list[str]) -> str:
    out = ["<table><tr>"]
    out += [f"<th>{html.escape(h)}</th>" for h in header]
    out.append("</tr>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def build_body(state: StateStore, keystore: KeyStore) -> str:
    parts: list[str] = []
    runs = state.list_runs()
    in_flight = [r for r in runs if r["status"] == "running"]
    done = [r for r in runs if r["status"] == "done"]
    failed = [r for r in runs if r["status"] == "failed"]

    parts.append("<h2>runs</h2>")
    parts.append(
        f"<p><span class='run'>{len(in_flight)} in flight</span> · "
        f"<span class='ok'>{len(done)} done</span> · "
        f"<span class='bad'>{len(failed)} failed</span></p>"
    )
    parts.append(
        _rows_html(
            [
                [
                    r["run_id"],
                    r["status"],
                    r.get("current_stage") or "-",
                    r.get("email") or "-",
                    r.get("failure_reason") or "-",
                ]
                for r in runs[:25]
            ],
            ["run", "status", "stage", "email", "failure"],
        )
    )

    keys = keystore.masked_list()
    parts.append(f"<h2>keys minted — {len(keys)}</h2>")
    parts.append(
        _rows_html(
            [[k["key_id"], k["masked"], k["email"], k["created_at"]] for k in keys],
            ["key_id", "key", "email", "created"],
        )
    )

    breakdown = state.stage_breakdown()
    parts.append("<h2>stage breakdown</h2>")
    parts.append(
        _rows_html(
            [[s, st.get("done", 0), st.get("failed", 0)] for s, st in breakdown.items()],
            ["stage", "done", "failed"],
        )
    )

    rate = state.challenge_first_pass_rate()
    parts.append("<h2>challenge first-pass rate</h2>")
    parts.append(f"<p>{'n/a' if rate is None else f'{rate * 100:.0f}%'}</p>")

    hist = state.failure_histogram()
    parts.append("<h2>failure reasons</h2>")
    parts.append(_rows_html([[k, v] for k, v in hist.items()], ["reason", "count"]))
    return "".join(parts)


class _Handler(BaseHTTPRequestHandler):
    state: StateStore
    keystore: KeyStore
    refresh_s: int = 4

    def do_GET(self):  # noqa: N802
        if self.path == "/api/state":
            self._json(self._state_dict())
            return
        body = _PAGE.format(refresh=self.refresh_s, body=build_body(self.state, self.keystore))
        self._send(body.encode(), "text/html; charset=utf-8")

    def _state_dict(self):
        return {
            "runs": self.state.list_runs(),
            "keys": self.keystore.masked_list(),
            "stage_breakdown": self.state.stage_breakdown(),
            "challenge_first_pass_rate": self.state.challenge_first_pass_rate(),
            "failure_histogram": self.state.failure_histogram(),
        }

    def _send(self, payload: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj):
        self._send(json.dumps(obj, default=str).encode(), "application/json")

    def log_message(self, *a):  # keep stdout clean
        pass


def serve(state: StateStore, keystore: KeyStore, host: str, port: int, refresh_s: int = 4):
    handler = type("Handler", (_Handler,), {})
    handler.state = state
    handler.keystore = keystore
    handler.refresh_s = refresh_s
    httpd = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
