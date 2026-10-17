from dataclasses import replace
import json

from scripts import probe_batch_wb as batch
from scripts.probe_live_wb import ProbeFailure
from wb_serp.client import FetchError
from wb_serp.config import Settings
from wb_serp.models import SearchPage


def test_batch_retains_partial_and_stops_blocked(monkeypatch, tmp_path):
    class Client:
        metrics = {'requests': 0, 'retries': 0, 'session_refreshes': 0}
        def __enter__(self): return self
        def __exit__(self, *args): pass

    client = Client()
    monkeypatch.setattr(batch, 'build_client', lambda cfg: client)
    def probe(client, *, query, **kwargs):
        client.metrics['requests'] += 1
        if query == 'partial':
            raise ProbeFailure('short response', observed=106, page_sizes=[100, 6, 0])
        if query == 'duplicate':
            raise ProbeFailure('Duplicate nm_id; pagination may loop')
        if query == 'blocked':
            raise FetchError('check required', blocked=True, cause='browser_challenge', http_status=498)
        return {'status': 'PASS', 'query': query}
    monkeypatch.setattr(batch, 'probe', probe)
    cfg = replace(Settings.from_env(), query_delay_seconds=7)
    waits = []
    output = tmp_path / 'batch.jsonl'
    summary = batch.run_batch(cfg, ['ok', 'partial', 'duplicate', 'blocked', 'not-run'], output,
                              sleep=waits.append)
    assert waits == [7, 7, 7]
    assert summary['attempted'] == 4 and summary['not_run'] == 1
    assert summary['statuses'] == {'PASS': 1, 'INCOMPLETE': 1, 'FAIL': 2}
    assert summary['stopped_reason'] == 'browser_challenge'
    assert summary['observations_database_modified'] is False
    assert json.loads(output.read_text().splitlines()[1])['observed'] == 106
    assert json.loads(output.read_text().splitlines()[2])['status'] == 'FAIL'


def test_save_live_uses_collector_and_replays_without_extra_http(tmp_path, monkeypatch):
    path = tmp_path / 'queries.txt'
    path.write_text('full\npartial\n')
    cfg = replace(Settings.from_env(), database_path=tmp_path / 'live.db', queries_path=path,
                  top_n=2, query_delay_seconds=0)
    class Client:
        metrics = {'requests': 0, 'retries': 0, 'session_refreshes': 0}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def page(self, query, page_number):
            self.metrics['requests'] += 1
            products = [] if query == 'partial' and page_number == 2 else [
                {'id': page_number, 'name': 'Controlled fixture'}]
            return SearchPage(products=products, page_number=page_number)
    client = Client()
    monkeypatch.setattr(batch, 'build_client', lambda cfg: client)
    output = tmp_path / 'batch.jsonl'
    summary = batch.run_batch(cfg, ['full', 'partial'], output, save_live=True)
    assert client.metrics['requests'] == 4  # replay must not double the WB traffic
    assert summary['statuses'] == {'PASS': 1, 'INCOMPLETE': 1}
    assert summary['observations_database_modified'] is True
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [r['database_status'] for r in rows] == ['success', 'partial']
    assert [r['database_observed'] for r in rows] == [2, 1]
    from wb_serp.store import Store
    store = Store(cfg.database_path)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0] == 3
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_duplicate_retries_whole_snapshot_once_without_shifting_ranks(tmp_path, monkeypatch):
    path = tmp_path / 'queries.txt'
    path.write_text('query\n')
    cfg = replace(Settings.from_env(), database_path=tmp_path / 'live.db', queries_path=path,
                  top_n=2, query_delay_seconds=0)
    class Client:
        metrics = {'requests': 0, 'retries': 0, 'session_refreshes': 0}
        pages = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def page(self, query, page_number):
            self.pages.append(page_number)
            self.metrics['requests'] += 1
            nm_id = 1 if len(self.pages) <= 2 else page_number
            return SearchPage(products=[{'id': nm_id}], page_number=page_number)
    client = Client()
    monkeypatch.setattr(batch, 'build_client', lambda cfg: client)
    output = tmp_path / 'batch.jsonl'
    summary = batch.run_batch(cfg, ['query'], output, save_live=True)
    assert summary['statuses'] == {'PASS': 1}
    assert client.pages == [1, 2, 1, 2]
    row = json.loads(output.read_text())
    assert row['snapshot_attempts'] == 2
    assert row['nm_ids_in_rank_order'] == [1, 2]
    assert row['prior_attempts'][0]['database_status'] == 'failed'
    assert row['database_status'] == 'success'


def test_reported_short_catalog_stops_before_repeating_last_page(tmp_path, monkeypatch):
    path = tmp_path / 'queries.txt'
    path.write_text('narrow\n')
    cfg = replace(Settings.from_env(), database_path=tmp_path / 'live.db', queries_path=path,
                  top_n=200, query_delay_seconds=0)
    class Client:
        metrics = {'requests': 0, 'retries': 0, 'session_refreshes': 0}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def page(self, query, page_number):
            self.metrics['requests'] += 1
            assert page_number == 1
            return SearchPage(products=[{'id': 123}], page_number=1, total=1)
    client = Client()
    monkeypatch.setattr(batch, 'build_client', lambda cfg: client)
    output = tmp_path / 'batch.jsonl'
    summary = batch.run_batch(cfg, ['narrow'], output, save_live=True)
    row = json.loads(output.read_text())
    assert summary['statuses'] == {'INCOMPLETE': 1}
    assert row['reported_total'] == 1 and row['database_observed'] == 1
    assert row['database_status'] == 'partial' and row['snapshot_attempts'] == 1
    assert client.metrics['requests'] == 1
