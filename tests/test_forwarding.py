from __future__ import annotations

import asyncio
import errno
import ssl
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from guild_connector.connector import PrivateConnector
from guild_connector.http_forwarder import HttpForwarder
from guild_connector.models import Delivery, Destination, ProtocolError
from guild_connector.tunnel_client import TunnelClient
from guild_connector.urls import request_url


class LocalService(BaseHTTPRequestHandler):
    """Loopback HTTP service used to exercise the actual network forwarder."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(201)
        self.send_header("X-Received-Authorization", self.headers["Authorization"])
        self.send_header("X-Received-Path", self.path)
        self.send_header("X-Received-Host", self.headers["Host"])
        self.send_header(
            "X-Received-Idempotency-Key", self.headers.get("Idempotency-Key", "")
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/cookie"):
            body = self.headers.get("Cookie", "").encode()
            self.send_response(302 if self.path == "/cookie-redirect" else 200)
            if self.path in {"/cookie-set", "/cookie-redirect"}:
                self.send_header("Set-Cookie", "session=alice; Path=/")
            if self.path == "/cookie-redirect":
                self.send_header("Location", "/cookie-echo")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302 if self.path == "/redirect" else 418)
        if self.path == "/redirect":
            self.send_header("Location", "/must-not-be-followed")
        self.end_headers()
        self.wfile.write(b"upstream-response")

    def log_message(self, format, *args):
        pass


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalService)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.destination = Destination("local-service", self.origin)
        self.forwarder = HttpForwarder()

    async def asyncTearDown(self):
        await self.forwarder.aclose()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join()

    def delivery(self, **overrides):
        fields = {
            "id": str(uuid4()),
            "target_id": "local-service",
            "method": "GET",
            "endpoint": "/",
            "query": None,
            "headers": {},
            "body": b"",
            "expires_at": datetime.now(UTC) + timedelta(seconds=30),
        }
        return Delivery(**(fields | overrides))

    async def test_real_http_preserves_application_headers_body_and_query(self):
        delivery = self.delivery(
            method="POST",
            endpoint="/rows",
            query="limit=2",
            headers={
                "Authorization": "Bearer test-token",
                "Host": "original.example",
                "Idempotency-Key": "upstream-operation-42",
            },
            body=b"\x00\xff\x10payload",
        )
        response = await self.forwarder.forward(self.destination, delivery)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body, delivery.body)
        self.assertEqual(
            response.headers["x-received-authorization"], "Bearer test-token"
        )
        self.assertEqual(response.headers["x-received-path"], "/rows?limit=2")
        self.assertEqual(
            response.headers["x-received-idempotency-key"], "upstream-operation-42"
        )
        self.assertEqual(
            response.headers["x-received-host"], self.origin.removeprefix("http://")
        )

    async def test_upstream_error_response_is_preserved(self):
        response = await self.forwarder.forward(self.destination, self.delivery())
        self.assertEqual(response.status, 418)
        self.assertEqual(response.body, b"upstream-response")
        self.assertIsNone(response.error_code)

    async def test_paths_and_host_headers_cannot_select_another_origin(self):
        for path in (
            "/literal//elsewhere.invalid/steal",
            "/%2f%2felsewhere.invalid/steal",
            "/a%2Fb?x=%2F&y=%26",
            "/rows?a=1&a=2&sig=%2b%2F%3D&literal=+",
            "/%23%3F%40%5C?next=https%3A%2F%2Felsewhere.invalid",
            "/?",
            "/",
            "/@elsewhere.invalid/steal",
        ):
            with self.subTest(path=path):
                endpoint, separator, query = path.partition("?")
                response = await self.forwarder.forward(
                    self.destination,
                    self.delivery(
                        method="POST",
                        endpoint=endpoint,
                        query=query if separator else None,
                        body=b"safe-test",
                        headers={
                            "Host": "elsewhere.invalid",
                            "hOsT": "another.invalid",
                            "Authorization": "synthetic-test",
                        },
                    ),
                )
                self.assertEqual(response.status, 201)
                self.assertEqual(response.headers["x-received-path"], path)
                self.assertEqual(
                    response.headers["x-received-host"],
                    self.origin.removeprefix("http://"),
                )

    async def test_conflicting_framing_is_rebuilt_from_actual_body(self):
        response = await self.forwarder.forward(
            self.destination,
            self.delivery(
                method="POST",
                body=b"safe-test",
                headers={
                    "Content-Length": "1",
                    "Transfer-Encoding": "chunked",
                    "Connection": "close, X-Remove-Me",
                    "X-Remove-Me": "ignored",
                    "Authorization": "synthetic-test",
                },
            ),
        )
        self.assertEqual(response.body, b"safe-test")

    async def test_invalid_request_is_rejected_before_http(self):
        for invalid in (
            {"headers": {"Authorization": "synthetic\r\nX-Injected: value"}},
            {"headers": {"Bad Name": "value"}},
            {"method": "CONNECT"},
            {"method": "GET\r\n"},
        ):
            with (
                self.subTest(invalid=invalid),
                patch.object(
                    self.forwarder._client,
                    "stream",
                    side_effect=AssertionError("No network"),
                ) as stream,
            ):
                response = await self.forwarder.forward(
                    self.destination, self.delivery(**invalid)
                )
                self.assertEqual(response.error_code, "invalid_request")
                stream.assert_not_called()

    async def test_invalid_endpoint_cannot_replace_configured_authority(self):
        for endpoint in (
            "http://elsewhere.invalid/steal",
            "https://user:password@elsewhere.invalid/",
            "//elsewhere.invalid/",
            "///elsewhere.invalid/",
            "file:///tmp/data",
            "/\\elsewhere.invalid",
            "/../admin",
            "/#fragment",
            "/?injected=query",
            "/\r\nHost: elsewhere.invalid",
            "/not encoded",
            "relative/path",
            "",
        ):
            with (
                self.subTest(endpoint=endpoint),
                patch.object(
                    self.forwarder._client,
                    "stream",
                    side_effect=AssertionError("No network"),
                ) as stream,
            ):
                response = await self.forwarder.forward(
                    self.destination, self.delivery(endpoint=endpoint)
                )
                self.assertEqual(response.error_code, "destination_not_allowed")
                stream.assert_not_called()

    async def test_unencoded_query_does_not_reach_http(self):
        for query in ("x=#fragment", "x=not encoded", "x=\r\nHost: elsewhere.invalid"):
            with (
                self.subTest(query=query),
                patch.object(self.forwarder._client, "stream") as stream,
            ):
                result = await self.forwarder.forward(
                    self.destination, self.delivery(query=query)
                )
                self.assertEqual(result.error_code, "destination_not_allowed")
                stream.assert_not_called()

    async def test_target_and_deadline_are_checked_at_forwarder_entry(self):
        for fields, code in (
            ({"target_id": "other"}, "unknown_target"),
            (
                {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
                "delivery_expired",
            ),
        ):
            with (
                self.subTest(code=code),
                patch.object(self.forwarder._client, "stream") as stream,
            ):
                response = await self.forwarder.forward(
                    self.destination, self.delivery(**fields)
                )
                self.assertEqual(response.error_code, code)
                stream.assert_not_called()

    async def test_redirect_is_returned_without_following_it(self):
        response = await self.forwarder.forward(
            self.destination, self.delivery(endpoint="/redirect")
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/must-not-be-followed")

    async def test_response_cookies_are_returned_but_never_reused(self):
        for path in ("/cookie-set", "/cookie-redirect"):
            with self.subTest(path=path):
                first = await self.forwarder.forward(
                    self.destination, self.delivery(endpoint=path)
                )
                self.assertEqual(first.headers["set-cookie"], "session=alice; Path=/")
                second = await self.forwarder.forward(
                    self.destination, self.delivery(endpoint="/cookie-echo")
                )
                self.assertEqual(second.body, b"")

    async def test_concurrent_deliveries_only_send_their_supplied_cookies(self):
        await self.forwarder.forward(
            self.destination, self.delivery(endpoint="/cookie-set")
        )
        cookies = ["session=bob; preference=dark", None, "session=carol", None]
        responses = await asyncio.gather(
            *(
                self.forwarder.forward(
                    self.destination,
                    self.delivery(
                        endpoint="/cookie-set",
                        headers={"Cookie": cookie} if cookie else {},
                    ),
                )
                for cookie in cookies
            )
        )
        self.assertEqual(
            [response.body for response in responses],
            [(cookie or "").encode() for cookie in cookies],
        )
        after = await self.forwarder.forward(
            self.destination, self.delivery(endpoint="/cookie-echo")
        )
        self.assertEqual(after.body, b"")


class ForwardingDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.destination = Destination("local-service", "https://local.example")
        self.connector = PrivateConnector(
            tunnel=Mock(spec=TunnelClient),
            destinations={self.destination.id: self.destination},
        )
        self.delivery = Delivery(
            id=str(uuid4()),
            target_id=self.destination.id,
            method="POST",
            endpoint="/private-path",
            query="token=private-query",
            headers={"Authorization": "Bearer private-credential"},
            body=b"private-body",
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )

    async def asyncTearDown(self):
        await self.connector._forwarder.aclose()

    async def test_local_failure_logs_ids_and_safe_reason_with_generic_response(self):
        private_error = (
            f"private-exception {self.delivery.endpoint}?{self.delivery.query} "
            f"{self.delivery.headers} {self.delivery.body} private-response"
        )
        tls_error = httpx.ConnectError(private_error)
        tls_error.__cause__ = ssl.SSLCertVerificationError(private_error)
        cases = (
            (
                httpx.ConnectError(private_error),
                "Local forwarding connection failed; check the service URL and network access",
            ),
            (
                tls_error,
                "Local forwarding TLS handshake failed; check the CA and hostname",
            ),
            (
                httpx.RemoteProtocolError(private_error),
                "Local forwarding received an invalid HTTP response",
            ),
            (
                httpx.ReadError(private_error),
                "Local forwarding HTTP transfer failed; check network access",
            ),
            (
                ssl.SSLError(private_error),
                "TLS handshake failed; check the local service certificate",
            ),
            (TimeoutError(private_error), "Local request timed out"),
            (OSError(errno.ECONNREFUSED, private_error), "Connection refused"),
            (ValueError(private_error), "Invalid local request or response"),
        )
        for error, reason in cases:
            with (
                self.subTest(error=type(error).__name__),
                patch.object(self.connector._forwarder, "forward", side_effect=error),
                self.assertLogs("guild_connector.connector", level="WARNING") as logs,
            ):
                result = await self.connector.forward(self.delivery)
                self.assertEqual(result.delivery_id, self.delivery.id)
                self.assertEqual(result.status, 502)
                self.assertEqual(result.error_code, "local_forward_error")
                self.assertEqual(result.headers, {})
                self.assertEqual(result.body, b"")
                self.assertEqual(
                    [record.getMessage() for record in logs.records],
                    [
                        f"Local forwarding failed (delivery={self.delivery.id!r} "
                        f"target={self.destination.id!r}): {reason}"
                    ],
                )
                self.assertNotIn("private-", "\n".join(logs.output))
                self.assertNotIn("https://", "\n".join(logs.output))

    async def test_target_id_cannot_inject_log_lines(self):
        target_id = "local-service\nforged-line"
        self.connector._destinations[target_id] = Destination(
            target_id, self.destination.origin
        )
        self.delivery = replace(self.delivery, target_id=target_id)
        with (
            patch.object(
                self.connector._forwarder,
                "forward",
                side_effect=ValueError("private-error"),
            ),
            self.assertLogs("guild_connector.connector", level="WARNING") as logs,
        ):
            await self.connector.forward(self.delivery)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("\n", logs.output[0])
        self.assertIn(repr(target_id), logs.output[0])


class DeliveryContractTests(unittest.TestCase):
    def payload(self):
        return {
            "id": str(uuid4()),
            "target_id": "local-service",
            "method": "GET",
            "type": "request",
            "endpoint": "/rows",
            "query": None,
            "headers": {},
            "body_base64": "AP8=",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "timeout": {"connect": 5, "read": 60, "write": 10, "pool": None},
        }

    def test_accepts_tunnel_wire_fields_without_synthetic_metadata(self):
        delivery = Delivery.from_payload(self.payload())
        self.assertEqual(delivery.body, b"\x00\xff")
        self.assertFalse(delivery.is_expired())
        self.assertEqual(
            delivery.timeout, httpx.Timeout(connect=5, read=60, write=10, pool=None)
        )

    def test_invalid_payload_is_rejected(self):
        for invalid in (
            {"body_base64": "%%%"},
            {"expires_at": "2099-01-01"},
            {"headers": {"x": 4}},
            {"path": "/different"},
            {"query_string": "different=1"},
            {"type": "other"},
            {"timeout": {"read": 5}},
            {"timeout": {"connect": 5, "read": -1, "write": 5, "pool": 5}},
            {"timeout": {"connect": 5, "read": float("inf"), "write": 5, "pool": 5}},
            {"timeout": {"connect": 5, "read": True, "write": 5, "pool": 5}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                Delivery.from_payload(self.payload() | invalid)

    def test_absolute_url_is_not_accepted_in_place_of_endpoint(self):
        legacy = self.payload()
        del legacy["endpoint"]
        legacy.update(url="https://unconfigured.example/rows")
        with self.assertRaises(ProtocolError):
            Delivery.from_payload(legacy)

    def test_requests_use_configured_authority_and_keep_encoded_path(self):
        for allowed, expected in (
            ("https://FOO.internal:443", "https://foo.internal/a%2Fb?x=%2F"),
            ("http://foo.internal", "http://foo.internal/a%2Fb?x=%2F"),
            ("http://[::1]:80", "http://[::1]/a%2Fb?x=%2F"),
            ("http://10.0.0.1:8080", "http://10.0.0.1:8080/a%2Fb?x=%2F"),
            ("https://bücher.internal", "https://xn--bcher-kva.internal/a%2Fb?x=%2F"),
        ):
            with self.subTest(allowed=allowed):
                destination = Destination("target", allowed)
                self.assertEqual(
                    str(request_url(destination.url, "/a%2Fb", "x=%2F")), expected
                )

    def test_allowlist_configuration_rejects_ambiguous_authority_or_target(self):
        for origin in (
            "https://foo.internal/path",
            "https://foo.internal?",
            "https://foo.internal#",
            "https://user@foo.internal",
            "http://[foo]/",
            "http://[::1]extra",
            "http://foo.internal:0",
            "http://foo.internal:65536",
            "file:///tmp/data",
            "http://",
            "http://foo.internal:invalid",
        ):
            with self.subTest(origin=origin), self.assertRaises(ProtocolError):
                Destination("target", origin)


if __name__ == "__main__":
    unittest.main()
