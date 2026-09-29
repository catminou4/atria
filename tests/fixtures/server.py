"""Local fixture site mirroring the Atria registration surface.

  GET /register            -> email form
  POST /api/register       -> creates account, drops verify link in outbox/
  GET /verify?t=           -> page embedding the challenge iframe
  GET /widget?n=           -> slider widget (Alibaba-signature markup;
                            widget_mode=attr exposes data-gap, =img serves
                            /puzzle-img PNGs instead — gap only in the image)
  GET /puzzle-img          -> generated bg/piece PNG (cv2), gap baked in
  GET /mystery-widget?n=   -> unknown widget variant (no known signature)
  POST /api/challenge-token-> widget-side token minting after trace check
  POST /api/verify-challenge -> host-side verification, issues api_key
  GET/POST /__state|__config|__reset -> test instrumentation
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REGISTER_PAGE = """<!doctype html><html><body>
<h1>Atria — Dawn preview</h1>
<form id="reg">
  <input id="email" name="email" type="email" required>
  <button type="submit" id="register-submit">Register</button>
</form>
<script>
document.getElementById('reg').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = document.getElementById('email').value;
  const r = await fetch('/api/register', {method:'POST',
    headers:{'content-type':'application/json'}, body: JSON.stringify({email})});
  const j = await r.json();
  document.getElementById('reg').innerHTML = j.ok ? '<p>Check your inbox.</p>' : '<p>error</p>';
});
</script></body></html>"""

VERIFY_PAGE = """<!doctype html><html><body>
<div id="verify-box" data-nonce="{nonce}">
  <iframe id="challenge-widget" data-variant="alibaba-slide"
          src="/widget?n={nonce}" width="360" height="200"></iframe>
</div>
<div id="result"></div>
<script>
window.addEventListener('message', async (e) => {{
  const d = e.data || {{}};
  if (d.type !== 'challenge') return;
  if (!d.token) {{
    document.getElementById('result').textContent = 'challenge rejected — retrying';
    return;
  }}
  const r = await fetch('/api/verify-challenge', {{method:'POST',
    headers:{{'content-type':'application/json'}},
    body: JSON.stringify({{nonce:'{nonce}', token:d.token}})}});
  const j = await r.json();
  if (j.api_key) {{
    window.__issuedKey = j;
    const el = document.createElement('div');
    el.className = 'issued-key';
    el.setAttribute('data-api-key', j.api_key);
    el.setAttribute('data-key-id', j.key_id);
    el.textContent = j.api_key;
    document.getElementById('result').appendChild(el);
  }}
}});
</script></body></html>"""

VERIFY_VARIANT_PAGE = """<!doctype html><html><body>
<div id="verify-box" data-nonce="{nonce}">
  <iframe id="promo-frame" src="/mystery-widget?n={nonce}" width="360" height="140"></iframe>
</div></body></html>"""

WIDGET_PAGE = """<!doctype html><html><body>
<div class="slider-track" data-role="track" data-gap="{gap}"
     style="position:relative;width:300px;height:40px;background:#e8e8e8">
  <div class="slider-btn" data-role="handle"
       style="position:absolute;left:0;top:0;width:44px;height:40px;background:#3662d8"></div>
  <div class="slider-state" data-state="idle"></div>
