"""Sequential live checks, without creating artificial observation history."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from scripts.probe_live_wb import ProbeFailure, probe
from wb_serp.cli import build_client, prepare
from wb_serp.client import FetchError
from wb_serp.config import Settings
from wb_serp.parsing import SchemaError
from wb_serp.queries import read_queries
from wb_serp.locking import exclusive_collector
from wb_serp.service import Collector


class RecordedPages:
    """Validate the exact pages just saved by Collector, without another WB request."""
    def __init__(self, client):
        self.client = client
        self.pages = {}
        self.replaying = False

    def page(self, query, page_number):
        key = (query, page_number)
        if not self.replaying:
            try:
                self.pages[key] = self.client.page(query, page_number)
            except FetchError as exc:
                self.pages[key] = exc
        value = self.pages[key]
        if isinstance(value, FetchError):
            raise value
        return value


def probe_attempt(cfg, client, phrase, store, query_ids):
    blocked = False
    collection = None
    validator = client
    if store:
        validator = RecordedPages(client)
        collection = Collector(store, validator, destination=cfg.destination,
                               top_n=cfg.top_n, page_limit=cfg.page_limit).collect(
                                   query_ids[phrase], phrase)
        validator.replaying = True
    try:
        row = probe(validator, query=phrase, top_n=cfg.top_n, page_limit=cfg.page_limit)
    except (FetchError, ProbeFailure, SchemaError, ValueError) as exc:
        row = {'status': 'FAIL', 'query': phrase, 'reason': str(exc),
               'error_type': type(exc).__name__}
        if isinstance(exc, ProbeFailure):
            row.update(status='INCOMPLETE' if exc.page_sizes else 'FAIL',
                       observed=exc.observed, page_sizes=exc.page_sizes, cause=exc.cause,
                       reported_total=exc.reported_total)
        if isinstance(exc, FetchError):
            blocked = exc.blocked
            row.update(http_status=exc.http_status, cause=exc.cause,
                       response_headers=exc.response_headers)
    if collection is not None:
        row.update(run_id=collection.run_id, database_status=collection.status,
                   database_observed=collection.observed)
    return row, blocked


def run_batch(cfg: Settings, phrases: list[str], output: Path, *, sleep=time.sleep,
              save_live: bool = False) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    attempted = 0
    stopped = None
    started = datetime.now(timezone.utc).isoformat(timespec='seconds')
    store = prepare(cfg) if save_live else None
    query_ids = {q['phrase']: q['id'] for q in store.enabled_queries()} if store else {}
    if store and any(phrase not in query_ids for phrase in phrases):
        raise ValueError('Saving live data requires phrases from the configured query list')
    # Exclusive creation protects prior verification evidence from overwriting.
    with (output.open('x', encoding='utf-8') as stream,
          exclusive_collector(cfg.database_path) if store else nullcontext(),
          build_client(cfg) as client):
        for index, phrase in enumerate(phrases, 1):
            if attempted and cfg.query_delay_seconds:
                sleep(cfg.query_delay_seconds)
            before = dict(client.metrics)
            prior_attempts = []
            for snapshot_attempt in range(1, 3):
                row, blocked = probe_attempt(cfg, client, phrase, store, query_ids)
                if row.get('cause') != 'duplicate_nm_id' or snapshot_attempt == 2:
                    break
                prior_attempts.append(dict(row))
                print(f'{index}/{len(phrases)} duplicate ranks; retrying entire snapshot once', flush=True)
                if cfg.query_delay_seconds:
                    sleep(cfg.query_delay_seconds)
            row['snapshot_attempts'] = snapshot_attempt
            if prior_attempts:
                row['prior_attempts'] = prior_attempts
            row.update(index=index, destination=cfg.destination,
                       transport=cfg.http_transport,
                       metrics={key: value - before.get(key, 0)
                                for key, value in client.metrics.items()})
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            attempted += 1
            counts[row['status']] += 1
            print(f"{index}/{len(phrases)} {row['status']}: {phrase}", flush=True)
            if blocked:
                stopped = row.get('cause') or 'blocked'
                break
        summary = {
            'started_at_utc': started,
            'finished_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'planned': len(phrases), 'attempted': attempted, 'not_run': len(phrases) - attempted,
            'statuses': dict(counts), 'stopped_reason': stopped,
            'metrics': dict(client.metrics), 'destination': cfg.destination,
            'transport': cfg.http_transport, 'observations_database_modified': save_live,
        }
        browser = getattr(client, '_browser_transport', None)
        if browser is not None:
            summary['browser_metrics'] = dict(browser.metrics)
            summary['browser_diagnostics'] = dict(browser.diagnostics)
    output.with_suffix('.summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return summary


def main(argv: list[str] | None = None) -> int:
    cfg = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', action='append', help='Repeat for a selected subset; default all configured')
    parser.add_argument('--save-live', action='store_true',
                        help='Save these actual WB responses using the application Collector and SQLite')
    parser.add_argument('--output', type=Path, default=cfg.database_path.parent / 'verification' /
                        (datetime.now(timezone.utc).strftime('batch_%Y%m%dT%H%M%SZ') + '.jsonl'))
    args = parser.parse_args(argv)
    summary = run_batch(cfg, args.query or read_queries(cfg.queries_path), args.output,
                        save_live=args.save_live)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['statuses'].get('PASS') == summary['planned'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
