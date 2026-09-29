"""MailboxReader protocol + implementations.

The pipeline depends only on the protocol: allocate a per-run recipient
address, then poll until the verification message for that recipient lands
and extract its confirmation link and/or OTP. A temp-mail API reader can be
added later without touching the pipeline.
"""

from __future__ import annotations

import imaplib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Protocol

from .errors import VerificationTimeout

log = logging.getLogger("atria_keys.mailbox")

_LINK_RE = re.compile(r"https?://[^\s\"'<>)]+")
_VERIFY_HINT = re.compile(r"verify|confirm|activate|token|valid", re.IGNORECASE)
_OTP_RE = re.compile(r"\b(\d{4,8})\b")


@dataclass
class VerificationMessage:
    recipient: str
    link: str | None = None
    otp: str | None = None
    subject: str = ""
    raw_path: str = ""


def _extract(text: str) -> tuple[str | None, str | None]:
    links = _LINK_RE.findall(text)
    link = next((u for u in links if _VERIFY_HINT.search(u)), links[0] if links else None)
    otp_m = _OTP_RE.search(text)
    return link, (otp_m.group(1) if otp_m else None)


class MailboxReader(Protocol):
    def allocate_address(self, run_id: str) -> str: ...
    def wait_for_message(
        self, recipient: str, timeout_s: float, poll_s: float = 5.0
    ) -> VerificationMessage: ...


def render_address(template: str, run_id: str, domain: str) -> str:
    return template.format(run_id=run_id, catchall_domain=domain)


class FixtureMailboxReader:
    """Polls a local outbox directory for `<recipient>.txt|.eml` files.
    Used by the offline fixture mode and tests."""

    def __init__(self, outbox_dir: str | Path, template: str, domain: str):
        self.outbox_dir = Path(outbox_dir)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.template = template
        self.domain = domain

    def allocate_address(self, run_id: str) -> str:
        return render_address(self.template, run_id, self.domain)

    def wait_for_message(
        self, recipient: str, timeout_s: float, poll_s: float = 5.0
    ) -> VerificationMessage:
        deadline = time.monotonic() + timeout_s
        stems = [recipient]
        while time.monotonic() < deadline:
            for stem in stems:
                for ext in (".txt", ".eml"):
                    p = self.outbox_dir / f"{stem}{ext}"
                    if p.exists():
                        text = p.read_text(encoding="utf-8", errors="replace")
                        link, otp = _extract(text)
                        return VerificationMessage(
                            recipient=recipient, link=link, otp=otp, raw_path=str(p)
                        )
            time.sleep(poll_s)
        raise VerificationTimeout(f"no message for {recipient} within {timeout_s}s")