</div>
<script>
(function() {{
  const track = document.querySelector('[data-role=track]');
  const handle = document.querySelector('[data-role=handle]');
  const state = document.querySelector('.slider-state');
  const GAP = parseFloat(track.dataset.gap);
  {body_js}
}})();
</script></body></html>"""

# Image-mode widget: no data-gap attr — the cutout position exists only in
# the puzzle PNG. A new render (widget reload) shifts the gap, like the
# real NC puzzle refresh. `released` still equals the drag distance, so
# GAP here is the cutout's x-offset relative to the track's left edge.
WIDGET_IMG_PAGE = """<!doctype html><html><body>
<div data-role="track" style="position:relative;width:300px;height:156px">
  <img data-role="puzzle-bg" src="/puzzle-img?n={nonce}&l={load}&k=bg"
       width="300" height="120" style="position:absolute;left:0;top:0">
  <img data-role="puzzle-piece" src="/puzzle-img?n={nonce}&l={load}&k=piece"
       width="46" height="40" style="position:absolute;left:0;top:40px">
  <div data-role="refresh" title="refresh"
       style="position:absolute;right:3px;top:3px;width:18px;height:18px;
              background:#fff;border:1px solid #999;cursor:pointer"></div>
  <div style="position:absolute;left:0;top:120px;width:300px;height:36px;
              background:#e8e8e8">
    <div class="slider-btn" data-role="handle"
         style="position:absolute;left:0;top:0;width:44px;height:36px;background:#3662d8"></div>
  </div>
  <div class="slider-state" data-state="idle"></div>
</div>
<script>
(function() {{
  const track = document.querySelector('[data-role=track]');
  const handle = document.querySelector('[data-role=handle]');
  const state = document.querySelector('.slider-state');
  const GAP = {gap};
  document.querySelector('[data-role=refresh]')
    .addEventListener('click', () => location.reload());
  {body_js}
}})();
</script></body></html>"""

# Shared drag/track-validation logic, interpolated into both widget pages.
_WIDGET_BODY_JS = """
  let trace = [], dragging = false, t0 = 0;
  function stat() {
    state.setAttribute('data-state', 'idle');
  }
  handle.addEventListener('mousedown', (e) => {
    dragging = true; t0 = performance.now(); trace = []; e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const r = track.getBoundingClientRect();
    const x = Math.max(0, Math.min(300, e.clientX - r.left));
    trace.push({x: x, y: e.clientY - r.top, t: performance.now() - t0});
    handle.style.left = (x - 22) + 'px';
  });
  document.addEventListener('mouseup', async () => {
    if (!dragging) return;
    dragging = false;
    const dur = performance.now() - t0;
    const ys = trace.map(p => p.y);
    const yspread = ys.length ? Math.max(...ys) - Math.min(...ys) : 0;
    const dts = [];
    for (let i = 1; i < trace.length; i++) dts.push(trace[i].t - trace[i-1].t);
    const mean = dts.reduce((a,b)=>a+b,0) / Math.max(1,dts.length);
    const sd = Math.sqrt(dts.map(v=>(v-mean)**2).reduce((a,b)=>a+b,0) / Math.max(1,dts.length));
    const released = parseFloat(handle.style.left || '0');
    // server side re-validates; client pre-checks only for UX
    const plausible = trace.length >= 15 && dur >= 300 && dur <= 15000
      && yspread > 0.2 && sd > 0.5 && Math.abs(released - GAP) <= 8;
    if (!plausible) {
      state.setAttribute('data-state','failed');
      parent.postMessage({type:'challenge', failed:true}, '*');
      handle.style.left = '0px';
      setTimeout(stat, 400);
      return;
    }
    const r = await fetch('/api/challenge-token', {method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({nonce:'__NONCE__', trace_len:trace.length,
        duration_ms:dur, released:released})});
    if (r.ok) {
      const j = await r.json();
      state.setAttribute('data-state','solved');
      parent.postMessage({type:'challenge', token:j.token}, '*');
    } else {
      state.setAttribute('data-state','failed');
      parent.postMessage({type:'challenge', failed:true}, '*');
      handle.style.left = '0px';
      setTimeout(stat, 400);
    }
  });
"""

MYSTERY_PAGE = """<!doctype html><html><body>
<div class="puzzle-holder" data-kind="mystery">
  <span class="knob"></span>
  <p>unfamiliar widget markup — no known signature</p>
