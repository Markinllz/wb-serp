"""Collector command line: init, collect once, worker, doctor."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import signal
import sys
import time
from collections.abc import Callable

from wb_serp.client import FetchError, WildberriesClient
from wb_serp.config import Settings
from wb_serp.locking import exclusive_collector
from wb_serp.parsing import SchemaError, parse_product
from wb_serp.queries import read_queries
from wb_serp.service import Collector
from wb_serp.store import Store

log = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout,
    )


def build_client(cfg: Settings) -> WildberriesClient:
    return WildberriesClient(
        search_url=cfg.search_url, destination=cfg.destination,
        timeout=cfg.request_timeout_seconds, retries=cfg.request_retries,
        delay_seconds=cfg.request_delay_seconds,
        http_transport=cfg.http_transport, session_file=cfg.session_file,
        retry_base_seconds=cfg.retry_base_seconds, retry_max_seconds=cfg.retry_max_seconds,
        retry_jitter_seconds=cfg.retry_jitter_seconds,
        browser_profile_path=cfg.browser_profile_path,
        browser_timeout_seconds=cfg.browser_timeout_seconds,
        browser_engine=cfg.browser_engine,
    )


def prepare(cfg: Settings) -> Store:
    store = Store(cfg.database_path)
    with exclusive_collector(cfg.database_path):
        store.initialize()
        store.sync_queries(read_queries(cfg.queries_path))
        interrupted = store.interrupt_abandoned_runs()
        if interrupted:
            log.warning("Marked %s abandoned runs as interrupted", interrupted)
    return store


def run_cycle(cfg: Settings, store: Store, *, phrase: str | None = None,
              force: bool = False, scheduled_for: str | None = None,
              should_stop: Callable[[], bool] | None = None) -> tuple[int, int, int]:
    # Only one active HTTP collection cycle per shared VPS volume.
    with exclusive_collector(cfg.database_path):
        return _run_cycle_locked(cfg, store, phrase=phrase,
                                 force=force, scheduled_for=scheduled_for,
                                 should_stop=should_stop)


def _run_cycle_locked(cfg: Settings, store: Store, *, phrase: str | None,
                      force: bool, scheduled_for: str | None,
                      should_stop: Callable[[], bool] | None = None) -> tuple[int, int, int]:
    slot = scheduled_for or datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    queries = store.enabled_queries()
    if phrase is not None:
        queries = [q for q in queries if q["phrase"] == phrase]
        if not queries:
            raise ValueError(f"Query not configured: {phrase}")
    completed, skipped, failed = 0, 0, 0
    with build_client(cfg) as client:
        collector = Collector(store, client, destination=cfg.destination,
                              top_n=cfg.top_n, page_limit=cfg.page_limit)
        for query in queries:
            if should_stop is not None and should_stop():
                log.info("Stop requested: finish current query, defer remaining searches")
                break
            if not force and store.has_completed_slot(query["id"], slot, cfg.top_n):
                skipped += 1
                continue
            if completed and cfg.query_delay_seconds:
                time.sleep(cfg.query_delay_seconds)
                if should_stop is not None and should_stop():
                    break
            result = collector.collect(query["id"], query["phrase"], scheduled_for=slot)
            completed += 1
            if result.status != "success":
                failed += 1
            if result.status == "blocked":
                log.error("WB blocked requests. Ending this cycle instead of hammering the endpoint")
                break
    log.info("Cycle %s done: attempted=%s, skipped successful=%s, failed=%s",
             slot, completed, skipped, failed)
    return completed, skipped, failed

def run_worker(cfg: Settings, store: Store) -> None:
    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False
        log.info("Shutdown requested")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    period = cfg.collection_period_seconds
    last_slot: int | None = int(time.time()) // period if not cfg.run_on_startup else None
    if cfg.run_on_startup:
        last_slot = int(time.time()) // period
        run_cycle(cfg, store, scheduled_for=datetime.fromtimestamp(
            last_slot * period, tz=timezone.utc).isoformat(timespec="seconds"),
            should_stop=lambda: not running)
    while running:
        now_slot = int(time.time()) // period
        if now_slot > (last_slot if last_slot is not None else -1):
            last_slot = now_slot
            run_cycle(cfg, store, scheduled_for=datetime.fromtimestamp(
                now_slot * period, tz=timezone.utc).isoformat(timespec="seconds"),
                should_stop=lambda: not running)
        if running:
            time.sleep(min(5, max(0.2, (now_slot + 1) * period - time.time())))


def doctor(cfg: Settings) -> int:
    """Non-mutating endpoint smoke test. HTTP 200 alone is not evidence of working search."""
    phrase = read_queries(cfg.queries_path)[0]
    with build_client(cfg) as client:
        try:
            page = client.page(phrase, 1)
            if not page.products:
                raise SchemaError("Search response contains zero products")
            sample_product = parse_product(page.products[0], 1, datetime.now(timezone.utc))
        except (FetchError, SchemaError, ValueError) as exc:
            print(f"UNAVAILABLE: {exc}")
            return 2
    sample = page.products[0]
    print(json.dumps({"available": True, "query": phrase,
                      "products_page_1": len(page.products),
                      "first_nm_id": sample_product.nm_id,
                      "sample_response_keys": sorted(sample.keys()),
                      "warning": "Region and delivery accuracy require checking against the WB browser."},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wildberries hourly SERP analytics")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create SQLite schema and synchronize query source")
    collect = sub.add_parser("collect", help="Collect one cycle or one exact query")
    collect.add_argument("--query", help="One exact phrase from config/queries.txt")
    collect.add_argument("--force", action="store_true", help="Allow another run in this hourly slot")
    sub.add_parser("worker", help="Run the hourly scheduler in foreground")
    sub.add_parser("doctor", help="Check live endpoint, WITHOUT writing fake observations")
    args = parser.parse_args(argv)
    configure_logging()
    cfg = Settings.from_env()
    if args.command == "doctor":
        return doctor(cfg)
    store = prepare(cfg)
    if args.command == "collect":
        attempted, skipped, failed = run_cycle(cfg, store, phrase=args.query, force=args.force)
        if failed:
            log.error("Collection finished with %d unsuccessful query run(s)", failed)
            return 2
    elif args.command == "worker":
        run_worker(cfg, store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
