"""Native in-process driver for the embedded risk-control widget.

ChallengeDriver protocol + AlibabaCloudChallengeDriver implementation for
the slider variant: locate the widget iframe, resolve the slide distance,
drag the handle over a human-plausible pointer trajectory (bezier easing,
non-constant velocity, overshoot + correction), then submit via the
widget's own JS contract — the token the widget itself emits to the host
page. No external solver calls; all computation on-device. Canvas /
fingerprint noise is left intact: we do not patch fingerprints.

Unknown widget layouts are never blind-retried: they emit
UnsupportedChallengeVariant with a DOM snapshot artifact.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from playwright.sync_api import Frame, Page
from playwright.sync_api import TimeoutError as PWTimeout

from .errors import (
    ChallengeExhausted,
    ChallengeRejected,
    DeadSelectorError,
    UnsupportedChallengeVariant,
)

log = logging.getLogger("atria_keys.challenge")

# Signatures seen on Alibaba Cloud risk-control sliders (nc / aliyun
# containers, captcha/slide iframes). The fixture widget carries the same
# signature class so tests exercise the real detection path.
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
]

HANDLE_SELECTORS = [
    "[data-role='handle']",
    ".nc_iconfont.btn_slide",
    "#nc_1_n1t",
    "[id*='nc_'][id*='n1t']",
    ".btn_slide",
    ".slider-btn",
    ".slider-handle",
]

TRACK_SELECTORS = [
    "[data-role='track']",
    "#nc_1_n1z",
    "[id*='nc_'][id*='n1z']",
    ".nc_scale",
    ".slider-track",
    ".track",
]

SUCCESS_SELECTORS = [
    "[data-state='solved']",
    ".nc-lang-cnt .btn_ok",
    ".nc_iconfont.btn_ok",
    ".slider-success",
    "[class*='success']",
]

FAIL_SELECTORS = [
    "[data-state='failed']",
    ".nc_iconfont.btn_close",
    ".errloading",
    "[class*='fail']",
]

# Init script: capture whatever token the widget hands to the host page,
# across the contract shapes in the wild (postMessage payloads, global
# callbacks, window variables).
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
    """Return [(x, y, dt_ms)] samples along a slide of `distance` px.

    Position follows an ease-out bezier (fast start, decelerating arrival),
    overshoots the target by 2-6% and corrects back, and carries sinusoidal
    vertical jitter so the trace is never a straight line. Sampling times
    are jittered with occasional micro-pauses — velocity is never constant.
    """
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
            pts.append((x, y, rng.uniform(60, 140)))  # micro-pause, same spot
    # Land exactly on target with release-level y.
    pts.append((distance, rng.uniform(-1.0, 1.0), rng.uniform(8, 20)))
    return pts


def generate_approach_path(
    from_xy: tuple[float, float],
    to_xy: tuple[float, float],
    rng: random.Random,
) -> list[tuple[float, float, float]]:
    """Short hover path from current pointer position to the handle."""
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
    ):
        self.artifacts_dir = Path(artifacts_dir)
        self.max_attempts = max_attempts
        self.post_solve_wait_ms = post_solve_wait_ms
        self.rng = random.Random(seed)

    # -- detection ------------------------------------------------------

    def _find_widget_frame(self, page: Page) -> Frame:
        for sel in WIDGET_SIGNATURES:
            el = page.query_selector(sel)
            if el is None:
                continue
            frame = el.content_frame() if el.evaluate("e => e.tagName") == "IFRAME" else None
            if frame is not None:
                return frame
            # Signature matched a non-iframe container: the widget lives on
            # the host page itself.
            return page.main_frame
        self._snapshot(page, "no_known_widget_signature")
        raise UnsupportedChallengeVariant(
            "no known challenge-widget signature found",
            artifact_path=str(self._last_artifact),
        )

    _last_artifact: Path | None = None

    def _snapshot(self, page: Page, reason: str) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = self.artifacts_dir / f"challenge-{reason}-{ts}.html"
        try:
            content = page.content()
        except Exception:
            content = "<unavailable: page.content() failed>"
        path.write_text(
            f"<!-- url={page.url} reason={reason} -->\n{content}", encoding="utf-8"
        )
        self._last_artifact = path
        log.error("unsupported challenge variant; DOM snapshot at %s", path)
        return path

    def _pick(self, frame: Frame, selectors: list[str], what: str):
        for sel in selectors:
            el = frame.query_selector(sel)
            if el is not None:
                return el
        raise DeadSelectorError(f"{what}: no selector matched {selectors}")

    # -- solving ----------------------------------------------------------

    def _resolve_distance(self, frame: Frame, track) -> float:
        box = track.bounding_box()
        if box is None:
            raise DeadSelectorError("track has no bounding box")
        # Widgets that expose the gap programmatically (our fixture mirrors
        # this); otherwise the slide target is the usable track width minus
        # the handle's own width.
        gap = track.get_attribute("data-gap")
        if gap:
            return float(gap)
        return box["width"] * 0.98

    def _drag(self, page: Page, frame: Frame, handle, track) -> None:
        hbox = handle.bounding_box()
        tbox = track.bounding_box()
        if not hbox or not tbox:
            raise DeadSelectorError("handle/track bounding box unavailable")
        hx = hbox["x"] + hbox["width"] / 2
        hy = hbox["y"] + hbox["height"] / 2
        distance = self._resolve_distance(frame, track)

        for x, y, dt in generate_approach_path((0, 0), (hx, hy), self.rng):
            page.mouse.move(x, y)
            page.wait_for_timeout(dt)
        page.mouse.move(hx, hy)
        page.wait_for_timeout(self.rng.uniform(80, 220))
        page.mouse.down()
        page.wait_for_timeout(self.rng.uniform(120, 300))

        for dx, dy, dt in generate_slide_path(distance, self.rng):
            page.mouse.move(hx + dx, hy + dy)
            page.wait_for_timeout(dt)

        page.wait_for_timeout(self.rng.uniform(80, 250))
        page.mouse.up()

    def _read_token(self, page: Page, frame: Frame, timeout_ms: int) -> str | None:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            tok = page.evaluate("() => window.__atriaChallenge && window.__atriaChallenge.token")
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
            page.wait_for_timeout(120)
        return None

    def _attempt_failed(self, frame: Frame) -> bool:
        for sel in FAIL_SELECTORS:
            if frame.query_selector(sel):
                return True
        return False

    def _attempt_succeeded(self, frame: Frame) -> bool:
        for sel in SUCCESS_SELECTORS:
            if frame.query_selector(sel):
                return True
        return False

    def solve(self, page: Page) -> ChallengeResult:
        frame = self._find_widget_frame(page)
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            handle = self._pick(frame, HANDLE_SELECTORS, "slider handle")
            track = self._pick(frame, TRACK_SELECTORS, "slider track")
            log.info("challenge attempt %d/%d", attempt, self.max_attempts)
            self._drag(page, frame, handle, track)
            page.wait_for_timeout(self.post_solve_wait_ms)
            if self._attempt_succeeded(frame):
                token = self._read_token(page, frame, timeout_ms=2000)
                return ChallengeResult(
                    token=token or "",
                    attempts=attempt,
                    first_pass=attempt == 1,
                    meta={"driver": self.name},
                )
            if self._attempt_failed(frame):
                log.warning("challenge attempt %d rejected", attempt)
                continue
            # No verdict markers — last chance is the token itself.
            token = self._read_token(page, frame, timeout_ms=1500)
            if token:
                return ChallengeResult(
                    token=token,
                    attempts=attempt,
                    first_pass=attempt == 1,
                    meta={"driver": self.name, "verdict": "token"},
                )
            log.warning("challenge attempt %d produced no verdict", attempt)
        raise ChallengeRejected(f"widget rejected {self.max_attempts} attempts")


CHALLENGE_DRIVERS = {"alibaba": AlibabaCloudChallengeDriver}
