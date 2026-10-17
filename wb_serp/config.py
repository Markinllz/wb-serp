"""Validated application configuration (one VPS, one SQLite file)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEARCH_URL = "https://u-search.wb.ru/exactmatch/ru/common/v18/search"


def _integer(name: str, default: int, *, minimum: int = 1) -> int:
    result = int(os.environ.get(name, default))
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def _float(name: str, default: float, *, minimum: float = 0) -> float:
    result = float(os.environ.get(name, default))
    if not minimum <= result < float("inf"):
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    queries_path: Path
    search_url: str
    destination: int
    top_n: int
    request_delay_seconds: float
    request_timeout_seconds: float
    request_retries: int
    page_limit: int
    collection_period_seconds: int
    run_on_startup: bool
    http_transport: str = 'httpx'
    session_file: Path | None = None
    query_delay_seconds: float = 5
    retry_base_seconds: float = 5
    retry_max_seconds: float = 60
    retry_jitter_seconds: float = 1
    browser_profile_path: Path = Path('/app/data/browser-profile')
    browser_timeout_seconds: float = 45
    browser_engine: str = 'playwright'

    @classmethod
    def from_env(cls) -> Settings:
        endpoint = os.environ.get("WB_SEARCH_URL", DEFAULT_SEARCH_URL)
        if not endpoint.startswith("https://"):
            raise ValueError("WB_SEARCH_URL must be an HTTPS URL")
        transport = os.environ.get('WB_HTTP_TRANSPORT', 'httpx')
        if transport not in ('httpx', 'curl_cffi', 'browser'):
            raise ValueError('WB_HTTP_TRANSPORT must be httpx, curl_cffi or browser')
        session_file = os.environ.get('WB_SESSION_FILE', '')
        browser_engine = os.environ.get('WB_BROWSER_ENGINE', 'playwright')
        if browser_engine not in ('playwright', 'cdp'):
            raise ValueError('WB_BROWSER_ENGINE must be playwright or cdp')
        retries = _integer('WB_REQUEST_RETRIES', 3)
        if retries > 8:
            raise ValueError('WB_REQUEST_RETRIES must be at most 8 total attempts')
        retry_base = _float('WB_RETRY_BASE_SECONDS', 5)
        retry_max = _float('WB_RETRY_MAX_SECONDS', 60)
        if retry_max < retry_base:
            raise ValueError('WB_RETRY_MAX_SECONDS must be at least WB_RETRY_BASE_SECONDS')
        return cls(
            database_path=Path(os.environ.get("WB_DB_PATH", str(ROOT / "data" / "serp.db"))),
            queries_path=Path(os.environ.get("WB_QUERIES_PATH", str(ROOT / "config" / "queries.txt"))),
            search_url=endpoint,
            # Destination is a search-region code, NOT a validated precise Moscow address.
            destination=int(os.environ.get("WB_DESTINATION", "-1257786")),
            top_n=_integer("WB_TOP_N", 200),
            request_delay_seconds=_float("WB_REQUEST_DELAY_SECONDS", 5),
            request_timeout_seconds=_float("WB_REQUEST_TIMEOUT_SECONDS", 20, minimum=0.1),
            request_retries=retries,
            page_limit=_integer("WB_PAGE_LIMIT", 15),
            collection_period_seconds=_integer("WB_COLLECTION_PERIOD_SECONDS", 3600, minimum=60),
            run_on_startup=os.environ.get("WB_RUN_ON_STARTUP", "true").lower() in ("true", "1", "yes"),
            http_transport=transport,
            session_file=Path(session_file) if session_file else None,
            query_delay_seconds=_float('WB_QUERY_DELAY_SECONDS', 5),
            retry_base_seconds=retry_base,
            retry_max_seconds=retry_max,
            retry_jitter_seconds=_float('WB_RETRY_JITTER_SECONDS', 1),
            browser_profile_path=Path(os.environ.get('WB_BROWSER_PROFILE_PATH',
                                                     '/app/data/browser-profile')),
            browser_timeout_seconds=_float('WB_BROWSER_TIMEOUT_SECONDS', 45, minimum=1),
            browser_engine=browser_engine,
        )
