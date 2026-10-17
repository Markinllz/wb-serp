"""Data structures free of DB, HTTP, and UI dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

RunStatus = Literal["running", "success", "partial", "failed", "blocked", "interrupted"]


@dataclass(frozen=True, slots=True)
class Product:
    nm_id: int
    position: int
    price_rub: Decimal | None
    rating: float | None
    reviews: int | None
    delivery_days: int | None
    seller: str | None
    title: str | None


@dataclass(frozen=True, slots=True)
class SearchPage:
    products: tuple[dict, ...]
    page_number: int
    total: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionResult:
    query: str
    run_id: str
    status: RunStatus
    observed: int
    requested: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None
