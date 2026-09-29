"""Acceptance suite for atria-keys — all stages run against the local
fixture site (registration + Alibaba-style slider widget + file outbox).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import server as fixture_server  # noqa: E402

from atria_keys.config import Config  # noqa: E402
from atria_keys.errors import PacingHalt  # noqa: E402
from atria_keys.keystore import KeyStore, key_hash, mask_key  # noqa: E402
from atria_keys.mailbox import build_reader  # noqa: E402
from atria_keys.pacing import Pacer  # noqa: E402
from atria_keys.pipeline import Pipeline  # noqa: E402
from atria_keys.state import StateStore  # noqa: E402


def fixture_config(server, tmp: Path) -> Config:
    return Config.from_dict(
        {
            "target": {
                "base_url": server.base_url,
                "register_path": "/register",
                "selectors": {
                    "email_input": ["input[type=email]", "input[name=email]", "#email"],
                    "submit": ["button[type=submit]", "#register-submit"],
                    "api_key_holder": ["[data-api-key]", ".issued-key"],
                },
            },
            "mailbox": {
                "reader": "fixture",
                "catchall_domain": "atria-fixture.test",
                "address_template": "svc-{run_id}@{catchall_domain}",
                "fixture": {"outbox_dir": str(server.outbox_dir)},
                "poll_interval_s": 0.1,
                "timeout_s": 15,
            },
            "challenge": {"driver": "alibaba", "max_attempts_per_run": 4,
                          "post_solve_wait_ms": 350},
            "browser": {"headless": True, "slow_mo_ms": 0,
                        "session_dir": str(tmp / "sessions")},
            "pacing": {
                "min_interval_s": 0.1, "max_interval_s": 0.2, "jitter_s": 0.05,
                "max_runs_per_day": 10000, "challenge_backoff_s": 0.02,
                "challenge_backoff_multipliers": [1, 2],
                "max_consecutive_challenge_failures": 4,
            },
            "keystore": {"path": str(tmp / "keys.jsonl"),
                         "export_txt": str(tmp / "keys.txt"),
                         "export_env": str(tmp / "keys.env")},
            "state": {"db_path": str(tmp / "state.db")},
            "artifacts": {"dir": str(tmp / "artifacts")},
            "run": {"stage_retries": 1, "retry_jitter_s": 0.01},
        },
        tmp,
    )


def make_pipeline(cfg: Config) -> Pipeline:
    state = StateStore(cfg.path("state.db_path", "keys/state.db"))
    ks = KeyStore(cfg.path("keystore.path", "keys/keys.jsonl"))
    pacer = Pacer(
        state,
        min_interval_s=cfg.get("pacing.min_interval_s"),
        max_interval_s=cfg.get("pacing.max_interval_s"),
        jitter_s=cfg.get("pacing.jitter_s"),
        max_runs_per_day=cfg.get("pacing.max_runs_per_day"),
        challenge_backoff_s=cfg.get("pacing.challenge_backoff_s"),
        challenge_backoff_multipliers=cfg.get("pacing.challenge_backoff_multipliers"),
        max_consecutive_challenge_failures=cfg.get(
            "pacing.max_consecutive_challenge_failures"),
    )
    return Pipeline(cfg, state, ks, build_reader(cfg), pacer)


class HarnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fixture_server.FixtureServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.post("/__reset", {})
        self.tmp = Path(tempfile.mkdtemp(prefix="atria-test-"))
        self.cfg = fixture_config(self.server, self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ------------------------------------------------------------------

    def test_pipeline_stages_end_to_end(self):
        pipe = make_pipeline(self.cfg)
        run_id = uuid.uuid4().hex[:12]
        pipe.state.new_run(run_id)
        ctx = pipe.run(run_id)

        run = pipe.state.get_run(run_id)
        self.assertEqual(run["status"], "done")
        recs = pipe.keystore.records()
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["api_key"].startswith("ak_fix_"))
        self.assertEqual(recs[0]["email"], ctx["mailbox"]["email"])
        for stage in ("mailbox", "register", "challenge", "key_extract", "persist"):
            self.assertIsNotNone(pipe.state.stage_payload(run_id, stage))
        self.assertEqual(self.server.get_state()["counters"]["verify_challenge_ok"], 1)

    def test_dedupe_and_resume_skip_register(self):
        pipe = make_pipeline(self.cfg)
        run_id = uuid.uuid4().hex[:12]
        pipe.state.new_run(run_id)
        ctx = pipe.run(run_id)
        self.assertEqual(pipe.state.get_run(run_id)["status"], "done")
        key = ctx["key_extract"]["api_key"]

        # Re-running the same run_id must not re-register or re-append.
        pipe2 = make_pipeline(self.cfg)
        pipe2.run(run_id)
        self.assertEqual(self.server.get_state()["counters"]["register_calls"], 1)
        self.assertEqual(len(pipe2.keystore.records()), 1)

        # Direct append of the same key is deduped by hash.
        inserted, _ = pipe2.keystore.append(
            key_id="dup", api_key=key, email="x@y.z", run_id="r2")
        self.assertFalse(inserted)
        self.assertEqual(len(pipe2.keystore.records()), 1)

    def test_crash_resume_mid_flow(self):
        pipe = make_pipeline(self.cfg)
        run_id = uuid.uuid4().hex[:12]
        pipe.state.new_run(run_id)

        class CrashDriver:
            name = "crash"

            def solve(self, page):
                raise RuntimeError("simulated crash inside challenge stage")

        pipe._challenge = CrashDriver()
        with self.assertRaises(RuntimeError):
            pipe.run(run_id)

        # Fresh pipeline resumes the same run — register must not repeat.
        pipe2 = make_pipeline(self.cfg)
        ctx = pipe2.run(run_id)
        self.assertEqual(pipe2.state.get_run(run_id)["status"], "done")
        self.assertEqual(self.server.get_state()["counters"]["register_calls"], 1)
        self.assertEqual(len(pipe2.keystore.records()), 1)

    def test_unsupported_variant_emits_artifact_and_fatal(self):
        self.server.post("/__config", {"variant": True})
        pipe = make_pipeline(self.cfg)
        run_id = uuid.uuid4().hex[:12]
        pipe.state.new_run(run_id)
        pipe.run(run_id)
        run = pipe.state.get_run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_class"], "fatal")
        artifacts = list((self.tmp / "artifacts").glob("challenge-*.html"))
        self.assertTrue(artifacts, "expected a DOM snapshot artifact")
        self.assertIn("mystery", artifacts[0].read_text())

    def test_challenge_rejection_retries_then_succeeds(self):
        self.server.post("/__config", {"flaky_rejects": 1})
        pipe = make_pipeline(self.cfg)
        run_id = uuid.uuid4().hex[:12]
        pipe.state.new_run(run_id)
        pipe.run(run_id)
        run = pipe.state.get_run(run_id)
        self.assertEqual(run["status"], "done")
        counters = self.server.get_state()["counters"]
        self.assertEqual(counters["challenge_token_rej"], 1)
        self.assertEqual(counters["challenge_token_ok"], 1)

    # ------------------------------------------------------------------
    # Keystore atomicity / masking
    # ------------------------------------------------------------------

    def test_keystore_concurrent_append(self):
        ks = KeyStore(self.tmp / "keys.jsonl")
        threads = []
        for t in range(8):
            threads.append(threading.Thread(
                target=lambda n=t: [
                    ks.append(key_id=f"k{n}-{i}", api_key=f"ak_t{n}_{i}",
                              email="e@x", run_id="r")
                    for i in range(20)
                ]))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = ks.records()
        self.assertEqual(len(recs), 160)
        for r in recs:
            self.assertIn("key_hash", r)
        # dedupe under concurrency
        dup = [ks.append(key_id="k0-0", api_key="ak_t0_0", email="e@x", run_id="r")
               for _ in range(4)]
        self.assertTrue(all(not ok for ok, _ in dup))
        self.assertEqual(len(ks.records()), 160)

    def test_key_masking(self):
        self.assertEqual(mask_key("ak_fix_abcdef1234"), "ak_fix_…1234")
        self.assertNotIn("abcdef", mask_key("ak_fix_abcdef1234")[7:])

    # ------------------------------------------------------------------
    # Pacing
    # ------------------------------------------------------------------

    def test_pacing_enforces_interval_and_cap(self):
        sleeps: list[float] = []
        clock = {"t": time.time()}  # match StateStore's real clock

        def fake_now():
            return clock["t"]

        def fake_sleep(s):
            sleeps.append(s)
            clock["t"] += s

        state = StateStore(self.tmp / "state.db")
        pacer = Pacer(
            state, min_interval_s=10, max_interval_s=10, jitter_s=0,
            max_runs_per_day=2, challenge_backoff_s=1,
            challenge_backoff_multipliers=[1, 2, 4],
            max_consecutive_challenge_failures=3,
            sleep=fake_sleep, now=fake_now,
        )
        pacer.wait_for_slot("r1")            # first run: no wait
        state.new_run("r1")
        clock["t"] += 3                      # only 3s elapsed
        pacer.wait_for_slot("r2")
        self.assertGreaterEqual(sleeps[0], 6.0)   # ~10s gap minus elapsed
        self.assertLessEqual(sleeps[0], 7.5)
        state.new_run("r2")
        with self.assertRaises(PacingHalt):  # daily cap
            pacer.wait_for_slot("r3")

        # backoff escalates then trips the breaker
        w1 = pacer.on_challenge_failure()
        w2 = pacer.on_challenge_failure()
        self.assertGreater(w2, w1 * 0.5)
        with self.assertRaises(PacingHalt):
            pacer.on_challenge_failure()

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def test_dashboard_api_masks_keys(self):
        from atria_keys.dashboard import serve

        pipe = make_pipeline(self.cfg)
        run_id = uuid.uuid4().hex[:12]
        pipe.state.new_run(run_id)
        ctx = pipe.run(run_id)
        httpd = serve(pipe.state, pipe.keystore, "127.0.0.1", 0)
        try:
            port = httpd.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as r:
                data = json.loads(r.read())
            self.assertEqual(len(data["keys"]), 1)
            masked = data["keys"][0]["masked"]
            self.assertIn("…", masked)
            self.assertNotIn(ctx["key_extract"]["api_key"], masked)
            self.assertIn("challenge_first_pass_rate", data)
        finally:
            httpd.shutdown()

    # ------------------------------------------------------------------
    # CLI acceptance: `run --count 2 --fixture`
    # ------------------------------------------------------------------

    def test_cli_fixture_run(self):
        keys_dir = ROOT / "keys"
        before = 0
        keys_file = keys_dir / "keys.jsonl"
        if keys_file.exists():
            before = sum(1 for _ in keys_file.open())
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.run(
            [sys.executable, "-m", "atria_keys.cli", "run",
             "--count", "2", "--fixture"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        after = sum(1 for _ in keys_file.open())
        self.assertEqual(after - before, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
