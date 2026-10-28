from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from guild_connector.connector import PrivateConnector
from guild_connector.http_forwarder import HttpForwarder
from guild_connector.models import Delivery, Destination
from guild_connector.tunnel_client import TunnelClient


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delays = [0.15]
        self.handlers = set()
        self.started = asyncio.Event()
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.destination = Destination("test", f"http://127.0.0.1:{port}")
        self.forwarder = HttpForwarder()

    async def asyncTearDown(self):
        await self.forwarder.aclose()
        self.server.close()
        await self.server.wait_closed()
        handlers = list(self.handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.started.set()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(self.delays)}\r\n\r\n".encode()
            )
            await writer.drain()
            for delay in self.delays:
                await asyncio.sleep(delay)
                writer.write(b"x")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    def delivery(self, timeout, start_window=25):
        return Delivery(
            id=str(uuid4()),
            target_id="test",
            method="GET",
            endpoint="/",
            query=None,
            headers={},
            body=b"",
            expires_at=datetime.now(UTC) + timedelta(seconds=start_window),
            timeout=timeout,
        )

    async def test_short_read_timeout_is_enforced(self):
        with self.assertRaises(httpx.ReadTimeout):
            await self.forwarder.forward(
                self.destination, self.delivery(httpx.Timeout(0.05))
            )

    async def test_sixty_second_timeout_allows_more_than_thirty_seconds(self):
        self.delays = [31]
        response = await self.forwarder.forward(
            self.destination, self.delivery(httpx.Timeout(60), start_window=1)
        )
        self.assertEqual(response.body, b"x")

    async def test_read_timeout_measures_inactivity_not_total_duration(self):
        self.delays = [0.08] * 4
        response = await self.forwarder.forward(
            self.destination, self.delivery(httpx.Timeout(0.2), start_window=0.1)
        )
        self.assertEqual(response.body, b"xxxx")

    async def test_disabled_timeout_is_preserved(self):
        response = await self.forwarder.forward(
            self.destination, self.delivery(httpx.Timeout(None))
        )
        self.assertEqual(response.body, b"x")

    async def test_caller_cancellation_stops_forwarding(self):
        task = asyncio.create_task(
            self.forwarder.forward(self.destination, self.delivery(httpx.Timeout(None)))
        )
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_timeout_phase_is_returned_without_exception_secrets(self):
        connector = PrivateConnector(
            tunnel=Mock(spec=TunnelClient), destinations={"test": self.destination}
        )
        try:
            for error, code in (
                (httpx.ConnectTimeout, "connect_timeout"),
                (httpx.ReadTimeout, "read_timeout"),
                (httpx.WriteTimeout, "write_timeout"),
                (httpx.PoolTimeout, "pool_timeout"),
            ):
                with (
                    self.subTest(code=code),
                    patch.object(
                        connector._forwarder, "forward", side_effect=error("secret")
                    ),
                    self.assertLogs(
                        "guild_connector.connector", level="WARNING"
                    ) as logs,
                ):
                    response = await connector.forward(self.delivery(httpx.Timeout(60)))
                    self.assertEqual(response.error_code, code)
                    self.assertNotIn("secret", str(response) + str(logs.output))
        finally:
            await connector._forwarder.aclose()
