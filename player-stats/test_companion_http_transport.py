"""Proxy failure recovery preserves HTTP decisions and a bounded HTTPS budget."""
import http.client
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

from companion.http_transport import OfficialHTTPSConnection, ServiceOpener


OFFICIAL = "https://sd2.rui-blog.link/api/companion/v3/rooms"


class TransportTests(unittest.TestCase):
    def transport(self, *, proxies=None, bypass=False):
        primary, direct = Mock(), Mock()
        with patch("companion.http_transport.request.getproxies", return_value=
                   {"https": "http://proxy.invalid:8000"} if proxies is None else proxies), \
                patch("companion.http_transport.request.build_opener", side_effect=[primary, direct]), \
                patch("companion.http_transport.request.proxy_bypass", return_value=bypass):
            opener = ServiceOpener(request.HTTPRedirectHandler)
        return opener, primary, direct

    def test_failed_direct_connection_retries_identical_request_through_proxy_with_remaining_budget(self):
        opener, primary, direct = self.transport()
        direct.open.side_effect = error.URLError(TimeoutError())
        outgoing = request.Request(OFFICIAL, headers={"Authorization": "Bearer fixture"})
        with patch("companion.http_transport.request.proxy_bypass", return_value=False), \
                patch("companion.http_transport.time.monotonic", side_effect=[100.0, 102.0]):
            self.assertIs(opener.open(outgoing, timeout=6), primary.open.return_value)
        direct.open.assert_called_once_with(outgoing, timeout=3)
        self.assertEqual(primary.open.call_count, 1)
        retried = primary.open.call_args.args[0]
        self.assertEqual(retried.full_url, outgoing.full_url)
        self.assertEqual(retried.headers, outgoing.headers)
        self.assertEqual(primary.open.call_args.kwargs["timeout"], 4)

    def test_request_mutation_cannot_change_retry_target_payload_or_credentials(self):
        opener, primary, direct = self.transport()
        outgoing = request.Request(OFFICIAL, data=b'{"id":"same-request"}', method="POST",
                                   headers={"Authorization": "Bearer fixture"})
        def mutate_then_fail(value, **kwargs):
            value.set_proxy("proxy.invalid:8000", "http")
            value.add_header("Proxy-Authorization", "private-proxy-credential")
            raise error.URLError(TimeoutError())
        direct.open.side_effect = mutate_then_fail
        with patch("companion.http_transport.request.proxy_bypass", return_value=False):
            opener.open(outgoing, timeout=6)
        retried = primary.open.call_args.args[0]
        self.assertEqual(outgoing.host, "proxy.invalid:8000")
        self.assertEqual(retried.host, "sd2.rui-blog.link")
        self.assertIsNone(retried._tunnel_host)
        self.assertEqual(retried.full_url, OFFICIAL)
        self.assertEqual(retried.data, b'{"id":"same-request"}')
        self.assertEqual(retried.get_method(), "POST")
        self.assertEqual(retried.get_header("Authorization"), "Bearer fixture")
        self.assertIsNone(retried.get_header("Proxy-authorization"))

    def test_direct_success_is_not_affected_by_a_broken_environment_proxy(self):
        opener, primary, direct = self.transport()
        outgoing = request.Request(OFFICIAL)
        with patch("companion.http_transport.request.proxy_bypass", return_value=False):
            self.assertIs(opener.open(outgoing, timeout=6), direct.open.return_value)
        primary.open.assert_not_called()

    def test_http_responses_including_challenges_redirects_and_auth_errors_never_retry(self):
        for code in (301, 302, 401, 403, 429, 503):
            with self.subTest(code=code):
                opener, primary, direct = self.transport()
                failure = error.HTTPError(OFFICIAL, code, "controlled fixture", {}, None)
                direct.open.side_effect = failure
                with patch("companion.http_transport.request.proxy_bypass", return_value=False), \
                        self.assertRaises(error.HTTPError) as caught:
                    opener.open(request.Request(OFFICIAL), timeout=6)
                self.assertIs(caught.exception, failure)
                primary.open.assert_not_called()

    def test_connection_failures_reserve_retry_budget(self):
        for failure in (OSError(), TimeoutError(), http.client.RemoteDisconnected()):
            with self.subTest(kind=type(failure).__name__):
                opener, primary, direct = self.transport()
                direct.open.side_effect = failure
                with patch("companion.http_transport.request.proxy_bypass", return_value=False):
                    opener.open(request.Request(OFFICIAL), timeout=20)
                self.assertEqual(direct.open.call_args.kwargs["timeout"], 5)
                self.assertLessEqual(primary.open.call_args.kwargs["timeout"], 20)

    def test_certificate_verification_failure_does_not_try_a_proxy(self):
        opener, primary, direct = self.transport()
        direct.open.side_effect = error.URLError(ssl.SSLCertVerificationError())
        with patch("companion.http_transport.request.proxy_bypass", return_value=False), \
                self.assertRaises(error.URLError):
            opener.open(request.Request(OFFICIAL), timeout=6)
        primary.open.assert_not_called()

    def test_expired_budget_and_direct_failure_do_not_create_retry_loops(self):
        opener, primary, direct = self.transport()
        direct.open.side_effect = TimeoutError()
        with patch("companion.http_transport.request.proxy_bypass", return_value=False), \
                patch("companion.http_transport.time.monotonic", side_effect=[100.0, 107.0]), \
                self.assertRaises(TimeoutError):
            opener.open(request.Request(OFFICIAL), timeout=6)
        primary.open.assert_not_called()
        primary.open.side_effect = error.URLError(OSError())
        with patch("companion.http_transport.request.proxy_bypass", return_value=False), \
                self.assertRaises(error.URLError):
            opener.open(request.Request(OFFICIAL), timeout=6)
        self.assertEqual(primary.open.call_count, 1)

    def test_custom_servers_plain_http_and_other_ports_keep_their_proxy_policy(self):
        for address in ("https://operator.example/rooms", "http://sd2.rui-blog.link/rooms",
                        "https://sd2.rui-blog.link:8443/rooms"):
            with self.subTest(address=address):
                opener, primary, direct = self.transport()
                primary.open.side_effect = OSError()
                with self.assertRaises(OSError):
                    opener.open(request.Request(address), timeout=6)
                self.assertEqual(primary.open.call_args.kwargs["timeout"], 6)
                direct.open.assert_not_called()

    def test_a_bypassed_or_unconfigured_proxy_does_not_retry_the_same_direct_route(self):
        for proxies, bypass in (({}, False), ({"https": "http://proxy.invalid"}, True)):
            with self.subTest(proxies_present=bool(proxies), bypass=bypass):
                opener, primary, direct = self.transport(proxies=proxies)
                direct.open.side_effect = OSError()
                with patch("companion.http_transport.request.proxy_bypass", return_value=bypass), \
                        self.assertRaises(OSError):
                    opener.open(request.Request(OFFICIAL), timeout=6)
                primary.open.assert_not_called()


