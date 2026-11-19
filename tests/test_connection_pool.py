from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from guild_connector.http_forwarder import HttpForwarder
from guild_connector.models import Delivery, Destination


class ConnectionPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_one_hundred_streams_can_reach_the_service(self):
        arrived = 0
        all_arrived = asyncio.Event()
        release = asyncio.Event()
        handlers = set()

        async def serve(reader, writer):
            nonlocal arrived
            handler = asyncio.current_task()
            handlers.add(handler)
            try:
                await reader.readuntil(b"\r\n\r\n")
                arrived += 1
                if arrived == 128:
                    all_arrived.set()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n")
                await writer.drain()
                await release.wait()
                writer.write(b"x")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(handler)

        server = await asyncio.start_server(serve, "127.0.0.1", 0, backlog=256)
        destination = Destination(
            "test", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        )
        forwarder = HttpForwarder()

        async def consume():
            delivery = Delivery(
                id=str(uuid4()),
                target_id="test",
                method="GET",
                endpoint="/",
                query=None,
                headers={},
                body=b"",
                expires_at=datetime.now(UTC) + timedelta(seconds=30),
                timeout=httpx.Timeout(10),
                stream_response=True,
            )
            frames = [
                frame async for frame in forwarder.stream_forward(destination, delivery)
            ]
            self.assertEqual(frames[0]["response_status"], 200)
            self.assertEqual(frames[-1]["type"], "response_end")

        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(128):
                    group.create_task(consume())
                await asyncio.wait_for(all_arrived.wait(), 8)
                release.set()
            self.assertEqual(arrived, 128)
        finally:
            release.set()
            await forwarder.aclose()
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.gather(*handlers)
