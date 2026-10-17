"""Black-box-ish integration checks with real HTTPX -> service -> SQLite -> analytics.

Network responses are controlled fixtures, never claimed to be live Wildberries data.
"""
from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from wb_serp import cli
from wb_serp.client import WildberriesClient
from wb_serp.config import Settings
from wb_serp.store import Store
from wb_serp.charts import chart_rank_history
from tests.conftest import product
from scripts.backup import backup


def settings(tmp_path, *, top_n=4):
    query_path = tmp_path / "queries.txt"
    query_path.write_text("постельное бельё\nпастельный комплект 1,5\n", encoding="utf-8")
    return replace(Settings.from_env(), database_path=tmp_path / "serp.db",
                   queries_path=query_path, destination=1234, top_n=top_n,
                   request_delay_seconds=0, request_retries=2, page_limit=5, query_delay_seconds=0)


def mock_client(cfg, handler):
    return WildberriesClient(search_url="https://example.test/search", destination=cfg.destination,
                             client=httpx.Client(transport=httpx.MockTransport(handler)),
                             retries=cfg.request_retries, delay_seconds=0, sleeper=lambda _: None)


def test_wires_to_db_dashboard_graph_and_csv_like_export(tmp_path, monkeypatch):
    """Real client code, real SQL, 3 timed full snapshots, missing rank represented by NULL."""
    cfg = settings(tmp_path)
    store = cli.prepare(cfg)
    seen_requests = []
    snapshots = [((1, 2), (3, 4)), ((2, 5), (6, 7)), ((1, 7), (8, 9))]
    number = 0

    def handler(request):
        seen_requests.append(request)
        assert request.url.params["query"] == "постельное бельё"
        assert request.url.params["dest"] == "1234"
        assert request.url.params["sort"] == "popular"
        page = int(request.url.params["page"])
        ids = snapshots[number][page - 1]
        return httpx.Response(200, json={"data": {"products": [product(n) for n in ids]}})

    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(config, handler))
    for cycle in range(3):
        number = cycle
        attempted, skipped, failed = cli.run_cycle(
            cfg, store, phrase="постельное бельё",
            scheduled_for=f"2026-10-16T{cycle + 1:02}:00:00+00:00")
        assert (attempted, skipped, failed) == (1, 0, 0)
    assert len(seen_requests) == 6
    query = store.enabled_queries()[0]
    query_id = query["id"]
    runs = store.recent_runs(query_id)
    assert len(runs) == 3
    assert all(r["status"] == "success" and r["observed_count"] == 4 for r in runs)
    assert [r["nm_id"] for r in store.latest_observations(query_id)] == [1, 7, 8, 9]
    history = store.rank_history(query_id, [1, 2, 9])
    one = [row["position"] for row in history if row["nm_id"] == 1]
    two = [row["position"] for row in history if row["nm_id"] == 2]
    nine = [row["position"] for row in history if row["nm_id"] == 9]
    assert one == [1, None, 1]
    assert two == [2, 1, None]
    assert nine == [None, None, 4]
    chart = chart_rank_history(history, [1, 2, 9])
    assert chart.layout.yaxis.autorange == "reversed"
    assert all(trace.connectgaps is False for trace in chart.data)
    assert len(store.known_products(query_id)) == 9
    with store.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 12
    # A scheduled re-run must not duplicate observations or erase an earlier snapshot.
    attempted, skipped, failed = cli.run_cycle(
        cfg, store, phrase="постельное бельё", scheduled_for="2026-10-16T03:00:00+00:00")
    assert (attempted, skipped, failed) == (0, 1, 0)
    assert len(store.recent_runs(query_id)) == 3
    # A consistent online backup restores the real historical records.
    out = tmp_path / "backup.sqlite"
    backup(cfg.database_path, out)
    with closing(sqlite3.connect(out)) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 12
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_429_after_one_page_is_blocked_not_success(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    store = cli.prepare(cfg)
    query = store.enabled_queries()[0]
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"data": {"products": [product(1), product(2)]}})
        return httpx.Response(429, json={"error": "rate limit"})

    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(config, handler))
    attempts, skipped, failed = cli.run_cycle(cfg, store, phrase=query["phrase"])
    assert (attempts, skipped, failed) == (1, 0, 1)
    assert count == 3  # first page + two bounded attempts for the rate-limited second page
    assert store.recent_runs(query["id"])[0]["status"] == "blocked"
    assert store.latest_observations(query["id"]) == []
    assert store.rank_history(query["id"], [1]) == []
    with store.connect() as db:
        # Diagnostics retain actually observed rows; no fabricated success.
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_http_5xx_retried_then_success():
    attempts = []
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"products": [product(1)]}})
    cfg = Settings.from_env()
    with mock_client(cfg, handler) as client:
        page = client.page("тест", 1)
    assert len(attempts) == 2
    assert len(page.products) == 1


