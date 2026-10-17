"""Optional curl_cffi transport behind the existing HTTPX client contract."""
from __future__ import annotations

import httpx


class CurlTransport(httpx.BaseTransport):
    def __init__(self, *, user_agent: str = '') -> None:
        try:
            from curl_cffi.requests import Session
        except ImportError:
            raise ValueError('curl_cffi is not installed; rebuild the application image') from None
        safari = ('Safari/' in user_agent and 'Version/' in user_agent
                  and not any(name in user_agent for name in ('Chrome/', 'Chromium/', 'Edg/')))
        profile = ('safari_ios' if any(device in user_agent for device in ('iPhone', 'iPad'))
                   else 'safari') if safari else 'chrome'
        self.session = Session(impersonate=profile)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        timeouts = request.extensions.get('timeout', {})
        timeout = max((v for v in timeouts.values() if v is not None), default=20)
        headers = dict(request.headers)
        if headers.get('user-agent', '').startswith('WBSerpResearch/'):
            # Let curl_cffi use the User-Agent matching its Chrome TLS profile.
            headers.pop('user-agent')
        try:
            response = self.session.request(
                request.method, str(request.url), headers=headers,
                timeout=timeout, allow_redirects=False,
            )
        except Exception as exc:
            # Never propagate a cURL command or session headers into application logs.
            raise httpx.TransportError(
                f'curl_cffi network error ({type(exc).__name__})', request=request,
            ) from None
        # curl_cffi has already decompressed the buffered body. Forwarding the
        # wire encoding would make HTTPX decompress it again; wire byte counts
        # and chunked framing also no longer describe this representation.
        decoded_headers = [(name, value) for name, value in response.headers.multi_items()
                           if name.lower() not in {
                               'content-encoding', 'content-length', 'transfer-encoding',
                           }]
        return httpx.Response(response.status_code, headers=decoded_headers,
                              content=response.content, request=request)

    def close(self) -> None:
        self.session.close()
