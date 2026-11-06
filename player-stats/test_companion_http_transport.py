"""Proxy failure recovery preserves HTTP decisions and a bounded HTTPS budget."""
import http.client
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

from companion.http_transport import ServiceOpener


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


if __name__ == "__main__":
    unittest.main()
