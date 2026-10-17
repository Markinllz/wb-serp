"""Read-only operational metrics; never sends requests to Wildberries."""
from contextlib import closing
from datetime import datetime
import logging
import os
from pathlib import Path
import sqlite3
import time

from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import GaugeMetricFamily

STATUSES = ('running', 'success', 'partial', 'failed', 'blocked', 'interrupted')


def timestamp(value):
    return datetime.fromisoformat(value).timestamp() if value else 0


class DatabaseMetrics:
    def __init__(self, path: Path):
        self.path = path

    def describe(self):
        # Registering must not open the database or create a missing file.
        return []

    def collect(self):
        up = GaugeMetricFamily('wb_database_up', 'Database can be read')
        metrics = []
        try:
            with closing(sqlite3.connect(f'{self.path.absolute().as_uri()}?mode=ro', uri=True,
                                         timeout=3)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                enabled = db.execute('SELECT count(*) FROM search_queries WHERE enabled=1').fetchone()[0]
                metrics.append(GaugeMetricFamily('wb_queries_enabled', 'Configured active queries', value=enabled))
                for table in ('observations', 'collection_runs', 'product_catalog'):
                    value = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    metrics.append(GaugeMetricFamily(f'wb_{table}', f'Rows in {table}', value=value))
                runs = GaugeMetricFamily('wb_runs', 'Saved collection attempts', labels=['status'])
                counts = dict(db.execute('SELECT status,count(*) FROM collection_runs GROUP BY status'))
                recent = GaugeMetricFamily('wb_runs_last_hour', 'Attempts completed within an hour', labels=['status'])
                recent_counts = dict(db.execute("""SELECT status,count(*) FROM collection_runs
                    WHERE julianday(completed_at)>=julianday('now','-1 hour') GROUP BY status"""))
                for status in STATUSES:
                    runs.add_metric([status], counts.get(status, 0))
                    recent.add_metric([status], recent_counts.get(status, 0))
                metrics.extend([runs, recent])
                latest = db.execute("""SELECT q.id,q.phrase,r.status,r.observed_count,r.requested_top_n,
                        r.started_at,r.completed_at,
                        (SELECT max(v.completed_at) FROM collection_runs v WHERE v.query_id=q.id
                         AND v.status IN ('success','partial') AND v.observed_count>0)
                    FROM search_queries q LEFT JOIN collection_runs r ON r.id=(
                        SELECT id FROM collection_runs WHERE query_id=q.id ORDER BY started_at DESC,id DESC LIMIT 1)
                    WHERE q.enabled=1 ORDER BY q.display_order""").fetchall()
                labels = ['query_id', 'query']
                last_valid = GaugeMetricFamily('wb_query_last_valid_timestamp_seconds',
                                               'Last complete or partial snapshot', labels=labels)
                observed = GaugeMetricFamily('wb_query_observed', 'Items in latest attempt', labels=labels)
                requested = GaugeMetricFamily('wb_query_requested', 'Requested depth in latest attempt', labels=labels)
                duration = GaugeMetricFamily('wb_query_duration_seconds', 'Duration of latest completed attempt', labels=labels)
                state = GaugeMetricFamily('wb_query_latest_status', 'Latest attempt status, one-hot', labels=labels+['status'])
                for qid, phrase, status, count, top_n, started, completed, valid in latest:
                    values = [str(qid), phrase]
                    last_valid.add_metric(values, timestamp(valid))
                    observed.add_metric(values, count or 0)
                    requested.add_metric(values, top_n or 0)
                    if completed and started:
                        duration.add_metric(values, max(0, timestamp(completed)-timestamp(started)))
                    for known in (*STATUSES, 'never'):
                        state.add_metric(values+[known], int((status or 'never') == known))
                metrics.extend([last_valid, observed, requested, duration, state])
            size = sum(p.stat().st_size for p in (self.path, Path(str(self.path)+'-wal')) if p.exists())
            metrics.append(GaugeMetricFamily('wb_database_bytes', 'SQLite and WAL bytes', value=size))
            up.add_metric([], 1)
        except (sqlite3.Error, OSError, ValueError):
            logging.exception('Database metrics unavailable')
            up.add_metric([], 0)
            metrics = []
        yield up
        yield from metrics


def main():
    registry = CollectorRegistry()
    registry.register(DatabaseMetrics(Path(os.environ.get('WB_DB_PATH', '/app/data/serp.db'))))
    start_http_server(9101, addr='0.0.0.0', registry=registry)
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
