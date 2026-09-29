"""Session warm-up: age a shared browser profile with human-plausible
visits so the captcha risk engine doesn't see a cold session at slide
time. No registration, no captcha interaction — pure dwell/navigation.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

from .config import Config
from .driver import BrowserDriver

log = logging.getLogger("atria_keys.warmup")

# Pages worth dwelling on; the captcha session benefits from real
# browsing history + aged cookies on the same eTLD+1.
WARM_URLS = [
    "https://api.atria-asi.ai/",
    "https://api.atria-asi.ai/sign-in",
    "https://auth.atria-asi.ai/",
]


def _dwell(page, seconds: float, rng: random.Random) -> None:
    """Scroll + pointer moves over `seconds` — cheap human-plausibility."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        action = rng.choice(("scroll", "move", "idle"))
        if action == "scroll":
            page.mouse.wheel(0, rng.randint(120, 480))
        elif action == "move":
            page.mouse.move(rng.randint(80, 1200), rng.randint(80, 720))
        page.wait_for_timeout(rng.randint(600, 2400))


def warm(cfg: Config, run_id: str = "warm") -> dict:
    """One warm visit on the shared warmup profile. Returns a summary
    dict with pages visited and dwell seconds (logged to state)."""
    rng = random.Random()
    headless = bool(cfg.get("browser.headless", False))
    visited = []
    started = datetime.now(timezone.utc).isoformat()
    with BrowserDriver(cfg, run_id=run_id) as drv:
        for url in WARM_URLS:
            try:
                drv.goto(url)
            except Exception as exc:
                log.warning("warm visit %s failed: %s", url, exc)
                continue
            dwell = rng.uniform(20, 50)
            _dwell(drv.page, dwell, rng)
            visited.append({"url": url, "dwell_s": round(dwell, 1)})
    summary = {"ts": started, "pages": visited, "headless": headless}
    log.info("warm visit done: %d pages, %.0fs total dwell",
             len(visited), sum(v["dwell_s"] for v in visited))
    return summary
