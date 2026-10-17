"""Non-mutating live search probe: validate 200 unique ranked WB products.

Usage: python -m scripts.probe_live_wb [--query PHRASE]

This checks the same HTTP adapter and product mapper as the application. It
NEVER writes to the observations database. Success requires 200 unique cards;
all missing optional attributes are reported as missing, not fabricated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from wb_serp.client import FetchError, WildberriesClient
from wb_serp.parsing import SchemaError, parse_product
from wb_serp.browser_session import load_session
from wb_serp.config import Settings

ENDPOINTS = (
    "https://u-search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
)


class ProbeFailure(RuntimeError):
    """Insufficient evidence of an accurate top-N search result."""

    def __init__(self, message: str, *, observed: int = 0, page_sizes: list[int] | None = None,
                 cause: str | None = None, reported_total: int | None = None):
        super().__init__(message)
        self.observed = observed
        self.page_sizes = page_sizes or []
        self.cause = cause
        self.reported_total = reported_total


def probe(
    client: WildberriesClient, *, query: str, top_n: int = 200, page_limit: int = 10,
) -> dict[str, Any]:
    if top_n < 1 or page_limit < 1:
        raise ValueError("top_n and page_limit must be positive")
    started_at = datetime.now(timezone.utc)
    seen: set[int] = set()
    products = []
    page_sizes: list[int] = []
    for page_number in range(1, page_limit + 1):
        page = client.page(query, page_number)
        page_sizes.append(len(page.products))
        if not page.products:
            raise ProbeFailure(f"Page {page_number} is empty; only {len(products)}/{top_n} products",
                               observed=len(products), page_sizes=page_sizes)
        for item in page.products:
            if len(products) == top_n:
                break
            product = parse_product(item, len(products) + 1, started_at)
            if product.nm_id in seen:
                raise ProbeFailure(
                    f"Duplicate nm_id={product.nm_id} on page {page_number}; pagination may loop",
                    cause='duplicate_nm_id',
                )
            seen.add(product.nm_id)
            products.append(product)
        if len(products) == top_n:
            break
        if page.total is not None and len(products) == page.total:
            raise ProbeFailure(f'WB reports total={page.total}; only {len(products)}/{top_n} products',
                               observed=len(products), page_sizes=page_sizes,
                               cause='source_exhausted', reported_total=page.total)
    if len(products) != top_n:
        raise ProbeFailure(f"Insufficient products: {len(products)}/{top_n}; page_limit={page_limit}",
                           observed=len(products), page_sizes=page_sizes)

    attributes = ("price_rub", "rating", "reviews", "delivery_days", "seller")
    populated = {field: sum(getattr(p, field) is not None for p in products) for field in attributes}
    return {
        "status": "PASS",
        "query": query,
        "collected_at_utc": started_at.isoformat(timespec="seconds"),
        "requested_top_n": top_n,
        "pages_requested": len(page_sizes),
        "page_sizes": page_sizes,
        "reported_total": page.total,
        "unique_nm_ids": len(seen),
        "nm_ids_in_rank_order": [p.nm_id for p in products],
        "sequential_rank_range": [products[0].position, products[-1].position],
        "non_null_attribute_counts": populated,
        "sample_products": [
            {
                "nm_id": p.nm_id,
                "position": p.position,
                "title": p.title,
                "price_rub": str(p.price_rub) if p.price_rub is not None else None,
                "rating": p.rating,
                "reviews": p.reviews,
                "delivery_days": p.delivery_days,
                "seller": p.seller,
            }
            for p in products[:3]
        ],
        "qualification": "Region and buyer price must still be checked in the WB browser.",
    }


def main(argv: list[str] | None = None) -> int:
    cfg = Settings.from_env()
    parser = argparse.ArgumentParser(description="Check actual WB top-N without modifying SQLite")
    parser.add_argument("--query", default="постельное белье 2 спальное")
    parser.add_argument("--endpoint", action="append",
                        help="Override the default two HTTPS endpoints (repeatable)")
    parser.add_argument("--dest", type=int, default=cfg.destination)
    parser.add_argument('--transport', choices=('httpx', 'curl_cffi', 'browser'), default=cfg.http_transport)
    parser.add_argument('--session-file', type=Path, default=cfg.session_file)
    parser.add_argument("--top-n", type=int, default=200)
    parser.add_argument("--page-limit", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=cfg.request_timeout_seconds)
    parser.add_argument("--delay", type=float, default=cfg.request_delay_seconds)
    parser.add_argument('--retries', type=int, default=cfg.request_retries)
    args = parser.parse_args(argv)
    if args.top_n < 1 or args.page_limit < 1 or args.timeout <= 0 or args.delay < 0:
        parser.error("top-n, page-limit, timeout must be positive; delay must be nonnegative")
    if not 1 <= args.retries <= 8:
        parser.error('retries must be 1..8 total attempts')
    try:
        session = load_session(args.session_file) if args.session_file else None
    except ValueError as exc:
        parser.error(str(exc))
    endpoints = args.endpoint or ([cfg.search_url] if args.transport == 'browser' else
                                  [session.endpoint] if session else
                                  list(dict.fromkeys([cfg.search_url, *ENDPOINTS])))
    if any(not endpoint.startswith("https://") for endpoint in endpoints):
        parser.error("Each endpoint must start with https://")
    results = []
    for endpoint in endpoints:
        try:
            with WildberriesClient(
                search_url=endpoint, destination=args.dest, retries=args.retries,
                timeout=args.timeout, delay_seconds=args.delay,
                http_transport=args.transport, session_file=args.session_file,
                retry_base_seconds=cfg.retry_base_seconds, retry_max_seconds=cfg.retry_max_seconds,
                retry_jitter_seconds=cfg.retry_jitter_seconds,
                browser_profile_path=cfg.browser_profile_path,
                browser_timeout_seconds=cfg.browser_timeout_seconds,
                browser_engine=cfg.browser_engine,
            ) as client:
                result = probe(client, query=args.query, top_n=args.top_n,
                               page_limit=args.page_limit)
        except (FetchError, SchemaError, ProbeFailure, ValueError) as exc:
            result = {"status": "FAIL", "error_type": type(exc).__name__, "reason": str(exc)}
            if isinstance(exc, FetchError):
                result.update(http_status=exc.http_status, cause=exc.cause,
                              response_headers=exc.response_headers)
        results.append({"endpoint": endpoint, "dest": args.dest, **result})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if any(r["status"] == "PASS" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
