"""Read-only local ops page on localhost:8686.

Runs in flight, minted keys (count + masked list), per-stage success/
failure breakdown and timing, challenge first-pass rate and
distance-confidence histogram, failure reasons, artifact links, per-run
log tails, and a kill-switch banner. GET only.
"""

from __future__ import annotations

import html
import json
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import overrides
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
.banner{{background:#f85149;color:#fff;padding:8px 14px;border-radius:6px;font-weight:bold}}
a{{color:#58a6ff}}
.sw{{display:inline-block;background:#21262d;border:1px solid #30363d;border-radius:6px;padding:4px 10px;margin-right:8px}}
button{{background:#238636;color:#fff;border:0;border-radius:6px;padding:4px 12px;cursor:pointer}}
</style></head><body>
<h1>atria-keys — ops</h1>
{body}
</body></html>"""


def _rows_html(rows: list[list[str]], header: list[str]) -> str:
    out = ["<table><tr>"]
    out += [f"<th>{html.escape(h)}</th>" for h in header]
    out.append("</tr>")
    for r in rows:
        out.append("<tr>" + "".join(
            f"<td>{c}</td>" if isinstance(c, _Link) else f"<td>{html.escape(str(c))}</td>"
            for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


class _Link(str):
    """Trusted HTML cell — already escaped where needed."""


def _conf_histogram(values: list[float]) -> dict[str, int]:
    buckets = {"<0.4": 0, "0.4-0.7": 0, "0.7-0.9": 0, ">=0.9": 0}
    for v in values:
        if v < 0.4:
            buckets["<0.4"] += 1
        elif v < 0.7:
            buckets["0.4-0.7"] += 1
        elif v < 0.9:
            buckets["0.7-0.9"] += 1
        else:
            buckets[">=0.9"] += 1
    return buckets


def _switch_button(field: str, current: str, options: list[str]) -> str:
    out = []
    for opt in options:
        if opt == current:
            out.append(f"<span class='sw'>{html.escape(opt)} <b>on</b></span>")
        else:
            out.append(
                f"<form method='post' action='/api/settings' style='display:inline'>"
                f"<input type='hidden' name='{html.escape(field)}' value='{html.escape(opt)}'>"
                f"<button type='submit'>{html.escape(opt)}</button></form>"
            )
    return "".join(out)


def build_body(state: StateStore, keystore: KeyStore, artifacts_dir: Path,
               cfg=None) -> str:
    parts: list[str] = []

    paused = state.paused()
    if paused:
        parts.append(f"<p class='banner'>PAUSED — {html.escape(paused)}</p>")

    if cfg is not None:
        mb = overrides.mailbox_reader(cfg)
        px = "proxy" if overrides.proxy_enabled(cfg) else "direct"
        parts.append("<h2>switches</h2><p>")
        parts.append(f"mailbox: {_switch_button('mailbox_reader', mb, ['imap', 'tempmaillol'])}")
        parts.append("&nbsp;&nbsp;")
        parts.append(f"egress: {_switch_button('proxy', px, ['direct', 'proxy'])}")
        parts.append("</p>")

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
                    _Link(f"<a href='/api/log?run_id={r['run_id']}'>{r['run_id']}</a>"),
                    r["status"],
                    r.get("current_stage") or "-",
                    r.get("email") or "-",
                    (f"{r['duration_s']:.0f}s" if r.get("duration_s") else "-"),
                    (f"{r['bytes_est'] / 1e6:.1f}" if r.get("bytes_est") else "-"),
                    r.get("mailbox_kind") or "-",
                    r.get("proxy_mode") or "-",
                    r.get("failure_reason") or "-",
                ]
                for r in runs[:25]
            ],
            ["run", "status", "stage", "email", "dur", "MB",
             "mail", "net", "failure"],
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
    durations = state.avg_stage_durations()
    parts.append("<h2>stage breakdown (avg duration s)</h2>")
    parts.append(
        _rows_html(
            [[s, st.get("done", 0), st.get("failed", 0),
              f"{durations.get(s, 0):.1f}"] for s, st in breakdown.items()],
            ["stage", "done", "failed", "avg_s"],
        )
    )

    rate = state.challenge_first_pass_rate()
    parts.append("<h2>challenge first-pass rate</h2>")
    parts.append(f"<p>{'n/a' if rate is None else f'{rate * 100:.0f}%'}</p>")

    confs = state.distance_confidences()
    if confs:
        parts.append("<h2>distance-confidence histogram</h2>")
        parts.append(
            _rows_html([[k, v] for k, v in _conf_histogram(confs).items()],
                       ["confidence", "count"])
        )

    hist = state.failure_histogram()
    parts.append("<h2>failure reasons</h2>")
    parts.append(_rows_html([[k, v] for k, v in hist.items()], ["reason", "count"]))

    if artifacts_dir.is_dir():
        files = sorted(artifacts_dir.rglob("*"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:30]
        files = [f for f in files if f.is_file()]
        if files:
            parts.append("<h2>artifacts</h2>")
            parts.append(
                _rows_html(
                    [[_Link(f"<a href='/artifacts/{urllib.parse.quote(str(f.relative_to(artifacts_dir)))}'>"
                            f"{html.escape(f.name)}</a>")] for f in files],
                    ["file"],
                )
            )
    return "".join(parts)


class _Handler(BaseHTTPRequestHandler):
    state: StateStore
    keystore: KeyStore
    artifacts_dir: Path
    refresh_s: int = 4
    cfg = None

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/settings" and self.cfg is not None:
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            updates = {}
            mb = (form.get("mailbox_reader") or [None])[0]
            if mb in overrides.MAILBOX_KINDS:
                updates["mailbox_reader"] = mb
            px = (form.get("proxy") or [None])[0]
            if px in overrides.PROXY_MODES:
                updates["proxy"] = px
            if updates:
                overrides.write(self.cfg, **updates)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        self._send(b"not found", "text/plain", 404)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/state":
            self._json(self._state_dict())
            return
        if parsed.path == "/api/log":
            run_id = (qs.get("run_id") or [""])[0]
            self._json({"run_id": run_id, "events": self.state.events_for_run(run_id)})
            return
        if parsed.path.startswith("/artifacts/"):
            self._serve_artifact(parsed.path[len("/artifacts/"):])
            return
        body = _PAGE.format(
            refresh=self.refresh_s,
            body=build_body(self.state, self.keystore, self.artifacts_dir, self.cfg),
        )
        self._send(body.encode(), "text/html; charset=utf-8")

    def _serve_artifact(self, rel: str):
        root = self.artifacts_dir.resolve()
        target = (root / urllib.parse.unquote(rel)).resolve()
        if root not in target.parents and target != root:
            self._send(b"forbidden", "text/plain", 403)
            return
        if not target.is_file():
            self._send(b"not found", "text/plain", 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(target.read_bytes(), ctype)

    def _state_dict(self):
        return {
            "paused": self.state.paused(),
            "runs": self.state.list_runs(),
            "keys": self.keystore.masked_list(),
            "stage_breakdown": self.state.stage_breakdown(),
            "avg_stage_durations": self.state.avg_stage_durations(),
            "challenge_first_pass_rate": self.state.challenge_first_pass_rate(),
            "confidence_histogram": _conf_histogram(self.state.distance_confidences()),
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

    def log_message(self, *a):
        pass


def serve(state: StateStore, keystore: KeyStore, host: str, port: int,
          refresh_s: int = 4, artifacts_dir: str | Path = "keys/artifacts",
          cfg=None):
    handler = type("Handler", (_Handler,), {})
    handler.state = state
    handler.keystore = keystore
    handler.refresh_s = refresh_s
    handler.artifacts_dir = Path(artifacts_dir)
    handler.cfg = cfg
    httpd = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
