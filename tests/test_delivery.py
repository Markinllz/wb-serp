from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pytest

from wb_serp.browser_transport import BrowserTransport
from wb_serp.delivery import calendar_date
from wb_serp.parsing import parse_product


@pytest.mark.parametrize('label,expected', [('Сегодня', '2026-10-17'), ('Сегодня утром', '2026-10-17'),
    ('Завтра', '2026-10-18'), ('Послезавтра', '2026-10-19'), ('20 октября', '2026-10-20'),
    ('20 окт', '2026-10-20'), ('1–4 часа', '2026-10-17'), ('За час', '2026-10-17'),
    ('Доставим позже', None), ('31 февраля', None), ('16 октября', None), (None, None)])
def test_delivery_calendar_labels(label, expected):
    assert calendar_date(label, date(2026, 10, 17)) == expected


def test_delivery_new_year_and_moscow_midnight():
    assert calendar_date('2 января', date(2026, 12, 31)) == '2027-01-02'
    at = datetime(2026, 10, 16, 21, 30, tzinfo=timezone.utc)
    assert parse_product({'id':1, '_wb_delivery_date':'2026-10-18'}, 1, at).delivery_days == 1
    assert parse_product({'id':1, 'time1':2, 'time2':24}, 1, at).delivery_days is None


def test_frontend_delivery_enrichment_preserves_order_and_unknowns(tmp_path):
    t = BrowserTransport(profile_path=tmp_path)
    t._page = SimpleNamespace(evaluate=lambda script, products: {
        'today':'2026-10-17', 'products':['Послезавтра', None, '20 октября']})
    original = {'products':[{'id':7}, {'id':3}, {'id':8}]}
    enriched = json.loads(t._with_delivery(json.dumps(original).encode()))
    assert [p['id'] for p in enriched['products']] == [7,3,8]
    assert enriched['products'][0]['_wb_delivery_date'] == '2026-10-19'
    assert '_wb_delivery_date' not in enriched['products'][1]
    t._page.evaluate = lambda *args: None
    body = json.dumps(original).encode()
    assert t._with_delivery(body) == body
