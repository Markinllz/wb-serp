"""Collection use case: validate an entire rank list, then atomically persist it."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Protocol
from uuid import uuid4

from wb_serp.client import FetchError
from wb_serp.models import CollectionResult, Product, SearchPage
from wb_serp.parsing import SchemaError, parse_product
from wb_serp.store import Store

logger = logging.getLogger(__name__)


class SearchClient(Protocol):
    def page(self, query: str, page_number: int) -> SearchPage: ...


class Collector:
    def __init__(self, store: Store, client: SearchClient, *, destination: int,
                 top_n: int = 200, page_limit: int = 15) -> None:
        if top_n < 1 or page_limit < 1:
            raise ValueError("top_n and page_limit must be positive")
        self.store = store
        self.client = client
        self.destination = destination
        self.top_n = top_n
        self.page_limit = page_limit

    def collect(self, query_id: int, query: str, *, scheduled_for: str | None = None) -> CollectionResult:
        now = datetime.now(timezone.utc)
        slot = scheduled_for or now.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        run_id = str(uuid4())
        self.store.start_run(run_id=run_id, query_id=query_id, scheduled_for=slot,
                             started_at=now, requested_top_n=self.top_n, destination=self.destination)
        products: list[Product] = []
        seen: set[int] = set()
        status: str = "success"
        error: str | None = None
        try:
            for page_number in range(1, self.page_limit + 1):
                page = self.client.page(query, page_number)
                if not page.products:
                    error = f"Empty page {page_number}, top-{self.top_n} not reached"
                    status = "partial"
                    break
                for raw in page.products:
                    if len(products) >= self.top_n:
                        break
                    product = parse_product(raw, len(products) + 1, now)
                    if product.nm_id in seen:
                        # Can't silently remove duplicates: downstream ranks would shift.
                        raise SchemaError(f"Duplicate nm_id={product.nm_id} across pages")
                    seen.add(product.nm_id)
                    products.append(product)
                if len(products) >= self.top_n:
                    break
                if page.total is not None and len(products) == page.total:
                    status = 'partial'
                    error = f'WB reports total={page.total}; top-{self.top_n} not available'
                    break
            if len(products) < self.top_n and status == "success":
                status = "partial"
                error = f"Reached page_limit={self.page_limit} with only {len(products)} rows"
        except FetchError as exc:
            status = "blocked" if exc.blocked else "failed"
            error = str(exc)
        except (SchemaError, ValueError) as exc:
            status = "failed"
            error = f"Invalid SERP data: {exc}"
        except Exception as exc:
            # Record and propagate programming mistakes; don't mask them as HTTP failures.
            self.store.complete_run(run_id=run_id, products=products, status="failed",
                                    finished_at=datetime.now(timezone.utc), error=repr(exc))
            raise
        finished = datetime.now(timezone.utc)
        self.store.complete_run(run_id=run_id, products=products, status=status,
                                finished_at=finished, error=error)
        logger.info("Collection %s: status=%s, %s/%s, error=%s",
                    query, status, len(products), self.top_n, error)
        return CollectionResult(query=query, run_id=run_id, status=status, observed=len(products),
                                requested=self.top_n, started_at=now, finished_at=finished, error=error)
