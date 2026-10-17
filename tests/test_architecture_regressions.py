"""Regression tests for historical integrity and single-VPS failure scenarios.

All data lives in pytest temp directories. No simulated history enters user storage.
"""
from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest

from wb_serp import cli
from wb_serp.client import FetchError
from wb_serp.config import Settings
from wb_serp.models import Product, SearchPage
from wb_serp.service import Collector
from wb_serp.store import Store


class Pages:
    def __init__(self, *pages):
        self.pages = pages

    def page(self, query: str, page_number: int) -> SearchPage:
        return SearchPage(tuple(self.pages[page_number - 1]), page_number)


def item(n: int) -> dict:
    return {"id": n, "name": f"Product {n}", "sizes": [{"price": {"product": 10995}}]}


def completed(store: Store, query_id: int, run_id: str, ids: list[int], depth: int) -> None:
    at = datetime.now(timezone.utc)
    store.start_run(run_id=run_id, query_id=query_id, scheduled_for="2026-10-16T10:00:00+00:00",
                    started_at=at, requested_top_n=depth, destination=23)
    store.complete_run(run_id=run_id, status="success", finished_at=at,
                       products=[Product(nm_id=n, position=i, price_rub=Decimal('109.95'),
                                         rating=None, reviews=None, delivery_days=None,
                                         seller=None, title=None)
                                 for i, n in enumerate(ids, 1)])


def test_changing_top_n_requires_new_collection_same_slot(tmp_path, monkeypatch):
    path = tmp_path / "storage.sqlite"
    settings = replace(Settings.from_env(), database_path=path, top_n=1)
    store = Store(path)
    store.initialize()
    store.sync_queries(["оригинальный запрос"])
    class Dummy:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def page(self, query, page_number): return SearchPage((item(1), item(2)), page_number)
    monkeypatch.setattr(cli, 'build_client', lambda cfg: Dummy())
    slot = "2026-10-16T10:00:00+00:00"
    assert cli.run_cycle(settings, store, scheduled_for=slot) == (1, 0, 0)
    assert cli.run_cycle(settings, store, scheduled_for=slot) == (0, 1, 0)
    deeper = replace(settings, top_n=2)
    assert cli.run_cycle(deeper, store, scheduled_for=slot) == (1, 0, 0)
    assert store.available_depths(store.enabled_queries()[0]['id']) == [2, 1]
    assert len(store.recent_runs()) == 2
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 3


