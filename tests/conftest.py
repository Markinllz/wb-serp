from __future__ import annotations

import pytest

from wb_serp.store import Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "serp.db")
    result.initialize()
    result.sync_queries(["постельное бельё", "пастельный комплект 1,5"])
    return result


def product(nm: int, *, price: int | None = 152350, delivery: str | None = None) -> dict:
    result = {"id": nm, "name": f"Товар {nm}", "reviewRating": 4.9,
              "feedbacks": 56, "supplier": "Продавец", "sizes": []}
    if price is not None:
        result["sizes"] = [{"price": {"product": price}}]
    if delivery is not None:
        result["deliveryDate"] = delivery
    return result
