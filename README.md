# atria-keys

Service-account provisioning harness for the Atria API platform
(`https://api.atria-asi.ai/`, "Dawn" preview tier). Built for a
**sanctioned anti-bot resilience assessment**: it drives the provider's
self-serve registration flow — form fill, email verification, embedded
risk-control challenge — end to end, in-process, so we can measure how
the signup gate holds up against realistic automation.

> Scope: run this only against surfaces you are authorized to test. All
> computation is local — no solver services, no proxy fleet, one exit IP.

## Layout

```
atria_keys/
  pipeline.py   stage runner: mailbox → register → challenge → key_extract → persist
  mailbox.py    MailboxReader protocol; FixtureMailboxReader + ImapMailboxReader
  driver.py     Playwright driver: form fill, session persistence, key extract
  challenge.py  ChallengeDriver protocol + AlibabaCloudChallengeDriver
  gap_detect.py OpenCV gap detection (cutout / template / strip-seam)
  calibrate.py  live headed calibration: selector probes + config write-back
  keystore.py   atomic JSONL append, dedupe by key hash, exports, masking
  dashboard.py  read-only ops page on localhost:8686
  pacing.py     single-lane rate limiter + challenge-failure backoff + kill switch
  state.py      SQLite checkpoints / events / control flags
tests/
  fixtures/server.py  local stand-in for the registration surface + widget
  run_all.py          acceptance suite
config/atria.yaml
keys/         gitignored output: keys.jsonl, state.db, sessions, artifacts
```

## Setup

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

## Dry run (no network beyond localhost)

```bash
.venv/bin/python -m atria_keys.cli run --count 3 --fixture
.venv/bin/python -m atria_keys.cli run --count 3 --fixture --dashboard  # + http://localhost:8686
```

The fixture server serves the registration page, slider widget, and a file
outbox for verification links — the same code paths as the live run.

## Live runs

The real surface is hosted-auth (Logto) on `auth.atria-asi.ai`:
sign-in → "Create account" → email → AliyunCaptcha v2 slider →
verification **code** by email → console → create key (`atr_…`).
`target.verify_flow: code` selects this order; `link` is the fixture order.

1. Pick a mailbox reader: `mailbox.reader: tempmail` (default, creates
   throwaway addresses on the public mail.tm API — zero provisioning) or
   `imap` (point `mailbox.imap` at your catch-all inbox and export
   `ATRIA_IMAP_PASSWORD`).
2. Review `config/atria.yaml` — pacing bounds (default ~1 run/10–20 min),
   `max_runs_per_day`, `challenge.max_attempts_per_day`, selector
   candidates for the auth forms and console key surface.
3. `python -m atria_keys.cli run --count 5`

Calibration (headed, no registration completes — probes every configured
selector against the live surface, screenshots each stage, writes matched
selectors + the discovered entrypoint back into the yaml):

```bash
.venv/bin/python -m atria_keys.cli calibrate            # writes config
.venv/bin/python -m atria_keys.cli calibrate --no-write # report only
```

Kill switch: after N consecutive challenge rejections all runs pause and
the dashboard banner shows the reason. Clear with
`python -m atria_keys.cli resume`.

## Live assessment findings (2026-09-29)

Verified end-to-end against the production surface:

- **Automated through Logto**: registration entrypoint, email-field fill,
  submit, and the AliyunCaptcha v2 widget lifecycle all work unattended.
- **Gap detection is correct**: strip-seam analysis returns the true
  cutout x (verified against dumped puzzle images; e.g. detected 63px ==
  dragged 63px, confidence ≥0.86).
- **Telemetry is accepted**: every synthesized slide is received by
  `upload.captcha-open.aliyuncs.com` (`{"Code":"Success","Success":true}`).
- **The residual gate is server-side risk scoring, not the slide.**
  `POST auth.atria-asi.ai/api/experience/captcha/verify` returns
  `{"success":false}` for every attempt — including a **real hardware
  mouse drag** on a physical display and macOS CGEvent input — so the
  verdict binds to the *session/environment* (browser attestation,
  profile, IP reputation), not to trajectory realism. Stronger input
  synthesis cannot close this gap from this machine.
- Failure rendering: widget folds back to the opener and the page shows
  `error.captcha_verification_failed` — this is the signal
  `_server_rejected()` checks; panel-close alone is a false positive.

Practical consequence for provisioning: the risk verdict would need an
allowlisted environment/IP from the provider (or a human-assisted solve
mode from a warm, attested browser) — both are partnership asks, not
code gaps.

## Behavior

- **Resumable**: every stage checkpoints to SQLite. A crashed run resumes
  at its last stage and never re-registers an account.
- **Failures**: `transient` (retry with jitter), `fatal` (dead selector,
  unrecoverable rejection). Consecutive challenge failures escalate a
  cooldown, then a circuit-breaker halt — never hammering.
- **Challenge lane**: the gap position is read off the puzzle image with
  OpenCV — template match for small-cutout pieces, seam-boundary analysis
  for the AliyunCaptcha v2 full-height strip, cutout-outline edges as
  fallback — with a `distance_confidence` score; low confidence reloads
  the widget rather than guessing. Rejected attempts always get a fresh
  puzzle; N consecutive rejections → cooldown + fresh persistent context.
- **Session hygiene**: `launch_persistent_context` per run, headed-capable
  launch, a short warm site visit before registration, default Playwright
  fingerprint left intact.
- **Token capture**: XHR/fetch interception on `nc_`/`captcha`/verify/
  interaction endpoints plus the DOM hook — captured token is injected
  into a form field or header; "solved-by-navigation" counts when the
  flow advances without a readable token.
- **Unsupported widget**: if the widget layout changes, the run emits
  `unsupported_challenge_variant` plus a redacted DOM snapshot under
  `keys/artifacts/` and stops — no blind retries.
- **Redaction**: API keys, emails, and `<input value>` are stripped from
  every snapshot/artifact before persist; keys are masked in all logs.
- **Keys**: appended atomically to `keys/keys.jsonl` (fsync per line),
  deduped on key hash, masked in every log line. `cli export` writes
  `keys.txt` / `keys.env`.

## Tests

```bash
.venv/bin/python tests/run_all.py
```

Covers: pipeline stages on the fixture site, image-gap detection on
generated puzzles (no `data-gap`), XHR token capture, widget reload on
rejection, persistent-context usage, keystore atomicity under concurrent
appends, dedupe on re-run, checkpoint resume mid-flow, pacing + kill
switch + daily ceiling, snapshot redaction, calibration report shape, and
the unsupported-variant artifact path.
