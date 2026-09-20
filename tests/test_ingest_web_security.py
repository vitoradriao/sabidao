import logging
import socket
import unittest
from unittest.mock import Mock, patch

import httpx

import config
import ingest


_PUBLIC_IPV4 = "93.184.216.34"
_PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"


def _resolver(addresses_by_host):
    def resolve(host, port, *, family, type):
        value = addresses_by_host[host]
        if isinstance(value, BaseException):
            raise value
        entries = []
        for address in value:
            address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
            socket_address = (
                (address, port, 0, 0)
                if address_family == socket.AF_INET6
                else (address, port)
            )
            entries.append(
                (address_family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", socket_address)
            )
        return entries

    return resolve


class _CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class TestWebIngestSecurity(unittest.TestCase):
    def _client(self, handler):
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )

    def test_http_client_applies_timeout_and_disables_automatic_redirects(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        with patch.multiple(
            config,
            WEB_FETCH_TIMEOUT_SECONDS=7.5,
            WEB_USER_AGENT="TesteSeguro/1.0",
        ), patch.object(ingest, "_http_client", None):
            client = ingest._get_http_client()
            try:
                self.assertFalse(client.follow_redirects)
                self.assertEqual(client.timeout.connect, 7.5)
                self.assertEqual(client.timeout.read, 7.5)
            finally:
                client.close()

    def test_redirect_to_private_address_is_blocked_before_second_request(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "http://private.test/segredo"},
            )

        resolver = _resolver(
            {
                "allowed.test": [_PUBLIC_IPV4],
                "private.test": ["127.0.0.1"],
            }
        )
        with self._client(handler) as client, patch.multiple(
            config,
            WEB_ALLOWED_HOSTS=(),
            WEB_MAX_REDIRECTS=5,
            WEB_MAX_DOWNLOAD_BYTES=1024,
        ), patch("ingest.socket.getaddrinfo", side_effect=resolver), patch(
            "ingest._get_http_client", return_value=client
        ):
            with self.assertRaises(ingest.WebFetchError) as raised:
                ingest.read_url("https://allowed.test/inicio")

        self.assertEqual(requests, ["https://allowed.test/inicio"])
        self.assertNotIn("private.test", str(raised.exception))
        self.assertNotIn("segredo", str(raised.exception))

    def test_redirect_to_public_host_outside_allowlist_is_blocked(self):
        requests = []

        def handler(request):
            requests.append(request.url.host)
            return httpx.Response(
                302,
                headers={"Location": "https://other.test/documento"},
            )

        resolver = _resolver(
            {
                "allowed.test": [_PUBLIC_IPV4],
                "other.test": [_PUBLIC_IPV4],
            }
        )
        with self._client(handler) as client, patch.multiple(
            config,
            WEB_ALLOWED_HOSTS=("allowed.test",),
            WEB_MAX_REDIRECTS=5,
            WEB_MAX_DOWNLOAD_BYTES=1024,
        ), patch("ingest.socket.getaddrinfo", side_effect=resolver), patch(
            "ingest._get_http_client", return_value=client
        ):
            with self.assertRaisesRegex(ingest.WebFetchError, "allowlist"):
                ingest.read_url("https://allowed.test/inicio")

        self.assertEqual(requests, ["allowed.test"])

    def test_resolution_failure_and_any_non_public_address_fail_closed(self):
        client_factory = Mock()
        failing_resolver = _resolver(
            {"unresolved.test": socket.gaierror("detalhe interno")}
        )
        with patch.multiple(
            config,
            WEB_ALLOWED_HOSTS=(),
            WEB_MAX_REDIRECTS=5,
            WEB_MAX_DOWNLOAD_BYTES=1024,
        ), patch(
            "ingest.socket.getaddrinfo", side_effect=failing_resolver
        ), patch("ingest._get_http_client", client_factory):
            with self.assertRaises(ingest.WebFetchError) as raised:
                ingest.read_url("https://unresolved.test/documento?token=secreto")

        self.assertNotIn("unresolved.test", str(raised.exception))
        self.assertNotIn("secreto", str(raised.exception))
        client_factory.assert_not_called()

        mixed_resolver = _resolver(
            {"mixed.test": [_PUBLIC_IPV4, _PUBLIC_IPV6, "::1"]}
        )
        with patch("ingest.socket.getaddrinfo", side_effect=mixed_resolver):
            with self.assertRaisesRegex(ingest.WebFetchError, "bloqueado"):
                ingest._validate_web_destination("https://mixed.test/documento")

        multicast_resolver = _resolver({"multicast.test": ["224.0.0.1"]})
        with patch("ingest.socket.getaddrinfo", side_effect=multicast_resolver):
            with self.assertRaisesRegex(ingest.WebFetchError, "bloqueado"):
                ingest._validate_web_destination("https://multicast.test/documento")

    def test_oversized_response_stops_stream_without_reading_full_body(self):
        stream = _CountingStream([b"aaaa", b"bbbb", b"cccc"])

        def handler(_request):
            return httpx.Response(200, stream=stream)

        resolver = _resolver({"allowed.test": [_PUBLIC_IPV4]})
        with self._client(handler) as client, patch.multiple(
            config,
            WEB_ALLOWED_HOSTS=("allowed.test",),
            WEB_MAX_REDIRECTS=1,
            WEB_MAX_DOWNLOAD_BYTES=5,
        ), patch("ingest.socket.getaddrinfo", side_effect=resolver), patch(
            "ingest._get_http_client", return_value=client
        ):
            with self.assertRaises(ingest.WebFetchError) as raised:
                ingest.read_url("https://allowed.test/doc?token=nao-expor")

        self.assertEqual(stream.yielded, 2)
        self.assertNotIn("nao-expor", str(raised.exception))
        self.assertNotIn("allowed.test", str(raised.exception))

    def test_redirect_limit_is_enforced(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"Location": "/proximo"})

        resolver = _resolver({"allowed.test": [_PUBLIC_IPV4]})
        with self._client(handler) as client, patch.multiple(
            config,
            WEB_ALLOWED_HOSTS=("allowed.test",),
            WEB_MAX_REDIRECTS=1,
            WEB_MAX_DOWNLOAD_BYTES=1024,
        ), patch("ingest.socket.getaddrinfo", side_effect=resolver), patch(
            "ingest._get_http_client", return_value=client
        ):
            with self.assertRaisesRegex(ingest.WebFetchError, "redirecionamentos"):
                ingest.read_url("https://allowed.test/inicio")

        self.assertEqual(len(requests), 2)

    def test_authorized_public_source_remains_supported(self):
        def handler(_request):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=b"<html><h1>Guia autorizado</h1><p>Conteudo valido.</p></html>",
            )

        resolver = _resolver({"docs.allowed.test": [_PUBLIC_IPV4, _PUBLIC_IPV6]})
        with self._client(handler) as client, patch.multiple(
            config,
            WEB_ALLOWED_HOSTS=("*.allowed.test",),
            WEB_MAX_REDIRECTS=2,
            WEB_MAX_DOWNLOAD_BYTES=4096,
        ), patch("ingest.socket.getaddrinfo", side_effect=resolver), patch(
            "ingest._get_http_client", return_value=client
        ):
            text, doc_type, title = ingest.read_url(
                "https://docs.allowed.test/guia"
            )

        self.assertEqual(doc_type, "html")
        self.assertEqual(title, "Guia autorizado")
        self.assertIn("Conteudo valido.", text)


if __name__ == "__main__":
    unittest.main()
