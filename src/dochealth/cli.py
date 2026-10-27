"""Command-line entry point: dochealth <command> [--dry-run] [--config config.yaml]."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .atlassian.confluence import Confluence
from .atlassian.http import AtlassianError, AtlassianHTTP
from .config import Config, ConfigError, check_no_api_key, load_config
from .db import Database
from .llm.claude_cli import ClaudeCLI, LLMError
from .phases.inventory import run_inventory
from .phases.triage import run_triage
from .publisher import Publisher
from .report.data import build_report_data
from .report.markdown import render_markdown
from .report.storage import render_storage

log = logging.getLogger("dochealth")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_RATE_LIMIT = 0, 1, 2, 75

COMMANDS = {
    "inventory": "Fetch pages of the configured spaces into the local database",
    "triage": "Classify stale/unread pages (relevant, obsolete, duplicate, historical_record)",
    "duplicates": "Cluster similar pages and find contradictions (Phase 2, not yet built)",
    "tune-threshold": "Print sample page pairs at several similarity thresholds (Phase 2)",
    "changes": "Propose edits from resolved Jira issues (Phase 3, not yet built)",
    "report": "Build the report page (+ local Markdown copy)",
    "run-all": "inventory + triage + report",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dochealth", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name, help_text in COMMANDS.items():
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--config", default="config.yaml", help="path to config.yaml")
        sp.add_argument("--dry-run", action="store_true",
                        help="write report/comments to local files instead of Confluence")
        sp.add_argument("--space", action="append", metavar="KEY",
                        help="override the configured spaces (repeatable)")
        sp.add_argument("--max-llm-calls", type=int, help="override llm.max_calls_per_run")
        sp.add_argument("-v", "--verbose", action="store_true")
    return p


def setup_logging(output_dir: Path, verbose: bool) -> None:
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    file_handler = logging.FileHandler(log_dir / f"dochealth-{datetime.now():%Y%m%d}.log",
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@contextmanager
def run_lock(output_dir: Path):
    """Prevent two scheduled runs from overlapping."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError("Another dochealth run is in progress (output/.lock)") from None
        yield


def make_confluence(cfg: Config) -> Confluence | None:
    if cfg.credentials is None:
        return None
    c = cfg.credentials
    return Confluence(AtlassianHTTP(c.site_url, c.email, c.api_token,
                                    max_retries=cfg["atlassian"]["max_retries"]))


def do_report(cfg: Config, db: Database, confluence: Confluence | None, dry_run: bool,
              run_id: int, stats: dict) -> str:
    data = build_report_data(cfg, db, run_id, stats)
    reports_dir = cfg.path("output_dir") / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{data.report_date}-{datetime.now():%H%M%S}.md"
    md_path.write_text(render_markdown(data), encoding="utf-8")
    log.info("Markdown report saved to %s", md_path)

    rcfg = cfg["report"]
    if not rcfg["space"]:
        raise ConfigError("config: report.space is required")
    publisher = Publisher(confluence, dry_run, cfg.path("output_dir"))
    location = publisher.publish_report(
        rcfg["space"], str(rcfg["parent_id"]) if rcfg["parent_id"] else None,
        rcfg["parent_title"], data.title,
        render_storage(data, rcfg["max_rows_per_category"]))
    if not dry_run:
        db.set_meta("last_report_run_id", str(run_id))
    stats["report"] = {"location": location, "markdown": str(md_path)}
    return location


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        check_no_api_key()
        needs_atlassian = not (args.command == "report" and args.dry_run)
        cfg = load_config(args.config, require_credentials=needs_atlassian)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.space:
        cfg.data["spaces"] = args.space
    if args.max_llm_calls is not None:
        cfg.data["llm"]["max_calls_per_run"] = args.max_llm_calls
    setup_logging(cfg.path("output_dir"), args.verbose)

    if args.command in ("duplicates", "tune-threshold", "changes"):
        log.error("'%s' is not implemented yet (later phase)", args.command)
        return EXIT_ERROR

    try:
        with run_lock(cfg.path("output_dir")):
            return _run(args, cfg)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG


def _run(args, cfg: Config) -> int:
    db = Database(cfg.path("database"))
    run_id = db.start_run(args.command, args.dry_run)
    stats: dict = {"command": args.command, "dry_run": args.dry_run, "errors": []}
    llm = ClaudeCLI(db, cfg["llm"]["claude_bin"], cfg["llm"]["max_calls_per_run"],
                    cfg["llm"]["timeout_seconds"])
    confluence = make_confluence(cfg)
    status, code = "ok", EXIT_OK
    log.info("Run %d: %s%s, spaces=%s", run_id, args.command,
             " (dry-run)" if args.dry_run else "", cfg.spaces)
    try:
        if args.command in ("inventory", "run-all"):
            stats["inventory"] = run_inventory(cfg, db, confluence, run_id)
        if args.command in ("triage", "run-all"):
            stats["triage"] = run_triage(cfg, db, confluence, llm, run_id)
            if stats["triage"]["stopped"]:
                status = "partial"
                if stats["triage"]["stopped"] == "rate_limit":
                    code = EXIT_RATE_LIMIT
        stats["llm"] = llm.stats.as_dict()
        if args.command in ("report", "run-all"):
            location = do_report(cfg, db, confluence, args.dry_run, run_id, stats)
            print(f"Report: {location}")
    except (AtlassianError, LLMError, ConfigError) as exc:
        log.error("%s", exc)
        stats["errors"].append(str(exc))
        status, code = "failed", EXIT_ERROR
    except Exception as exc:  # keep run history consistent, then re-raise
        stats["errors"].append(repr(exc))
        db.finish_run(run_id, "failed", stats)
        raise
    finally:
        stats["llm"] = llm.stats.as_dict()

    db.finish_run(run_id, status, stats)
    s = llm.stats
    log.info("Run %d finished: %s | LLM calls=%d cache_hits=%d failures=%d",
             run_id, status, s.calls, s.cache_hits, s.failures)
    db.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
