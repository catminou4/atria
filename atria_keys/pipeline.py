"""Stage runner: mailbox -> register -> challenge -> key_extract -> persist.

Each stage checkpoints into SQLite. A crashed run resumes at its last
incomplete stage and never re-registers an already-created account.
Transient failures retry with jitter; fatal failures log and move on.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import random
import time
import uuid
from typing import Any, Callable

from . import overrides
from .challenge import CHALLENGE_DRIVERS, ChallengeResult
from .driver import BrowserDriver
from .errors import (
    ChallengeRejected,
    FatalError,
    PacingHalt,
    PipelineError,
    TransientError,
)
from .state import STAGES, StateStore

log = logging.getLogger("atria_keys.pipeline")


class Pipeline:
    def __init__(
        self,
        cfg,
        state: StateStore,
        keystore,
        mailbox,
        pacer,
        driver_factory: Callable[[Any, str], BrowserDriver] = BrowserDriver,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.state = state
        self.keystore = keystore
        self.mailbox = mailbox
        self.pacer = pacer
        self.driver_factory = driver_factory
        self.sleep = sleep
        self.stage_retries = int(cfg.get("run.stage_retries", 2))
        self.retry_jitter_s = float(cfg.get("run.retry_jitter_s", 5))
        self.artifacts_dir = cfg.path("artifacts.dir", "keys/artifacts")
        self._driver = None
        self._challenge = CHALLENGE_DRIVERS[cfg.get("challenge.driver", "alibaba")](
            artifacts_dir=self.artifacts_dir,
            max_attempts=int(cfg.get("challenge.max_attempts_per_run", 3)),
            post_solve_wait_ms=int(cfg.get("challenge.post_solve_wait_ms", 2500)),
            opener_deadline_s=float(cfg.get("challenge.opener_deadline_s", 25)),
            manual=overrides.challenge_mode(cfg) == "manual",
            manual_timeout_s=float(cfg.get("challenge.manual_timeout_s", 240)),
            captured_token_fn=lambda: (
                self._driver.captured_token() if self._driver else None
            ),
        )

    # -- orchestration -----------------------------------------------------

    def orchestrate(self, count: int) -> list[str]:
        workers = overrides.run_workers(self.cfg)
        if workers > 1:
            return self._orchestrate_parallel(count, workers)
        run_ids = []
        for _ in range(count):
            run_id = self._start_run(run_ids)
            if run_id is None:
                break
            self.run(run_id)
        return run_ids

    def _start_run(self, run_ids: list[str], honor_gap: bool = True) -> str | None:
        """Claim a pacing slot and register the run. Returns None when
        orchestration must stop (kill switch or pacing halt)."""
        paused = self.state.paused()
        if paused:
            log.error("kill switch engaged (%s) — halting orchestration", paused)
            self.state.event(None, None, "orchestration_paused", paused)
            return None
        # Dashboard override applies to the next run — the pacer's own
        # cap check reads this value.
        self.pacer.max_per_day = overrides.max_runs_per_day(self.cfg)
        run_id = uuid.uuid4().hex[:12]
        try:
            if honor_gap:
                self.pacer.wait_for_slot(run_id)
            elif self.state.runs_today() >= self.pacer.max_per_day:
                raise PacingHalt(
                    f"daily cap reached ({self.pacer.max_per_day} runs/day)")
        except PacingHalt as exc:
            self.state.event(run_id, None, "pacing_halt", str(exc))
            self.state.set_control("paused", str(exc))
            log.error("pacing halt: %s", exc)
            return None
        self.state.new_run(run_id)
        run_ids.append(run_id)
        return run_id

    def _orchestrate_parallel(self, count: int, workers: int) -> list[str]:
        """Manual-batch mode: N runs at once, each in its own thread,
        browser, and pipeline. Slots are claimed serially first (pacing
        still applies), then runs execute concurrently — each pauses at
        the captcha for the human."""
        run_ids: list[str] = []
        pending: list[str] = []
        for i in range(count):
            # First run of the batch honors the inter-run pacing gap;
            # the rest launch together — the human solver is the rate
            # limit, not a wall-clock interval.
            run_id = self._start_run(run_ids, honor_gap=(i == 0))
            if run_id is None:
                break
            pending.append(run_id)
        if not pending:
            return run_ids
        log.info("orchestrating %d run(s) across %d worker(s)",
                 len(pending), workers)

        def _work(run_id: str):
            sub = Pipeline(
                self.cfg, self.state, self.keystore, self.mailbox,
                self.pacer, driver_factory=self.driver_factory,
                sleep=self.sleep,
            )
            return sub.run(run_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in concurrent.futures.as_completed(
                [ex.submit(_work, rid) for rid in pending]
            ):
                try:
                    fut.result()
                except Exception as exc:
                    # A worker escaping run()'s own error handling must not
                    # kill the whole batch.
                    log.error("parallel worker crashed: %s", exc)
        return run_ids

    # -- single run ---------------------------------------------------------

    def run(self, run_id: str) -> dict[str, Any]:
        ctx: dict[str, Any] = {"run_id": run_id}
        driver = None
        run_t0 = time.monotonic()
        try:
            driver = self.driver_factory(self.cfg, run_id)
            driver.__enter__()
            self._driver = driver
            for stage in STAGES:
                done = self.state.stage_payload(run_id, stage)
                if done is not None:
                    ctx[stage] = done
                    continue
                self.state.set_current_stage(run_id, stage)
                t0 = time.monotonic()
                ctx[stage] = self._with_retry(run_id, stage, ctx, driver)
                duration = time.monotonic() - t0
                ctx[stage]["duration_s"] = round(duration, 2)
                self.state.event(
                    run_id, stage, "stage_done",
                    json.dumps({"duration_s": ctx[stage]["duration_s"]}),
                )
                self.state.complete_stage(run_id, stage, ctx[stage])
            self.state.finish_run(
                run_id,
                "done",
                api_key_hash=ctx["persist"].get("key_hash"),
            )
            self.state.event(run_id, None, "run_done", "")
            return ctx
        except PipelineError as exc:
            cls = exc.classification
            self.state.event(run_id, self.state.get_run(run_id)["current_stage"],
                             "run_failed", f"{cls}:{exc}")
            self.state.finish_run(run_id, "failed", cls, str(exc))
            log.error("run %s failed (%s): %s", run_id, cls, exc)
            return {"run_id": run_id, "failed": str(exc), "class": cls}
        finally:
            if driver is not None:
                try:
                    self.state.set_run_stats(
                        run_id,
                        duration_s=round(time.monotonic() - run_t0, 1),
                        bytes_est=getattr(driver, "bytes_used", None),
                        proxy_mode=getattr(driver, "proxy_mode", None),
                        mailbox_kind=getattr(self.mailbox, "last_kind", None)
                        or type(self.mailbox).__name__,
                    )
                except Exception:
                    pass
                driver.__exit__(None, None, None)

    def _with_retry(self, run_id, stage, ctx, driver):
        retries = self.stage_retries
        for attempt in range(retries + 1):
            try:
                return getattr(self, f"_stage_{stage}")(ctx, driver)
            except TransientError as exc:
                self.state.event(run_id, stage, "transient", str(exc))
                if attempt >= retries:
                    raise
                wait = self.retry_jitter_s * (attempt + 1)
                self.sleep(wait * random.uniform(0.5, 1.5))
                if isinstance(exc, ChallengeRejected) and attempt >= 1:
                    # Repeated rejections burn the session — continue the
                    # retry on a fresh persistent context.
                    driver.reset_context()
                    verify_url = ctx.get("register", {}).get("verify_url")
                    if verify_url:
                        driver.goto(verify_url)

    # -- stages --------------------------------------------------------------

    def _stage_mailbox(self, ctx, driver) -> dict:
        email = self.mailbox.allocate_address(ctx["run_id"])
        log.info("mailbox: run %s -> %s", ctx["run_id"], email)
        self.state.set_email(ctx["run_id"], email)
        self.state.event(ctx["run_id"], "mailbox", "address_allocated", email)
        return {"email": email}

    def _stage_register(self, ctx, driver) -> dict:
        email = ctx["mailbox"]["email"]
        # Warm the session before registration — a cold context that lands
        # straight on the form is a risk-engine tell.
        driver.warm_visit()
        driver.fill_registration(email)
        self.state.event(ctx["run_id"], "register", "form_submitted", email)
        verify_flow = self.cfg.get("target.verify_flow", "link")
        if verify_flow == "code":
            # Logto order: submit -> captcha -> verification code by email.
            # The mailbox wait runs in key_extract, after the challenge.
            return {"verify_flow": "code", "email": email}
        msg = self.mailbox.wait_for_message(
            email,
            timeout_s=float(self.cfg.get("mailbox.timeout_s", 180)),
            poll_s=float(self.cfg.get("mailbox.poll_interval_s", 5)),
        )
        if not msg.link:
            raise TransientError("verification message carried no link")
        driver.goto(msg.link)
        driver.save_session()
        self.state.event(ctx["run_id"], "register", "verified", msg.link)
        return {"verify_url": msg.link, "verify_flow": "link",
                "email": email}

    def _stage_challenge(self, ctx, driver) -> dict:
        # A resumed run reloads the persisted session and lands back on the
        # challenge surface instead of re-registering.
        verify_url = ctx.get("register", {}).get("verify_url")
        if verify_url and "/verify" not in (driver.page.url or ""):
            driver.goto(verify_url)
        max_day = int(self.cfg.get("challenge.max_attempts_per_day", 0) or 0)
        if max_day and self.state.challenge_attempts_today() >= max_day:
            self.state.set_control("paused", "challenge attempts/day ceiling")
            raise PacingHalt("challenge attempts/day ceiling reached")
        try:
            result: ChallengeResult = self._challenge.solve(driver.page)
        except ChallengeRejected as exc:
            self.state.event(
                ctx["run_id"], "challenge", "challenge_attempt",
                json.dumps({"run_id": ctx["run_id"], "solved": False,
                            "confidence": self._challenge.last_confidence}),
            )
            try:
                wait = self.pacer.on_challenge_failure()
            except PacingHalt as halt:
                self.state.set_control("paused", str(halt))
                raise
            self.state.event(
                ctx["run_id"], "challenge", "challenge_rejected",
                json.dumps({"cooldown": wait}),
            )
            raise exc
        self.pacer.on_challenge_success()
        driver.save_session()
        if result.token:
            try:
                info = driver.inject_token(result.token)
                self.state.event(
                    ctx["run_id"], "challenge", "token_injected",
                    json.dumps(info),
                )
            except Exception as exc:
                log.debug("token injection skipped: %s", exc)
        self.state.event(
            ctx["run_id"], "challenge", "challenge_solved",
            json.dumps(
                {
                    "run_id": ctx["run_id"],
                    "attempt": result.attempts,
                    "solved": True,
                    "first_pass": result.first_pass,
                    "confidence": result.meta.get("confidence"),
                    "distance_method": result.meta.get("distance_method"),
                }
            ),
        )
        self.state.event(
            ctx["run_id"], "challenge", "challenge_attempt",
            json.dumps({"run_id": ctx["run_id"], "solved": True,
                        "attempt": result.attempts,
                        "confidence": result.meta.get("confidence")}),
        )
        return {"token": result.token, "attempts": result.attempts,
                "confidence": result.meta.get("confidence")}

    def _stage_key_extract(self, ctx, driver) -> dict:
        if ctx.get("register", {}).get("verify_flow") == "code":
            # The code only goes out after the captcha was accepted, so the
            # mailbox wait happens here — post-challenge, on the same page.
            email = ctx["register"]["email"]
            msg = self.mailbox.wait_for_message(
                email,
                timeout_s=float(self.cfg.get("mailbox.timeout_s", 180)),
                poll_s=float(self.cfg.get("mailbox.poll_interval_s", 5)),
            )
            if not msg.otp:
                raise TransientError("verification message carried no code")
            driver.enter_verification_code(msg.otp)
            self.state.event(ctx["run_id"], "key_extract", "verified", "code")
        api_key, key_id = driver.extract_api_key()
        self.state.event(ctx["run_id"], "key_extract", "key_seen", key_id)
        return {"api_key": api_key, "key_id": key_id}

    def _stage_persist(self, ctx, driver) -> dict:
        from .keystore import key_hash, mask_key

        extracted = ctx["key_extract"]
        inserted, record = self.keystore.append(
            key_id=extracted["key_id"],
            api_key=extracted["api_key"],
            email=ctx["mailbox"]["email"],
            run_id=ctx["run_id"],
            meta={"challenge_attempts": ctx.get("challenge", {}).get("attempts")},
        )
        self.state.event(
            ctx["run_id"], "persist",
            "key_stored" if inserted else "key_duplicate",
            mask_key(record["api_key"]),
        )
        return {"key_id": record["key_id"], "key_hash": record["key_hash"], "inserted": inserted}
