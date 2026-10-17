from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from wb_serp.card_prices import displayed_kopecks
from wb_serp.models import Product
from wb_serp.parsing import parse_product
from wb_serp.store import Store
from wb_serp.charts import chart_product_history


@pytest.mark.parametrize(
    "text,amount",
    [
        ("1\u00a0982\u00a0₽", 198200),
        ("1 982,50 ₽", 198250),
        ("0 ₽", 0),
        ("12.5 ₽", 1250),
        ("от 100 ₽", None),
        ("2 044 ₽ 1 982 ₽", None),
        ("", None),
        (None, None),
    ],
)
def test_only_printed_ruble_amounts_are_parsed(text, amount):
    assert displayed_kopecks(text) == amount


def test_raw_price_does_not_become_wallet_price_without_card_observation():
    at = datetime.now(timezone.utc)
    raw = {"id": 1, "sizes": [{"price": {"product": 204400, "wallet": 3}}]}
    old = parse_product(raw, 1, at)
    assert old.price_rub == Decimal("2044.00") and old.card_price_rub is None
    new = parse_product({**raw, "_wb_card_price": {"kopecks": 198200, "kind": "wallet"}}, 1, at)
    assert new.price_rub == Decimal("2044.00") and new.card_price_rub == Decimal("1982.00")
    assert new.card_price_kind == "wallet"
    for invalid in (
        {"kopecks": True, "kind": "wallet"},
        {"kopecks": 198200, "kind": "guess"},
        {"kopecks": -1, "kind": "wallet"},
    ):
        assert parse_product({**raw, "_wb_card_price": invalid}, 1, at).card_price_rub is None


def test_card_prices_are_joined_by_id_without_region_or_query_leak(tmp_path):
    from wb_serp.browser_transport import BrowserTransport

    t = BrowserTransport(profile_path=tmp_path)
    t._renders_cards = True
    t._current_query = t._rendered_query = "first"
    t._rendered_destination = "99"
    t._page = SimpleNamespace(
        evaluate=lambda script: [
            {"nm_id": 7, "text": "1 982 ₽", "kind": "wallet"},
            {"nm_id": 3, "text": "100 ₽", "kind": "displayed"},
        ]
    )
    body = json.dumps({"products": [{"id": 3}, {"id": 7}]}).encode()
    result = json.loads(t._with_card_prices(body, "99"))
    assert [p["id"] for p in result["products"]] == [3, 7]
    assert result["products"][1]["_wb_card_price"]["kopecks"] == 198200
    assert t._with_card_prices(body, "100") == body
    t._current_query = "second"
    assert t._with_card_prices(body, "99") == body


def test_schema5_migration_preserves_every_old_value_and_keeps_card_history_empty(tmp_path):
    store = Store(tmp_path / "prices.db")
    store.initialize()
    store.sync_queries(["query"])
    at = datetime.now(timezone.utc)
    p = Product(7, 1, Decimal("2044"), None, None, None, None, "Name")
    store.start_run(
        run_id="old",
        query_id=1,
        scheduled_for=at.isoformat(),
        started_at=at,
        requested_top_n=1,
        destination=99,
    )
    store.complete_run(run_id="old", products=[p], status="success", finished_at=at)
    with store.connect() as db:
        db.execute("ALTER TABLE observations DROP COLUMN card_price_kind")
        db.execute("ALTER TABLE observations DROP COLUMN card_price_kopecks")
        db.execute("PRAGMA user_version=5")
        before = [tuple(r) for r in db.execute("SELECT * FROM observations")]
    store.initialize()
    store.initialize()
    with store.connect() as db:
        assert [tuple(r)[: len(before[0])] for r in db.execute("SELECT * FROM observations")] == before
    old = store.latest_observations(1)[0]
    assert old["price_kopecks"] == 204400 and old["card_price_kopecks"] is None
    new = replace(p, card_price_rub=Decimal("1982"), card_price_kind="wallet")
    store.start_run(
        run_id="new",
        query_id=1,
        scheduled_for=at.isoformat(),
        started_at=at,
        requested_top_n=1,
        destination=99,
    )
    store.complete_run(run_id="new", products=[new], status="success", finished_at=at)
    history = store.rank_history(1, [7])
    assert [r["card_price_kopecks"] for r in history] == [None, 198200]
    figure = chart_product_history(history, [7], metric="price", price_field="card_price_kopecks")
    assert list(figure.data[0].y) == [None, 1982]
    assert figure.data[0].connectgaps is False


def test_browser_reloads_cards_for_each_query_and_new_snapshot(tmp_path, monkeypatch):
    import base64
    import httpx
    import sys
    from types import ModuleType
    fake = ModuleType("playwright.sync_api")
    fake.Error = RuntimeError
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    from wb_serp.cdp_transport import CdpBrowserTransport
    transport = CdpBrowserTransport(profile_path=tmp_path)
    warmed = []
    def warmup():
        warmed.append(transport._current_query)
        transport._ready = True
        transport._rendered_query = transport._current_query
        transport._rendered_destination = '99'
        transport._card_snapshot_open = False
        return True
    monkeypatch.setattr(transport, '_start', lambda: None)
    monkeypatch.setattr(transport, '_warmup', warmup)
    monkeypatch.setattr(transport, '_with_delivery', lambda body: body)
    monkeypatch.setattr(transport, '_with_card_prices', lambda body, destination: body)
    monkeypatch.setattr(transport, '_fetch_frontend_page', lambda request: {
        'status':200,'headers':[],'body':base64.b64encode(b'{"products":[]}').decode()})
    for query, page in [('first',1),('first',2),('second',1),('second',1)]:
        request=httpx.Request('GET', 'https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search',
                              params={'query':query,'page':page,'dest':99})
        transport.handle_request(request)
    assert warmed == ['first','second','second']