class ImapMailboxReader:
    """Polls a catch-all IMAP inbox for mail addressed to the run's
    recipient and extracts the verification link / OTP."""

    def __init__(self, host: str, port: int, username: str, password_env: str,
                 folder: str, template: str, domain: str):
        self.host, self.port = host, port
        self.username = username
        self.password_env = password_env
        self.folder = folder
        self.template = template
        self.domain = domain

    def allocate_address(self, run_id: str) -> str:
        return render_address(self.template, run_id, self.domain)

    def _connect(self) -> imaplib.IMAP4_SSL:
        password = os.environ.get(self.password_env, "")
        if not password:
            raise VerificationTimeout(
                f"IMAP password env {self.password_env} is not set"
            )
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        conn.login(self.username, password)
        conn.select(self.folder)
        return conn

    @staticmethod
    def _body_text(msg: Message) -> str:
        parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type().startswith("text/"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
        return "\n".join(parts)

    def _scan(self, conn: imaplib.IMAP4_SSL, recipient: str) -> VerificationMessage | None:
        typ, data = conn.search(None, f'(TO "{recipient}")')
        if typ != "OK" or not data or not data[0]:
            return None
        for num in data[0].split():
            typ, fetched = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            msg = message_from_bytes(fetched[0][1])
            link, otp = _extract(self._body_text(msg) + "\n" + (msg.get("Subject") or ""))
            if link or otp:
                return VerificationMessage(
                    recipient=recipient,
                    link=link,
                    otp=otp,
                    subject=msg.get("Subject", ""),
                )
        return None

    def wait_for_message(
        self, recipient: str, timeout_s: float, poll_s: float = 5.0
    ) -> VerificationMessage:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                conn = self._connect()
                try:
                    found = self._scan(conn, recipient)
                finally:
                    try:
                        conn.logout()
                    except Exception:
                        pass
                if found:
                    return found
            except VerificationTimeout:
                raise
            except Exception as exc:  # connection flap is transient
                log.warning("imap poll failed: %s", exc)
            time.sleep(poll_s)
        raise VerificationTimeout(f"no message for {recipient} within {timeout_s}s")


class TempMailReader:
    """Disposable-mailbox reader over the public mail.tm API (free, no key).

    Each run's recipient is a real account created on the service — the
    provider's mail actually lands there, unlike a synthesized catch-all.
    """

    def __init__(self, api_base: str, template: str, local_domain: str):
        self.api_base = api_base.rstrip("/")
        self.template = template
        self.local_domain = local_domain
        self._tokens: dict[str, str] = {}

    def _call(self, method: str, path: str, body=None, token: str = ""):
        req = urllib.request.Request(
            self.api_base + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    @staticmethod
    def _members(body) -> list:
        # Accept:application/json returns a bare list; ld+json wraps it.
        if isinstance(body, list):
            return body
        return body.get("hydra:member", [])

    def _active_domain(self) -> str:
        for d in self._members(self._call("GET", "/domains")):
            if d.get("isActive"):
                return d["domain"]
        raise VerificationTimeout("mail.tm: no active domain")

    def allocate_address(self, run_id: str) -> str:
        # Reuse the configured template for the local part; the domain comes
        # from the service, not our config.
        local = self.template.format(run_id=run_id, catchall_domain="x").split("@")[0]
        address = f"{local or f'svc-{run_id[:12]}'}@{self._active_domain()}"
        password = secrets.token_urlsafe(18)
        for attempt in range(5):
            try:
                self._call("POST", "/accounts", {"address": address, "password": password})
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 4:
                    raise
                time.sleep(8 * (attempt + 1))
        token = self._call("POST", "/token", {"address": address, "password": password})
        self._tokens[address] = token["token"]
        return address

    def _scan(self, recipient: str) -> VerificationMessage | None:
        token = self._tokens.get(recipient)
        if not token:
            return None
        listing = self._members(self._call("GET", "/messages", token=token))
        for m in listing:
            full = self._call("GET", f"/messages/{m['id']}", token=token)
            html = "\n".join(full.get("html") or [])
            text = "\n".join([
                full.get("text") or "", html,
                re.sub(r"<[^>]+>", " ", html), full.get("subject") or "",
            ])
            link, otp = _extract(text)
            if link or otp:
                return VerificationMessage(
                    recipient=recipient, link=link, otp=otp,
                    subject=full.get("subject", ""),
                )
        return None

    def wait_for_message(
        self, recipient: str, timeout_s: float, poll_s: float = 5.0
    ) -> VerificationMessage:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                found = self._scan(recipient)
                if found:
                    return found
            except Exception as exc:
                log.warning("tempmail poll failed: %s", exc)
            time.sleep(poll_s)
        raise VerificationTimeout(f"no message for {recipient} within {timeout_s}s")


class TempMailLolReader:
    """Disposable-mailbox reader over the tempmail.lol v2 API (free, no key).

    Rotates across less-flagged domains than mail.tm; each run gets a real
    inbox created on the service. API: POST /v2/inbox/create →
    {address, token}; GET /v2/inbox?token=… → {emails: [...], expired}.
    """

    def __init__(self, api_base: str, template: str, local_domain: str):
        self.api_base = api_base.rstrip("/")
        self.template = template
        self.local_domain = local_domain
        self._tokens: dict[str, str] = {}

    def _call(self, method: str, path: str, body=None):
        url = self.api_base + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, method=method, data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                # urllib's default UA is CF-blocked with 403.
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def allocate_address(self, run_id: str) -> str:
        local = self.template.format(run_id=run_id, catchall_domain="x").split("@")[0]
        prefix = re.sub(r"[^a-zA-Z0-9]", "", (local or f"svc{run_id[:8]}").lower()) or "atria"
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                inbox = self._call("POST", "/v2/inbox/create", {"prefix": prefix})
                address = inbox["address"]
                self._tokens[address] = inbox["token"]
                return address
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code != 429 or attempt == 4:
                    raise
                time.sleep(8 * (attempt + 1))
        raise last_exc or VerificationTimeout("tempmail.lol: could not create inbox")

    def _scan(self, recipient: str) -> VerificationMessage | None:
        token = self._tokens.get(recipient)
        if not token:
            return None
        body = self._call(
            "GET", f"/v2/inbox?token={urllib.parse.quote(token)}"
        )
        for m in body.get("emails") or []:
            html = m.get("html") or ""
            text = "\n".join([
                m.get("body") or "", html,
                re.sub(r"<[^>]+>", " ", html), m.get("subject") or "",
            ])
            link, otp = _extract(text)
            if link or otp:
                return VerificationMessage(
                    recipient=recipient, link=link, otp=otp,
                    subject=m.get("subject", ""),
                )
        return None

    def wait_for_message(
        self, recipient: str, timeout_s: float, poll_s: float = 5.0
    ) -> VerificationMessage:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                found = self._scan(recipient)
                if found:
                    return found
            except Exception as exc:
                log.warning("tempmail.lol poll failed: %s", exc)
            time.sleep(poll_s)
        raise VerificationTimeout(f"no message for {recipient} within {timeout_s}s")


def build_reader(cfg) -> MailboxReader:
    kind = cfg.get("mailbox.reader", "imap")
    template = cfg.get("mailbox.address_template", "svc-{run_id}@{catchall_domain}")
    domain = cfg.get("mailbox.catchall_domain", "example.com")
    if kind == "fixture":
        return FixtureMailboxReader(cfg.path("mailbox.fixture.outbox_dir", "outbox"), template, domain)
    if kind == "tempmail":
        return TempMailReader(
            api_base=cfg.get("mailbox.tempmail.api_base", "https://api.mail.tm"),
            template=template,
            local_domain=domain,
        )
    if kind == "tempmaillol":
        return TempMailLolReader(
            api_base=cfg.get("mailbox.tempmaillol.api_base", "https://api.tempmail.lol"),
            template=template,
            local_domain=domain,
        )
    if kind == "imap":
        return ImapMailboxReader(
            host=cfg.get("mailbox.imap.host", ""),
            port=int(cfg.get("mailbox.imap.port", 993)),
            username=cfg.get("mailbox.imap.username", ""),
            password_env=cfg.get("mailbox.imap.password_env", "ATRIA_IMAP_PASSWORD"),
            folder=cfg.get("mailbox.imap.folder", "INBOX"),
            template=template,
            domain=domain,
        )
    raise ValueError(f"unknown mailbox reader: {kind}")
