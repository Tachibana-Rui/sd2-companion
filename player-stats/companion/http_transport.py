"""Connect to the official API directly, with the configured proxy as fallback.

Windows desktop launches can inherit a stale development proxy even when the
game and browser work. Only a connection failure may retry the same HTTPS
request through the configured proxy; HTTP responses and TLS verification keep
their normal meaning. No proxy setting or credential is logged or modified.
"""
from __future__ import annotations

import http.client
import socket
import ssl
import time
from urllib import error, request
from urllib.parse import urlsplit


OFFICIAL_HOST = "sd2.rui-blog.link"


class OfficialHTTPSConnection(http.client.HTTPSConnection):
    """Try each DNS answer through TLS, retaining the original hostname.

    socket.create_connection retries TCP failures, but a Cloudflare edge can
    accept TCP and then stall the TLS handshake. Retrying DNS candidates here
    covers that case without pinning addresses or weakening certificate checks.
    """
    def connect(self):
        if (self.host.casefold() != OFFICIAL_HOST or self.port != 443
                or self._tunnel_host or not isinstance(self.timeout, (float, int))):
            return super().connect()
        original_timeout, original_connect = self.timeout, self._create_connection
        deadline = time.monotonic() + original_timeout
        addresses = list(dict.fromkeys(socket.getaddrinfo(
            self.host, self.port, type=socket.SOCK_STREAM)))
        last_error = None
        try:
            for index, address in enumerate(addresses):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTPS connection budget exhausted")
                self.timeout = remaining / (len(addresses) - index)
                address_deadline = time.monotonic() + self.timeout
                self._create_connection = self._connect_address(address, address_deadline)
                try:
                    super().connect()
                except ssl.SSLCertVerificationError:
                    self._close_socket()
                    raise
                except (OSError, http.client.HTTPException) as exc:
                    self._close_socket()
                    last_error = exc
                else:
                    self.sock.settimeout(original_timeout)
                    return
            if last_error is not None:
                raise last_error
            raise OSError("No HTTPS addresses resolved")
        finally:
            self.timeout, self._create_connection = original_timeout, original_connect

    def _close_socket(self):
        # HTTPConnection.close() also resets its request state. A connect retry
        # must preserve the request already being sent by urllib.
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    @staticmethod
    def _connect_address(address, deadline):
        def connect(unused_address, timeout, source_address):
            family, kind, protocol, _, endpoint = address
            stream = socket.socket(family, kind, protocol)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTPS connection budget exhausted")
                stream.settimeout(remaining)
                if source_address:
                    stream.bind(source_address)
                stream.connect(endpoint)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTPS connection budget exhausted")
                # TCP and TLS share this candidate's budget.
                stream.settimeout(remaining)
                return stream
            except BaseException:
                stream.close()
                raise
        return connect


class OfficialHTTPSHandler(request.HTTPSHandler):
    def https_open(self, outgoing):
        return self.do_open(OfficialHTTPSConnection, outgoing, context=self._context)


class ServiceOpener:
    def __init__(self, redirect_handler):
        proxies = request.getproxies()
        self._has_https_proxy = bool(proxies.get("https"))
        self._primary = request.build_opener(request.ProxyHandler(proxies), redirect_handler)
        self._direct = request.build_opener(request.ProxyHandler({}), redirect_handler,
                                            OfficialHTTPSHandler())

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
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                raise
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise
            return self._primary.open(retry_request, timeout=remaining)
