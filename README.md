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
  keystore.py   atomic JSONL append, dedupe by key hash, exports, masking
  dashboard.py  read-only ops page on localhost:8686
  pacing.py     single-lane rate limiter + challenge-failure backoff
  state.py      SQLite checkpoints / events
  errors.py     transient / fatal taxonomy
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

1. Point `mailbox.imap` at your catch-all inbox and export
   `ATRIA_IMAP_PASSWORD`.
2. Review `config/atria.yaml` — pacing bounds (default ~1 run/10–20 min),
   `max_runs_per_day`, selector candidates for the registration form and
   key surface (first live run will likely need selector tuning; a dead
   selector is a `fatal` classification, not a retry).
3. `python -m atria_keys.cli run --count 5`

## Behavior

- **Resumable**: every stage checkpoints to SQLite. A crashed run resumes
  at its last stage and never re-registers an account.
- **Failures**: `transient` (retry with jitter), `fatal` (dead selector,
  unrecoverable rejection). Consecutive challenge failures escalate a
  cooldown, then a circuit-breaker halt — never hammering.
- **Unsupported widget**: if the widget layout changes, the run emits
  `unsupported_challenge_variant` plus a DOM snapshot under
  `keys/artifacts/` and stops — no blind retries.
- **Keys**: appended atomically to `keys/keys.jsonl` (fsync per line),
  deduped on key hash, masked in every log line. `cli export` writes
  `keys.txt` / `keys.env`.

## Tests

```bash
.venv/bin/python tests/run_all.py
```

Covers: pipeline stages on the fixture site, keystore atomicity under
concurrent appends, dedupe on re-run, checkpoint resume mid-flow, pacing
enforcement, and the unsupported-variant artifact path.
