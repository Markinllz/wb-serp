"""Synthetic throughput/load smoke, not actual WB SERP history."""
from dataclasses import replace
from pathlib import Path
import time

import httpx

from wb_serp import cli
from wb_serp.client import WildberriesClient
from wb_serp.config import Settings


def test_full_70_queries_top_200_all_saved_atomically(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'config' / 'queries.txt'
    cfg = replace(Settings.from_env(), database_path=tmp_path / 'bulk.db',
                  queries_path=path, top_n=200, page_limit=6,
                  request_delay_seconds=0, request_retries=1, query_delay_seconds=0)
    store = cli.prepare(cfg)
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        page = int(request.url.params['page'])
        assert 1 <= page <= 4
        start = (page - 1) * 50
        return httpx.Response(200, json={'data': {'products': [
            {'id': i + 1, 'name': f'Test #{i + 1}', 'reviewRating': 4.7,
             'supplier': 'Mock seller', 'feedbacks': 2,
             'sizes': [{'price': {'product': 150000}}]}
            for i in range(start, start + 50)
        ]}})
    transport = httpx.MockTransport(handler)
    def build_client(settings):
        return WildberriesClient(search_url='https://example.test/search',
                                 destination=settings.destination,
                                 client=httpx.Client(transport=transport),
                                 delay_seconds=0)
    monkeypatch.setattr(cli, 'build_client', build_client)
    start = time.monotonic()
    attempted, skipped, failed = cli.run_cycle(cfg, store, scheduled_for='2026-10-16T01:00:00+00:00')
    duration = time.monotonic() - start
    assert (attempted, skipped, failed) == (70, 0, 0)
    assert count == 70 * 4
    assert len(store.recent_runs(limit=100)) == 70
    assert all(q['last_status'] == 'success' and q['last_count'] == 200 for q in store.queries_dashboard())
    first_id = store.enabled_queries()[0]['id']
    last_id = store.enabled_queries()[-1]['id']
    assert [v['position'] for v in store.latest_observations(first_id)] == list(range(1, 201))
    assert [v['position'] for v in store.latest_observations(last_id)] == list(range(1, 201))
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0] == 14000
        assert db.execute('SELECT count(*) FROM product_catalog').fetchone()[0] == 14000
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    # These are synthetic fixture responses and the duration is a local DB/CPU
    # benchmark, not an estimate of WB network rate or production RPS.
    print(f'70x200 fake HTTP cycle: {duration:.3f}s, 280 fixture requests, 14000 rows')
