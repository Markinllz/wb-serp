"""Defensive parsing: unknown values are None; prices come from explicit fields."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from wb_serp.models import Product

MOSCOW = ZoneInfo("Europe/Moscow")


class SchemaError(ValueError):
    """Search response cannot substantiate its claimed absolute rank."""


def extract_products(payload: Any) -> tuple[dict, ...]:
    if not isinstance(payload, dict):
        raise SchemaError("Expected JSON object")
    data = payload.get("data", payload)
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise SchemaError("Missing data.products array")
    products = data["products"]
    if any(not isinstance(p, dict) for p in products):
        raise SchemaError("Product must be a JSON object")
    return tuple(products)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _price(item: Mapping[str, Any]) -> Decimal | None:
    # 'sizes[*].price.product' and 'salePriceU' are in kopecks in known payloads.
    sizes = item.get("sizes")
    if isinstance(sizes, list):
        for size in sizes:
            if isinstance(size, dict) and isinstance(size.get("price"), dict):
                kopecks = _decimal(size["price"].get("product"))
                if kopecks is not None and 0 <= kopecks < 2**63:
                    try:
                        return (kopecks / 100).quantize(Decimal("0.01"))
                    except InvalidOperation:
                        continue
    kopecks = _decimal(item.get("salePriceU"))
    if kopecks is not None and 0 <= kopecks < 2**63:
        try:
            return (kopecks / 100).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    return None


def _card_price(item: Mapping[str, Any]) -> tuple[Decimal | None, str | None]:
    raw = item.get('_wb_card_price')
    if not isinstance(raw, dict) or raw.get('kind') not in ('wallet', 'displayed'):
        return None, None
    kopecks = raw.get('kopecks')
    if type(kopecks) is not int or not 0 <= kopecks < 2**63:
        return None, None
    return (Decimal(kopecks) / 100).quantize(Decimal('0.01')), raw['kind']


def _delivery(item: Mapping[str, Any], collected_at: datetime) -> int | None:
    # Explicit date, or the date obtained from WB's frontend delivery display.
    raw = item.get("deliveryDate", item.get("_wb_delivery_date"))
    if not isinstance(raw, str):
        return None
    try:
        days = (date.fromisoformat(raw[:10]) - collected_at.astimezone(MOSCOW).date()).days
        return days if days >= 0 else None
    except ValueError:
        return None


def parse_product(item: Mapping[str, Any], position: int, at: datetime) -> Product:
    nm_id = item.get("id", item.get("nmId"))
    if isinstance(nm_id, bool) or not isinstance(nm_id, int) or not 0 < nm_id <= 2**63 - 1:
        raise SchemaError(f"Invalid nm_id at rank {position}")
    rating = _decimal(item.get("reviewRating", item.get("rating")))
    review_count = item.get("feedbacks", item.get("nmFeedbacks"))
    seller = item.get("supplier", item.get("supplierName"))
    title = item.get("name")
    card_price, card_kind = _card_price(item)
    return Product(
        nm_id=nm_id,
        position=position,
        price_rub=_price(item),
        card_price_rub=card_price, card_price_kind=card_kind,
        rating=float(rating) if rating is not None and 0 <= rating <= 5 else None,
        reviews=review_count if type(review_count) is int and review_count >= 0 else None,
        delivery_days=_delivery(item, at),
        seller=seller.strip() if isinstance(seller, str) and seller.strip() else None,
        title=title.strip() if isinstance(title, str) and title.strip() else None,
    )
