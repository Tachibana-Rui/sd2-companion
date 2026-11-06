"""Connect to the official API directly, with the configured proxy as fallback.

Windows desktop launches can inherit a stale development proxy even when the
game and browser work. Only a connection failure may retry the same HTTPS
request through the configured proxy; HTTP responses and TLS verification keep
their normal meaning. No proxy setting or credential is logged or modified.
"""
from __future__ import annotations

import http.client
import time
from urllib import error, request
from urllib.parse import urlsplit


OFFICIAL_HOST = "sd2.rui-blog.link"


class ServiceOpener:
    def __init__(self, redirect_handler):
        proxies = request.getproxies()
        self._has_https_proxy = bool(proxies.get("https"))
        self._primary = request.build_opener(request.ProxyHandler(proxies), redirect_handler)
        self._direct = request.build_opener(request.ProxyHandler({}), redirect_handler)

    def open(self, outgoing, *, timeout):
        parts = urlsplit(outgoing.full_url)
        official = (parts.scheme == "https" and parts.hostname == OFFICIAL_HOST
                    and parts.port in (None, 443))
        if not official:
            return self._primary.open(outgoing, timeout=timeout)
        retry_proxy = self._has_https_proxy and not request.proxy_bypass(OFFICIAL_HOST)
        if not retry_proxy:
            return self._direct.open(outgoing, timeout=timeout)
        # ProxyHandler mutates its Request. Keep retries independent so a
        # previous route cannot carry a tunnel or proxy credential into another.
        retry_request = request.Request(outgoing.full_url, data=outgoing.data,
            headers=dict(outgoing.header_items()), origin_req_host=outgoing.origin_req_host,
            unverifiable=outgoing.unverifiable, method=outgoing.get_method())
        started = time.monotonic()
        # Reserve at least half the caller's connection budget for the retry.
        direct_timeout = min(5.0, timeout / 2)
        try:
            return self._direct.open(outgoing, timeout=direct_timeout)
        except error.HTTPError:
            # A real response must never be evaded using a different route.
            raise
        except (OSError, http.client.HTTPException):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise
            return self._primary.open(retry_request, timeout=remaining)
