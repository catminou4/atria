"""Stage runner: mailbox -> register -> challenge -> key_extract -> persist.

Each stage checkpoints into SQLite. A crashed run resumes at its last
incomplete stage and never re-registers an already-created account.
Transient failures retry with jitter; fatal failures log and move on.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from typing import Any, Callable

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
        self._challenge = CHALLENGE_DRIVERS[cfg.get("challenge.driver", "alibaba")](
            artifacts_dir=self.artifacts_dir,
            max_attempts=int(cfg.get("challenge.max_attempts_per_run", 3)),
            post_solve_wait_ms=int(cfg.get("challenge.post_solve_wait_ms", 2500)),
        )

    # -- orchestration -----------------------------------------------------

    def orchestrate(self, count: int) -> list[str]:
        run_ids = []
        for _ in range(count):
            run_id = uuid.uuid4().hex[:12]
            try:
                self.pacer.wait_for_slot(run_id)
            except PacingHalt as exc:
                self.state.event(run_id, None, "pacing_halt", str(exc))
                log.error("pacing halt: %s", exc)
                break
            self.state.new_run(run_id)
            run_ids.append(run_id)
            self.run(run_id)
        return run_ids

    # -- single run ---------------------------------------------------------

    def run(self, run_id: str) -> dict[str, Any]:
        ctx: dict[str, Any] = {"run_id": run_id}
        driver = None
        try:
            driver = self.driver_factory(self.cfg, run_id)
            driver.__enter__()
            for stage in STAGES:
                done = self.state.stage_payload(run_id, stage)
                if done is not None:
                    ctx[stage] = done
                    continue
                self.state.set_current_stage(run_id, stage)
                ctx[stage] = self._with_retry(run_id, stage, ctx, driver)
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

    # -- stages --------------------------------------------------------------

    def _stage_mailbox(self, ctx, driver) -> dict:
        email = self.mailbox.allocate_address(ctx["run_id"])
        self.state.set_email(ctx["run_id"], email)
        self.state.event(ctx["run_id"], "mailbox", "address_allocated", email)
        return {"email": email}

    def _stage_register(self, ctx, driver) -> dict:
        email = ctx["mailbox"]["email"]
        driver.fill_registration(email)
        self.state.event(ctx["run_id"], "register", "form_submitted", email)
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
        return {"verify_url": msg.link, "email": email}

    def _stage_challenge(self, ctx, driver) -> dict:
        # A resumed run reloads the persisted session and lands back on the
        # challenge surface instead of re-registering.
        verify_url = ctx.get("register", {}).get("verify_url")
        if verify_url and "/verify" not in (driver.page.url or ""):
            driver.goto(verify_url)
        try:
            result: ChallengeResult = self._challenge.solve(driver.page)
        except ChallengeRejected:
            wait = self.pacer.on_challenge_failure()
            self.state.event(
                ctx["run_id"], "challenge", "challenge_rejected",
                json.dumps({"cooldown": wait}),
            )
            raise
        self.pacer.on_challenge_success()
        self.state.event(
            ctx["run_id"], "challenge", "challenge_solved",
            json.dumps(
                {
                    "run_id": ctx["run_id"],
                    "attempt": result.attempts,
                    "solved": True,
                    "first_pass": result.first_pass,
                }
            ),
        )
        return {"token": result.token, "attempts": result.attempts}

    def _stage_key_extract(self, ctx, driver) -> dict:
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
