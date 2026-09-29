"""Live calibration run — headed, no registration submit.

Navigates the real registration surface, probes every configured selector
candidate plus the widget signatures, screenshots each stage, emits a
selector report, and writes the corrected selectors + discovered
registration URL back into the config.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import yaml
from playwright.sync_api import TimeoutError as PWTimeout

from .challenge import (
    BG_IMAGE_SELECTORS,
    HANDLE_SELECTORS,
    OPENER_SELECTORS,
    PIECE_IMAGE_SELECTORS,
    TRACK_SELECTORS,
    WIDGET_SIGNATURES,
)
from .driver import BrowserDriver

log = logging.getLogger("atria_keys.calibrate")


def _probe(page, selectors: list[str], timeout_ms: int = 1800) -> list[dict]:
    out = []
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            out.append({"selector": sel, "matched": bool(el)})
        except PWTimeout:
            out.append({"selector": sel, "matched": False})
        except Exception as exc:
            out.append({"selector": sel, "matched": False, "error": str(exc)[:200]})
    return out


def calibrate(cfg, config_path: str | Path | None = None,
              headless: bool | None = None) -> dict:
    cfg._raw.setdefault("browser", {})["headless"] = (
        False if headless is None else headless
    )
    out_dir = cfg.path("artifacts.dir", "keys/artifacts") / "calibrate"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    report: dict = {
        "generated_at": ts,
        "target": cfg.get("target.base_url"),
        "stages": [],
    }
    driver = BrowserDriver(cfg, run_id=f"calibrate-{ts}")
    with driver:
        page = driver.page
        # -- entry (sign-in on the live site) -----------------------------
        entry = cfg.get("target.registration_url") or (
            cfg.get("target.base_url", "").rstrip("/")
            + cfg.get("target.register_path", "/register")
        )
        try:
            driver.goto(entry)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
        except Exception as exc:
            report["navigation_error"] = str(exc)
        shot = out_dir / f"00-entry-{ts}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            pass
        stage = {
            "stage": "entry",
            "url_requested": entry,
            "url_final": page.url,
            "screenshot": str(shot),
            "groups": {
                "create_account_link": _probe(
                    page,
                    cfg.get("target.selectors.create_account_link", []),
                    timeout_ms=1500,
                )
            },
        }
        report["stages"].append(stage)

        # -- create-account click -> registration form --------------------
        for p in stage["groups"]["create_account_link"]:
            if p.get("matched"):
                try:
                    page.click(p["selector"])
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
                break
        shot = out_dir / f"01-register-{ts}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            pass
        stage = {
            "stage": "register",
            "url_final": page.url,
            "screenshot": str(shot),
            "groups": {},
        }
        for group in ("email_input", "submit"):
            stage["groups"][group] = _probe(
                page, cfg.get(f"target.selectors.{group}", [])
            )
        report["stages"].append(stage)

        # -- probe-submit to surface the captcha (never completes a run: --
        #    the probe address is not a deliverable mailbox) --------------
        email_sel = next(
            (
                p["selector"]
                for p in stage["groups"]["email_input"]
                if p.get("matched")
            ),
            None,
        )
        submit_sel = next(
            (
                p["selector"]
                for p in stage["groups"]["submit"]
                if p.get("matched")
            ),
            None,
        )
        if email_sel and submit_sel:
            try:
                probe_email = (
                    f"calibrate-probe@{cfg.get('mailbox.catchall_domain', 'example.com')}"
                )
                page.fill(email_sel, probe_email)
                page.click(submit_sel)
                page.wait_for_timeout(3500)
            except Exception:
                pass
        widget_hits = _probe(page, WIDGET_SIGNATURES, timeout_ms=600)
        stage = {
            "stage": "challenge_widget",
            "present": any(w["matched"] for w in widget_hits),
            "signatures": widget_hits,
        }
        if stage["present"]:
            # Open the panel if it's collapsed (AliyunCaptcha v2).
            for p in _probe(page, OPENER_SELECTORS, timeout_ms=400):
                if p.get("matched"):
                    try:
                        page.click(p["selector"])
                        page.wait_for_timeout(3000)
                    except Exception:
                        pass
                    break
            stage["groups"] = {
                "handle": _probe(
                    driver.page.main_frame, HANDLE_SELECTORS, timeout_ms=500
                ),
                "track": _probe(
                    driver.page.main_frame, TRACK_SELECTORS, timeout_ms=500
                ),
                "bg_image": _probe(
                    driver.page.main_frame, BG_IMAGE_SELECTORS, timeout_ms=500
                ),
                "piece_image": _probe(
                    driver.page.main_frame, PIECE_IMAGE_SELECTORS, timeout_ms=500
                ),
            }
            shot = out_dir / f"02-widget-{ts}.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                stage["screenshot"] = str(shot)
            except Exception:
                pass
        report["stages"].append(stage)

        # -- key surface ---------------------------------------------------
        stage = {
            "stage": "key_extract",
            "groups": {
                "code_input": _probe(
                    page, cfg.get("target.selectors.code_input", []), timeout_ms=800
                ),
                "console_create_button": _probe(
                    page,
                    cfg.get("target.selectors.console_create_button", []),
                    timeout_ms=800,
                ),
                "api_key_holder": _probe(
                    page,
                    cfg.get("target.selectors.api_key_holder", []),
                    timeout_ms=800,
                )
            },
        }
        report["stages"].append(stage)

    report_path = out_dir / f"selector-report-{ts}.json"
    report_path.write_text(json.dumps(report, indent=2))
    report["report_path"] = str(report_path)

    # -- write corrected config back --------------------------------------
    if config_path:
        _write_back(cfg, Path(config_path), report, out_dir, ts)
    return report


def _write_back(cfg, config_path: Path, report: dict, out_dir: Path, ts: str) -> None:
    raw = dict(cfg._raw)
    sel_updates: dict[str, list[str]] = {}
    for stage in report["stages"]:
        for group, probes in stage.get("groups", {}).items():
            matched = [p["selector"] for p in probes if p.get("matched")]
            missed = [p["selector"] for p in probes if not p.get("matched")]
            if matched:
                # Winners first — order is the probe priority.
                sel_updates[group] = matched + missed
    if sel_updates:
        raw.setdefault("target", {}).setdefault("selectors", {}).update(sel_updates)
    for stage in report["stages"]:
        if stage["stage"] == "entry" and stage.get("url_final"):
            raw["target"]["registration_url"] = stage["url_final"]
    backup = config_path.with_suffix(config_path.suffix + f".bak-{ts}")
    backup.write_text(config_path.read_text())
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    log.info("config updated (backup at %s)", backup)
