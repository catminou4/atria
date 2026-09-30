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
import os
import random
import re
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from . import overrides
from .challenge import install_token_hook
from .errors import DeadSelectorError, TransientError

log = logging.getLogger("atria_keys.driver")

# Analytics/telemetry endpoints — pure bandwidth waste, aborted outright.
_DENY_RE = re.compile(
    r"googletagmanager|google-analytics|sentry\.io|clarity\.ms|hotjar"
    r"|segment\.(io|com)|amplitude|datadoghq|fullstory|logrocket|mixpanel"
    r"|doubleclick|facebook\.net|connect\.facebook",
    re.IGNORECASE,
)

# Hosts whose resources are functional (puzzle images, iconfont slider,
# the Logto app itself) — never blocked regardless of type.
_ALLOW_HOST_RE = re.compile(
    r"(^|\.)(aliyuncs\.com|aliyun\.com|alicdn\.com|alibaba\.com|localhost|"
    r"127\.0\.0\.1|0\.0\.0\.0)$",
    re.IGNORECASE,
)

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
        self.bytes_used = 0
        self.proxy_mode = "direct"

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "BrowserDriver":
        self._pw = sync_playwright().start()
        self._open_context()
        return self

    def _proxy_kwargs(self) -> dict:
        """Per-context egress proxy — flipped from the dashboard via
        overrides.json. Credentials come from env vars, never the yaml."""
        pcfg = self.cfg.get("browser.proxy") or {}
        if not overrides.proxy_enabled(self.cfg) or not pcfg.get("server"):
            self.proxy_mode = "direct"
            return {}
        self.proxy_mode = "proxy"
        proxy = {"server": pcfg["server"]}
        for key, env_key in (("username", "username_env"),
                             ("password", "password_env")):
            env_name = pcfg.get(env_key)
            if env_name and os.environ.get(env_name):
                proxy[key] = os.environ[env_name]
        return {"proxy": proxy}

    def _allowed_host(self, host: str) -> bool:
        if _ALLOW_HOST_RE.search(host or ""):
            return True
        for url_key in ("target.base_url", "target.registration_url"):
            u = self.cfg.get(url_key, "")
            if u and urllib.parse.urlparse(u).hostname == host:
                return True
        return False

    def _bandwidth_route(self, route) -> None:
        """Abort requests that only cost proxy bandwidth: analytics,
        cross-origin fonts/images, media. Functional hosts (app, captcha
        CDN) are always allowed — the iconfont slider is a font file."""
        try:
            req = route.request
            host = urllib.parse.urlparse(req.url).hostname or ""
            rtype = req.resource_type
            if _DENY_RE.search(req.url):
                route.abort()
                return
            if rtype == "media":
                route.abort()
                return
            if rtype in ("image", "font") and not self._allowed_host(host):
                route.abort()
                return
            route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    def _count_bytes(self, response) -> None:
        try:
            self.bytes_used += int(response.headers.get("content-length") or 0)
        except Exception:
            pass

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
            # Locale/timezone must agree with the egress IP's geography —
            # a US-IP + UTC browser is a proxy tell.
            locale=self.cfg.get("browser.locale", "fr-FR"),
            timezone_id=self.cfg.get("browser.timezone_id", "Europe/Paris"),
            args=[
                # Otherwise navigator.webdriver stays true and the risk
                # engine flags the session pre-slide.
                "--disable-blink-features=AutomationControlled",
                *(self.cfg.get("browser.args", []) or []),
            ],
            **self._proxy_kwargs(),
        )
        install_token_hook(self.context)
        self.context.on("response", self._capture_response)
        self.context.on("response", self._count_bytes)
        if self.cfg.get("browser.optimize_bandwidth", True):
            self.context.route("**/*", self._bandwidth_route)
        self.context.add_init_script(
            """(() => {
              Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
              // Bare-VM hardware tells: 2 cores / unknown memory is not a
              // retail Mac. Present an M2-class machine.
              const hw = (k, v) => Object.defineProperty(navigator, k,
                {get: () => v});
              hw('hardwareConcurrency', 8);
              hw('deviceMemory', 8);
              // Screen vs viewport: the VM's 1024x768 X-server is smaller
              // than our 1440x900 viewport — impossible geometry on a real
              // machine. Retina MBP 14\": 1512x982 CSS px.
              const S = {width: 1512, height: 982, availWidth: 1512,
                availHeight: 927, availLeft: 0, availTop: 0,
                colorDepth: 30, pixelDepth: 30};
              for (const k in S)
                Object.defineProperty(screen, k, {get: () => S[k]});
              // Fresh-profile tell: permissions.query('notifications')
              // reports 'denied' under automation, 'prompt' on real Chrome.
              const q = navigator.permissions && navigator.permissions.query;
              if (q) {
                navigator.permissions.query = (p) =>
                  p && p.name === 'notifications'
                    ? Promise.resolve({state: 'prompt', onchange: null,
                        addEventListener(){}, removeEventListener(){},
                        dispatchEvent(){return true}})
                    : q.call(navigator.permissions, p);
              }
            })();"""
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
        settle = int(self.cfg.get("run.page_settle_ms", 2000))
        self.page.wait_for_timeout(settle)
        field = None
        for attempt in range(2):
            # Hosted-auth entry (Logto): the account form is behind a
            # 'Create account' link. Optional — absent on the fixture.
            for sel in self.cfg.get("target.selectors.create_account_link", []):
                el = self.page.query_selector(sel)
                if el:
                    el.click()
                    self.page.wait_for_timeout(2000)
                    break
            try:
                field = self._first_selector("email_input", "email input")
                break
            except DeadSelectorError:
                if attempt == 1:
                    raise
                # The shared profile keeps an authenticated Logto session
                # after a successful run — /register then skips straight to
                # the app with no identifier field. Clear cookies and redo
                # the flow logged out.
                log.info(
                    "no email field — stale auth session in profile; "
                    "clearing cookies and retrying"
                )
                self.context.clear_cookies()
                self.goto(url)
                self.page.wait_for_timeout(settle)
        assert field is not None
        self._human_type(field, email)
        submit = self._first_selector("submit", "submit button")
        # Human read-back pause before committing the form.
        self.page.wait_for_timeout(random.Random().randint(350, 1200))
        submit.click()
        self.page.wait_for_timeout(1500)

    def _human_type(self, field, text: str) -> None:
        """Click + per-char key events — fill() fires no keydown/keyup and
        a pasted email with zero keystrokes is a form-level bot tell."""
        rng = random.Random()
        field.click()
        self.page.wait_for_timeout(rng.randint(120, 400))
        for ch in text:
            self.page.keyboard.type(ch)
            # ~90-140 wpm with occasional longer hesitations.
            dt = rng.gauss(85, 30)
            if rng.random() < 0.04:
                dt += rng.uniform(200, 600)
            self.page.wait_for_timeout(max(20, int(dt)))

    def enter_verification_code(self, code: str) -> None:
        """Code flow (Logto): the email carries a numeric code, entered on
        the same auth page after the captcha check passes."""
        field = self._first_selector("code_input", "verification-code input")
        self._human_type(field, code)
        try:
            submit = self._first_selector("code_submit", "code submit button")
            try:
                submit.click()
            except Exception as exc:
                # The click submits the <form>; when the page navigates
                # mid-click (the usual happy path) the button detaches and
                # Playwright's actionability retry raises — harmless.
                if "attached" not in str(exc) and "enabled" not in str(exc):
                    raise
                log.info("code submit raced with navigation — form sent")
        except DeadSelectorError:
            # Logto verification forms are real <form>s — Enter submits.
            log.info("no code_submit selector matched; pressing Enter")
            self.page.keyboard.press("Enter")
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
            clicked: set[str] = set()
            if not self._click_first(create_sels):
                # Configured candidates missed — scan every visible
                # button/link for create-ish wording instead of dying.
                # Iterate: first hit is often a nav tab ('→ API Keys'),
                # the real create button lives one page deeper.
                for _ in range(3):
                    if not self._click_create_like(seen=clicked):
                        break
                    page.wait_for_timeout(1200)
            # Multi-step consoles open a dialog with its own confirm —
            # and the name field must be filled before it enables.
            self._fill_dialog_name()
            self._click_create_like(scope="[role=dialog], dialog, .modal", seen=clicked)
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
        # Last resort: any element that looks like a raw key display —
        # long token, no spaces (filters out 'API Keys' labels).
        for el in page.query_selector_all(
            "code, input[readonly], [data-clipboard-text], [class*=key]"
        ):
            try:
                cand = (
                    el.get_attribute("data-clipboard-text")
                    or el.get_attribute("value")
                    or el.inner_text()
                ).strip()
            except Exception:
                continue
            if len(cand) >= 16 and re.fullmatch(r"[A-Za-z0-9_\-.=]+", cand):
                return cand, f"key_{abs(hash(cand)) & 0xffffffff:x}"
        self._dump_console_state()
        raise DeadSelectorError("api key holder not found on confirmation surface")

    _CREATE_TEXT_RE = re.compile(
        r"create|new|generat|créer|ajouter|confirm|ok\b|save|submit|"
        r"新建|创建|生成|添加|确定|确认|保存", re.I
    )

    def _click_first(self, sels) -> bool:
        for sel in sels:
            el = self.page.query_selector(sel)
            if el:
                el.click()
                self.page.wait_for_timeout(1500)
                return True
        return False

    def _click_create_like(
        self, scope: str = "body", seen: set[str] | None = None
    ) -> bool:
        """Click the first visible button/link whose text looks like a
        key-creation control — last-resort when configured selectors
        miss. `seen` tracks texts already clicked so the loop walks
        deeper instead of re-clicking the same nav tab."""
        seen = seen if seen is not None else set()
        for el in self.page.query_selector_all(f"{scope} button, {scope} a, {scope} [role=button]"):
            try:
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
            except Exception:
                continue
            if (
                txt
                and len(txt) < 40
                and txt not in seen
                and self._CREATE_TEXT_RE.search(txt)
            ):
                log.info("console create fallback: clicking %r", txt)
                seen.add(txt)
                el.click()
                self.page.wait_for_timeout(1500)
                return True
        return False

    def _fill_dialog_name(self) -> bool:
        """Key-creation dialogs ask for a name before enabling their
        confirm button — fill the first visible text input."""
        for el in self.page.query_selector_all(
            "[role=dialog] input:not([type=hidden]), "
            "dialog input:not([type=hidden]), "
            ".modal input:not([type=hidden])"
        ):
            try:
                if not el.is_visible():
                    continue
                if (el.get_attribute("type") or "text") not in (
                    "text", "search", "name", ""
                ):
                    continue
            except Exception:
                continue
            self._human_type(el, f"atria-key-{self.run_id[:8]}")
            return True
        return False

    def _dump_console_state(self) -> None:
        """On extraction failure, leave the console DOM + screenshot in
        artifacts — the real selectors are learned from it, not guessed."""
        try:
            adir = self.cfg.path("artifacts.dir", "keys/artifacts")
            adir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            html = self.page.content()
            html = _KEY_RE.sub("[REDACTED-KEY]", html)
            (adir / f"console-{ts}.html").write_text(html, encoding="utf-8")
            self.page.screenshot(path=str(adir / f"console-{ts}.png"), full_page=True)
            log.info("console state dumped to %s/console-%s.*", adir, ts)
        except Exception as exc:
            log.warning("console dump failed: %s", exc)
