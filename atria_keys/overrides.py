"""Runtime overrides flipped from the dashboard without editing the yaml.

Persisted as JSON at `overrides.path` (default keys/overrides.json):
{"mailbox_reader": "imap"|"tempmaillol", "proxy": "proxy"|"direct"}
Reads are per-run so a switch in the dashboard applies to the next run.
"""

from __future__ import annotations

import json
from pathlib import Path

MAILBOX_KINDS = ("imap", "tempmaillol")
PROXY_MODES = ("direct", "proxy")
CHALLENGE_MODES = ("auto", "manual")


def _path(cfg) -> Path:
    return Path(cfg.path("overrides.path", "keys/overrides.json"))


def read(cfg) -> dict:
    try:
        return json.loads(_path(cfg).read_text())
    except Exception:
        return {}


def write(cfg, **updates) -> dict:
    data = read(cfg)
    for k, v in updates.items():
        if v is not None:
            data[k] = v
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    return data


def mailbox_reader(cfg) -> str:
    base = cfg.get("mailbox.reader", "imap")
    if base not in MAILBOX_KINDS:
        return base  # fixture mode etc. — never overridden
    kind = read(cfg).get("mailbox_reader")
    return kind if kind in MAILBOX_KINDS else base


def proxy_enabled(cfg) -> bool:
    mode = read(cfg).get("proxy")
    if mode in PROXY_MODES:
        return mode == "proxy"
    return bool(cfg.get("browser.proxy.enabled", False))


def challenge_mode(cfg) -> str:
    mode = read(cfg).get("challenge_mode")
    if mode in CHALLENGE_MODES:
        return mode
    base = cfg.get("challenge.mode", "auto")
    return base if base in CHALLENGE_MODES else "auto"


def run_workers(cfg) -> int:
    raw = read(cfg).get("run_workers")
    try:
        n = int(raw) if raw is not None else int(cfg.get("run.workers", 1))
    except (TypeError, ValueError):
        return 1
    return max(1, min(8, n))


def max_runs_per_day(cfg) -> int:
    raw = read(cfg).get("max_runs_day")
    try:
        n = int(raw) if raw is not None else int(
            cfg.get("pacing.max_runs_per_day", 30))
    except (TypeError, ValueError):
        return 30
    return max(1, min(500, n))
