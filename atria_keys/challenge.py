"""Native in-process driver for the embedded risk-control widget.

ChallengeDriver protocol + AlibabaCloudChallengeDriver implementation for
the NC slider variant: locate the widget iframe, resolve the slide
distance (widget attr → puzzle-image CV → refuse), drag the handle over a
human-plausible pointer trajectory (bezier easing, non-constant velocity,
overshoot + correction), and read the token the widget emits through its
own JS contract. No external solver calls; all computation on-device.
Canvas/fingerprint noise is left intact.

Unknown widget layouts are never blind-retried: they emit
UnsupportedChallengeVariant with a redacted DOM snapshot artifact.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from playwright.sync_api import Frame, Page

from .errors import (
    ChallengeRejected,
    DeadSelectorError,
    UnsupportedChallengeVariant,
)
from .gap_detect import (
    CONFIDENCE_ACCEPT,
    CONFIDENCE_TEMPLATE_ACCEPT,
    detect_gap_x,
    image_is_blank,
    image_width,
)

log = logging.getLogger("atria_keys.challenge")

# Signatures seen on Alibaba Cloud NC sliders (nc/aliyun containers,
# captcha/slide iframes). The fixture widget carries the same signature
# class so tests exercise the real detection path.
WIDGET_SIGNATURES = [
    "iframe[src*='aliyun']",
    "iframe[src*='captcha']",
    "iframe[src*='nc_']",
    "iframe[name*='nc_']",
    "iframe[src*='slide']",
    "iframe[data-variant='alibaba-slide']",
    "iframe#challenge-widget",
    ".aliyun-captcha",
    "[class*='nc-container']",
    "#nc_wrapper",
    ".nc_scale",
    "#aliyunCaptcha-captcha-wrapper",
    "#aliyunCaptcha-window-float",
    "#aliyun-captcha-widget",
]

# AliyunCaptcha v2 renders a collapsed box first — clicking it opens the
# sliding panel (`#aliyunCaptcha-window-float`). The element stays present
# after the panel opens, so an absent slider handle means "not opened yet".
OPENER_SELECTORS = [
    "#aliyunCaptcha-captcha-text-box",
    "#aliyunCaptcha-start-icon",
    "#aliyunCaptcha-captcha-body",
    ".aliyunCaptcha-captcha-text-box",
    ".aliyunCaptcha-start-icon",
    "[data-role='captcha-opener']",
]

# Panel visibility marks a live puzzle window.
PANEL_SELECTOR = "#aliyunCaptcha-window-float"

HANDLE_SELECTORS = [
    "[data-role='handle']",
    "#aliyunCaptcha-sliding-slider",
    ".nc_iconfont.btn_slide",
    "#nc_1_n1t",
    "[id*='nc_'][id*='n1t']",
    ".btn_slide",
    ".slider-btn",
    ".slider-handle",
    ".slider-move",
]

TRACK_SELECTORS = [
    "[data-role='track']",
    "#aliyunCaptcha-sliding-body",
    "#aliyunCaptcha-sliding-text-box",
    "#nc_1_n1z",
    "[id*='nc_'][id*='n1z']",
    ".nc_scale",
    ".slider-track",
    ".track",
]

# Puzzle imagery: the gap position lives in these, not in the track DOM.
BG_IMAGE_SELECTORS = [
    "[data-role='puzzle-bg']",
    "#aliyunCaptcha-img",
    "#aliyunCaptcha-img-box img",
    "img[class*='puzzle'][class*='bg']",
    "img.puzzle-bg",
    "img.yunhuni",
    ".nc_scale canvas",
    "img[src*='puzzle']",
    "canvas",
]
PIECE_IMAGE_SELECTORS = [
    "[data-role='puzzle-piece']",
    "#aliyunCaptcha-puzzle",
    "img[class*='piece']",
    "img.puzzle-piece",
    ".nc_puzzle img",
    "img[src*='piece']",
]

REFRESH_SELECTORS = [
    "[data-role='refresh']",
    "#aliyunCaptcha-btn-refresh",
    ".nc_refresh",
    ".yidun_refresh",
    "[class*='refresh']",
]

SUCCESS_SELECTORS = [
    "[data-state='solved']",
    ".nc-lang-cnt .btn_ok",
    ".nc_iconfont.btn_ok",
    ".slider-success",
    "#aliyunCaptcha-certifyId[value]",
    ".aliyunCaptcha-success",
    "[class*='success']",
]

FAIL_SELECTORS = [
    "[data-state='failed']",
    ".nc_iconfont.btn_close",
    ".errloading",
    ".aliyunCaptcha-fail",
    "[class*='fail']",
]

# Init script: capture whatever token the widget hands the host page.
# Listens only — does not touch canvas, WebGL, or navigator fingerprints.
_TOKEN_HOOK_JS = """
(() => {
  window.__atriaChallenge = window.__atriaChallenge || { token: null, events: [] };
  window.addEventListener('message', (e) => {
    try {
      const d = typeof e.data === 'object' && e.data ? e.data : {};
      const tok = d.token || (d.nc && d.nc.token) || (d.data && d.data.token);
      if (tok) window.__atriaChallenge.token = tok;
      window.__atriaChallenge.events.push(d);
    } catch (_) {}
  });
  const wrap = (name) => {
    const orig = window[name];
    window[name] = function (...args) {
      try {
        const d = args && args[0];
        const tok = d && (d.token || (d.nc && d.nc.token));
        if (tok) window.__atriaChallenge.token = tok;
      } catch (_) {}
      if (typeof orig === 'function') return orig.apply(this, args);
    };
  };
  ['nc_callback', 'onChallengeSolved', '__nc_cb', 'challengeCallback'].forEach(wrap);
})();
"""

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_KEYLIKE_RE = re.compile(r"\b(ak|key|sk|tk)[-_][A-Za-z0-9_\-]{8,}\b")
_INPUT_VALUE_RE = re.compile(r'(<input[^>]*\svalue=")[^"]*(")', re.IGNORECASE)


def redact_snapshot(html: str) -> str:
    """Strip emails, key-like material and input values from a DOM
    snapshot before it hits the artifact store."""
    html = _EMAIL_RE.sub("***@***", html)
    html = _KEYLIKE_RE.sub(r"\1_********", html)
    html = _INPUT_VALUE_RE.sub(r"\1***\2", html)
    return html


@dataclass
class ChallengeResult:
    token: str
    attempts: int
    first_pass: bool
    meta: dict[str, Any] = field(default_factory=dict)


class ChallengeDriver(Protocol):
    name: str

    def solve(self, page: Page) -> ChallengeResult: ...


def install_token_hook(context) -> None:
    context.add_init_script(_TOKEN_HOOK_JS)


# ---------------------------------------------------------------------------
# Trajectory synthesis
# ---------------------------------------------------------------------------

def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _ease_in_out(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def generate_slide_path(
    distance: float, rng: random.Random
) -> list[tuple[float, float, float]]:
    """[(x, y, dt_ms)] along a slide of `distance` px — ease-out bezier,
    2-6% overshoot + correction, sinusoidal vertical jitter, jittered
    sample times with occasional micro-pauses."""
    overshoot = distance * rng.uniform(0.02, 0.06)
    n = max(26, min(80, int(distance / 7)))
    split = int(n * rng.uniform(0.78, 0.86))
    amps = [rng.uniform(0.4, 1.8) for _ in range(3)]
    freqs = [rng.uniform(1.0, 3.5) for _ in range(3)]
    phases = [rng.uniform(0, math.tau) for _ in range(3)]

    pts: list[tuple[float, float, float]] = []
    for i in range(n):
        t = i / (n - 1)
        if i <= split:
            x = (distance + overshoot) * _ease_out_cubic(i / split)
        else:
            u = (i - split) / (n - 1 - split)
            x = (distance + overshoot) - overshoot * _ease_in_out(u)
        y = sum(a * math.sin(math.tau * f * t + p) for a, f, p in zip(amps, freqs, phases))
        y += rng.uniform(-0.4, 0.4)
        dt = max(6.0, min(45.0, rng.gauss(14, 5)))
        pts.append((x, y, dt))
        if rng.random() < 0.03:
            pts.append((x, y, rng.uniform(60, 140)))
    pts.append((distance, rng.uniform(-1.0, 1.0), rng.uniform(8, 20)))
    return pts


def generate_approach_path(
    from_xy: tuple[float, float], to_xy: tuple[float, float], rng: random.Random
) -> list[tuple[float, float, float]]:
    x0, y0 = from_xy
    x1, y1 = to_xy
    dist = math.hypot(x1 - x0, y1 - y0)
    n = max(4, min(12, int(dist / 30)))
    pts = []
    for i in range(1, n + 1):
        t = _ease_in_out(i / n)
        pts.append(
            (
                x0 + (x1 - x0) * t + rng.uniform(-1.5, 1.5),
                y0 + (y1 - y0) * t + rng.uniform(-1.5, 1.5),
                max(6.0, rng.gauss(14, 5)),
            )
        )
    return pts


# ---------------------------------------------------------------------------
# Alibaba Cloud slider driver
# ---------------------------------------------------------------------------

class AlibabaCloudChallengeDriver:
    name = "alibaba"

    def __init__(
        self,
        artifacts_dir: str | Path,
        max_attempts: int = 3,
        post_solve_wait_ms: int = 2500,
        seed: int | None = None,
        captured_token_fn=None,
        opener_deadline_s: float = 10.0,
    ):
        self.artifacts_dir = Path(artifacts_dir)
        self.max_attempts = max_attempts
        self.post_solve_wait_ms = post_solve_wait_ms
        self.opener_deadline_s = opener_deadline_s
        self.rng = random.Random(seed)
        # Returns the freshest token the driver's network tap captured.
        self.captured_token_fn = captured_token_fn or (lambda: None)
        self.last_confidence: float | None = None
        # Last puzzle capture, for artifact dumps on rejection.
        self._last_puzzle: dict | None = None

    # -- detection --------------------------------------------------------

    def _find_widget_frame(self, page: Page) -> Frame:
        for sel in WIDGET_SIGNATURES:
            el = page.query_selector(sel)
            if el is None:
                continue
            try:
                is_iframe = el.evaluate("e => e.tagName === 'IFRAME'")
            except Exception:
                is_iframe = False
            if is_iframe:
                frame = el.content_frame()
                if frame is not None:
                    return frame
            else:
                return page.main_frame
        artifact = self._snapshot(page, "no_known_widget_signature")
        raise UnsupportedChallengeVariant(
            "no known challenge-widget signature found",
            artifact_path=str(artifact),
        )

    def _snapshot(self, page: Page, reason: str) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = self.artifacts_dir / f"challenge-{reason}-{ts}.html"
        try:
            content = page.content()
        except Exception:
            content = "<unavailable: page.content() failed>"
        path.write_text(
            f"<!-- url={page.url} reason={reason} -->\n" + redact_snapshot(content),
            encoding="utf-8",
        )
        log.error("unsupported challenge variant; redacted snapshot at %s", path)
        return path

    def _pick(self, frame: Frame, selectors: list[str], what: str):
        for sel in selectors:
            el = frame.query_selector(sel)
            if el is not None:
                return el
        raise DeadSelectorError(f"{what}: no selector matched {selectors}")

    def _pick_opt(self, frame: Frame, selectors: list[str]):
        for sel in selectors:
            el = frame.query_selector(sel)
            if el is not None:
                return el
        return None

    # -- distance resolution (AK-201) --------------------------------------

    def _image_bytes(self, page: Page, el) -> bytes | None:
        """Element pixels: canvas via toDataURL, raw img src (direct fetch
        through the page's request context so cookies apply), element
        screenshot as last resort."""
        try:
            data_url = el.evaluate(
                "(e) => e.tagName === 'CANVAS' ? e.toDataURL('image/png') : null"
            )
            if data_url and data_url.startswith("data:image"):
                return base64.b64decode(data_url.split(",", 1)[1])
        except Exception:
            pass
        try:
            # Raw src first — an element screenshot of the background img
            # would capture the piece element composited on top of it.
            # e.src resolves relative URLs against the element's own frame.
            src = el.evaluate("(e) => e.src || null") or el.get_attribute("src")
            if src:
                if src.startswith("data:image"):
                    return base64.b64decode(src.split(",", 1)[1])
                resp = page.context.request.get(src)
                if resp.ok:
                    return resp.body()
        except Exception:
            pass
        try:
            return el.screenshot()
        except Exception:
            pass
        return None

    def resolve_distance(self, page: Page, frame: Frame, track) -> tuple[float, float, str]:
        """Fallback chain — widget attr, puzzle-image CV, refuse.
        Returns (distance_px, confidence, method); never guesses."""
        gap = track.get_attribute("data-gap")
        if gap:
            return float(gap), 1.0, "attr"

        bg_el = self._pick_opt(frame, BG_IMAGE_SELECTORS)
        if bg_el is not None:
            bg_bytes = self._image_bytes(page, bg_el)
            if bg_bytes and image_is_blank(bg_bytes):
                # Element captured before the CDN image painted — one
                # re-capture beats an attempt burned on a phantom gap.
                page.wait_for_timeout(450)
                bg_bytes = self._image_bytes(page, bg_el)
            piece_el = self._pick_opt(frame, PIECE_IMAGE_SELECTORS)
            piece_bytes = self._image_bytes(page, piece_el) if piece_el else None
            if bg_bytes:
                det = detect_gap_x(bg_bytes, piece_bytes)
                confidence = det.confidence
                # A template hit above its own accept bar is a real match —
                # CCOEFF on edge maps saturates well below 1.0.
                if det.method in ("template", "strip") and confidence >= CONFIDENCE_TEMPLATE_ACCEPT:
                    confidence = 0.7 + confidence * 0.3
                # det.gap_x is in source-image pixels (raw src natural size,
                # or screenshot px at device_scale_factor>1); the drag needs
                # CSS px. Rescale through the rendered background box.
                ibox = bg_el.bounding_box() or track.bounding_box()
                img_w = image_width(bg_bytes)
                css_scale = ibox["width"] / img_w if (ibox and img_w) else 1.0
                gap_css = det.gap_x * css_scale
                # Piece element's current x → distance = gap - piece_x.
                # Origin is the background image box (on v2 widgets the
                # slider bar sits below the puzzle and offsets differ).
                distance = gap_css
                if piece_el is not None:
                    pbox = piece_el.bounding_box()
                    if pbox and ibox:
                        distance = gap_css - (pbox["x"] - ibox["x"])
                self._last_puzzle = {
                    "bg": bg_bytes, "piece": piece_bytes or b"",
                    "gap_x": det.gap_x, "distance": distance,
                    "gap_css": gap_css, "bg_box": ibox,
                }
                # Closed-loop servo target: the piece offset (relative to
                # the bg box) the widget must reach. Elements are
                # re-queried inside the drag — the widget may swap the
                # piece node once the pointer goes down.
                self._servo_gap_off = gap_css if piece_el is not None else None
                return distance, confidence, f"cv:{det.method}"
        return 0.0, 0.0, "unresolved"

    # -- widget lifecycle ----------------------------------------------------

    @staticmethod
    def _rendered(el) -> bool:
        try:
            return el is not None and el.bounding_box() is not None
        except Exception:
            return False

    @staticmethod
    def _first_box(frame: Frame, selectors: list[str]) -> dict | None:
        """Fresh bounding box of the first rendered match — re-queries the
        DOM every call so mid-drag node swaps don't go stale."""
        for sel in selectors:
            try:
                el = frame.query_selector(sel)
                if el is None:
                    continue
                box = el.bounding_box()
            except Exception:
                continue
            if box:
                return box
        return None

    def _open_widget(self, page: Page, frame: Frame) -> None:
        """AliyunCaptcha v2 mounts a collapsed box; the puzzle panel only
        exists after the opener is clicked. The widget session init can be
        slow/throttled — retry the click instead of failing on one shot."""
        for attempt in range(3):
            frame = self._find_widget_frame(page) or frame
            # The panel may have mounted during the previous attempt's
            # settle wait — re-clicking the opener would toggle it shut.
            if self._rendered(self._pick_opt(frame, HANDLE_SELECTORS)):
                return
            # Slow link/proxy: the widget JS takes seconds to mount its
            # collapsed opener — wait for it instead of clicking blind.
            opener = None
            wait_end = time.monotonic() + self.opener_deadline_s
            while time.monotonic() < wait_end and opener is None:
                for sel in OPENER_SELECTORS:
                    opener = frame.query_selector(sel)
                    if opener is not None:
                        break
                if opener is None:
                    page.wait_for_timeout(200)
            clicked = False
            if opener is not None:
                try:
                    opener.click()
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                page.wait_for_timeout(1500)
                continue
            deadline = time.monotonic() + self.opener_deadline_s
            while time.monotonic() < deadline:
                if self._rendered(self._pick_opt(frame, HANDLE_SELECTORS)):
                    return
                page.wait_for_timeout(150)
            page.wait_for_timeout(1500)
        raise DeadSelectorError(
            "captcha opener clicked but no slider handle appeared"
        )

    def _panel_open(self, frame: Frame) -> bool | None:
        """v2 verdict source: panel visible => puzzle still unsolved; gone =>
        the widget closed (success) or was never open. None => not a v2
        widget, use the selector verdicts instead."""
        el = frame.query_selector(PANEL_SELECTOR)
        if el is None:
            return None
        try:
            return el.is_visible()
        except Exception:
            return None

    def _dump_puzzle(self, reason: str) -> None:
        """Persist the last captured puzzle images for offline analysis.
        The bg gets an annotated copy with the detected gap x drawn in —
        comparing the line against the visible hole shows instantly
        whether a rejection was bad detection or server-side scoring."""
        if not self._last_puzzle or not self._last_puzzle.get("bg"):
            return
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        for name in ("bg", "piece"):
            data = self._last_puzzle.get(name)
            if data:
                (self.artifacts_dir / f"challenge-{reason}-{name}-{ts}.png").write_bytes(data)
        gap_x = self._last_puzzle.get("gap_x")
        if gap_x is not None:
            try:
                img = cv2.imdecode(
                    np.frombuffer(self._last_puzzle["bg"], np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if img is not None:
                    x = int(round(gap_x))
                    cv2.line(img, (x, 0), (x, img.shape[0]), (0, 0, 255), 3)
                    ok, enc = cv2.imencode(".png", img)
                    if ok:
                        (
                            self.artifacts_dir
                            / f"challenge-{reason}-annotated-{ts}.png"
                        ).write_bytes(enc.tobytes())
            except Exception:
                pass

    def _dump_panel(self, page: Page, reason: str) -> None:
        """Screenshot the widget panel post-verdict — shows where the
        piece actually landed vs the visible cutout. Also writes an
        annotated copy with the detected gap x drawn on top, so one image
        carries the hole, the landed piece, and what the detector aimed
        at."""
        try:
            frame = self._find_widget_frame(page)
            panel = frame.query_selector(PANEL_SELECTOR) if frame else None
            if panel is None:
                return
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            shot = panel.screenshot()
            (self.artifacts_dir / f"challenge-panel-{reason}-{ts}.png").write_bytes(shot)
            lp = self._last_puzzle or {}
            gap_css = lp.get("gap_css")
            bg_box = lp.get("bg_box")
            panel_box = panel.bounding_box()
            if gap_css is None or not bg_box or not panel_box:
                return
            img = cv2.imdecode(np.frombuffer(shot, np.uint8), cv2.IMREAD_COLOR)
            if img is None or not panel_box["width"]:
                return
            scale = img.shape[1] / panel_box["width"]
            x = int(round((bg_box["x"] - panel_box["x"] + gap_css) * scale))
            cv2.line(img, (x, 0), (x, img.shape[0]), (0, 0, 255), 3)
            ok, enc = cv2.imencode(".png", img)
            if ok:
                (
                    self.artifacts_dir
                    / f"challenge-panel-{reason}-annotated-{ts}.png"
                ).write_bytes(enc.tobytes())
        except Exception:
            pass

    def _reload_widget(self, page: Page) -> Frame:
        """A rejected attempt must reload the widget — never re-drag on a
        dead puzzle."""
        frame0 = None
        try:
            frame0 = self._find_widget_frame(page)
            refresh = self._pick_opt(frame0, REFRESH_SELECTORS)
        except UnsupportedChallengeVariant:
            refresh = None
        if refresh is not None:
            try:
                refresh.click()
                page.wait_for_timeout(1200)
                return self._find_widget_frame(page)
            except Exception:
                pass
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        return self._find_widget_frame(page)

    def _drag(self, page: Page, frame: Frame, handle, track) -> tuple[float, str]:
        hbox = handle.bounding_box()
        if not hbox:
            raise DeadSelectorError("handle has no bounding box")
        hx = hbox["x"] + hbox["width"] / 2
        hy = hbox["y"] + hbox["height"] / 2
        distance, confidence, method = self.resolve_distance(page, frame, track)
        self.last_confidence = confidence
        if confidence < CONFIDENCE_ACCEPT or distance <= 0:
            raise ChallengeRejected(
                f"gap unresolved/low-confidence ({confidence:.2f}, {method})"
            )
        log.info(
            "slide distance %.0fpx (confidence %.2f, %s)", distance, confidence, method
        )

        # Reading pause — a human studies the puzzle for a moment before
        # grabbing the handle. Solve-latency is a scored signal.
        page.wait_for_timeout(self.rng.uniform(700, 1900))
        approach = generate_approach_path((0, 0), (hx, hy), self.rng)
        slide = generate_slide_path(distance, self.rng)
        for x, y, dt in approach:
            page.mouse.move(x, y)
            page.wait_for_timeout(dt)
        page.mouse.move(hx, hy)
        page.wait_for_timeout(self.rng.uniform(80, 220))
        page.mouse.down()
        page.wait_for_timeout(self.rng.uniform(120, 300))
        # Open-loop phase: bezier path up to ~75% of the distance — cheap
        # and looks human. Then the servo closes on the measured piece
        # position (the widget's elastic mapping makes blind drags land
        # consistently short).
        cursor = 0.0
        rough = distance * 0.75
        for dx, dy, dt in slide:
            if dx > rough:
                break
            cursor = dx
            page.mouse.move(hx + dx, hy + dy)
            page.wait_for_timeout(dt)
        servo_done = False
        gap_off = getattr(self, "_servo_gap_off", None)
        servo_err = None
        piece_moves = False
        if gap_off is not None:
            start_off = None
            for i in range(70):
                # Fresh lookups every step — the piece node can be swapped
                # or remounted once the drag starts.
                pbox = self._first_box(frame, PIECE_IMAGE_SELECTORS)
                ibox = self._first_box(frame, BG_IMAGE_SELECTORS)
                if not pbox or not ibox:
                    break
                off = pbox["x"] - ibox["x"]
                if start_off is None:
                    start_off = off
                piece_moves = piece_moves or abs(off - start_off) > 0.5
                servo_err = gap_off - off
                if abs(servo_err) <= 1.5:
                    servo_done = True
                    break
                if i >= 3 and not piece_moves:
                    # Non-elastic widget (e.g. fixture): the piece doesn't
                    # track the pointer — finish open-loop to `distance`.
                    break
                step = servo_err * 0.35
                mag = max(2.0, min(16.0, abs(step)))
                cursor += math.copysign(mag, step)
                page.mouse.move(
                    hx + cursor,
                    hy + self.rng.uniform(-1.5, 1.5),
                )
                page.wait_for_timeout(self.rng.uniform(12, 30))
            # Settle — the piece eases into place; confirm before release.
            page.wait_for_timeout(self.rng.uniform(100, 200))
            log.info(
                "servo: %s err=%s moves=%s",
                "converged" if servo_done else "incomplete",
                f"{servo_err:.1f}px" if servo_err is not None else "n/a",
                piece_moves,
            )
        if not servo_done:
            # No servo target, unreadable elements, or a piece that never
            # responded — finish the drag open-loop to the full distance.
            for dx, dy, dt in slide:
                if dx <= cursor + 1:
                    continue
                page.mouse.move(hx + dx, hy + dy)
                page.wait_for_timeout(dt)
        page.wait_for_timeout(self.rng.uniform(80, 250))
        page.mouse.up()
        return distance, method

    def _read_token(self, page: Page, frame: Frame, timeout_ms: int) -> str | None:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            tok = page.evaluate(
                "() => window.__atriaChallenge && window.__atriaChallenge.token"
            )
            if tok:
                return tok
            try:
                tok = frame.evaluate(
                    "() => window.__challengeToken || document.body.dataset.token || null"
                )
                if tok:
                    return tok
            except Exception:
                pass
            tok = self.captured_token_fn()
            if tok:
                return tok
            page.wait_for_timeout(120)
        return None

    def _attempt_failed(self, frame: Frame) -> bool:
        for sel in FAIL_SELECTORS:
            if frame.query_selector(sel):
                return True
        # v2: a still-open panel after the post-solve wait means the drag
        # did not pass — the widget reloads the puzzle for another try.
        if self._panel_open(frame) is True:
            return True
        return False

    _SERVER_REJECT_HINTS = (
        "captcha_verification_failed",
        "captcha verification failed",
        "验证失败",
    )

    def _server_rejected(self, page: Page) -> bool:
        """The widget panel can close while the site's own captcha/verify
        call fails — Logto renders `error.captcha_verification_failed`.
        Panel-close alone is NOT a pass."""
        try:
            txt = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        return any(h in txt for h in self._SERVER_REJECT_HINTS)

    def _attempt_succeeded(self, frame: Frame) -> bool:
        for sel in SUCCESS_SELECTORS:
            if frame.query_selector(sel):
                return True
        # v2: the panel closing means the verify POST was accepted.
        if self._panel_open(frame) is False:
            return True
        return False

    def solve(self, page: Page) -> ChallengeResult:
        frame = self._find_widget_frame(page)
        url_before = page.url
        for attempt in range(1, self.max_attempts + 1):
            # AliyunCaptcha v2 pre-mounts a hidden slider — 'present but not
            # rendered' means the panel was never opened.
            if not self._rendered(self._pick_opt(frame, HANDLE_SELECTORS)):
                self._open_widget(page, frame)
                frame = self._find_widget_frame(page)
            handle = self._pick(frame, HANDLE_SELECTORS, "slider handle")
            track = self._pick(frame, TRACK_SELECTORS, "slider track")
            log.info("challenge attempt %d/%d", attempt, self.max_attempts)
            try:
                distance, method = self._drag(page, frame, handle, track)
            except ChallengeRejected as exc:
                # Low-confidence distance → fresh puzzle, not a fresh guess.
                frame = self._reload_widget(page)
                if attempt == self.max_attempts:
                    raise
                continue
            page.wait_for_timeout(self.post_solve_wait_ms)
            if self._server_rejected(page):
                # Logto folds the widget back to its opener — the next loop
                # iteration re-opens a fresh puzzle. No in-place reload:
                # the previous panel instance is dead.
                log.warning("challenge attempt %d rejected by server", attempt)
                self._dump_puzzle("server_rejected")
                self._dump_panel(page, "server_rejected")
                continue
            if self._attempt_succeeded(frame):
                token = self._read_token(page, frame, timeout_ms=2000)
                return ChallengeResult(
                    token=token or "",
                    attempts=attempt,
                    first_pass=attempt == 1,
                    meta={
                        "driver": self.name,
                        "distance_px": distance,
                        "distance_method": method,
                        "confidence": self.last_confidence,
                    },
                )
            if self._attempt_failed(frame):
                log.warning("challenge attempt %d rejected — reloading widget", attempt)
                if attempt < self.max_attempts:
                    frame = self._reload_widget(page)
                continue
            token = self._read_token(page, frame, timeout_ms=1500)
            if token:
                return ChallengeResult(
                    token=token,
                    attempts=attempt,
                    first_pass=attempt == 1,
                    meta={"driver": self.name, "verdict": "token",
                          "distance_px": distance, "distance_method": method,
                          "confidence": self.last_confidence},
                )
            # Solved-by-navigation: some NC variants auto-advance without a
            # readable token.
            if page.url != url_before:
                return ChallengeResult(
                    token="",
                    attempts=attempt,
                    first_pass=attempt == 1,
                    meta={"driver": self.name, "verdict": "navigation",
                          "distance_px": distance, "distance_method": method,
                          "confidence": self.last_confidence},
                )
            log.warning("challenge attempt %d produced no verdict", attempt)
        raise ChallengeRejected(f"widget rejected {self.max_attempts} attempts")


CHALLENGE_DRIVERS = {"alibaba": AlibabaCloudChallengeDriver}
