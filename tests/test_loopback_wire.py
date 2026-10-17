"""Actual TCP/HTTP wire-level test against a loopback fixture server.

This is NOT a request to Wildberries; only verifies the HTTP transport, URL
encoding, JSON parsing, and error status detection over a real local socket.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo
import json
import gzip
import zlib

import pytest

from wb_serp.client import FetchError, WildberriesClient
from wb_serp.parsing import parse_product
from scripts.probe_live_wb import probe


class Handler(BaseHTTPRequestHandler):
    calls = []
    response_status = 200

    def do_GET(self):
        route = urlsplit(self.path)
        self.__class__.calls.append((route.path, parse_qs(route.query)))
        if self.response_status == 200:
            payload = json.dumps({"data": {"products": [{"id": 121, "name": "Тест"}]}},
                                 ensure_ascii=False).encode("utf-8")
        else:
            payload = b'{"error": "request blocked"}'
        self.send_response(self.response_status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


@pytest.mark.parametrize('http_status,expected_blocked', [(200, False), (403, True), (429, True)])
def test_real_local_http_requests(http_status, expected_blocked):
    class LocalHandler(Handler):
        calls = []
        response_status = http_status

    server = ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with WildberriesClient(search_url=f'http://127.0.0.1:{server.server_port}/search',
                               destination=-123, delay_seconds=0, retries=1, timeout=2) as client:
            if expected_blocked:
                with pytest.raises(FetchError) as error:
                    client.page('постельное бельё 1,5', 1)
                assert error.value.blocked is True
            else:
                page = client.page('постельное бельё 1,5', 1)
                assert page.products[0]['id'] == 121
        assert LocalHandler.calls[0][0] == '/search'
        assert LocalHandler.calls[0][1]['query'] == ['постельное бельё 1,5']
        assert LocalHandler.calls[0][1]['dest'] == ['-123']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('transport,encoding', [
    ('httpx', None), ('curl_cffi', None), ('curl_cffi', 'gzip'),
    ('curl_cffi', 'deflate'), ('curl_cffi', 'br'), ('curl_cffi', 'zstd'),
])
def test_real_local_http_top_200_and_all_product_attributes(nested, transport, encoding):
    """Two real HTTP pages through the production adapter; all data is synthetic."""
    at = datetime.now(ZoneInfo('Europe/Moscow'))
    delivery_date = (at.date() + timedelta(days=3)).isoformat()
    phrase = 'постельное бельё 1,5'

    class PagedHandler(BaseHTTPRequestHandler):
        calls = []

        def do_GET(self):
            params = parse_qs(urlsplit(self.path).query)
            self.calls.append(params)
            page = int(params['page'][0])
            products = [
                {'id': 100000 + i, 'name': f'Тест {i}', 'reviewRating': 4.8,
                 'feedbacks': i, 'supplier': 'Тестовый продавец',
                 'deliveryDate': delivery_date,
                 'sizes': [{'price': {'product': 123450 + i * 100}}]}
                for i in range((page - 1) * 100 + 1, page * 100 + 1)
            ]
            body = {'products': products}
            payload = json.dumps({'data': body} if nested else body).encode('utf-8')
            if encoding == 'gzip':
                payload = gzip.compress(payload)
            elif encoding == 'deflate':
                payload = zlib.compress(payload)
            elif encoding in ('br', 'zstd'):
                # pyarrow is already a Streamlit dependency; use its native codecs
                # to produce real compressed wire bytes, not a mocked decoder.
                import pyarrow
                codec = 'brotli' if encoding == 'br' else encoding
                payload = pyarrow.compress(payload, codec=codec).to_pybytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            if encoding:
                self.send_header('Content-Encoding', encoding)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    class RecordingClient(WildberriesClient):
        def page(self, query, page_number):
            page = super().page(query, page_number)
            received.extend(page.products)
            return page

    received = []
    responses = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), PagedHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with RecordingClient(search_url=f'http://127.0.0.1:{server.server_port}/search',
                             destination=-1257786, retries=1, delay_seconds=0,
                             timeout=2, http_transport=transport) as client:
            client._client.event_hooks['response'].append(responses.append)
            result = probe(client, query=phrase)
        assert result['status'] == 'PASS'
        assert result['unique_nm_ids'] == 200
        assert result['sequential_rank_range'] == [1, 200]
        assert result['page_sizes'] == [100, 100]
        assert all(count == 200 for count in result['non_null_attribute_counts'].values())
        assert [call['page'] for call in PagedHandler.calls] == [['1'], ['2']]
        assert all(call['query'] == [phrase] and call['dest'] == ['-1257786']
                   for call in PagedHandler.calls)
        assert len(received) == 200
        if transport == 'curl_cffi':
            for response in responses:
                assert 'content-encoding' not in response.headers
                assert 'transfer-encoding' not in response.headers
                assert int(response.headers['content-length']) == len(response.content)
        for position, raw in enumerate(received, 1):
            product = parse_product(raw, position, at)
            assert product.nm_id == 100000 + position
            assert product.position == position
            assert product.price_rub == Decimal('1234.50') + position
            assert product.rating == 4.8
            assert product.reviews == position
            assert product.delivery_days == 3
            assert product.seller == 'Тестовый продавец'
            assert product.title == f'Тест {position}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('transport', ['httpx', 'curl_cffi'])
def test_corrupt_compressed_response_is_fetch_failure_not_unhandled_traceback(transport):
    class CorruptHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'not-a-gzip-stream'
            self.send_response(200)
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), CorruptHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with WildberriesClient(search_url=f'http://127.0.0.1:{server.server_port}/search',
                               destination=-1257786, retries=1, delay_seconds=0,
                               timeout=2, http_transport=transport) as client:
            with pytest.raises(FetchError):
                client.page('test', 1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
