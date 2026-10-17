from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wb_serp.parsing import SchemaError, extract_products, parse_product
from tests.conftest import product

AT = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def test_explicit_fields_and_calendar_delivery():
    value = parse_product(product(42, delivery="2026-10-18"), 1, AT)
    assert value.nm_id == 42
    assert value.position == 1
    assert value.price_rub == Decimal("1523.50")
    assert value.delivery_days == 2
    assert value.reviews == 56
    assert value.seller == "Продавец"


def test_missing_values_are_none_not_fabricated():
    value = parse_product({"id": 123, "time1": 1, "time2": 10}, 7, AT)
    assert value.position == 7
    assert value.delivery_days is None
    assert value.price_rub is None
    assert value.seller is None
    assert value.rating is None


def test_invalid_nm_id_and_schema_rejected():
    with pytest.raises(SchemaError):
        parse_product({"id": "bad"}, 1, AT)
    with pytest.raises(SchemaError):
        extract_products({"data": {"products": "not a list"}})
    with pytest.raises(SchemaError):
        extract_products({"other": []})


def test_suspicious_price_is_not_synthesized():
    value = parse_product({"id": 1, "salePriceU": -1, "rating": 6}, 1, AT)
    assert value.price_rub is None
    assert value.rating is None


def test_malformed_huge_price_and_id_do_not_crash_sqlite():
    # Upstream may return malformed numeric JSON: never let this poison persistence.
    assert parse_product({"id": 1, "salePriceU": "1E+999999"}, 1, AT).price_rub is None
    assert parse_product({"id": 1, "sizes": [{"price": {"product": "1E+999999"}}]},
                         1, AT).price_rub is None
    with pytest.raises(SchemaError, match="nm_id"):
        parse_product({"id": 2**70}, 1, AT)


def test_price_falls_back_from_invalid_size_to_item_level_field():
    value = parse_product({"id": 10, "sizes": [{"price": {"product": "1E+999999"}}],
                           "salePriceU": 12450}, 1, AT)
    assert value.price_rub == Decimal('124.50')
