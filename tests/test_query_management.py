from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from tests.conftest import product
from wb_serp import cli
from wb_serp.config import Settings
from wb_serp.models import Product, SearchPage
from wb_serp.store import Store


def configuration(tmp_path):
    phrases = tmp_path / "queries.txt"
    phrases.write_text("Первая\nВторая\n")
    return replace(
        Settings.from_env(),
        queries_path=phrases,
        database_path=tmp_path / "history.db",
        top_n=1,
        request_delay_seconds=0,
        query_delay_seconds=0,
    )


def test_add_pause_resume_and_restart_preserve_ids_and_history(tmp_path):
    cfg = configuration(tmp_path)
    store = cli.prepare(cfg)
    preset = store.enabled_queries()[0]["id"]
    qid, created = store.add_query("Новая фраза ё 100%")
    assert created
    now = datetime.now(timezone.utc)
    store.start_run(
        run_id="saved",
        query_id=qid,
        scheduled_for=now.isoformat(),
        started_at=now,
        requested_top_n=1,
        destination=cfg.destination,
    )
    store.complete_run(
        run_id="saved",
        products=[Product(23, 1, None, None, None, None, None, "Товар")],
        finished_at=now,
        status="success",
    )
    store.set_query_enabled(qid, False)
    store.set_query_enabled(preset, False)
    restarted = cli.prepare(cfg)
    assert [q["phrase"] for q in restarted.enabled_queries()] == ["Вторая"]
    assert len(restarted.queries_dashboard(include_paused=True)) == 3
    assert restarted.latest_observations(qid)[0]["nm_id"] == 23
    resumed, created = restarted.add_query("Новая фраза ё 100%")
    assert resumed == qid and not created
    assert restarted.latest_observations(qid)[0]["nm_id"] == 23
    assert not next(q for q in restarted.queries_dashboard(include_paused=True) if q["id"] == preset)[
        "enabled"
    ]
    with pytest.raises(ValueError, match="уже собирается"):
        restarted.add_query("Новая фраза ё 100%")


@pytest.mark.parametrize(
    "phrase", ["", "  ", " leading", "trailing ", "bad\nline", "bad\x00line", "x" * 301]
)
def test_invalid_query_never_changes_database(tmp_path, phrase):
    store = cli.prepare(configuration(tmp_path))
    with pytest.raises(ValueError):
        store.add_query(phrase)
    assert len(store.queries_dashboard(include_paused=True)) == 2


def test_readonly_dashboard_cannot_modify_queries(tmp_path):
    cfg = configuration(tmp_path)
    store = cli.prepare(cfg)
    readonly = Store(cfg.database_path, readonly=True)
    with pytest.raises(ValueError, match="Read-only"):
        readonly.add_query("Новая")
    with pytest.raises(ValueError, match="Read-only"):
        readonly.set_query_enabled(store.enabled_queries()[0]["id"], False)


def test_query_changes_apply_to_next_cycle_without_touching_current_snapshot(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    store = cli.prepare(cfg)
    second = store.enabled_queries()[1]["id"]
    calls = []
    changed = False

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def page(self, query, page):
            nonlocal changed
            calls.append(query)
            if not changed:
                changed = True
                store.set_query_enabled(second, False)
                store.add_query("Третья")
            return SearchPage((product(123),), page, total=1)

    monkeypatch.setattr(cli, "build_client", lambda _: Client())
    cli.run_cycle(cfg, store, scheduled_for="2026-10-17T01:00:00+00:00")
    assert calls == ["Первая", "Вторая"]
    calls.clear()
    cli.run_cycle(cfg, store, scheduled_for="2026-10-17T02:00:00+00:00")
    assert calls == ["Первая", "Третья"]
    assert store.latest_observations(second)[0]["nm_id"] == 123


def test_schema3_migration_only_adds_settings_and_preserves_records(tmp_path):
    cfg = configuration(tmp_path)
    store = cli.prepare(cfg)
    qid = store.enabled_queries()[0]["id"]
    at = datetime.now(timezone.utc) - timedelta(hours=4)
    store.start_run(
        run_id="before",
        query_id=qid,
        scheduled_for=at.isoformat(),
        started_at=at,
        requested_top_n=1,
        destination=cfg.destination,
    )
    store.complete_run(
        run_id="before",
        products=[Product(5, 1, None, None, None, None, None, "Name")],
        finished_at=at,
        status="success",
    )
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("ALTER TABLE search_queries DROP COLUMN enabled_since")
        db.execute("PRAGMA user_version=3")
        original = {
            t: db.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
            for t in ("collection_runs", "observations", "product_catalog")
        }
    store.initialize()
    store.initialize()
    with sqlite3.connect(cfg.database_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT min(enabled_since) FROM search_queries").fetchone()[0] > at.timestamp()
        for table, rows in original.items():
            assert db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows
