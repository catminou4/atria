"""Playwright browser driver for api.atria-asi.ai.

Owns the per-run browser context (cookies/session persisted under
browser.session_dir so a resumed run keeps its authenticated state),
registration-form filling via configurable selector sets, and API-key
extraction. Selector exhaustion is a dead-selector fatal — never a retry.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from .challenge import install_token_hook
from .errors import DeadSelectorError, TransientError

log = logging.getLogger("atria_keys.driver")

_KEY_RE = re.compile(r"\b(ak[-_][A-Za-z0-9_\-]{8,})\b")


class BrowserDriver:
    def __init__(self, cfg, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.session_path = cfg.path("browser.session_dir", "keys/sessions") / f"{run_id}.json"

    def __enter__(self) -> "BrowserDriver":
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=bool(self.cfg.get("browser.headless", True)),
            slow_mo=int(self.cfg.get("browser.slow_mo_ms", 0)),
        )
        kwargs = {}
        if self.session_path.exists():
            kwargs["storage_state"] = str(self.session_path)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800}, **kwargs
        )
        install_token_hook(self.context)
        self.page = self.context.new_page()
        return self

    def save_session(self) -> None:
        if self.context is None:
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.session_path))

    def __exit__(self, *exc) -> None:
        try:
            self.save_session()
        except Exception:
            pass
        for closer in (self.context, self.browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()

    # -- page helpers -----------------------------------------------------

    def _first_selector(self, key: str, what: str):
        page = self.page
        assert page is not None
        for sel in self.cfg.get(f"target.selectors.{key}", []):
            el = page.wait_for_selector(sel, timeout=2500, state="visible") \
                if page.query_selector(sel) else page.query_selector(sel)
            if el:
                return el
        raise DeadSelectorError(f"{what}: exhausted selectors for '{key}'")

    def goto(self, url: str) -> None:
        assert self.page is not None
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout as exc:
            raise TransientError(f"navigation timeout: {url}") from exc

    def fill_registration(self, email: str) -> None:
        """Open the registration page and submit the per-run address."""
        base = self.cfg.get("target.base_url", "").rstrip("/")
        path = self.cfg.get("target.register_path", "/register")
        self.goto(base + path)
        field = self._first_selector("email_input", "email input")
        field.fill(email)
        submit = self._first_selector("submit", "submit button")
        submit.click()
        assert self.page is not None
        self.page.wait_for_timeout(1200)

    def extract_api_key(self) -> tuple[str, str]:
        """Pull the issued key + key_id from the confirmation surface."""
        page = self.page
        assert page is not None
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
        # Fallback: key embedded in page text / JS variable.
        body = page.content()
        m = _KEY_RE.search(body)
        if m:
            return m.group(1), f"key_{abs(hash(m.group(1))) & 0xffffffff:x}"
        tok = page.evaluate("() => window.__issuedKey && window.__issuedKey.api_key")
        if tok:
            kid = page.evaluate("() => window.__issuedKey.key_id")
            return tok, kid or "key_inline"
        raise DeadSelectorError("api key holder not found on confirmation surface")