def test_different_depth_timelines_are_separated(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.initialize()
    store.sync_queries(['одна фраза'])
    qid = store.enabled_queries()[0]['id']
    completed(store, qid, 'A', [5, 6], 2)
    completed(store, qid, 'B', [6, 7, 8], 3)
    assert store.available_depths(qid) == [3, 2]
    assert [r['position'] for r in store.rank_history(qid, [5], depth=2)] == [1]
    assert [r['position'] for r in store.rank_history(qid, [5], depth=3)] == [None]
    assert len(store.rank_history(qid, [5])) == 2  # raw API remains backward-compatible
    with pytest.raises(ValueError, match='Depth'):
        store.rank_history(qid, [5], depth=-1)


def test_can_fix_wrong_region_before_first_success_but_not_after(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.initialize()
    store.sync_queries(['одна фраза'])
    query = store.enabled_queries()[0]
    class Blocked:
        def page(self, query, page_number):
            raise FetchError('HTTP 403', blocked=True)
    assert Collector(store, Blocked(), destination=99, top_n=1).collect(
        query['id'], query['phrase']).status == 'blocked'
    assert Collector(store, Pages([item(12)]), destination=23, top_n=1).collect(
        query['id'], query['phrase']).status == 'success'
    with store.connect() as db:
        assert db.execute("SELECT value FROM dataset_settings WHERE key='destination'").fetchone()[0] == '23'
        assert [r[0] for r in db.execute('SELECT destination FROM collection_runs ORDER BY rowid')] == [99, 23]
    with pytest.raises(ValueError, match='WB_DESTINATION'):
        Collector(store, Pages([item(12)]), destination=99, top_n=1).collect(
            query['id'], query['phrase'])
    assert len(store.recent_runs()) == 2


def test_synchronous_full_applies_to_every_writer_connection(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.initialize()
    for _ in range(3):
        with store.connect() as db:
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert db.execute('PRAGMA synchronous').fetchone()[0] == 2  # FULL
            assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.mark.parametrize('version', [1, 2])
def test_rejects_fake_schema_version_with_missing_tables(tmp_path, version):
    path = tmp_path / 'broken.sqlite'
    with closing(sqlite3.connect(path)) as db:
        db.execute(f'PRAGMA user_version={version}')
    with pytest.raises(RuntimeError, match='schema is incomplete'):
        Store(path).initialize()


def test_legacy_migration_preserves_existing_history(tmp_path):
    store = Store(tmp_path / 'legacy.sqlite')
    store.initialize()
    store.sync_queries(['одна фраза'])
    query = store.enabled_queries()[0]
    completed(store, query['id'], 'before-migration', [7], 1)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute('DROP TABLE dataset_settings')
        db.execute('PRAGMA user_version=1')
        db.commit()
    store.initialize()
    assert store.rank_history(query['id'], [7])[0]['position'] == 1
    with store.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_catalog_search_is_bounded_escapes_like_and_retains_exact_ids(tmp_path):
    store = Store(tmp_path / 'large.sqlite')
    store.initialize()
    store.sync_queries(['одна фраза'])
    qid = store.enabled_queries()[0]['id']
    at = datetime.now(timezone.utc)
    store.start_run(run_id='bulk', query_id=qid,
                    scheduled_for='2026-10-16T10:00:00+00:00',
                    started_at=at, requested_top_n=500, destination=23)
    store.complete_run(run_id='bulk', status='success', finished_at=at,
        products=[Product(nm_id=n, position=n, price_rub=None, rating=None,
                          reviews=None, delivery_days=None,
                          seller='Продавец%10' if n == 500 else 'seller',
                          title='Тест_специальный' if n == 499 else f'Товар {n}')
                  for n in range(1, 501)])
    assert store.product_count(qid) == 500
    assert len(store.search_products(qid)) == 250
    assert [r['nm_id'] for r in store.search_products(qid, '499')] == [499]
    assert [r['nm_id'] for r in store.search_products(qid, 'специальный')] == [499]
    assert [r['nm_id'] for r in store.search_products(qid, '%10')] == [500]
    assert [r['nm_id'] for r in store.search_products(qid, '_специальный')] == [499]
    assert [r['nm_id'] for r in store.products_by_ids(qid, [1, 500])] == [1, 500]
    with pytest.raises(ValueError, match='limit'):
        store.search_products(qid, limit=501)


def test_graceful_shutdown_stops_before_next_query(tmp_path, monkeypatch):
    settings = replace(Settings.from_env(), database_path=tmp_path / 'stop.sqlite', top_n=1)
    store = Store(settings.database_path)
    store.initialize()
    store.sync_queries(['first query', 'second query'])
    stop_flag = [False]
    calls = []
    class StopAfterFirst:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def page(self, query, page_number):
            calls.append(query)
            stop_flag[0] = True
            return SearchPage((item(10),), page_number)
    monkeypatch.setattr(cli, 'build_client', lambda cfg: StopAfterFirst())
    result = cli.run_cycle(settings, store,
                           scheduled_for='2026-10-16T10:00:00+00:00',
                           should_stop=lambda: stop_flag[0])
    assert result == (1, 0, 0)
    assert calls == ['first query']
    assert len(store.recent_runs()) == 1
    # After restart the same scheduled slot can collect missing query, not overwrite previous history.
    assert cli.run_cycle(settings, store, scheduled_for='2026-10-16T10:00:00+00:00') == (1, 1, 0)
    assert len(store.recent_runs()) == 2


def test_wal_reader_does_not_block_writer(tmp_path):
    store = Store(tmp_path / 'test.sqlite')
    store.initialize()
    store.sync_queries(['one'])
    query_id = store.enabled_queries()[0]['id']
    # A read transaction can keep its snapshot while another connection writes.
    with store.connect() as reader:
        reader.execute('BEGIN')
        assert reader.execute('SELECT count(*) FROM collection_runs').fetchone()[0] == 0
        completed(store, query_id, 'run1', [1], 1)
        assert reader.execute('SELECT count(*) FROM collection_runs').fetchone()[0] == 0
    assert len(store.recent_runs()) == 1


def test_missing_performance_index_is_detected_before_serving_queries(tmp_path):
    path = tmp_path / 'damaged.db'
    store = Store(path)
    store.initialize()
    with closing(sqlite3.connect(path)) as db:
        db.execute('DROP INDEX catalog_latest')
        db.commit()
    with pytest.raises(RuntimeError, match='indexes are incomplete'):
        store.initialize()


def test_history_limit_can_be_disclosed_instead_of_silently_truncating(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    store.initialize()
    store.sync_queries(['one'])
    qid = store.enabled_queries()[0]['id']
    completed(store, qid, 'R1', [1], 1)
    completed(store, qid, 'R2', [2], 1)
    completed(store, qid, 'R3', [1], 1)
    assert store.count_success_runs(qid, depth=1) == 3
    assert store.count_success_runs(qid, depth=999) == 0
    rows = store.rank_history(qid, [1], depth=1, limit_runs=2)
    assert len(rows) == 2 and rows[-1]['position'] == 1


def test_single_vps_compose_defaults_keep_dashboard_private_and_db_persistent():
    root = __import__('pathlib').Path(__file__).resolve().parents[1]
    compose = (root / 'compose.yaml').read_text()
    dockerfile = (root / 'Dockerfile').read_text()
    caddyfile = (root / 'Caddyfile').read_text()
    assert '"127.0.0.1:8501:8501"' in compose
    assert 'profiles: ["public"]' in compose
    assert 'wb_data:/app/data' in compose
    assert 'stop_grace_period: 240s' in compose
    assert 'USER app' in dockerfile
    assert 'basic_auth {' in caddyfile
