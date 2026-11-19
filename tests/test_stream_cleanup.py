from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from guild_connector.connector import PrivateConnector
from guild_connector.http_forwarder import HttpForwarder
from guild_connector.models import Delivery, Destination
from guild_connector.tunnel_client import TunnelClient


class StreamCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_closing_partial_stream_closes_upstream_before_returning(self):
        class Upstream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"data: first\n\n"
                yield b"data: second\n\n"

            async def aclose(self):
                self.closed = True

        upstream = Upstream()
        original_client = httpx.AsyncClient
        with patch(
            "guild_connector.http_forwarder.httpx.AsyncClient",
            side_effect=lambda **kwargs: original_client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=upstream)
                ),
                **kwargs,
            ),
        ):
            forwarder = HttpForwarder()
        with patch("guild_connector.connector.HttpForwarder", return_value=forwarder):
            connector = PrivateConnector(
                tunnel=Mock(spec=TunnelClient),
                destinations={"test": Destination("test", "https://upstream.test")},
            )
        delivery = Delivery(
            id=str(uuid4()),
            target_id="test",
            method="GET",
            endpoint="/",
            query=None,
            headers={},
            body=b"",
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            stream_response=True,
        )
        frames = connector.stream_forward(delivery)
        try:
            self.assertEqual((await anext(frames))["type"], "response_start")
            self.assertEqual((await anext(frames))["type"], "response_chunk")
            await frames.aclose()
            self.assertTrue(upstream.closed)
        finally:
            await frames.aclose()
            await forwarder.aclose()
