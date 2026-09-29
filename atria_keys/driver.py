"""Playwright browser driver for api.atria-asi.ai.

Session hygiene (the risk engine scores it):
- `launch_persistent_context` per run — a real cookie/session profile,
  not a cold fresh context.
- Headed-capable launch (real display; use Xvfb on Linux): headless-shell
  fingerprints are detectable.
- A short warm visit of the site before registration so the captcha
  session isn't cold.
- Default fingerprint left intact — no canvas/WebGL stealth patching.

Also owns network-level token capture: responses on NC-style endpoints
(`nc_`, `slidebar`, `analysis`, `challenge`, `verify`, `captcha`) are
recorded — the widget's own verification submit is the most reliable
token source.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from .challenge import install_token_hook
from .errors import DeadSelectorError, TransientError

log = logging.getLogger("atria_keys.driver")

_KEY_RE = re.compile(r"\b(?:ak|atr)[-_][A-Za-z0-9_\-]{8,}\b")

# URL fragments on which a risk-control verification/token submit rides.
TOKEN_ENDPOINT_RE = re.compile(
    r"nc_|slidebar|analysis|challenge|captcha|verify|nc\.aliyun|cf\.aliyun"
    r"|interaction|certify|verification",
    re.IGNORECASE,
)

# Hidden-field names a registration submit may want the widget token in.
TOKEN_FIELD_RE = re.compile(r"token|nc_|captcha|csrf|slide|verify", re.IGNORECASE)


class BrowserDriver:
    def __init__(self, cfg, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.user_data_dir = (
            # A stable profile lets device-trust state (cookies, history)
            # accumulate across runs instead of every run starting cold.
            cfg.path("browser.session_dir", "keys/sessions")
            / (cfg.get("browser.profile") or run_id)
        )
        self.captured: list[dict] = []
        self._generation = 0

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "BrowserDriver":
        self._pw = sync_playwright().start()
        self._open_context()
        return self

    def _open_context(self) -> None:
        headless = bool(self.cfg.get("browser.headless", True))
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        channel = self.cfg.get("browser.channel")  # "chrome" = retail binary
        vp = self.cfg.get("browser.viewport") or {"width": 1440, "height": 900}
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=headless,
            channel=channel or None,
            slow_mo=int(self.cfg.get("browser.slow_mo_ms", 0)),
            viewport=vp,
            # Retina Mac scale — dpr=1 is a VM tell.
            device_scale_factor=float(self.cfg.get("browser.device_scale_factor", 2)),
            args=[
                # Otherwise navigator.webdriver stays true and the risk
                # engine flags the session pre-slide.
                "--disable-blink-features=AutomationControlled",
                *(self.cfg.get("browser.args", []) or []),
            ],
        )
        install_token_hook(self.context)
        self.context.on("response", self._capture_response)
        self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        renderer = self.cfg.get(
            "browser.webgl_renderer",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
        )
        vendor = self.cfg.get("browser.webgl_vendor", "Google Inc. (Apple)")
        # VM GPU ("Apple Paravirtual device") is a hard bot tell — present a
        # real Apple Silicon renderer string instead.
        self.context.add_init_script(
            f"""(() => {{
              const R = {renderer!r}, V = {vendor!r};
              const patch = (proto) => {{
                const gp = proto.getParameter;
                proto.getParameter = function (p) {{
                  if (p === 0x9245) return V;   // UNMASKED_VENDOR_WEBGL
                  if (p === 0x9246) return R;   // UNMASKED_RENDERER_WEBGL
                  return gp.call(this, p);
                }};
              }};
              patch(WebGLRenderingContext.prototype);
              if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
            }})();"""
        )
        self.page = self.context.new_page()

    def reset_context(self) -> None:
        """Fresh persistent context (new profile dir) — used after repeated
        widget rejections so the next attempt isn't on a burned session."""
        self._generation += 1
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.user_data_dir = (
            self.cfg.path("browser.session_dir", "keys/sessions")
            / f"{self.run_id}-g{self._generation}"
        )
        self.captured.clear()
        self._open_context()
        log.info("context reset (generation %d)", self._generation)

    def save_session(self) -> None:
        # Persistent contexts write state continuously; also keep a
        # storage_state snapshot for inspection/resume tooling.
        if self.context is None:
            return
        try:
            path = self.user_data_dir / "storage_state.json"
            self.context.storage_state(path=str(path))
        except Exception:
            pass

    def __exit__(self, *exc) -> None:
        try:
            self.save_session()
        except Exception:
            pass
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        if self._pw:
            self._pw.stop()

    # -- token capture (AK-202) --------------------------------------------

    def _capture_response(self, response) -> None:
        url = response.url
        if not TOKEN_ENDPOINT_RE.search(url):
            return
        entry: dict = {"url": url, "status": response.status, "ts": time.time()}
        try:
            entry["json"] = response.json()
        except Exception:
            try:
                entry["text"] = response.text()[:4000]
            except Exception:
                pass
        for k in ("x-nc-token", "set-cookie"):
            v = response.headers.get(k)
            if v:
                entry.setdefault("headers", {})[k] = v
        self.captured.append(entry)
        log.debug("captured %s (%d)", url, response.status)

    def captured_token(self) -> str | None:
        """First token-ish value seen on a captured endpoint, most recent
        last. Common shapes: {token}, {data:{token}}, {ses}, {sig}."""
        for entry in reversed(self.captured):
            j = entry.get("json")
            if not isinstance(j, dict):
                continue
            for key in ("token", "ses", "sig", "nc_token", "ctoken"):
                if j.get(key):
                    return str(j[key])
            data = j.get("data")
            if isinstance(data, dict):
                for key in ("token", "ses", "sig"):
                    if data.get(key):
                        return str(data[key])
        return None

    def inject_token(self, token: str) -> dict:
        """Deliver the widget token to the registration surface both ways a
        real integration can want it: hidden form field and request header
        (via a route on the register submit path)."""
        page = self.page
        assert page is not None
        filled = page.evaluate(
            """(tok) => {
                let hit = null;
                for (const el of document.querySelectorAll('input')) {
                    const name = (el.name || '') + ' ' + (el.id || '');
                    if (/token|nc_|captcha|csrf|slide|verify/i.test(name)) {
                        el.value = tok; hit = el.name || el.id;
                    }
                }
                return hit;
            }""",
            token,
        )
        header_name = self.cfg.get("target.token_header")
        if header_name:
            def _route(route):
                headers = {**route.request.headers, header_name.lower(): token}
                route.continue_(headers=headers)

            page.route("**/*", _route)
        return {"form_field": filled, "header": header_name}

    # -- page helpers -------------------------------------------------------

    def _first_selector(self, key: str, what: str):
        page = self.page
        assert page is not None
        for sel in self.cfg.get(f"target.selectors.{key}", []):
            el = page.query_selector(sel)
            if el:
                return el
        raise DeadSelectorError(f"{what}: exhausted selectors for '{key}'")

    def goto(self, url: str) -> None:
        assert self.page is not None
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout as exc:
            raise TransientError(f"navigation timeout: {url}") from exc

    def warm_visit(self, rng: random.Random | None = None) -> None:
        """Short browse of the site before registration — the risk engine
        scores a cold context that lands straight on the form."""
        rng = rng or random.Random()
        dwell = float(self.cfg.get("session.warm_dwell_s", 0))
        if dwell <= 0:
            return
        base = self.cfg.get("target.base_url", "").rstrip("/")
        paths = self.cfg.get("session.warm_paths", ["/"])
        page = self.page
        assert page is not None
        for path in paths[:2]:
            try:
                self.goto(base + path)
            except TransientError:
                continue
        deadline = time.monotonic() + dwell
        while time.monotonic() < deadline:
            if rng.random() < 0.5:
                page.mouse.wheel(0, rng.uniform(120, 500))
            else:
                page.mouse.move(
                    rng.uniform(100, 1100), rng.uniform(100, 700)
                )
            page.wait_for_timeout(rng.uniform(700, 2500))

    def fill_registration(self, email: str) -> None:
        """Open the registration surface and submit the per-run address.

        `target.registration_url` (absolute) wins over base_url + register_path
        — on the live site the entrypoint is the sign-in page, which only
        hands you a Logto interaction session; the account form is one
        'create account' click deeper.
        """
        url = self.cfg.get("target.registration_url") or (
            self.cfg.get("target.base_url", "").rstrip("/")
            + self.cfg.get("target.register_path", "/register")
        )
        self.goto(url)
        assert self.page is not None
        self.page.wait_for_timeout(int(self.cfg.get("run.page_settle_ms", 2000)))
        # Hosted-auth entry (Logto): the account form is behind a
        # 'Create account' link. Optional — absent on the fixture.
        for sel in self.cfg.get("target.selectors.create_account_link", []):
            el = self.page.query_selector(sel)
            if el:
                el.click()
                self.page.wait_for_timeout(2000)
                break
        field = self._first_selector("email_input", "email input")
        field.fill(email)
        submit = self._first_selector("submit", "submit button")
        submit.click()
        self.page.wait_for_timeout(1500)

    def enter_verification_code(self, code: str) -> None:
        """Code flow (Logto): the email carries a numeric code, entered on
        the same auth page after the captcha check passes."""
        field = self._first_selector("code_input", "verification-code input")
        field.fill(code)
        submit = self._first_selector("code_submit", "code submit button")
        submit.click()
        assert self.page is not None
        self.page.wait_for_timeout(2500)

    def extract_api_key(self) -> tuple[str, str]:
        """Pull the issued key + key_id from the confirmation surface.

        When `target.console_path` + `selectors.console_create_button` are
        configured the surface is the API console: navigate, click the
        create-key control, then read the freshly shown key."""
        page = self.page
        assert page is not None
        console_path = self.cfg.get("target.console_path")
        create_sels = self.cfg.get("target.selectors.console_create_button", [])
        if console_path and create_sels:
            base = self.cfg.get("target.base_url", "").rstrip("/")
            self.goto(base + console_path)
            page.wait_for_timeout(2500)
            for sel in create_sels:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    page.wait_for_timeout(1500)
                    break
        for sel in self.cfg.get("target.selectors.api_key_holder", ["[data-api-key]"]):
            try:
                el = page.wait_for_selector(sel, timeout=8000, state="attached")
            except PWTimeout:
                continue
            if el:
                key = el.get_attribute("data-api-key") or el.inner_text().strip()
                key_id = el.get_attribute("data-key-id") or ""
                if key:
                    return key, key_id or f"key_{abs(hash(key)) & 0xffffffff:x}"
        body = page.content()
        m = _KEY_RE.search(body)
        if m:
            return m.group(0), f"key_{abs(hash(m.group(0))) & 0xffffffff:x}"
        tok = page.evaluate("() => window.__issuedKey && window.__issuedKey.api_key")
        if tok:
            kid = page.evaluate("() => window.__issuedKey.key_id")
            return tok, kid or "key_inline"
        raise DeadSelectorError("api key holder not found on confirmation surface")