@pytest.mark.parametrize("payload", [{"data": {"products": []}},
                                      {"data": {"products": [{"id": "broken"}]}},
                                      {"data": {"notproducts": []}}])
def test_doctor_refuses_empty_or_invalid_serp(tmp_path, monkeypatch, capsys, payload):
    cfg = settings(tmp_path)
    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(
        config, lambda request: httpx.Response(200, json=payload)))
    assert cli.doctor(cfg) == 2
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_doctor_reports_real_valid_sample_without_writing(tmp_path, monkeypatch, capsys):
    cfg = settings(tmp_path)
    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(
        config, lambda request: httpx.Response(200, json={"data": {"products": [product(17)]}})))
    assert cli.doctor(cfg) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["first_nm_id"] == 17
    assert parsed["products_page_1"] == 1
    assert not cfg.database_path.exists()  # doctor must never write to DB


def test_cli_collect_nonzero_exit_when_endpoint_blocked(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    monkeypatch.setattr(cli.Settings, "from_env", lambda: cfg)
    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(
        config, lambda request: httpx.Response(403)))
    code = cli.main(["collect", "--query", "постельное бельё"])
    assert code == 2
    store = Store(cfg.database_path)
    assert store.recent_runs()[0]["status"] == "blocked"


def test_readonly_queries_survive_reopening_and_reject_mutations(tmp_path):
    cfg = settings(tmp_path)
    store = cli.prepare(cfg)
    readonly = Store(cfg.database_path, readonly=True)
    assert len(readonly.queries_dashboard()) == 2
    with pytest.raises(sqlite3.OperationalError):
        with readonly.connect() as db:
            db.execute("DELETE FROM search_queries")
    assert len(store.enabled_queries()) == 2


def test_top_n_partial_is_not_exposed_even_when_api_returned_3_products(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    store = cli.prepare(cfg)
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        items = [product(1), product(2), product(3)] if count == 1 else []
        return httpx.Response(200, json={"data": {"products": items}})
    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(config, handler))
    attempted, skipped, failed = cli.run_cycle(cfg, store, phrase="постельное бельё")
    assert (attempted, skipped, failed) == (1, 0, 1)
    assert store.recent_runs()[0]["status"] == "partial"
    assert store.latest_observations(store.enabled_queries()[0]["id"]) == []


def test_exact_search_phrase_and_sql_parameters_do_not_interpolate(tmp_path, monkeypatch):
    cfg = settings(tmp_path, top_n=1)
    search = "пастельный комплект 1,5"
    store = cli.prepare(cfg)
    def handler(request):
        assert request.url.params["query"] == search
        return httpx.Response(200, json={"data": {"products": [product(321)]}})
    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(config, handler))
    assert cli.run_cycle(cfg, store, phrase=search) == (1, 0, 0)
    assert store.rank_history(store.enabled_queries()[1]["id"], [321])[0]["position"] == 1
    assert len(store.enabled_queries()) == 2
    assert not store.rank_history(store.enabled_queries()[0]["id"], [321])


def test_backup_refuses_existing_filename(tmp_path):
    cfg = settings(tmp_path)
    cli.prepare(cfg)
    location = tmp_path / "snapshot.sqlite"
    backup(cfg.database_path, location)
    with pytest.raises(FileExistsError):
        backup(cfg.database_path, location)


def test_bad_destination_does_not_pollute_existing_history(tmp_path, monkeypatch):
    cfg = settings(tmp_path, top_n=1)
    store = cli.prepare(cfg)
    monkeypatch.setattr(cli, "build_client", lambda config: mock_client(
        config, lambda request: httpx.Response(200, json={"data": {"products": [product(1)]}})))
    assert cli.run_cycle(cfg, store, phrase="постельное бельё") == (1, 0, 0)
    with pytest.raises(ValueError, match="WB_DESTINATION"):
        cli.run_cycle(replace(cfg, destination=5678), store, phrase="постельное бельё", force=True)
    assert len(store.recent_runs()) == 1


def test_invalid_and_missing_env_settings(monkeypatch):
    monkeypatch.setenv("WB_SEARCH_URL", "http://unsafe.local/search")
    with pytest.raises(ValueError, match="HTTPS"):
        Settings.from_env()
    monkeypatch.setenv("WB_SEARCH_URL", "https://example.test")
    monkeypatch.setenv("WB_TOP_N", "-1")
    with pytest.raises(ValueError, match="WB_TOP_N"):
        Settings.from_env()
