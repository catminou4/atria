"""atria-keys CLI.

  python -m atria_keys.cli run --count 3 [--fixture] [--config config/atria.yaml]
  python -m atria_keys.cli export --format env
  python -m atria_keys.cli dashboard
  python -m atria_keys.cli status
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
import tempfile
import threading
from pathlib import Path

from .config import Config
from .errors import PipelineError
from .keystore import KeyStore, mask_key
from .mailbox import build_reader
from .pacing import Pacer
from .pipeline import Pipeline
from .state import StateStore

log = logging.getLogger("atria_keys")


def _load_fixture_server(base_dir: Path):
    """Start the local fixture site (tests/fixtures/server.py) without
    touching the network beyond localhost."""
    path = base_dir / "tests" / "fixtures" / "server.py"
    if not path.exists():
        raise SystemExit(f"fixture server not found at {path}")
    spec = importlib.util.spec_from_file_location("atria_fixture_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = mod.FixtureServer(host="127.0.0.1", port=0)
    server.start()
    return server


def _fixture_overrides(cfg: Config, server) -> Config:
    raw = dict(cfg._raw)
    raw["target"] = {
        "base_url": server.base_url,
        "register_path": "/register",
        "selectors": {
            "email_input": ["input[type=email]", "input[name=email]", "#email"],
            "submit": ["button[type=submit]", "#register-submit"],
            "api_key_holder": ["[data-api-key]", ".issued-key"],
        },
    }
    raw["mailbox"] = {
        "reader": "fixture",
        "catchall_domain": "atria-fixture.test",
        "address_template": "svc-{run_id}@{catchall_domain}",
        "fixture": {"outbox_dir": str(server.outbox_dir)},
        "poll_interval_s": 0.1,
        "timeout_s": 15,
    }
    # Tighten pacing so fixture runs are back-to-back.
    raw["pacing"] = {
        "min_interval_s": 0.2,
        "max_interval_s": 0.4,
        "jitter_s": 0.1,
        "max_runs_per_day": 10000,
        "challenge_backoff_s": 0.05,
        "challenge_backoff_multipliers": [1, 2],
        "max_consecutive_challenge_failures": 4,
    }
    raw.setdefault("challenge", {})["post_solve_wait_ms"] = 300
    raw["mailbox"]["timeout_s"] = 15
    return Config.from_dict(raw, cfg.base_dir)


def _build_pipeline(cfg: Config) -> Pipeline:
    state = StateStore(cfg.path("state.db_path", "keys/state.db"))
    keystore = KeyStore(cfg.path("keystore.path", "keys/keys.jsonl"))
    mailbox = build_reader(cfg)
    pacer = Pacer(
        state,
        min_interval_s=float(cfg.get("pacing.min_interval_s", 600)),
        max_interval_s=float(cfg.get("pacing.max_interval_s", 1200)),
        jitter_s=float(cfg.get("pacing.jitter_s", 60)),
        max_runs_per_day=int(cfg.get("pacing.max_runs_per_day", 30)),
        challenge_backoff_s=float(cfg.get("pacing.challenge_backoff_s", 45)),
        challenge_backoff_multipliers=list(
            cfg.get("pacing.challenge_backoff_multipliers", [1, 2, 4, 8])
        ),
        max_consecutive_challenge_failures=int(
            cfg.get("pacing.max_consecutive_challenge_failures", 4)
        ),
    )
    return Pipeline(cfg, state, keystore, mailbox, pacer)


def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    if getattr(args, "headed", False):
        cfg._raw.setdefault("browser", {})["headless"] = False
    server = None
    if args.fixture:
        server = _load_fixture_server(cfg.base_dir)
        cfg = _fixture_overrides(cfg, server)
        log.info("fixture target: %s (outbox %s)", server.base_url, server.outbox_dir)
    pipeline = _build_pipeline(cfg)

    httpd = None
    if args.dashboard:
        from .dashboard import serve

        httpd = serve(
            pipeline.state,
            pipeline.keystore,
            cfg.get("dashboard.host", "127.0.0.1"),
            int(cfg.get("dashboard.port", 8686)),
            int(cfg.get("dashboard.refresh_s", 4)),
        )
        log.info(
            "dashboard: http://%s:%d", cfg.get("dashboard.host"), cfg.get("dashboard.port")
        )

    run_ids = pipeline.orchestrate(args.count)
    for rid in run_ids:
        run = pipeline.state.get_run(rid)
        log.info("run %s -> %s", rid, run["status"] if run else "?")
    if server:
        server.stop()
    if httpd:
        httpd.shutdown()
    return 0


def cmd_export(args) -> int:
    cfg = Config.load(args.config)
    ks = KeyStore(cfg.path("keystore.path", "keys/keys.jsonl"))
    if args.format == "env":
        n = ks.export_env(cfg.path("keystore.export_env", "keys/keys.env"))
    else:
        n = ks.export_txt(cfg.path("keystore.export_txt", "keys/keys.txt"))
    print(f"exported {n} keys ({args.format})")
    return 0


def cmd_dashboard(args) -> int:
    cfg = Config.load(args.config)
    from .dashboard import serve

    state = StateStore(cfg.path("state.db_path", "keys/state.db"))
    ks = KeyStore(cfg.path("keystore.path", "keys/keys.jsonl"))
    host = cfg.get("dashboard.host", "127.0.0.1")
    port = int(cfg.get("dashboard.port", 8686))
    serve(state, ks, host, port, int(cfg.get("dashboard.refresh_s", 4)))
    print(f"dashboard on http://{host}:{port} — Ctrl-C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args) -> int:
    cfg = Config.load(args.config)
    state = StateStore(cfg.path("state.db_path", "keys/state.db"))
    ks = KeyStore(cfg.path("keystore.path", "keys/keys.jsonl"))
    runs = state.list_runs()
    print(f"runs: {len(runs)}  keys: {len(ks.records())}")
    rate = state.challenge_first_pass_rate()
    print(f"challenge first-pass rate: {'n/a' if rate is None else f'{rate*100:.0f}%'}")
    for r in runs[:20]:
        print(f"  {r['run_id']}  {r['status']:<8} {r.get('email') or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atria-keys")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--config", default="config/atria.yaml")
    p.add_argument("--fixture", action="store_true",
                   help="dry-run against the bundled local fixture site")
    p.add_argument("--dashboard", action="store_true",
                   help="also serve the ops dashboard during the run")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p = sub.add_parser("export")
    p.add_argument("--format", choices=["txt", "env"], default="txt")
    p.add_argument("--config", default="config/atria.yaml")
    p = sub.add_parser("dashboard")
    p.add_argument("--config", default="config/atria.yaml")
    p = sub.add_parser("status")
    p.add_argument("--config", default="config/atria.yaml")
    args = ap.parse_args(argv)
    return {"run": cmd_run, "export": cmd_export,
            "dashboard": cmd_dashboard, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
