from __future__ import annotations

import gzip
import json
import unittest
from base64 import b64decode
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import httpx

from guild_connector.http_forwarder import HttpForwarder
from guild_connector.models import Delivery, Destination, ProxyResponse
from guild_connector.protocol import (
    MAX_FRAME_BYTES,
    MAX_HEADER_BYTES,
    MAX_HEADER_COUNT,
    encode_response,
    response_body_limit,
)


class ResponseFrameTests(unittest.TestCase):
    def test_exact_body_budget_fits_and_next_byte_returns_delivery_error(self):
        for headers in (
            {},
            {"x": "xxx"},
            {"x-metadata": 'quotes: "\\ and unicode: \u00e9\u2603'},
        ):
            with self.subTest(headers=headers):
                response = ProxyResponse(str(uuid4()), 200, headers, b"")
                limit = response_body_limit(response)
                body = b"x" * limit
                encoded = encode_response(
                    ProxyResponse(response.delivery_id, 200, headers, body)
                )
                frame = json.loads(encoded)
                self.assertEqual(frame["type"], "response")
                self.assertEqual(frame["response_headers"], headers)
                self.assertEqual(b64decode(frame["response_body_base64"]), body)
                self.assertLess(len(encoded.encode()), MAX_FRAME_BYTES)
                self.assertLessEqual(MAX_FRAME_BYTES - len(encoded.encode()), 4)
                self.assertEqual(
                    json.loads(
                        encode_response(
                            ProxyResponse(
                                response.delivery_id, 200, headers, body + b"x"
                            )
                        )
                    ),
                    {
                        "id": response.delivery_id,
                        "type": "error",
                        "error_code": "response_too_large",
                    },
                )

    def test_response_at_gateway_message_limit_returns_delivery_error(self):
        response = ProxyResponse(str(uuid4()), 200, {"x": "xxx"}, b"abc")
        encoded = encode_response(response)
        wire_bytes = len(encoded.encode())
        with patch("guild_connector.protocol.MAX_FRAME_BYTES", wire_bytes):
            self.assertEqual(
                json.loads(encode_response(response)),
                {
                    "id": response.delivery_id,
                    "type": "error",
                    "error_code": "response_too_large",
                },
            )
        with patch("guild_connector.protocol.MAX_FRAME_BYTES", wire_bytes + 1):
            self.assertEqual(encode_response(response), encoded)

    def test_twelve_mib_response_returns_error_without_raising(self):
        response = ProxyResponse(str(uuid4()), 200, {}, b"x" * (12 * 1024 * 1024))
        frame = json.loads(encode_response(response))
        self.assertEqual(frame["id"], response.delivery_id)
        self.assertEqual(frame["type"], "error")
        self.assertEqual(frame["error_code"], "response_too_large")

    def test_header_limits_return_delivery_errors(self):
        for headers in (
            {str(index): "x" for index in range(MAX_HEADER_COUNT + 1)},
            {"x-large": "x" * MAX_HEADER_BYTES},
        ):
            with self.subTest(header_count=len(headers)):
                response = ProxyResponse(str(uuid4()), 200, headers, b"")
                self.assertEqual(response_body_limit(response), -1)
                frame = json.loads(encode_response(response))
                self.assertEqual(frame["id"], response.delivery_id)
                self.assertEqual(frame["type"], "error")
                self.assertEqual(frame["error_code"], "response_too_large")

    def test_headers_alone_can_exceed_the_frame_budget(self):
        with patch("guild_connector.protocol.MAX_FRAME_BYTES", 1024):
            response = ProxyResponse(
                str(uuid4()), 200, {"x-large": "\u00e9" * 1024}, b""
            )
            encoded = encode_response(response)
            self.assertLess(len(encoded.encode()), 1024)
            self.assertEqual(json.loads(encoded)["error_code"], "response_too_large")


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class ForwardingLimitTests(unittest.IsolatedAsyncioTestCase):
    async def forward(self, stream, headers):
        def upstream(_request):
            return httpx.Response(200, headers=headers, stream=stream)

        original_client = httpx.AsyncClient
        with patch(
            "guild_connector.http_forwarder.httpx.AsyncClient",
            side_effect=lambda **kwargs: original_client(
                transport=httpx.MockTransport(upstream), **kwargs
            ),
        ):
            forwarder = HttpForwarder()
        try:
            return await forwarder.forward(
                Destination("foo", "https://foo.example"),
                Delivery(
                    str(uuid4()),
                    "foo",
                    "GET",
                    "/",
                    None,
                    {},
                    b"",
                    datetime.now(UTC) + timedelta(seconds=30),
                ),
            )
        finally:
            await forwarder.aclose()

    async def forward_stream(self, stream, headers):
        def upstream(_request):
            return httpx.Response(200, headers=headers, stream=stream)

        original_client = httpx.AsyncClient
        with patch(
            "guild_connector.http_forwarder.httpx.AsyncClient",
            side_effect=lambda **kwargs: original_client(
                transport=httpx.MockTransport(upstream), **kwargs
            ),
        ):
            forwarder = HttpForwarder()
        try:
            return [
                frame
                async for frame in forwarder.stream_forward(
                    Destination("foo", "https://foo.example"),
                    Delivery(
                        str(uuid4()),
                        "foo",
                        "GET",
                        "/",
                        None,
                        {},
                        b"",
                        datetime.now(UTC) + timedelta(seconds=30),
                        stream_response=True,
                    ),
                )
            ]
        finally:
            await forwarder.aclose()

    async def test_streaming_response_uses_chunks_and_stops_at_total_limit(self):
        stream = TrackedStream([b"x" * 4096] * 8)
        with patch("guild_connector.http_forwarder.MAX_STREAM_BYTES", 9000):
            frames = await self.forward_stream(
                stream, {"content-type": "text/event-stream"}
            )
        self.assertEqual(frames[0]["type"], "response_start")
        self.assertEqual(frames[-1]["error_code"], "response_too_large")
        self.assertEqual(
            b"".join(
                b64decode(frame["response_body_base64"])
                for frame in frames
                if frame["type"] == "response_chunk"
            ),
            b"x" * 8192,
        )
        self.assertLess(stream.reads, len(stream.chunks))
        self.assertTrue(stream.closed)

    async def test_large_stream_is_closed_early_without_trusting_content_length(self):
        for headers in ({}, {"content-length": "1"}):
            with self.subTest(headers=headers):
                stream = TrackedStream([b"x" * 4096] * 8)
                with patch("guild_connector.protocol.MAX_FRAME_BYTES", 16 * 1024):
                    result = await self.forward(stream, headers)
                self.assertEqual(result.error_code, "response_too_large")
                self.assertEqual(result.body, b"")
                self.assertGreater(stream.reads, 0)
                self.assertLess(stream.reads, len(stream.chunks))
                self.assertTrue(stream.closed)

    async def test_oversized_headers_close_response_before_reading_body(self):
        stream = TrackedStream([b"not needed"])
        with patch("guild_connector.protocol.MAX_FRAME_BYTES", 1024):
            result = await self.forward(stream, {"x-large": "x" * 1024})
        self.assertEqual(result.error_code, "response_too_large")
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_compressed_http_body_crosses_tunnel_without_decoding(self):
        wire = gzip.compress(b"a" * (13 * 1024 * 1024))
        result = await self.forward(
            TrackedStream([wire]),
            {"content-encoding": "gzip", "content-length": str(len(wire))},
        )
        self.assertIsNone(result.error_code)
        self.assertEqual(result.body, wire)
        frame = json.loads(encode_response(result))
        self.assertEqual(frame["type"], "response")
        self.assertEqual(frame["response_headers"]["content-encoding"], "gzip")
        self.assertEqual(b64decode(frame["response_body_base64"]), wire)