</div></body></html>"""


def _sha(s: str, n: int = 20) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


PIECE_W, PIECE_H, PIECE_Y = 46, 40, 40


def render_puzzle(gap_x: int, bg_w: int = 300, bg_h: int = 120) -> tuple[bytes, bytes]:
    """bg + piece PNGs: noisy gradient background, the cut region lifted
    into the piece, a darkened cutout with a light outline left behind.
    Edges are what the CV matcher keys on, like the real puzzle."""
    import cv2
    import numpy as np

    rng = np.random.default_rng(gap_x * 7919 + 13)
    xs = np.linspace(0.0, 1.0, bg_w, dtype=np.float32)[None, :, None]
    base = 50.0 + 150.0 * xs
    noise = rng.normal(0, 16, (bg_h, bg_w, 1)) + rng.normal(0, 8, (bg_h, bg_w, 3))
    bg = np.clip(base + noise, 0, 255).astype(np.uint8)
    piece = bg[PIECE_Y:PIECE_Y + PIECE_H, gap_x:gap_x + PIECE_W].copy()
    cv2.rectangle(piece, (0, 0), (PIECE_W - 1, PIECE_H - 1), (250, 250, 250), 2)
    cv2.rectangle(bg, (gap_x, PIECE_Y), (gap_x + PIECE_W, PIECE_Y + PIECE_H),
                  (0, 0, 0), -1)
    cv2.rectangle(bg, (gap_x, PIECE_Y), (gap_x + PIECE_W, PIECE_Y + PIECE_H),
                  (245, 245, 245), 2)
    return (cv2.imencode(".png", bg)[1].tobytes(),
            cv2.imencode(".png", piece)[1].tobytes())


def _gap_for(nonce: str, load: int) -> int:
    return 130 + ((load * 37 + int(_sha(nonce)[:4], 16)) % 105)


class FixtureServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 outbox_dir: str | Path | None = None):
        self.host = host
        self.outbox_dir = Path(outbox_dir or Path(__file__).parent / "outbox")
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.accounts: dict[str, dict] = {}
        self.variant = False
        self.flaky_rejects = 0
        self.widget_mode = "attr"          # "attr" | "img"
        self._widget_loads: dict[str, int] = {}
        self._puzzle_cache: dict[str, tuple[bytes, bytes]] = {}
        self.counters = {
            "register_calls": 0,
            "widget_loads": 0,
            "challenge_token_ok": 0,
            "challenge_token_rej": 0,
            "verify_challenge_ok": 0,
        }
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: str | bytes, ctype="text/html", code=200):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _json(self, obj, code=200):
                self._send(json.dumps(obj), "application/json", code)

            def _qs(self):
                return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            def _body(self):
                n = int(self.headers.get("content-length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                q = self._qs()
                if path == "/register":
                    self._send(REGISTER_PAGE)
                elif path == "/verify":
                    tok = (q.get("t") or [""])[0]
                    acct = outer._by_token(tok)
                    if not acct:
                        self._send("bad token", code=404)
                        return
                    if outer.variant:
                        self._send(VERIFY_VARIANT_PAGE.format(nonce=acct["nonce"]))
                    else:
                        self._send(VERIFY_PAGE.format(nonce=acct["nonce"]))
                elif path == "/widget":
                    nonce = (q.get("n") or [""])[0]
                    load = outer._widget_loads.get(nonce, 0) + 1
                    outer._widget_loads[nonce] = load
                    outer.counters["widget_loads"] += 1
                    gap = _gap_for(nonce, load)
                    body_js = _WIDGET_BODY_JS.replace("__NONCE__", nonce)
                    if outer.widget_mode == "img":
                        outer._puzzle_cache[f"{nonce}:{load}"] = render_puzzle(gap)
                        self._send(WIDGET_IMG_PAGE.format(
                            nonce=nonce, load=load, gap=gap, body_js=body_js))
                    else:
                        self._send(WIDGET_PAGE.format(
                            nonce=nonce, gap=gap, body_js=body_js))
                elif path == "/puzzle-img":
                    key = f"{(q.get('n') or [''])[0]}:{(q.get('l') or [''])[0]}"
                    kind = (q.get("k") or ["bg"])[0]
                    pair = outer._puzzle_cache.get(key)
                    if not pair:
                        self._send("not found", code=404)
                        return
                    self._send(pair[0] if kind == "bg" else pair[1], "image/png")
                elif path == "/mystery-widget":
                    self._send(MYSTERY_PAGE)
                elif path == "/__state":
                    self._json({"counters": outer.counters,
                                "accounts": {e: {k: v for k, v in a.items() if k != "token"}
                                             for e, a in outer.accounts.items()},
                                "variant": outer.variant,
                                "widget_mode": outer.widget_mode,
                                "widget_loads": outer._widget_loads})
                else:
                    self._send("not found", code=404)

            def do_POST(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                body = self._body()
                if path == "/api/register":
                    outer.counters["register_calls"] += 1
                    email = body.get("email", "")
                    token = _sha("verify:" + email, 24)
                    nonce = _sha("nonce:" + email + token, 16)
                    outer.accounts[email] = {
                        "token": token, "nonce": nonce, "verified": False,
                    }
                    link = f"{outer.base_url}/verify?t={token}"
                    (outer.outbox_dir / f"{email}.txt").write_text(
                        f"Atria — confirm your address: {link}\n")
                    self._json({"ok": True})
                elif path == "/api/challenge-token":
                    if (int(body.get("trace_len") or 0) >= 15
                            and float(body.get("duration_ms") or 0) >= 300):
                        if outer.flaky_rejects > 0:
                            outer.flaky_rejects -= 1
                            outer.counters["challenge_token_rej"] += 1
                            self._json({"err": "reject"}, code=400)
                            return
                        nonce = body.get("nonce", "")
                        outer.counters["challenge_token_ok"] += 1
                        outer._tokens[_sha("ct:" + nonce, 16)] = nonce
                        self._json({"token": _sha("ct:" + nonce, 16)})
                    else:
                        outer.counters["challenge_token_rej"] += 1
                        self._json({"err": "implausible"}, code=400)
                elif path == "/api/verify-challenge":
                    nonce = body.get("nonce", "")
                    token = body.get("token", "")
                    acct = outer._by_nonce(nonce)
                    if acct and outer._tokens.get(token) == nonce:
                        outer.counters["verify_challenge_ok"] += 1
                        acct["verified"] = True
                        api_key = f"ak_fix_{_sha(acct['email'] + ':' + nonce, 20)}"
                        self._json({"api_key": api_key,
                                    "key_id": f"key_{_sha(acct['email'], 8)}"})
                    else:
                        self._json({"err": "bad challenge token"}, code=400)
                elif path == "/__config":
                    outer.variant = bool(body.get("variant", outer.variant))
                    outer.flaky_rejects = int(body.get("flaky_rejects", outer.flaky_rejects))
                    if body.get("widget_mode"):
                        outer.widget_mode = body["widget_mode"]
                    self._json({"ok": True})
                elif path == "/__reset":
                    outer.accounts.clear()
                    outer._tokens.clear()
                    outer._widget_loads.clear()
                    outer._puzzle_cache.clear()
                    for k in outer.counters:
                        outer.counters[k] = 0
                    outer.variant = False
                    outer.flaky_rejects = 0
                    outer.widget_mode = "attr"
                    for f in outer.outbox_dir.glob("*.txt"):
                        f.unlink()
                    self._json({"ok": True})
                else:
                    self._send("not found", code=404)

        self._tokens: dict[str, str] = {}
        self.httpd = ThreadingHTTPServer((host, port), H)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://{host}:{self.port}"
        self._thread: threading.Thread | None = None

    def _by_token(self, token: str):
        for email, a in self.accounts.items():
            if a["token"] == token:
                a["email"] = email
                return a
        return None

    def _by_nonce(self, nonce: str):
        for email, a in self.accounts.items():
            if a["nonce"] == nonce:
                a["email"] = email
                return a
        return None

    def post(self, path: str, body: dict):
        import urllib.request
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def get_state(self):
        import urllib.request
        with urllib.request.urlopen(self.base_url + "/__state") as r:
            return json.loads(r.read())

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