class DNSHandshakeTests(unittest.TestCase):
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 443))]

    def test_tcp_success_tls_timeout_tries_next_dns_answer_with_same_verified_sni(self):
        clock = [100.0]
        streams = [Mock(), Mock()]
        context = ssl.create_default_context()
        connection = OfficialHTTPSConnection("sd2.rui-blog.link", timeout=6, context=context)
        # Keep the request state that urllib has already established.
        connection._HTTPConnection__state = "request-state-fixture"
        def wrap(stream, *, server_hostname):
            self.assertEqual(server_hostname, "sd2.rui-blog.link")
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            if stream is streams[0]:
                clock[0] += 3.0
                raise TimeoutError("first edge TLS stalled")
            clock[0] += 0.2
            return stream
        with patch("companion.http_transport.socket.getaddrinfo", return_value=self.addresses), \
                patch("companion.http_transport.socket.socket", side_effect=streams), \
                patch("companion.http_transport.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(context, "wrap_socket", side_effect=wrap) as tls:
            connection.connect()
        self.assertEqual(tls.call_count, 2)
        streams[0].connect.assert_called_once_with(("192.0.2.10", 443))
        streams[1].connect.assert_called_once_with(("192.0.2.20", 443))
        streams[0].close.assert_called_once()
        streams[1].close.assert_not_called()
        self.assertIs(connection.sock, streams[1])
        self.assertEqual(connection.host, "sd2.rui-blog.link")
        self.assertEqual(connection.timeout, 6)
        self.assertEqual(connection._HTTPConnection__state, "request-state-fixture")
        self.assertEqual(streams[1].settimeout.call_args.args[0], 6)

    def test_all_failed_tls_candidates_share_one_budget_and_close_their_sockets(self):
        clock = [100.0]
        streams = [Mock(), Mock()]
        context = ssl.create_default_context()
        def fail(stream, **kwargs):
            clock[0] += 3.0
            raise TimeoutError("edge TLS stalled")
        connection = OfficialHTTPSConnection("sd2.rui-blog.link", timeout=6, context=context)
        with patch("companion.http_transport.socket.getaddrinfo", return_value=self.addresses), \
                patch("companion.http_transport.socket.socket", side_effect=streams), \
                patch("companion.http_transport.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(context, "wrap_socket", side_effect=fail), \
                self.assertRaises(TimeoutError):
            connection.connect()
        self.assertEqual(clock[0], 106.0)
        for stream in streams:
            stream.close.assert_called_once()
            self.assertTrue(all(call.args[0] <= 3.0 for call in stream.settimeout.call_args_list))
        self.assertIsNone(connection.sock)
        self.assertEqual(connection.timeout, 6)

    def test_certificate_failure_stops_before_trying_another_ip(self):
        stream = Mock()
        context = ssl.create_default_context()
        connection = OfficialHTTPSConnection("sd2.rui-blog.link", timeout=6, context=context)
        with patch("companion.http_transport.socket.getaddrinfo", return_value=self.addresses), \
                patch("companion.http_transport.socket.socket", return_value=stream) as sockets, \
                patch.object(context, "wrap_socket", side_effect=ssl.SSLCertVerificationError()), \
                self.assertRaises(ssl.SSLCertVerificationError):
            connection.connect()
        self.assertEqual(sockets.call_count, 1)
        stream.close.assert_called_once()

    def test_custom_host_port_and_proxy_tunnel_use_the_standard_connection(self):
        for host, port, tunnel in (("custom.example", 443, None),
                                   ("sd2.rui-blog.link", 8443, None),
                                   ("sd2.rui-blog.link", 443, "proxy.example")):
            with self.subTest(host=host, port=port, tunnel=bool(tunnel)):
                connection = OfficialHTTPSConnection(host, port=port, timeout=6)
                connection._tunnel_host = tunnel
                with patch("http.client.HTTPSConnection.connect") as standard, \
                        patch("companion.http_transport.socket.getaddrinfo") as dns:
                    connection.connect()
                standard.assert_called_once()
                dns.assert_not_called()


if __name__ == "__main__":
    unittest.main()
