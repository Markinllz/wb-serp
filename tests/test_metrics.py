import sqlite3
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from wb_serp.metrics import DatabaseMetrics
from wb_serp.store import Store


def samples(path):
    registry = CollectorRegistry()
    registry.register(DatabaseMetrics(path))
    text = generate_latest(registry).decode()
    return [s for family in text_string_to_metric_families(text) for s in family.samples]


def test_missing_database_is_down_and_never_created(tmp_path):
    path = tmp_path / 'missing.db'
    result = samples(path)
    assert [(s.name, s.value) for s in result] == [('wb_database_up', 0)]
    assert not path.exists()


def test_readonly_export_distinguishes_partial_failed_and_never(tmp_path):
    path = tmp_path / 'history.db'
    store = Store(path)
    store.initialize()
    store.sync_queries(['Фраза "один"', 'Вторая', 'Третья'])
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO collection_runs
            (id,query_id,scheduled_for,started_at,completed_at,requested_top_n,destination,status,observed_count)
            VALUES ('a',1,'2026-10-17T00:00:00+00:00','2026-10-17T00:00:00+00:00',
                '2026-10-17T00:00:15+00:00',200,1259570991,'partial',106),
                ('b',2,'2026-10-17T00:00:00+00:00','2026-10-17T00:00:00+00:00',
                '2026-10-17T00:00:09+00:00',200,1259570991,'failed',0)""")
        before = db.execute('SELECT * FROM collection_runs ORDER BY id').fetchall()
    result = samples(path)
    def value(name, **labels):
        found = [s.value for s in result if s.name == name and all(s.labels.get(k) == v for k, v in labels.items())]
        assert len(found) == 1
        return found[0]
    assert value('wb_database_up') == 1
    assert value('wb_queries_enabled') == 3
    assert value('wb_query_observed', query_id='1') == 106
    assert value('wb_query_latest_status', query_id='1', status='partial') == 1
    assert value('wb_query_latest_status', query_id='1', status='success') == 0
    assert value('wb_query_latest_status', query_id='2', status='failed') == 1
    assert value('wb_query_latest_status', query_id='3', status='never') == 1
    assert value('wb_query_duration_seconds', query_id='1') == 15
    assert value('wb_query_last_valid_timestamp_seconds', query_id='1') > 0
    assert value('wb_query_last_valid_timestamp_seconds', query_id='2') == 0
    assert value('wb_query_enabled_since_timestamp_seconds', query_id='1') > 0
    assert value('wb_runs', status='blocked') == 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM collection_runs ORDER BY id').fetchall() == before

    store.set_query_enabled(2, False)
    paused = samples(path)
    assert not any(s.labels.get('query_id') == '2' for s in paused)
    assert next(s.value for s in paused if s.name == 'wb_queries_enabled') == 2


def test_invalid_schema_is_down_without_partial_metrics(tmp_path):
    path = Path(tmp_path / 'other.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE unrelated (value INTEGER)')
    assert [(s.name, s.value) for s in samples(path)] == [('wb_database_up', 0)]
