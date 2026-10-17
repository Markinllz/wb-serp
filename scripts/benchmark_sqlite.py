"""Disposable SQLite endurance benchmark; NEVER uses production DB or synthetic history.

Usage: python -m scripts.benchmark_sqlite --hours 24 --queries 70 --top 200
All records are generated inside TemporaryDirectory and deleted on completion.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import time

from wb_serp.models import Product
from wb_serp.store import Store


def bench(hours: int, queries: int, top: int) -> dict:
    if not all(1 <= value <= 500 for value in (hours, queries, top)):
        raise ValueError('Use numbers between 1 and 500')
    with tempfile.TemporaryDirectory(prefix='wb-serp-benchmark-') as folder:
        store = Store(Path(folder) / 'benchmark.db')
        store.initialize()
        store.sync_queries([f'test-only-{i}' for i in range(queries)])
        query_ids = [row['id'] for row in store.enabled_queries()]
        at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        t0 = time.perf_counter()
        for hour in range(hours):
            observed_at = at + timedelta(hours=hour)
            # Deliberately distinct IDs across hours to stress product_catalog too.
            products = [Product(nm_id=hour * top + number,
                                position=number, price_rub=Decimal('1200.99'),
                                rating=4.8, reviews=150, delivery_days=None,
                                seller='Fixture', title='Fixture, NOT Wildberries')
                        for number in range(1, top + 1)]
            for query_id in query_ids:
                run_id = f'{hour}:{query_id}'
                store.start_run(run_id=run_id, query_id=query_id,
                                scheduled_for=observed_at.isoformat(),
                                started_at=observed_at, requested_top_n=top,
                                destination=23)
                store.complete_run(run_id=run_id, products=products, status='success',
                                   finished_at=observed_at)
        elapsed = time.perf_counter() - t0
        with store.connect() as db:
            runs = db.execute('SELECT count(*) FROM collection_runs').fetchone()[0]
            observations = db.execute('SELECT count(*) FROM observations').fetchone()[0]
            unique = db.execute('SELECT count(*) FROM product_catalog').fetchone()[0]
            integrity = db.execute('PRAGMA quick_check').fetchone()[0]
        t1 = time.perf_counter()
        timeline = store.rank_history(query_ids[0], [1], depth=top)
        select_ms = round(1000 * (time.perf_counter() - t1), 2)
        size_mib = round(store.path.stat().st_size / (1024 ** 2), 2)
        assert integrity == 'ok' and runs == hours * queries
        assert observations == hours * queries * top == unique
        assert len(timeline) == hours and timeline[0]['position'] == 1
        assert sum(row['position'] is None for row in timeline) == hours - 1
        return dict(runs=runs, observations=observations, unique_catalog_rows=unique,
                    elapsed_seconds=round(elapsed, 2), rank_query_ms=select_ms,
                    db_file_mib=size_mib, sqlite_integrity=integrity,
                    source='synthetic disposable fixtures, not live Wildberries')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--queries', type=int, default=70)
    parser.add_argument('--top', type=int, default=200)
    args = parser.parse_args()
    print(bench(args.hours, args.queries, args.top))
